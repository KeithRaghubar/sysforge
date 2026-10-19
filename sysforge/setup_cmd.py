# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
setup_cmd.py — first-time sysforge configuration

Checks whether pacman is configured to ignore the 'sf-build' group during
system upgrades, and offers to add the setting automatically.

Why this matters
----------------
Every package sysforge builds is stamped with the 'sf-build' pacman group
via append_groups in profiles.toml. Without IgnoreGroup = sf-build in
/etc/pacman.conf, a plain 'pacman -Syu' will overwrite those packages with
unoptimized repo binaries, silently discarding the compiler flags sysforge
applied.

sysforge update already handles sysforge-managed packages correctly (it only
rebuilds packages recorded in build_state.toml, using the same profiled flags).
The risk is specifically from running pacman -Syu outside of sysforge.

Preferences (3.1.0-F4)
----------------------
At a terminal, setup then asks four questions and saves only the answers that
changed, through ``config.set_toml_key``: whether ``update`` confirms its plan
(``[build] confirm_plan``), how changed AUR/git sources are reviewed
(``[build] review``), whether to use the AUR (``[aur] enabled``) and, only when
devtools and the chroot exist, whether to build in the sandbox
(``[security] sandbox_builds``). Enter keeps the current answer. With no
terminal the step is skipped; a failed write prints the line to add by hand.

Presets (3.5.0-F9)
------------------
Before the preferences, setup offers one preset (``config_presets.PRESETS``):
defaults, performance, low-friction or secure. It shows the rows that would
change, asks once, and writes them as ordinary values; the preference questions
then start from those values. ``--preset NAME`` applies one without asking.

Public API
----------
    cmd_setup(args)
"""
import argparse
import os
import pwd
import re
import shutil
import sys
import tomllib
from pathlib import Path

from sysforge.primitives import config as _config
from sysforge.primitives import config_presets, pacman_hooks
from sysforge.primitives.fs_provision import FsProvisionError
from sysforge.primitives.paths import (
    CONFIG_PATHS,
    KERNEL_PATH,
    SYSFORGE_TOML_PATH,
    TOOLCHAIN_PATH,
    resolve_packages_path,
)
from sysforge.primitives.pkgbuild_review import (
    MODE_ACCEPT,
    MODE_OFF,
    MODE_PROMPT,
    resolve_review_policy,
)
from sysforge.primitives.prompt import prompt_choice
from sysforge.primitives.render import arrow


PACMAN_CONF = Path("/etc/pacman.conf")
_IGNORE_GROUP = "sf-build"

# Pattern matching any uncommented IgnoreGroup line (case-insensitive key).
_IGNOREGROUP_RE = re.compile(r"^[ \t]*IgnoreGroup[ \t]*=[ \t]*(.+)$", re.MULTILINE | re.IGNORECASE)


# ---------------------------------------------------------------------------
# Pure helpers (testable without filesystem)
# ---------------------------------------------------------------------------

def _check_ignore_group(conf_text: str) -> bool:
    """Return True if sf-build is already in an uncommented IgnoreGroup line."""
    for m in _IGNOREGROUP_RE.finditer(conf_text):
        groups = m.group(1).split()
        if _IGNORE_GROUP in groups:
            return True
    return False


def _patch_conf_text(conf_text: str) -> str:
    """
    Return conf_text with sf-build added to IgnoreGroup.

    If an uncommented IgnoreGroup line exists, sf-build is appended to it.
    Otherwise a new 'IgnoreGroup = sf-build' line is inserted inside [options].
    If no [options] section is found, it is appended at the end of the file.
    """
    # Append to an existing IgnoreGroup line.
    m = _IGNOREGROUP_RE.search(conf_text)
    if m:
        original = m.group(0)
        patched = original.rstrip() + f" {_IGNORE_GROUP}"
        return conf_text.replace(original, patched, 1)

    # Insert a new IgnoreGroup line inside [options].
    lines = conf_text.splitlines(keepends=True)
    in_options = False
    insert_after = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^\[options\]", stripped, re.IGNORECASE):
            in_options = True
            insert_after = i
        elif in_options and stripped.startswith("["):
            break
        elif in_options:
            insert_after = i

    if insert_after is not None:
        lines.insert(insert_after + 1, f"IgnoreGroup = {_IGNORE_GROUP}\n")
        return "".join(lines)

    # Fallback: no [options] section found — append at end.
    return conf_text.rstrip("\n") + f"\n\nIgnoreGroup = {_IGNORE_GROUP}\n"


# ---------------------------------------------------------------------------
# Write helper
# ---------------------------------------------------------------------------

def _write_conf(path: Path, text: str) -> bool:
    """
    Write text to path. Returns True on success, False on PermissionError.
    """
    try:
        path.write_text(text, encoding="utf-8")
        return True
    except PermissionError:
        return False


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------

_RISK_WARNING = """\
[SYSFORGE] pacman.conf not modified.

  Be aware: running 'pacman -Syu' will overwrite any package in the
  sf-build group with an unoptimized repo binary, silently discarding
  the compiler flags sysforge applied at build time.

  To avoid this, use 'sysforge update' instead of 'pacman -Syu' for
  packages managed by sysforge. If you change your mind, re-run:

      sysforge setup
