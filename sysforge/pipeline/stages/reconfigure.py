# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stages/reconfigure.py — stage 5: pre-build checkpoint

Runs after bootstrap configuration and before the build stages
(toolchain → packages → kernel). Safe to re-run on a live system at any time.

Presents a menu of available checks. The user can run all, a numbered subset,
a range, or refer to steps by name. Defaults to all when non-interactive.

Available steps:
  1  editor      Editor selection (SYSFORGE_EDITOR → sysforge.toml → $EDITOR → $VISUAL → detected)
  2  config      Config file review (flag_profiles, packages, toolchain, kernel, hardware_profile)
  3  build_mode  View/set packages.toml repo_mode (pacman | build_from_source)
  4  desktop     Pick a curated desktop environment (GNOME | KDE) as a packages.toml group
  5  makepkg     System makepkg.conf review (MAKEFLAGS, BUILDDIR, PKGDEST, flags)
  6  sudo        User / sudo verification
  7  disk        Disk space check
  8  network     Network connectivity probe (AUR, GitHub, mirrors)
  9  gpg         GPG keyring bootstrap (global key store import, refresh option)
  10 preview     Build preview (tentative profiles for all packages.toml entries)

Non-interactive / dry_run:
  All steps run without prompting. dry_run additionally skips writes
  (no file edits, no sysforge.toml updates, no GPG imports).