"""

_MANUAL_INSTRUCTION = """\
[SYSFORGE] Could not write {path} (permission denied).

  To apply the setting manually, add the following line to the
  [options] section of {path}:

      IgnoreGroup = sf-build

  Or re-run as root:

      sudo sysforge setup
"""


_HOOK_MANUAL_INSTRUCTION = """\
[SYSFORGE] Could not install pacman hooks ({err}).

  The hooks must be written into root-owned system directories. Re-run as root:

      sudo sysforge setup
"""


def cmd_setup(args) -> None:
    """Entry point for 'sysforge setup'."""
    conf_path = Path(getattr(args, "pacman_conf", None) or PACMAN_CONF)

    if not conf_path.exists():
        print(
            f"[SYSFORGE] {conf_path} not found: nothing to configure.",
            file=sys.stderr,
        )
        return

    conf_text = conf_path.read_text(encoding="utf-8")

    if _check_ignore_group(conf_text):
        print(
            f"[SYSFORGE] Already configured: IgnoreGroup = {_IGNORE_GROUP} "
            f"is present in {conf_path}."
        )
    else:
        print(
            f"\n  sysforge stamps every package it builds with the "
            f"'{_IGNORE_GROUP}' pacman group.\n"
            f"  Adding 'IgnoreGroup = {_IGNORE_GROUP}' to {conf_path} prevents 'pacman -Syu'\n"
            f"  from overwriting those packages with unoptimized repo binaries.\n"
        )

        answer = prompt_choice(
            f"  Add 'IgnoreGroup = {_IGNORE_GROUP}' to {conf_path}? [y/N] ",
            choices=("y", "yes", "n"),
            default="n",
        )

        if answer not in ("y", "yes"):
            print(_RISK_WARNING, file=sys.stderr)
        else:
            patched = _patch_conf_text(conf_text)
            if _write_conf(conf_path, patched):
                print(f"[SYSFORGE] Added 'IgnoreGroup = {_IGNORE_GROUP}' to {conf_path}.")
            else:
                print(_MANUAL_INSTRUCTION.format(path=conf_path), file=sys.stderr)

    _setup_pacman_hooks()
    _setup_preset(getattr(args, "preset", None))
    _setup_preferences()


# ---------------------------------------------------------------------------
# Preferences (3.1.0-F4)
# ---------------------------------------------------------------------------

_ACCEPT_RISK = (
    "  With review = \"accept\", changed AUR/git code is built and installed with\n"
    "  nobody having looked at it; every accepted change is still listed in the\n"
    "  update summary."
)
_ACCEPT_SANDBOX_NOTE = (
    "  The build sandbox ([security] sandbox_builds) is then your remaining\n"
    "  build-time protection, and it does not inspect what gets installed."
)
_REVIEW_VALUE = {MODE_PROMPT: True, MODE_ACCEPT: "accept", MODE_OFF: False}


def _has_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _read_table(path, section: str | None) -> dict:
    """One table of a TOML file; ``section=None`` = the top-level keys."""
    try:
        with Path(path).open("rb") as f:
            data = tomllib.load(f)
        return data if section is None else (data.get(section, {}) or {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _yes_no(question: str, current: bool) -> bool:
    hint = "[Y/n]" if current else "[y/N]"
    answer = prompt_choice(f"  {question} {hint} ", ("y", "yes", "n", "no"),
                           default="y" if current else "n")
    return answer in ("y", "yes")


def _setup_preferences() -> None:
    """Ask the consent-posture questions and save changed answers (3.1.0-F4).

    Enter keeps the current answer; only a changed answer is written, through
    ``config.set_toml_key`` (the one runtime TOML writer).
    """
    if not _has_terminal():
        print("[SYSFORGE] preferences: skipped (no terminal)")
        return
    from sysforge.primitives.build_sandbox import resolve_sandbox

    packages_path = resolve_packages_path(_config.load_config() or {})
    build_cfg = _read_table(packages_path, "build")
    sysforge_cfg = _config.load_sysforge_toml()
    aur_cfg = sysforge_cfg.get("aur", {}) or {}
    sandbox = resolve_sandbox(sysforge_cfg.get("security", {}) or {})
    writes: list[tuple] = []

    print("\n  Preferences (Enter keeps the current answer):")
    confirm_now = build_cfg.get("confirm_plan", True) is not False
    confirm_new = _yes_no("Ask before each update changes anything?", confirm_now)
    if confirm_new != confirm_now:
        writes.append((packages_path, "build", "confirm_plan", confirm_new))

    mode_now = resolve_review_policy(argparse.Namespace(), build_cfg).mode
    letter = {MODE_PROMPT: "r", MODE_ACCEPT: "a", MODE_OFF: "o"}[mode_now]
    answer = prompt_choice(
        f"  Changed AUR/git sources: [r]eview / [a]ccept and record / [o]ff? [{letter}] ",
        ("r", "a", "o"), default=letter)
    mode_new = {"r": MODE_PROMPT, "a": MODE_ACCEPT, "o": MODE_OFF}[answer]
    if mode_new == MODE_ACCEPT:
        print(_ACCEPT_RISK)
        if not sandbox.enabled:
            print(_ACCEPT_SANDBOX_NOTE)
    if mode_new != mode_now:
        writes.append((packages_path, "build", "review", _REVIEW_VALUE[mode_new]))

    aur_now = aur_cfg.get("enabled", True) is not False
    aur_new = _yes_no("Use the AUR?", aur_now)
    if aur_new != aur_now:
        writes.append((SYSFORGE_TOML_PATH, "aur", "enabled", aur_new))

    if _sandbox_ready(sandbox):
        sb_new = _yes_no("Build packages in a clean chroot?", sandbox.enabled)
        if sb_new != sandbox.enabled:
            writes.append((SYSFORGE_TOML_PATH, "security", "sandbox_builds", sb_new))
    else:
        print(_sandbox_hint(sandbox))

    _write_rows(writes)


def _sandbox_ready(sandbox) -> bool:
    """True when devtools and the clean chroot exist, so the sandbox can run."""
    chroot = sandbox.chroot_dir
    return (bool(shutil.which("makechrootpkg")) and chroot is not None
            and (chroot / "root").is_dir())


def _sandbox_hint(sandbox) -> str:
    return (f"  build sandbox available after: pacman -S devtools; "
            f"mkarchroot {sandbox.chroot_dir}/root base-devel")


def _write_rows(writes) -> None:
    """Write ``(path, section, key, value)`` rows; a failure prints the manual
    edit and the remaining rows still apply. ``[defaults] toolchain`` goes
    through ``set_default_toolchain``, its one home."""
    for path, section, key, value in writes:
        literal = _config.toml_literal(value)
        try:
            if (section, key) == ("defaults", "toolchain"):
                _config.set_default_toolchain(value, path=path)
            else:
                _config.set_toml_key(path, section, key, value)
        except Exception as e:  # noqa: BLE001 — any write failure gets the manual line
            header = ("  (top level, above the first [section] header)\n"
                      if section is None else f"  [{section}]\n")
            print(f"[SYSFORGE] could not write {path} ({e}); set it by hand:\n"
                  f"{header}  {key} = {literal}", file=sys.stderr)
        else:
            print(f"[SYSFORGE] set {_config.toml_location(section, key)} = {literal} "
                  f"in {path}")


# ---------------------------------------------------------------------------
# Presets (3.5.0-F9)
# ---------------------------------------------------------------------------

_HARDENED_BLOCK = """\
  Your profiles.toml does not see the shipped [profiles.hardened] (it replaces
  the system file). Add this to it, then re-run `sysforge setup --preset secure`:

  [profiles.hardened]
  extends  = "standard"
  CFLAGS   = "-march=native -O2 -pipe -ftrivial-auto-var-init=zero -fstrict-flex-arrays=3"
  CXXFLAGS = "$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"
  LDFLAGS  = "-Wl,-O1,--sort-common,--as-needed,-z,relro,-z,now,-z,noexecstack"
"""


_TOOLCHAIN_RESET_NOTE = (
    "  toolchain.toml has the toolchain stage enabled with compiler = {compiler}:\n"
    "  its next run resets [defaults] toolchain to match. Set compiler = \"llvm\"\n"
    "  there to keep LLVM."
)


def _invoking_user_profiles() -> Path | None:
    """Under ``sudo``, the invoking user's own profiles.toml when it exists.

    Root's home hides it from ``_active_profiles_path``, but it is the file the
    user's builds read (it shadows the system file), so a preset must write and
    check it there.
    """
    user = os.environ.get("SUDO_USER")
    if not user or os.geteuid() != 0:
        return None
    try:
        home = Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return None
    path = home / ".config" / "sysforge" / "profiles.toml"
    return path if path.exists() else None


def _resolved_profiles(user_file: Path | None) -> dict:
    """The profiles the user's builds resolve (``user_file`` first, then system)."""
    paths = [user_file, Path(CONFIG_PATHS[-1])] if user_file else None
    try:
        cfg = _config.load_config(paths) or {}
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return {}
    return cfg.get("profiles", {}) or {}


def _preset_paths() -> dict:
    """Preset row target -> the live file setup writes."""
    return {
        "profiles": _invoking_user_profiles() or _config._active_profiles_path(),
        "packages": resolve_packages_path(_config.load_config() or {}),
        "sysforge": SYSFORGE_TOML_PATH,
        "toolchain": TOOLCHAIN_PATH,
        "kernel": KERNEL_PATH,
    }