"""
import os
import re
import shutil
import socket
import subprocess
import tomllib
from pathlib import Path

from sysforge import log
_log = log.get_logger("RECONFIGURE")
from sysforge.pipeline.stages.base import BootstrapRebootRequired, Stage
from sysforge.pipeline.state import resolve_state_dir
from sysforge.primitives.config import (
    expand_package_groups,
    load_config,
    load_conflict_groups,
    load_sysforge_toml,
    set_makepkg_conf_keys,
    resolve_repo_mode,
    REPO_MODE_PACMAN,
    REPO_MODE_SOURCE,
    PKG_KEY_BUILD_FROM_SOURCE,
)
from sysforge.primitives.paths import (
    CONFIG_DIR,
    KERNEL_PATH,
    PACKAGES_PATH,
    SYSFORGE_TOML_PATH,
    TOOLCHAIN_PATH,
    resolve_packages_path,
)
from sysforge.primitives.editor import (
    describe_editor_chain,
    editor_usable as _editor_usable,
    resolve_editor as _resolve_editor,
    run_tty_argv as _run_editor_argv,
)
from sysforge.primitives.env_chain import collect_env_chain, sources_defining
from sysforge.primitives.env_persist import (
    apply_write,
    plan_write,
    system_target,
    user_target,
)
from sysforge.primitives.pkg_catalog import (
    DESKTOP_CATALOG,
    select_desktop,
    write_desktop_group,
)
from sysforge.primitives import storage_probe
from sysforge.primitives.privilege import privileged_argv
from sysforge.primitives.provides_lookup import files_db_present, sync_files_db
from sysforge.primitives.prompt import (
    is_interactive as _interactive,
    prompt_text as _prompt,
    prompt_choice as _prompt_choice,
    prompt_key as _prompt_key,
)

def _pipeline_stages() -> list[tuple[str, str]]:
    """Lazy import to avoid circular import from stages/__init__.py."""
    from sysforge.pipeline.stages import STAGES
    return [(s.name, s.description) for s in STAGES]

# Ordered step definitions: (key, short_label, description)
_STEPS = [
    ("editor",     "Editor selection",
     "Set preferred editor; save permanently to sysforge.toml"),
    ("config",     "Config file review",
     "Review profiles.toml, packages.toml, toolchain.toml, kernel.toml, hardware_profile.toml"),
    ("build_mode", "Build mode",
     "View/set packages.toml repo_mode (pacman | build_from_source); show per-package "
     "enable_build_from_source overrides"),
    ("desktop",    "Desktop environment",
     "Pick a curated desktop environment (GNOME / KDE) to install as a packages.toml group"),
    ("makepkg",    "makepkg.conf review",
     "Review /etc/makepkg.conf — MAKEFLAGS, BUILDDIR, PKGDEST, flag baselines"),
    ("sudo",       "User / sudo verification",
     "Confirm build user and sudoers are correctly configured for makepkg -si"),
    ("disk",       "Disk space check",
     "Estimate required build space and warn if free space is low"),
    ("network",    "Network probe",
     "Verify AUR, GitHub, Arch Linux, and pacman mirrors are reachable"),
    ("gpg",        "GPG keyring bootstrap",
     "Import global key store, report keyring status, offer keyserver refresh"),
    ("preview",    "Build preview",
     "Show what packages + kernel stages will build, with tentative profiles"),
]

_STEP_KEYS  = [k for k, _, _ in _STEPS]

_DISK_WARN_GB    = 20
_DISK_PER_PKG_GB = 3
_MAKEPKG_CONF_HIGHLIGHT = [
    "MAKEFLAGS", "BUILDDIR", "PKGDEST", "PKGEXT",
    "CFLAGS", "CXXFLAGS", "LDFLAGS",
]


# ---------------------------------------------------------------------------
# Helpers: sysforge.toml
# ---------------------------------------------------------------------------


def _save_sysforge_toml_ui(key: str, value: str) -> None:
    data = load_sysforge_toml()
    ui = dict(data.get("ui", {}))
    ui[key] = value
    data["ui"] = ui

    lines = []
    for section, settings in data.items():
        lines.append(f"[{section}]")
        for k, v in settings.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            else:
                lines.append(f"{k} = {v}")
        lines.append("")

    content = "\n".join(lines)
    try:
        SYSFORGE_TOML_PATH.parent.mkdir(parents=True, exist_ok=True)
        SYSFORGE_TOML_PATH.write_text(content)
        return
    except PermissionError:
        pass
    # Root-owned target (installed system: /etc/sysforge/sysforge.toml) —
    # stage to a temp file and escalate, mirroring the makepkg.conf write path.
    import tempfile
    fd, tmp_name = tempfile.mkstemp(suffix=".sysforge.toml")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        _log.info(f"  Writing (sudo): {SYSFORGE_TOML_PATH}")
        rc = subprocess.run(
            privileged_argv(["cp", str(tmp), str(SYSFORGE_TOML_PATH)])
        ).returncode
        if rc != 0:
            raise OSError(f"sudo cp exited {rc} — {SYSFORGE_TOML_PATH} unchanged")
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Stage progress summary
# ---------------------------------------------------------------------------

def _show_stage_summary(state) -> None:
    _log.ui("─── Pipeline progress ───────────────────────────────")
    _symbols = {
        "done":       "✓",
        "skipped_to": "↷",
        "running":    "→",
        "failed":     "✗",
    }
    for name, _desc in _pipeline_stages():
        status = state.stage_status(name)
        if name == "reconfigure":
            symbol, label = "→", "running"
        else:
            symbol = _symbols.get(status, "·")
            label  = status or "pending"
        _log.ui(f"  {symbol}  {name:<16}  {label}")
    _log.ui("─────────────────────────────────────────────────────")


# ---------------------------------------------------------------------------
# Menu: step selection
# ---------------------------------------------------------------------------

def _show_step_menu() -> None:
    _log.ui("─── Steps ───────────────────────────────────────────")
    _log.ui("  [0]  cancel     Skip all steps and exit")
    for i, (key, label, desc) in enumerate(_STEPS, 1):
        _log.ui(f"  [{i}]  {key:<10}  {label}")
        _log.ui(f"              {desc}")
    _log.ui("─────────────────────────────────────────────────────")


def _parse_step_selection(raw: str) -> tuple[list[str], list[str]]:
    """
    Parse a step selection string into ``(selected_keys, invalid_tokens)``.

    Accepts any combination of:
      - '' or 'all'        → all steps
      - '1 3 5'            → steps by number
      - '2-6'              → inclusive range
      - 'network gpg'      → steps by name
      - '1-3 gpg preview'  → mixed
      - '0' or 'cancel'    → cancel (empty result, empty invalid)

    ``invalid_tokens`` lists tokens that didn't match any step / number /
    range. The caller is responsible for warning the user and either
    re-prompting (when nothing valid was parsed) or proceeding (when at
    least one valid token was parsed alongside the invalid ones).
    """
    raw = raw.strip()
    if not raw or raw.lower() == "all":
        return list(_STEP_KEYS), []
    if raw == "0" or raw.lower() == "cancel":
        return [], []

    selected: list[str] = []
    invalid: list[str] = []

    for token in raw.split():
        if token == "0":  # noqa: S105 — parsing token, not a secret
            return [], []
        # Range: 2-6
        if re.match(r"^\d+-\d+$", token):
            start, end = (int(x) for x in token.split("-", 1))
            if 1 <= start <= end <= len(_STEP_KEYS):
                for i in range(start, end + 1):
                    key = _STEP_KEYS[i - 1]
                    if key not in selected:
                        selected.append(key)
            else:
                invalid.append(token)
        # Number
        elif token.isdigit():
            i = int(token)
            if 1 <= i <= len(_STEP_KEYS):
                key = _STEP_KEYS[i - 1]
                if key not in selected:
                    selected.append(key)
            else:
                invalid.append(token)
        # Name
        elif token in _STEP_KEYS:
            if token not in selected:
                selected.append(token)
        else:
            invalid.append(token)

    return selected, invalid


def _select_steps(options) -> list[str]:
    """Return ordered list of step keys to run for this invocation."""
    if not _interactive() or options.dry_run:
        return list(_STEP_KEYS)

    _show_step_menu()
    while True:
        raw = _prompt(
            "[RECONFIGURE] Steps to run "
            "[Enter for all, '0' to cancel, or e.g. '1 3', '2-5', 'network gpg']: "
        )
        steps, invalid = _parse_step_selection(raw)

        # Explicit cancel ('0' / 'cancel' / empty after cancel).
        if not steps and not invalid:
            if raw and raw.lower() not in ("all",):
                _log.ui("Cancelled.")
                return []
            # Empty/'all' selects everything (handled by parser already).
            return list(_STEP_KEYS)

        # Nothing recognized — warn and re-prompt instead of silently
        # falling back to "run all", which is what the caller used to do.
        if not steps and invalid:
            _log.warn(
                f"  Unrecognized input: {' '.join(invalid)}. "
                f"Valid step names: {', '.join(_STEP_KEYS)}. "
                f"Valid numbers: 1-{len(_STEP_KEYS)} (or ranges like '2-5'). "
                f"Press Enter for all, '0' to cancel."
            )
            continue

        # Partially valid — proceed with what parsed, but tell the user
        # what was ignored so typos don't go unnoticed.
        if invalid:
            _log.warn(f"  Ignoring unrecognized tokens: {' '.join(invalid)}")

        _log.info(f"Running steps: {', '.join(steps)}")
        return steps


# ---------------------------------------------------------------------------
# Step: editor selection
#
# Editor resolution / usability / TTY-safe launch live in
# sysforge.primitives.editor (imported above as _resolve_editor /
# _editor_usable / _run_editor_argv) so the merge verb and this stage share
# one home for the /dev/tty rebinding and resolution order.
# ---------------------------------------------------------------------------

def _packages_providing(editor_cmd: str) -> list[str]:
    """
    Use pacman's files database to find packages providing ``/usr/bin/<editor_cmd>``.
    Returns deduped package names (without the ``repo/`` prefix), or ``[]`` when
    nothing matches or the files DB is unavailable.

    Arch packages install binaries to ``/usr/bin/`` (never ``/usr/local/bin/``),
    so a single ``pacman -Fq`` query is sufficient. If the files DB has never
    been synced, ``pacman -Fq`` exits non-zero; the caller hints at ``pacman -Fy``.
    """
    if not shutil.which("pacman"):
        return []
    basename = Path(editor_cmd).name
    if not basename:
        return []
    result = subprocess.run(
        ["pacman", "-Fq", f"/usr/bin/{basename}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    pkgs: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pkg = line.split("/", 1)[1] if "/" in line else line
        if pkg not in pkgs:
            pkgs.append(pkg)
    return pkgs


def _choose_install_package(editor_cmd: str, options=None) -> str | None:
    """
    Pick a pacman package to install for ``editor_cmd`` without making the user
    memorize the bin→package mapping (``nvim`` is provided by ``neovim``, etc.).

    - one candidate     → confirm and install
    - multiple matches  → numbered picker
    - no matches        → fall back to a typed package name, validated against
      ``pacman -Si``

    The bin→package lookup needs pacman's files database. If it has never been
    synced, sync it automatically (``sudo pacman -Fy``) rather than dead-ending
    the flow with a "run it yourself" hint — except under ``--dry-run``, where
    the sync is reported but not run.

    Returns the chosen package name, or ``None`` if the user cancelled.
    """
    if not files_db_present():
        if options is not None and getattr(options, "dry_run", False):
            _log.ui("  [dry-run] would sync the pacman files db (sudo pacman -Fy)")
        else:
            _log.info("  Syncing the pacman files db (sudo pacman -Fy)…")
            if not sync_files_db():
                _log.warn(
                    "  Could not sync the files db; "
                    "package auto-detection may be incomplete."
                )

    candidates = _packages_providing(editor_cmd)

    if len(candidates) == 1:
        pkg = candidates[0]
        confirm = _prompt_choice(
            f"  /usr/bin/{editor_cmd} is provided by {pkg!r}. Install? [y/N]: ",
            choices=("y", "n"),
            default="n",
        )
        return pkg if confirm == "y" else None

    if len(candidates) > 1:
        _log.ui(f"  Multiple packages provide /usr/bin/{editor_cmd}:")
        for i, pkg in enumerate(candidates, 1):
            _log.ui(f"    [{i}] {pkg}")
        while True:
            try:
                raw = _prompt_key(
                    f"  Pick a package [1-{len(candidates)}, Enter to cancel]: "
                )
            except EOFError:
                return None
            if not raw:
                return None
            if raw.isdigit():
                idx = int(raw)
                if 1 <= idx <= len(candidates):
                    return candidates[idx - 1]
            _log.warn(f"  Invalid selection: {raw!r}")

    _log.ui(
        f"  pacman has no package providing /usr/bin/{editor_cmd}."
    )
    pkg_name = _prompt("  Pacman package name to install [Enter to cancel]: ")
    if not pkg_name:
        return None
    check = subprocess.run(
        ["pacman", "-Si", pkg_name],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        _log.ui(
            f"  {pkg_name!r} not found in pacman repos. "
            f"Run 'pacman -Ss {editor_cmd}' to find the right package name."
        )
        return None
    return pkg_name


def _try_install_editor(editor_cmd: str, options) -> bool:
    """
    Install the pacman package providing ``editor_cmd`` and verify the binary
    lands on PATH. The package is auto-detected via ``pacman -F`` so the user
    doesn't have to know which package ships which binary.

    Returns True only when the install actually produced ``editor_cmd``. False
    on user cancel, no match without a manual fallback, install error, missing
    binary post-install, or dry-run.
    """
    pkg_name = _choose_install_package(editor_cmd, options)
    if pkg_name is None:
        return False

    if options.dry_run:
        _log.ui(f"  [dry-run] would install {pkg_name!r}")
        return False

    # Sentinel scope: pacman -S is atomic within its own transaction, but
    # wrapping the call keeps install-bearing reconfigure paths consistent
    # with the toolchain / kernel / packages stages — any interruption
    # leaves a sentinel for the next sysforge invocation to surface.
    from sysforge.primitives.stage_sentinel import sentinel_scope

    state_dir = getattr(options, "state_dir", None)
    with sentinel_scope(
        state_dir,
        "reconfigure-editor",
        retry_cmd="sysforge run reconfigure",
        package=pkg_name,
        editor=editor_cmd,
    ):
        result = subprocess.run(
            privileged_argv(["pacman", "-S", "--needed", "--noconfirm", pkg_name])
        )
    if result.returncode != 0 or not shutil.which(editor_cmd):
        _log.ui(
            f"  Install of {pkg_name!r} did not produce {editor_cmd!r} on PATH. "
            f"Install manually (e.g. 'pacman -S <pkg>' where the package "
            f"provides /usr/bin/{editor_cmd})."
        )
        return False

    _log.ui(f"  {pkg_name} installed; {editor_cmd} now on PATH")
    return True


def _select_new_editor(prev_editor: str, have_prev: bool, options) -> str | None:
    """
    Loop until the user picks a working editor, installs one, or cancels.

    Returns the new editor command (different from ``prev_editor``) on success.
    Returns ``None`` when the user kept ``prev_editor`` or cancelled — the
    caller should keep the previous editor (which may be ``""`` if none).
    """
    while True:
        if have_prev:
            new_editor = _prompt(
                f"  Enter editor command (e.g. nano, vi) [{prev_editor}]: "
            ) or prev_editor
            if new_editor == prev_editor:
                return None  # user kept the current editor
        else:
            new_editor = _prompt(
                "  Enter editor command (e.g. nano, vi; Enter to skip): "
            )
            if not new_editor:
                _log.ui("  No editor selected — config file edits will be skipped.")
                return None

        if shutil.which(new_editor):
            if Path(new_editor).name in _KNOWN_EDITORS or _confirm_unknown_editor(new_editor):
                return new_editor
            continue  # off-list and user declined — pick again

        _log.ui(f"  {new_editor!r} not found in PATH.")
        action = _prompt_choice(
            "  [i]nstall via pacman / [r]e-enter editor / [Enter] cancel: ",
            choices=("i", "r"),
        )
        if action == "r":
            continue  # back to the editor-name prompt
        if action != "i":
            # Empty / cancel.
            if have_prev:
                _log.ui(f"  Keeping {prev_editor!r}.")
            return None

        if _try_install_editor(new_editor, options):
            # Install succeeded but the command might still be a non-editor
            # (user typed 'htop' as editor; pacman happily installed htop).
            # Confirm before persisting — but leave the package installed,
            # since rollback would surprise the user.
            if Path(new_editor).name in _KNOWN_EDITORS or _confirm_unknown_editor(new_editor):
                return new_editor
            continue
        # Install failed. Loop back to the editor-name prompt so the user can
        # try a different editor, retry the install with a different package
        # name, or cancel.


_EDITOR_SUGGESTIONS = ("nano", "vi", "vim", "nvim", "micro")

# Recognized text editors. Anything outside this set still works but requires
# an explicit "use anyway" confirmation — catches typos that would otherwise
# install (via sudo pacman -S) and persist a non-editor command (htop, tmux,
# less, …) as the user's default $EDITOR.
_KNOWN_EDITORS = frozenset({
    "nano", "vi", "vim", "nvim", "emacs", "emacsclient", "micro",
    "helix", "hx", "kak", "kakoune", "ed", "ne", "joe", "mg", "jed",
    "mcedit", "jove",
    "gvim", "gedit", "kate", "kwrite", "gnome-text-editor",
    "mousepad", "leafpad", "geany",
    "code", "codium", "code-oss", "subl", "sublime_text",
})


def _format_editor_suggestions() -> list[str]:
    """Return one log line per suggestion, annotated installed/installable."""
    lines = []
    for name in _EDITOR_SUGGESTIONS:
        tag = "installed" if shutil.which(name) else "installable"
        lines.append(f"    {name:<6}  ({tag})")
    return lines


def _confirm_unknown_editor(cmd: str) -> bool:
    """
    Warn that ``cmd`` isn't on the known-editor list and require explicit
    confirmation before accepting it. The hard case this guards against is a
    user installing+saving an arbitrary pacman package as their editor (e.g.
    ``htop`` typed when ``nano`` was intended).
    """
    _log.warn(
        f"  '{cmd}' is not on the known-editor list. "
        f"Saving an arbitrary command as $EDITOR can cause data loss "
        f"if a later config-edit prompt opens it."
    )
    return _prompt_choice(
        f"  Use {cmd!r} as editor anyway? [y/N]: ",
        choices=("y", "n"),
        default="n",
    ) == "y"


def _adopt_editor(new_editor: str) -> None:
    """
    Make ``new_editor`` visible to the rest of the process.

    The stage threads its editor choice through ``_run_selected_steps`` as a
    plain local, but every downstream consumer (``makepkg_invoke``'s PKGBUILD
    retry menu, the artifact verb) calls ``resolve_editor()`` fresh. Without
    this export, a pick the user declined to persist would be invisible the
    moment reconfigure returns, and the build stages would resolve
    ``("", "none")`` again. ``SYSFORGE_EDITOR`` is the highest-precedence
    input to :func:`sysforge.primitives.editor.resolve_editor`, so setting it
    covers this process and any child it spawns — no second resolution path.
    """
    if new_editor:
        os.environ["SYSFORGE_EDITOR"] = new_editor


def _parse_target_selection(raw: str, keys: list[str]) -> tuple[list[str], list[str]]:
    """Parse a persistence-target selection into ``(selected, invalid)``.

    Accepts numbers (``1``), names (``system``), or a mix (``1 user``), in the
    same input style as :func:`_parse_step_selection`. Empty input selects
    nothing — persistence is opt-in.
    """
    selected: list[str] = []
    invalid: list[str] = []
    for token in raw.split():
        if token.isdigit():
            i = int(token)
            if 1 <= i <= len(keys):
                if keys[i - 1] not in selected:
                    selected.append(keys[i - 1])
            else:
                invalid.append(token)
        elif token in keys:
            if token not in selected:
                selected.append(token)
        else:
            invalid.append(token)
    return selected, invalid


def _persist_to_file_target(target, new_editor: str) -> None:
    """Plan, confirm and apply one file target. Both variables or neither."""
    try:
        existing = target.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = None
    except (OSError, UnicodeDecodeError) as e:
        # Absence and failure are not the same thing: plan_write treats
        # existing=None as "file does not exist" and happily emits a
        # create-style plan. If the file actually exists with other content
        # but the read failed for some other reason, falling through here
        # would silently truncate it down to just EDITOR/VISUAL. Refuse and
        # move on instead — this target is skipped, not the whole stage.
        _log.warn(f"  Could not read {target.path}: {e}")
        return

    plan = plan_write(target, {"EDITOR": new_editor, "VISUAL": new_editor}, existing)
    _log.ui(f"  {target.path}:")
    if plan.action == "nochange":
        _log.ui("    already set — no change")
        return

    for change in plan.changes:
        current = change.current if change.current is not None else "unset"
        _log.ui(f"    {change.name:<8} currently {current:<10} → {change.new}")
    if _prompt_choice("  Apply both? [y/N]: ", choices=("y", "n"), default="n") != "y":
        _log.ui("  Skipped.")
        return

    try:
        apply_write(plan)
    except OSError as e:
        _log.warn(f"  Could not write {target.path}: {e}")
        return
    _log.ui(f"  Wrote {target.path} — takes effect: {target.scope_note}.")


def _warn_if_sysforge_toml_shadows(new_editor: str, selected: list[str]) -> None:
    """Warn when ``[ui] editor`` will override the ``EDITOR`` about to be written.

    ``sysforge.toml [ui] editor`` is rung 2 of the resolution chain and
    ``$EDITOR`` is rung 3, so persisting to a file target alone while rung 2
    holds a *different* value gives the user a system-wide ``EDITOR`` that
    sysforge itself ignores — silently, and reachable in two runs (pick target
    1 first, a different editor and target 2 second). This is the shadowing
    the chain display exists to surface; leaving the *write* path quiet about
    it undercuts the feature. Naming both values and what will actually launch
    is the whole point — a bare "conflict" line would not tell the user which
    editor they are getting.
    """
    if "sysforge" in selected:
        return                       # rung 2 is being rewritten to agree
    configured = load_sysforge_toml().get("ui", {}).get("editor")
    if not configured or configured == new_editor:
        return
    _log.warn(
        f"  sysforge.toml [ui] editor = {configured} outranks $EDITOR, so the "
        f"{new_editor} you are about to write will be ignored by sysforge "
        f"itself — it will keep launching {configured}. Other programs will "
        f"use {new_editor}. Include target 1 to change both."
    )


def _offer_persist_editor(new_editor: str) -> None:
    """Adopt ``new_editor`` for this run, then offer to persist it.

    Adoption is unconditional; persistence is the user's call. Declining the
    save no longer means the pick evaporates — it only means the next
    ``sysforge`` invocation starts from the same resolution order.

    Each target is confirmed and applied independently, but the two variables
    within a target move together: a mismatched ``EDITOR``/``VISUAL`` pair is
    worse than either value alone, so a declined confirm leaves the file
    untouched rather than half-written.
    """
    _adopt_editor(new_editor)

    targets = [system_target(), user_target()]
    keys = ["sysforge"] + [t.key for t in targets]

    _log.ui(f"  Persist {new_editor} as EDITOR and VISUAL:")
    _log.ui(f"    [1] sysforge only   {SYSFORGE_TOML_PATH}")
    for i, t in enumerate(targets, start=2):
        root = ", root" if t.needs_root else ""
        _log.ui(f"    [{i}] {t.label:<14} {t.path}    [{t.scope_note}{root}]")

    raw = _prompt("  Select (e.g. 1, or '1 2'; Enter to skip): ")
    selected, invalid = _parse_target_selection(raw, keys)
    if invalid:
        _log.warn(f"  Ignoring unrecognized selection: {' '.join(invalid)}")
    if not selected:
        _log.ui("  Not persisted — the pick applies to this run only.")
        return

    _warn_if_sysforge_toml_shadows(new_editor, selected)

    for key in selected:
        if key == "sysforge":
            if _prompt_choice(
                f"  Write [ui] editor = {new_editor} to {SYSFORGE_TOML_PATH}? [y/N]: ",
                choices=("y", "n"), default="n",
            ) != "y":
                _log.ui("  Skipped.")
                continue
            try:
                _save_sysforge_toml_ui("editor", new_editor)
                _log.ui(f"  Saved to {SYSFORGE_TOML_PATH}")
            except OSError as e:
                _log.warn(f"  Could not save preference: {e}")
            continue
        target = next(t for t in targets if t.key == key)
        _persist_to_file_target(target, new_editor)


def _require_usable_editor(prev_editor: str, options, *, needed_for: str) -> str:
    """
    Guarantee a usable editor before continuing with an editor-needing step.

    Returns a non-empty editor command that resolves on PATH. If the user
    cancels the picker without selecting a usable editor, raises
    RuntimeError to abort the reconfigure stage cleanly — preferable to
    silently failing every subsequent edit prompt.
    """
    if _editor_usable(prev_editor):
        return prev_editor

    _log.ui("─── Editor required ─────────────────────────────────")
    _log.ui(f"  The next step ({needed_for}) needs an editor.")
    _log.ui("  Suggested editors:")
    for line in _format_editor_suggestions():
        _log.ui(line)

    have_prev = bool(prev_editor)
    while True:
        new_editor = _select_new_editor(prev_editor, have_prev, options)
        if new_editor and _editor_usable(new_editor):
            _offer_persist_editor(new_editor)
            return new_editor

        # User cancelled / kept an unusable previous editor. No path forward
        # for the gated step — abort the stage with a clear message rather
        # than running through the rest of the queue as silent no-ops.
        raise RuntimeError(
            "[RECONFIGURE] Aborted: a usable editor is required for the "
            f"selected step ({needed_for}). Re-run with a different step "
            "selection to skip editor-needing steps."
        )


def _format_editor_chain() -> list[str]:
    """Render the EDITOR resolution order for display.

    Precedence is not recomputed here — ``describe_editor_chain`` owns it, so
    the display cannot disagree with the editor that actually launches. Rungs
    holding a value that lost are marked ``(shadowed by N)``: without it, two
    rungs showing different editors is ambiguous about which one runs, which
    is precisely the confusion this step exists to resolve.

    Env rungs carry a sub-listing of the files that assign them, each with the
    value it contributes — two files setting ``EDITOR`` differently is the very
    ambiguity this display resolves, so naming the source without its value
    only moves the question. ``$VISUAL`` is listed alongside ``$EDITOR``
    because the persistence step writes both.

    One :func:`collect_env_chain` snapshot serves every lookup: ``sources_defining``
    collects its own when passed none, and that reads ~14 init files and spawns
    a ``systemctl`` probe — per sub-listing, not per render.
    """
    rungs, winner = describe_editor_chain()
    snap = collect_env_chain()
    sub_listed = {"$EDITOR": "EDITOR", "$VISUAL": "VISUAL"}
    lines = [
        "  Resolution chain for EDITOR  "
        "(1 = highest priority if set; first match wins):"
    ]
    for i, rung in enumerate(rungs):
        shown = rung.value or "(unset)"
        if i == winner:
            note = "← in use"
        elif rung.value and not rung.usable:
            note = f"({rung.detail})"
        elif rung.source == "detected":
            note = "(last resort)"
        elif rung.value and winner >= 0 and i > winner:
            note = f"(shadowed by {winner + 1})"
        else:
            note = ""
        lines.append(f"    {rung.index}  {rung.label:<22} {shown:<12} {note}".rstrip())
        var = sub_listed.get(rung.source)
        if var is None:
            continue
        for j, row in enumerate(sources_defining(var, snap)):
            prefix = "from" if j == 0 else "also"
            reason = f"   (not offered: {row.reason})" if not row.offered else ""
            origin = row.path or row.source
            lines.append(f"         {prefix}  {origin:<34} = {row.value}{reason}")
    return lines


def _step_editor(config, state, options, editor: str) -> str:
    """Show current editor, offer to change. Returns editor to use."""
    editor, source = _resolve_editor()
    _log.ui("─── Editor selection ────────────────────────────────")

    if not _interactive() or options.dry_run:
        return editor

    for line in _format_editor_chain():
        _log.ui(line)

    have_editor = _editor_usable(editor)

    if have_editor:
        choice = _prompt_choice(
            f"  Editor: {editor} (from {source}). Change? [e]dit / [Enter] keep: ",
            choices=("e",),
        )
        if choice != "e":
            return editor
    else:
        # No editor on PATH — don't offer a "keep" path that would silently
        # propagate an unusable editor into the rest of the stage.
        _log.ui("  No editor found on PATH — pick one to use for config edits.")

    new_editor = _select_new_editor(editor, have_editor, options)
    if new_editor is None:
        return editor  # caller falls back to the previous editor (may be "")

    _offer_persist_editor(new_editor)
    return new_editor


# ---------------------------------------------------------------------------
# Step: config file review
# ---------------------------------------------------------------------------

def _validate_flag_profiles(path: Path) -> tuple[bool, str]:
    try:
        with path.open("rb") as f:
            tomllib.load(f)  # pre-validate TOML syntax
    except Exception as e:
        return False, f"TOML parse error: {e}"
    try:
        cfg = load_config(config_paths=[path])
        if cfg is None:
            return False, "load_config returned None"
        conflict_groups = load_conflict_groups()
        from sysforge.primitives.profile import merge_extends
        profiles = cfg.get("profiles", {})
        for name in profiles:
            merge_extends(name, profiles, conflict_groups=conflict_groups)
        return True, f"{len(profiles)} profiles, {len(cfg.get('rules', []))} rules"
    except Exception as e:
        return False, str(e)


def _edit_needs_sudo(path: Path) -> bool:
    """True when ``path`` can't be saved as the current user, so the editor
    must be launched under ``sudo``.

    The reconfigure stage runs as a normal user, but the shipped config files
    live in root-owned ``/etc/sysforge``. Opening them with a plain
    ``[editor, path]`` lets the editor *read* the file but silently refuse the
    write — the "opens read-only" symptom. We need sudo when either the file
    itself isn't writable *or* its parent directory isn't: TUI editors save via
    a temp-file + atomic rename, which needs write permission on the directory,
    not just the file. Running as root (``os.access`` sees W_OK everywhere)
    short-circuits to False, so this is a no-op in that case. Mirrors the
    ``sudo`` privilege model already used by the makepkg.conf edit path.
    """
    try:
        return not (os.access(path, os.W_OK) and os.access(path.parent, os.W_OK))
    except OSError:
        # If we can't even stat it, assume sudo is the safer bet.
        return True


def _open_in_editor(path: Path, editor: str) -> bool:
    """
    Launch ``editor`` on ``path``. Returns False when the editor couldn't be
    launched at all (no editor configured, not on PATH, FileNotFoundError),
    so the caller can skip the validation pass that would otherwise produce
    a misleading "✓" on a file that was never actually opened. A non-zero
    exit from a launched editor still counts as "ran" (returns True) — the
    user may have edited the file and closed with an error code, and we want
    to validate either way.

    Root-owned config files (``/etc/sysforge/*.toml``) are opened under ``sudo``
    so the editor can actually save; see :func:`_edit_needs_sudo`.
    """
    if not _editor_usable(editor):
        _log.ui(f"  Skipping {path.name} — no usable editor available.")
        return False
    argv = [editor, str(path)]
    if _edit_needs_sudo(path):
        argv = privileged_argv(argv)
        _log.info(f"  Opening (sudo): {editor} {path}")
    else:
        _log.info(f"  Opening: {editor} {path}")
    rc = _run_editor_argv(argv)
    if rc == -1:
        _log.ui(f"  Editor not found: {editor!r}")
        return False
    if rc != 0:
        _log.ui(f"  Editor {editor!r} exited with code {rc} for {path.name}")
    return True


def _review_config_file(
    label: str,
    path: Path,
    editor: str,
    dry_run: bool,
    validate_fn=None,
    warn: str | None = None,
) -> None:
    exists = path.exists()
    _log.ui(
        f"  {label}: {path}" if exists else f"  {label}: {path}  (not found — skipping)"
    )
    if not exists or not _interactive() or dry_run:
        return

    if _prompt_choice(
        f"    {label} ({path.name}) — [e]dit / [Enter] skip: ",
        choices=("e",),
    ) != "e":
        return

    if warn:
        _log.warn(f"    ⚠  {warn}")
        if _prompt_choice(
            "    Proceed? [y/N]: ",
            choices=("y", "n"),
            default="n",
        ) != "y":
            return

    while True:
        if not _open_in_editor(path, editor):
            # Editor couldn't be launched. Skip the validation pass entirely:
            # validating an unopened file would print a misleading "✓" and
            # bury the user's "I wanted to edit this" intent.
            return
        if validate_fn is None:
            break
        _log.info(f"    Validating {label}...")
        ok, msg = validate_fn(path)
        if ok:
            _log.ui(f"    ✓ {msg}")
            break
        _log.warn(f"    ✗ {msg}")
        action = _prompt_choice(
            "    [r]e-open in editor / [s]kip (keep previous) / [a]bort: ",
            choices=("r", "s", "a"),
            default="s",
        )
        if action == "r":
            continue
        elif action == "a":
            raise RuntimeError(f"[RECONFIGURE] Aborted at {label} validation failure")
        else:
            _log.warn("    Skipping — file may be invalid, proceeding with caution")
            break


def _step_config(config, state, options, editor: str) -> str:
    _log.ui("─── Config file review ──────────────────────────────")

    _review_config_file(
        "profiles.toml",
        CONFIG_DIR / "profiles.toml",
        editor, options.dry_run,
        validate_fn=_validate_flag_profiles,
    )
    _review_config_file(
        "packages.toml", PACKAGES_PATH, editor, options.dry_run,
    )

    if TOOLCHAIN_PATH.exists():
        _review_config_file("toolchain.toml", TOOLCHAIN_PATH, editor, options.dry_run)

    if KERNEL_PATH.exists():
        _review_config_file("kernel.toml", KERNEL_PATH, editor, options.dry_run)

    state_dir, _ = resolve_state_dir(None)
    hw_path = Path(config.get("hardware_profile") or state_dir / "hardware_profile.toml")
    if hw_path.exists():
        _review_config_file(
            "hardware_profile.toml", hw_path, editor, options.dry_run,
            warn="Machine-generated by the hardware stage. "
                 "Manual edits can cause driver mismatches or broken kconfig.",
        )
    return editor


# ---------------------------------------------------------------------------
# Step: build mode (repo_mode)
# ---------------------------------------------------------------------------

def _set_repo_mode(pkg_path: Path, mode: str) -> None:
    """
    Write repo_mode to the [build] section of packages.toml in-place,
    preserving all other content and comments.

    Search order:
    1. Replace existing repo_mode = "..." line.
    2. Insert after [build] header if not found.
    3. Append a new [build] section if none exists.
    """
    text = pkg_path.read_text(encoding="utf-8")

    new_text, n = re.subn(
        r'^(repo_mode\s*=\s*)"[^"]*"',
        f'\\1"{mode}"',
        text,
        flags=re.MULTILINE,
    )
    if n:
        pkg_path.write_text(new_text, encoding="utf-8")
        return

    # Insert immediately after [build] header
    new_text = re.sub(
        r'(\[build\][^\n]*\n)',
        f'\\1repo_mode = "{mode}"\n',
        text,
        count=1,
    )
    if new_text != text:
        pkg_path.write_text(new_text, encoding="utf-8")
        return

    # No [build] section — append one
    pkg_path.write_text(
        text.rstrip("\n") + f'\n\n[build]\nrepo_mode = "{mode}"\n', encoding="utf-8"
    )


def _step_build_mode(config, state, options, editor: str) -> str:
    _log.ui("─── Build mode ──────────────────────────────────────")

    pkg_path = resolve_packages_path(config)
    if not pkg_path.exists():
        _log.ui(f"  packages.toml not found at {pkg_path} — skipping")
        return editor

    try:
        with pkg_path.open("rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        _log.warn(f"  Could not load packages.toml: {e}")
        return editor

    build_cfg = data.get("build", {})
    repo_mode = resolve_repo_mode(build_cfg)
    packages  = expand_package_groups(data)
    enabled   = [p["name"] for p in packages if p.get(PKG_KEY_BUILD_FROM_SOURCE)]

    _log.ui(f"  File:       {pkg_path}")
    _log.ui(f"  repo_mode:  {repo_mode}")
    _log.ui("  pacman            — repo packages installed via pacman (no source build)")
    _log.ui("  build_from_source — repo packages cloned and built from source with profile flags")

    if enabled:
        _log.ui(
            f"  Per-package enable_build_from_source overrides ({len(enabled)}): "
            + ", ".join(enabled)
        )
    else:
        _log.ui("  No per-package enable_build_from_source overrides.")

    if not _interactive() or options.dry_run:
        return editor

    choice = _prompt_choice(
        f"  Change repo_mode from {repo_mode!r}? "
        "[p]acman / [s]build_from_source / [Enter] keep: ",
        choices=("p", "s", REPO_MODE_PACMAN, REPO_MODE_SOURCE),
    )

    if choice in ("p", REPO_MODE_PACMAN):
        new_mode = REPO_MODE_PACMAN
    elif choice in ("s", REPO_MODE_SOURCE):
        new_mode = REPO_MODE_SOURCE
    else:
        return editor

    if new_mode == repo_mode:
        _log.ui(f"  Already {repo_mode!r} — no change.")
        return editor

    try:
        _set_repo_mode(pkg_path, new_mode)
        _log.ui(f"  repo_mode set to {new_mode!r} in {pkg_path}")
    except OSError as e:
        _log.warn(f"  Could not write {pkg_path}: {e}")

    return editor


# ---------------------------------------------------------------------------
# Step: desktop environment
# ---------------------------------------------------------------------------

def _existing_desktop_group(pkg_path: Path) -> str | None:
    """Return the catalog key of an already-written ``[group.<de>]`` table in
    ``pkg_path``, or ``None``. Lets :func:`_step_desktop` skip re-prompting
    when the configure stage already wrote a desktop selection."""
    if not pkg_path.exists():
        return None
    try:
        with pkg_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for key in data.get("group", {}):
        if key in DESKTOP_CATALOG:
            return key
    return None


def _step_desktop(config, state, options, editor: str) -> str:  # noqa: ARG001
    """Offer a curated desktop-environment package group and write it as a
    ``[group.<de>]`` table into packages.toml. Reuses the shared selection
    prompt + writer in :mod:`sysforge.primitives.pkg_catalog`."""
    _log.ui("─── Desktop environment ─────────────────────────────")

    pkg_path = resolve_packages_path(config)
    existing = _existing_desktop_group(pkg_path)
    if existing:
        _log.ui(f"  [group.{existing}] already configured in {pkg_path} — skipping prompt.")
        return editor

    choice = select_desktop(interactive=_interactive(), preselected=None)
    if not choice:
        _log.ui("  No desktop environment selected.")
        return editor

    if options.dry_run:
        _log.ui(f"  [dry-run] would write [group.{choice}] to {pkg_path}")
        return editor

    try:
        write_desktop_group(pkg_path, choice)
        _log.ui(f"  Wrote [group.{choice}] to {pkg_path} — install with 'sysforge run packages'.")
    except OSError as e:
        _log.warn(f"  Could not write {pkg_path}: {e}")

    return editor


# ---------------------------------------------------------------------------
# Step: makepkg.conf review
# ---------------------------------------------------------------------------

def _git_packager_default() -> str:
    """Best-effort ``Name <email>`` from git config, or '' when unavailable."""
    def _get(key: str) -> str:
        try:
            r = subprocess.run(
                ["git", "config", "--get", key],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""

    name = _get("user.name")
    email = _get("user.email")
    if name and email:
        return f"{name} <{email}>"
    return name or ""


def _offer_makepkg_defaults(conf: dict, conf_path: Path) -> None:
    """Offer to fill in PACKAGER / MAKEFLAGS when missing or left at default.

    A fresh ``/etc/makepkg.conf`` ships ``PACKAGER="Unknown Packager"`` and no
    ``MAKEFLAGS``, so every locally built package is stamped anonymously and
    builds run single-threaded. Writes the system conf via a sudo ``cp`` of a
    staged temp file (mirroring the editor path's privilege model).
    """
    pending: dict[str, str] = {}

    packager = conf.get("PACKAGER", "").strip().strip("\"'")
    if not packager or packager == "Unknown Packager":
        default = _git_packager_default()
        suffix = f" [{default}]" if default else ""
        value = _prompt(
            f"  Set PACKAGER (Enter to skip){suffix}: ", default=default,
        ).strip()
        if value:
            pending["PACKAGER"] = value

    if "MAKEFLAGS" not in conf:
        suggest = f"-j{os.cpu_count() or 1}"
        if _prompt_choice(
            f"  Set MAKEFLAGS={suggest} for parallel builds? [y/Enter skip]: ",
            choices=("y",),
        ) == "y":
            pending["MAKEFLAGS"] = suggest

    if not pending:
        return

    import tempfile
    fd, tmp_name = tempfile.mkstemp(suffix=".makepkg.conf")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        set_makepkg_conf_keys(conf_path, pending, dest=tmp)
        _log.info(f"  Writing (sudo): {', '.join(pending)} → {conf_path}")
        rc = subprocess.run(privileged_argv(["cp", str(tmp), str(conf_path)])).returncode
        if rc != 0:
            _log.warn(f"  sudo cp exited {rc} — {conf_path} unchanged")
    finally:
        tmp.unlink(missing_ok=True)


def _step_makepkg(config, state, options, editor: str) -> str:
    _log.ui("─── System makepkg.conf ─────────────────────────────")

    from sysforge.primitives.config import (
        SYSTEM_MAKEPKG_CONF,
        parse_system_makepkg_conf,
    )

    conf_path = SYSTEM_MAKEPKG_CONF
    conf = parse_system_makepkg_conf()

    if not conf:
        _log.warn(f"  Could not read {conf_path}")
        return editor

    for key in _MAKEPKG_CONF_HIGHLIGHT:
        if key in conf:
            _log.ui(f"  {key:<12} = {conf[key]}")

    _log.ui(
        "  (sysforge profile overrides CFLAGS / CXXFLAGS / LDFLAGS at build time)"
    )

    if "BUILDDIR" in conf:
        builddir = Path(conf["BUILDDIR"].strip("\"'")).expanduser()
        if not builddir.exists():
            _log.warn(
                f"  ⚠  BUILDDIR {str(builddir)!r} does not exist"
            )
        else:
            space = storage_probe.probe_free_space(builddir)
            if space is not None:
                _log.ui(f"  BUILDDIR free: {space[0]:.1f} GB")

    if _interactive() and not options.dry_run:
        _offer_makepkg_defaults(conf, conf_path)
        if _prompt_choice(
            "  Edit /etc/makepkg.conf? (requires sudo) [e/Enter skip]: ",
            choices=("e",),
        ) == "e":
            if not _editor_usable(editor):
                _log.warn(
                    f"  Editor {editor!r} is not on PATH — skipping makepkg.conf edit."
                )
            else:
                _log.info(f"  Opening (sudo): {editor} {conf_path}")
                rc = _run_editor_argv(privileged_argv([editor, str(conf_path)]))
                if rc == -1:
                    _log.warn(f"  Editor not found: {editor!r}")
                elif rc != 0:
                    _log.warn(f"  Editor exited with code {rc} — makepkg.conf may be unchanged")

    return editor


# ---------------------------------------------------------------------------
# Step: user / sudo verification
# ---------------------------------------------------------------------------

def _step_sudo(config, state, options, editor: str) -> str:
    _log.ui("─── User / sudo verification ────────────────────────")

    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "(unknown)"
    _log.ui(f"  Running as: {user}")

    if os.geteuid() == 0:
        _log.warn(
            "  Running as root. SysForge should be run as a regular user with sudo access."
        )
        return editor

    result = subprocess.run(["sudo", "-n", "true"], capture_output=True)
    if result.returncode == 0:
        _log.ui("  sudo: OK (passwordless)")
        return editor

    result2 = subprocess.run(["id", "-Gn", user], capture_output=True, text=True)
    groups = result2.stdout.strip().split() if result2.returncode == 0 else []
    if "wheel" in groups or "sudo" in groups:
        _log.ui("  sudo: requires password (user is in wheel/sudo — OK)")
    else:
        _log.warn(
            f"  sudo: user {user!r} is not in wheel or sudo group. "
            "makepkg -si will fail when trying to install packages."
        )
    return editor


# ---------------------------------------------------------------------------
# Step: disk space
# ---------------------------------------------------------------------------

def _step_disk(config, state, options, editor: str) -> str:
    _log.ui("─── Disk space ──────────────────────────────────────")

    build_dir = Path(
        config.get("paths", {}).get("pkgbuild_src_dir", "~")
    ).expanduser()

    space = storage_probe.probe_free_space(build_dir)
    if space is None:
        _log.warn(f"  Could not check disk space on {build_dir}")
        return editor
    free_gb, total_gb = space

    # Count AUR/git packages for estimate
    n_aur = 0
    try:
        pkg_path = resolve_packages_path(config)
        if pkg_path.exists():
            with pkg_path.open("rb") as f:
                data = tomllib.load(f)
            n_aur = sum(
                1 for p in expand_package_groups(data)
                if p.get("source", "aur") in ("aur", "git")
            )
    except (OSError, tomllib.TOMLDecodeError) as e:
        _log.info(f"  packages.toml unreadable for disk estimate: {e}")

    est_gb = n_aur * _DISK_PER_PKG_GB
    _log.ui(
        f"  Build dir: {build_dir}  ({free_gb:.1f} GB free of {total_gb:.1f} GB)"
    )
    if n_aur:
        _log.ui(
            f"  Estimated build space: ~{est_gb} GB  "
            f"({n_aur} AUR/git packages × {_DISK_PER_PKG_GB} GB each)"
        )

    if free_gb < _DISK_WARN_GB:
        _log.warn(
            f"  ⚠  Only {free_gb:.1f} GB free — "
            f"recommended minimum is {_DISK_WARN_GB} GB"
        )
    elif n_aur and free_gb < est_gb:
        _log.warn(
            f"  ⚠  Estimated requirement ({est_gb} GB) exceeds "
            f"available space ({free_gb:.1f} GB)"
        )
    else:
        _log.ui("  Disk space: OK")

    return editor


# ---------------------------------------------------------------------------
# Step: network probe
# ---------------------------------------------------------------------------

def _probe_host(host: str, port: int = 443, timeout: int = 5) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _step_network(config, state, options, editor: str) -> str:
    _log.ui("─── Network connectivity ────────────────────────────")

    endpoints = [
        ("AUR",        "aur.archlinux.org", 443),
        ("GitHub",     "github.com",        443),
        ("Arch Linux", "archlinux.org",     443),
    ]

    try:
        for line in Path("/etc/pacman.d/mirrorlist").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("Server"):
                m = re.search(r"https?://([^/]+)/", line)
                if m:
                    endpoints.append(("Pacman mirror", m.group(1), 443))
                    break
    except OSError:
        pass

    all_ok = True
    for label, host, port in endpoints:
        ok = _probe_host(host, port)
        sym = "✓" if ok else "✗"
        if ok:
            _log.ui(f"  {sym}  {label} ({host})")
        else:
            _log.warn(f"  {sym}  {label} ({host}) — unreachable")
            all_ok = False

    if not all_ok:
        _log.warn(
            "  Some endpoints unreachable. Package clones or downloads may fail."
        )
    return editor


# ---------------------------------------------------------------------------
# Step: GPG keyring bootstrap
# ---------------------------------------------------------------------------

def _step_gpg(config, state, options, editor: str) -> str:
    _log.ui("─── GPG keyring bootstrap ───────────────────────────")

    if not shutil.which("gpg"):
        _log.warn("  gpg not found — key verification unavailable")
        return editor

    r = subprocess.run(
        ["gpg", "--list-keys", "--with-colons"],
        capture_output=True, text=True,
    )
    key_count = r.stdout.count("\npub:") if r.returncode == 0 else 0
    _log.ui(f"  GPG keyring: {key_count} public key(s)")

    global_keys_dir = CONFIG_DIR / "keys/pgp"
    if global_keys_dir.is_dir():
        asc_files = sorted(global_keys_dir.glob("*.asc"))
        if asc_files:
            _log.ui(
                f"  Importing {len(asc_files)} key(s) from {global_keys_dir}"
            )
            if not options.dry_run:
                r = subprocess.run(
                    ["gpg", "--import", *[str(f) for f in asc_files]],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    _log.warn(f"  GPG import failed:\n{r.stderr.strip()}")
                else:
                    _log.ui("  GPG: global key import succeeded")
        else:
            _log.ui(f"  {global_keys_dir}: no .asc files")
    else:
        _log.ui(
            f"  No global key store at {global_keys_dir} "
            "(per-build keys still imported by the build stage)"
        )

    if _interactive() and not options.dry_run:
        choice = _prompt_choice(
            "  Refresh all keys from keyserver? (gpg --refresh-keys) [y/N]: ",
            choices=("y", "n"),
            default="n",
        )
        if choice == "y":
            _log.info("Running gpg --refresh-keys (this may take a while)...")
            r = subprocess.run(["gpg", "--refresh-keys"])
            if r.returncode != 0:
                _log.warn("  gpg --refresh-keys failed")
            else:
                _log.info("GPG: keyring refresh complete")

    return editor


# ---------------------------------------------------------------------------
# Step: build preview
# ---------------------------------------------------------------------------

def _step_preview(config, state, options, editor: str) -> str:
    _log.ui("─── Build preview ───────────────────────────────────")

    pkg_path = resolve_packages_path(config)
    if not pkg_path.exists():
        _log.ui(f"  packages.toml not found at {pkg_path} — skipping")
        return editor

    try:
        with pkg_path.open("rb") as f:
            pkg_data = tomllib.load(f)
        packages  = expand_package_groups(pkg_data)
        build_cfg = pkg_data.get("build", {})
    except Exception as e:
        _log.warn(f"  Could not load packages.toml: {e}")
        return editor

    if not packages:
        _log.ui("  No packages defined in packages.toml")
        return editor

    repo_mode = resolve_repo_mode(build_cfg)

    _match_rules = None
    try:
        rules = config.get("rules", [])
        from sysforge.primitives.profile import match_rules as _match_rules
        can_match = True
    except ImportError as e:
        _log.info(f"  match_rules unavailable, profile preview disabled: {e}")
        rules, can_match = [], False

    _log.ui(f"  {'Package':<30}  {'Source':<6}  {'Action'}")
    _log.ui(f"  {'─'*30}  {'─'*6}  {'─'*30}")

    repo_count = build_count = 0
    for pkg in packages:
        name   = pkg.get("name", "?")
        source = pkg.get("source", "aur")
        effective_mode = REPO_MODE_SOURCE if pkg.get(PKG_KEY_BUILD_FROM_SOURCE) else repo_mode

        if source == "repo" and effective_mode == REPO_MODE_SOURCE:
            patch_note = (
                " (enable_build_from_source)"
                if pkg.get(PKG_KEY_BUILD_FROM_SOURCE) else " (repo_mode)"
            )
            action = f"build  [build_from_source]{patch_note}"
            build_count += 1
        elif source == "repo":
            action = "pacman -S --needed"
            repo_count += 1
        else:
            build_count += 1
            if can_match and _match_rules is not None:
                fake_meta = {"pkgname": name, "pkgbase": name, "makedepends": []}
                matched = _match_rules(fake_meta, rules)
                if matched:
                    winner = max(matched, key=lambda r: r.get("priority", 0))
                    action = f"build  [{winner['profile']}]"
                else:
                    action = (
                        f"build  [{config.get('defaults', {}).get('profile', 'standard')}]"
                        " (default)"
                    )
            else:
                action = "build"
        _log.ui(f"  {name:<30}  {source:<6}  {action}")

    _log.ui(
        f"  Total: {len(packages)}  |  repo_mode: {repo_mode}  |  "
        f"Repo (pacman): {repo_count}  |  Builds: {build_count}"
    )
    if can_match:
        _log.ui(
            "  Profiles are tentative (pkgname rules only). "
            "makedepends rules resolve per-PKGBUILD at build time."
        )

    if TOOLCHAIN_PATH.exists():
        try:
            with TOOLCHAIN_PATH.open("rb") as f:
                tcfg = tomllib.load(f)
            compiler = tcfg.get("compiler", "gcc")
            pgo = tcfg.get("pgo", True) if compiler == "llvm" else False
            pgo_label = " + PGO (4-pass)" if pgo else ""
            _log.ui(f"  Toolchain: {compiler}{pgo_label}")
        except (OSError, tomllib.TOMLDecodeError) as e:
            _log.ui(f"  Toolchain: toolchain.toml present but unreadable ({e})")
    else:
        _log.ui("  Toolchain: no toolchain.toml — toolchain stage will be a no-op")

    if KERNEL_PATH.exists():
        try:
            with KERNEL_PATH.open("rb") as f:
                kcfg = tomllib.load(f)
            _log.ui(
                f"  Kernel: {kcfg.get('pkgname', '?')}  "
                f"(bootloader: {kcfg.get('bootloader', 'systemd-boot')})"
            )
        except (OSError, tomllib.TOMLDecodeError) as e:
            _log.ui(f"  Kernel: kernel.toml present but unreadable ({e})")
    else:
        _log.ui("  Kernel: no kernel.toml — kernel stage will be a no-op")

    from sysforge.primitives import build_estimate
    from sysforge.primitives.build_state import BuildState
    _sd, _ = resolve_state_dir(getattr(options, "state_dir", None))
    _names = [p.get("name") for p in packages if p.get("name")]
    _est = build_estimate.format_estimate(_names, BuildState(_sd))
    if _est:
        _log.ui(f"  {_est}")
    else:
        _log.info("  Build-time estimate: no build history yet")

    return editor


# ---------------------------------------------------------------------------
# Step dispatch
# ---------------------------------------------------------------------------

_STEP_FNS = {
    "editor":     _step_editor,
    "config":     _step_config,
    "build_mode": _step_build_mode,
    "desktop":    _step_desktop,
    "makepkg":    _step_makepkg,
    "sudo":       _step_sudo,
    "disk":       _step_disk,
    "network":    _step_network,
    "gpg":        _step_gpg,
    "preview":    _step_preview,
}

# Steps that prompt the user to open files in $EDITOR. The gate in
# _run_selected_steps ensures a usable editor is available before any of
# these runs — otherwise every edit prompt within them silently fails and
# the user has no way to recover without restarting the stage.
_EDITOR_NEEDING_STEPS = frozenset({"config", "makepkg"})


def _gate_editor_for_pipeline(options) -> None:
    """
    Require a usable editor before handing control to the build stages.

    ``_EDITOR_NEEDING_STEPS`` only covers the two steps that open files *in
    this stage*. A step subset that skips both (or an ``editor`` step the user
    skipped) left the whole build pipeline with no editor at all — the failure
    surfaces much later as ``No usable $EDITOR`` in the PKGBUILD retry menu,
    with a half-built package and no way to fix the recipe.

    Skipped when ``--standalone`` (nothing runs after reconfigure) or when
    there's no TTY (the picker can't prompt; warn instead of hard-failing a
    non-interactive run).
    """
    editor, _ = _resolve_editor()
    if _editor_usable(editor):
        return
    if options.standalone:
        return
    if not _interactive() or options.dry_run:
        _log.warn(
            "  No usable editor resolved — build-failure recovery will not be "
            "able to open a PKGBUILD. Set SYSFORGE_EDITOR or [ui].editor."
        )
        return
    _require_usable_editor(editor, options, needed_for="the build stages")


def _run_selected_steps(step_keys: list[str], config, state, options) -> None:
    """
    Run selected steps in order. Editor is resolved upfront and threaded
    through — the editor step may update it for subsequent steps. Before
    each editor-needing step, _require_usable_editor enforces that the
    threaded editor is on PATH (raises RuntimeError if the user cancels).
    """
    editor, _ = _resolve_editor()
    for key in step_keys:
        if key in _EDITOR_NEEDING_STEPS and not _editor_usable(editor):
            editor = _require_usable_editor(editor, options, needed_for=key)
        editor = _STEP_FNS[key](config, state, options, editor)


def _validate_all_configs(config) -> None:
    """
    Re-parse and resolve every config file the downstream stages depend on,
    surfacing TOML/schema errors here instead of after a 10+ minute build run.
    Raises RuntimeError with a clear pointer if any file fails to load.
    """
    from sysforge.primitives.profile import merge_extends

    pkg_path = resolve_packages_path(config)
    if pkg_path.exists():
        try:
            with pkg_path.open("rb") as f:
                tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise RuntimeError(f"[RECONFIGURE] {pkg_path}: TOML parse error: {e}") from e

    profiles_path = CONFIG_DIR / "profiles.toml"
    if profiles_path.exists():
        try:
            cfg = load_config(config_paths=[profiles_path])
            conflict_groups = load_conflict_groups()
            for name in cfg.get("profiles", {}):
                merge_extends(name, cfg["profiles"], conflict_groups=conflict_groups)
        except (tomllib.TOMLDecodeError, ValueError, KeyError) as e:
            raise RuntimeError(f"[RECONFIGURE] {profiles_path}: {e}") from e

    for path in (TOOLCHAIN_PATH, KERNEL_PATH):
        if path.exists():
            try:
                with path.open("rb") as f:
                    tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise RuntimeError(f"[RECONFIGURE] {path}: TOML parse error: {e}") from e


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class ReconfigureStage(Stage):
    name = "reconfigure"
    description = "Pre-build checkpoint — review configs and system state"
    depends_on = ["configure"]
    stateless = True

    def run(self, config, state, options):
        if Path("/run/archiso").exists():
            from sysforge.pipeline.stages._bootstrap import load_bootstrap
            cfg = load_bootstrap()
            chroot_state_dir = Path(cfg.target) / "var/lib/sysforge"
            chroot_state_dir.mkdir(parents=True, exist_ok=True)
            if state.path.exists():
                try:
                    shutil.copy2(state.path, chroot_state_dir / state.path.name)
                    _log.info(f"State file copied to {chroot_state_dir / state.path.name}")
                except shutil.SameFileError:
                    _log.info(
                        f"State file already up-to-date at {chroot_state_dir / state.path.name}"
                    )
            raise BootstrapRebootRequired(
                "Bootstrap complete (stages 1–4 done). "
                "Reboot into the installed system before continuing."
            )

        reminder = Path("/etc/profile.d/sysforge-resume.sh")
        if reminder.exists():
            try:
                reminder.unlink()
                _log.ui("Removed login reminder (/etc/profile.d/sysforge-resume.sh)")
            except PermissionError:
                # Root-owned file. If we're root already, the unlink would have
                # worked — so we only get here as the builder user. Use sudo -n
                # so a missing-credentials cache fails fast instead of blocking
                # at a password prompt (this path runs from a profile.d login
                # chain on some systems; that has no TTY).
                result = subprocess.run(
                    privileged_argv(["rm", "-f", str(reminder)], noninteractive=True),
                    capture_output=True,
                )
                if result.returncode == 0:
                    _log.ui("Removed login reminder via sudo (/etc/profile.d/sysforge-resume.sh)")
                else:
                    _log.warn(
                        f"Cannot remove {reminder} — delete it manually with: sudo rm {reminder}"
                    )

        _show_stage_summary(state)

        step_keys = _select_steps(options)
        _run_selected_steps(step_keys, config, state, options)

        # Final pre-flight: catch TOML/schema errors before the user confirms,
        # so downstream stages can't fail 10+ minutes in on broken config.
        _validate_all_configs(config)

        # Everything past this point hands off to toolchain → packages →
        # kernel, whose failure-recovery menus need an editor. Gate here, not
        # only on the two editor-opening steps above.
        _gate_editor_for_pipeline(options)

        if _interactive() and not options.dry_run and not options.standalone:
            _log.ui("─────────────────────────────────────────────────────")
            choice = _prompt_choice(
                "[RECONFIGURE] Ready to proceed to toolchain → packages → kernel? [y/N]: ",
                choices=("y", "n"),
                default="n",
            )
            if choice != "y":
                raise RuntimeError(
                    "[RECONFIGURE] Aborted by user. Run with --resume to return to this stage."
                )

        _log.info("Pre-build checkpoint complete.")