def _live_value(path, row):
    """The row's current value in ``path``; the shipped default when unset."""
    try:
        with Path(path).open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return row.default
    table = data if row.section is None else (data.get(row.section) or {})
    return table.get(row.key, row.default)


def _choose_preset() -> str:
    letters = {name[0]: name for name in config_presets.PRESET_NAMES}
    menu = " / ".join(f"[{n[0]}]{n[1:]}" for n in config_presets.PRESET_NAMES)
    print("\n  Presets set several related options at once:")
    for p in config_presets.PRESETS.values():
        print(f"    {p.name:<13} {p.summary}")
    answer = prompt_choice(f"  Preset: {menu}? [d] ", tuple(letters), default="d")
    return letters[answer]


def _setup_preset(name: str | None) -> None:
    """Apply one preset's differing rows (3.5.0-F9).

    ``name`` comes from ``--preset`` and is the consent: no menu, no confirm,
    terminal or not. Without it, a terminal gets the menu and an ``Apply?``
    confirm; no terminal skips the step. Guarded rows (a profile the resolved
    config cannot see, a sandbox with no chroot) become printed notes.
    """
    explicit = name is not None
    if not explicit:
        if not _has_terminal():
            print("[SYSFORGE] preset: skipped (no terminal)")
            return
        name = _choose_preset()
    preset = config_presets.PRESETS[name]
    if not preset.rows:
        return
    from sysforge.primitives.build_sandbox import resolve_sandbox
    paths = _preset_paths()
    sandbox = resolve_sandbox(_config.load_sysforge_toml().get("security", {}) or {})
    profiles = _resolved_profiles(_invoking_user_profiles())
    plan, notes = [], list(preset.notes)
    for row in preset.rows:
        path = paths[row.target]
        old = _live_value(path, row)
        if old == row.value:
            continue
        if row.key == "profile" and row.value not in profiles:
            notes.append(_HARDENED_BLOCK if row.value == "hardened" else
                         f"  profile {row.value!r} is not in your profiles.toml; skipped.")
            continue
        if row.key == "sandbox_builds" and row.value is True and not _sandbox_ready(sandbox):
            notes.append(_sandbox_hint(sandbox))
            continue
        plan.append((row, path, old))
    if name == "low-friction":  # the sandbox is off once it applies
        notes.append(_ACCEPT_SANDBOX_NOTE)
    if name == "performance":
        tc = _read_table(paths["toolchain"], None)
        compiler = tc.get("compiler", "gcc")
        if tc.get("enabled") is True and compiler != "llvm":
            notes.append(_TOOLCHAIN_RESET_NOTE.format(
                compiler=_config.toml_literal(compiler) if isinstance(compiler, str)
                else repr(compiler)))
    print(f"\n  Preset {name}:")
    for row, path, old in plan:
        print(f"    {_config.toml_location(row.section, row.key)}: "
              f"{_config.toml_literal(old)} {arrow()} {_config.toml_literal(row.value)}"
              f"  ({path.name})")
    if not plan:
        print("    nothing to change")
    for note in notes:
        print(note)
    if not plan:
        return
    if not explicit:
        answer = prompt_choice("  Apply? [y/N] ", ("y", "yes", "n"), default="n")
        if answer not in ("y", "yes"):
            print("[SYSFORGE] preset not applied.")
            return
    _write_rows([(path, row.section, row.key, row.value) for row, path, _ in plan])


def _setup_pacman_hooks() -> None:
    """Install or refresh sysforge's libalpm hooks + helper to match the shipped
    source. The hooks feed `sysforge update`'s reminder/auto-demote logic; the
    PKGBUILD installs them at package time, but a source-checkout dev workflow
    (or an edited hook) needs this runtime refresh."""
    status = pacman_hooks.diff_status()
    pending = [(art, state) for art, state in status if state != pacman_hooks.STATE_OK]

    if not pending:
        print("[SYSFORGE] pacman hooks up to date.")
        return

    print("\n  sysforge ships pacman hooks that record kernel/toolchain/build-state")
    print("  changes for `sysforge update`. The following are out of sync:")
    for art, state in pending:
        print(f"    {state:>7}  {art.dest}")
    print()

    answer = prompt_choice(
        "  Install/refresh these pacman hooks? [y/N] ",
        choices=("y", "yes", "n"),
        default="n",
    )
    if answer not in ("y", "yes"):
        print("[SYSFORGE] pacman hooks not modified.", file=sys.stderr)
        return

    try:
        written = pacman_hooks.provision(status)
    except FsProvisionError as e:
        print(_HOOK_MANUAL_INSTRUCTION.format(err=e), file=sys.stderr)
        return

    for dest, state in written:
        action = "installed" if state == pacman_hooks.STATE_MISSING else "refreshed"
        print(f"[SYSFORGE] {action} {dest}.")


# ---------------------------------------------------------------------------
# Verb wrapper
# ---------------------------------------------------------------------------

from sysforge.verbs import ExecResult, PreCheckResult, Verb  # noqa: E402


class SetupVerb(Verb):
    """Configure pacman.conf IgnoreGroup for sysforge-built packages."""

    name = "setup"
    wants_run_log = True
    requires_sentinel = False

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("setup",
            help="Configure system integration (pacman IgnoreGroup for sf-build; "
                 "sysforge's pacman hooks), offer a config preset, and save update "
                 "preferences (plan confirmation, PKGBUILD review, AUR, build "
                 "sandbox).")
        p.add_argument("--pacman-conf", metavar="FILE", dest="pacman_conf",
            help="Path to pacman.conf (default: /etc/pacman.conf).")
        p.add_argument("--preset", choices=config_presets.PRESET_NAMES,
            help="Apply a config preset without asking: defaults, performance "
                 "(faster binaries), low-friction (fewest prompts) or secure "
                 "(hardened builds and sources).")
        p.set_defaults(verb_cls=cls)
        return p

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_setup(args)
        return ExecResult()
