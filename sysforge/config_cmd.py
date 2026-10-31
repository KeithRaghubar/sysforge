# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
config_cmd.py — `sysforge config` verbs: check, clean, reset (3.5.0-F18) and
merge (interactive .sfnew adoption).

`check` reports settings sysforge retired or does not know, `clean` rewrites
the retired ones (and with ``--unknown`` removes the unknown), and `reset` puts
keys, or a whole file, back to the shipped default. The schema and the edits
are primitives/config_schema.py; this module adds the reporting, the diff and
the consent. The rest of this docstring is about `merge`.

`make sync-config` adopts new shipped config defaults into the live config dir
via an add-only, key-anchored merge (tools/sync_config.py), but it cannot carry
documentation comments and commented-out example settings (``# interactive =
true``) that have no active key to anchor to. For those it drops a verbatim
``<name>.sfnew`` companion beside the live file — pacnew-style — for the
operator to diff and adopt manually. This verb is that adopt step: a pacdiff-
style loop over the residual ``.sfnew`` companions (and, on a packaged install,
pacman's own ``.pacnew``/``.pacsave`` for ``/etc/sysforge``).

A ``.sfnew`` is the *verbatim shipped file*, so blindly copying it over the live
file would clobber the user's customized values. The default model is therefore
pacdiff's: view the diff, open a side-by-side merge tool, hand-pick the comment
hunks, then drop the ``.sfnew``. ``[o]verwrite`` exists for the ``.pacnew``
"accept maintainer version" case but warns and confirms first — it is never the
default action.

Public API:
    cmd_config_check(args), cmd_config_clean(args), cmd_config_reset(args)
    cmd_config_merge(args)
    ConfigCheckVerb, ConfigCleanVerb, ConfigResetVerb, ConfigMergeVerb
"""
from __future__ import annotations

import difflib
import re
import time
from pathlib import Path

from sysforge import log
from sysforge.primitives import atomic_write, config_schema, json_out, paths, prompt
from sysforge.primitives.editor import resolve_merge_tool, run_tty_argv
from sysforge.primitives.pager import maybe_pager
from sysforge.primitives.privilege import run_privileged
from sysforge.primitives.prompt import prompt_choice, prompt_key
from sysforge.verbs import ExecResult, PreCheckResult, Verb, VerbGroup

_log = log.get_logger("CONFIG")

# Companion suffixes we offer to merge: sysforge's own .sfnew first, then
# pacman's variants for sysforge config files on a packaged install.
_SUFFIXES = (".sfnew", ".pacnew", ".pacsave")


def _resolve_config_dir(args) -> Path:
    """Live config dir: --config-dir override, else paths.CONFIG_DIR.

    paths.CONFIG_DIR already honours $SYSFORGE_CONFIG_DIR (else /etc/sysforge);
    we never read the env directly here.
    """
    raw = getattr(args, "config_dir", None)
    return Path(raw).expanduser() if raw else paths.CONFIG_DIR


def _candidates(config_dir: Path) -> list[tuple[Path, Path]]:
    """Return sorted (companion, live-target) pairs found in ``config_dir``."""
    pairs: list[tuple[Path, Path]] = []
    for suf in _SUFFIXES:
        for new_path in sorted(config_dir.glob(f"*{suf}")):
            # "foo.toml.sfnew".with_suffix("") -> "foo.toml": strip the final
            # companion extension to recover the live target name.
            pairs.append((new_path, new_path.with_suffix("")))
    return sorted(pairs, key=lambda p: p[0].name)


_HEADER_RE = re.compile(r"^[-+](\s*\[\[?[^\[\]]+\]\]?\s*)$")


def _moved_sections(diff: str) -> list[str]:
    """Section headers the diff shows as both removed and added (3.1.0-B9).

    ``difflib`` has no move detection, so a section that merely sits at a
    different offset in the two files is rendered as a deletion in one hunk and
    an addition in another, often hundreds of lines apart. Read top-down that
    looks like the section is absent from the shipped file — and hand-merging
    against that reading deletes a live section for real. A header appearing on
    both a ``-`` and a ``+`` line is present in both files; report it so the
    operator knows it was relocated, not dropped.
    """
    removed: set[str] = set()
    add: set[str] = set()
    for line in diff.splitlines():
        if m := _HEADER_RE.match(line):
            (removed if line[0] == "-" else add).add(m.group(1).strip())
    return sorted(removed & add)


def _diff_text(target: Path, new_path: Path) -> str:
    """Unified diff of the live target vs the companion (pure, no subprocess).

    A relocated-section banner is prepended when the diff contains any (see
    :func:`_moved_sections`), so a move can't be misread as a deletion.
    """
    if target.exists():
        a = target.read_text(encoding="utf-8").splitlines(keepends=True)
        a_label = str(target)
    else:
        a = []
        a_label = f"{target} (absent)"
    b = new_path.read_text(encoding="utf-8").splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(a, b, fromfile=a_label, tofile=str(new_path))
    )
    if moved := _moved_sections(diff):
        banner = (
            "# NOTE: these sections are present in BOTH files, only at different\n"
            "# offsets; the -/+ pairs below are moves, not deletions:\n"
            + "".join(f"#   {h}\n" for h in moved)
            + "#\n"
        )
        return banner + diff
    return diff


def _confirm(msg: str) -> bool:
    """y/N confirmation; any non-y input (incl. EOF) is a decline."""
    return prompt_choice(msg, ["y", "yes"], default="",
                         retry_on_invalid=False) in {"y", "yes"}


def _remove_companion(path: Path) -> None:
    """Delete a companion, escalating when its dir is root's, as /etc/sysforge
    is (4.0.0-B3)."""
    try:
        path.unlink()
    except PermissionError:
        # mutates=False: one unlink, nothing for an interruption to leave half done.
        run_privileged(["rm", "-f", str(path)], tag="CONFIG", mutates=False)


def _merge_one(target: Path, new_path: Path, use_pager: bool) -> str:
    """Drive the pacdiff-style loop for one companion file.

    Returns one of ``merged`` / ``skipped`` / ``removed`` / ``installed`` /
    ``aborted``. ``[m]erge`` launches the tool and re-loops (so the user can
    re-view and then remove when satisfied); only ``[r]emove`` resolves the
    companion in the no-overwrite path.
    """
    label = new_path.name
    while True:
        try:
            ans = prompt_key(
                f"{label}: [v]iew diff / [m]erge / [s]kip / "
                f"[r]emove {new_path.suffix.lstrip('.')} / [o]verwrite / a[b]ort? ",
                tag="CONFIG",
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return "aborted"

        if ans == "v":
            text = _diff_text(target, new_path)
            with maybe_pager(use_pager):
                print(text if text else f"{label}: live file is identical.")
        elif ans == "m":
            argv, source = resolve_merge_tool()
            if not argv:
                _log.warn("No merge tool found: set SYSFORGE_MERGE, "
                          "sysforge.toml [ui].merge, or $DIFFPROG, or install vimdiff.")
                continue
            _log.ui(f"  Launching {' '.join(argv)} ({source}): {target.name} {new_path.name}")
            rc = run_tty_argv([*argv, str(target), str(new_path)])
            if rc == -1:
                _log.warn(f"Merge tool not found: {argv[0]!r}")
            # Re-loop: the user re-views and removes the companion when satisfied.
        elif ans == "s":
            return "skipped"
        elif ans == "r":
            if _confirm(f"  Remove {label}? (live file is already complete) [y/N] "):
                _remove_companion(new_path)
                _log.ui(f"  Removed {label}")
                return "removed"
        elif ans == "o":
            if _confirm(f"  Overwrite {target.name} with {label} verbatim? "
                        "This discards your local values. [y/N] "):
                atomic_write.replace_file(
                    target, new_path.read_text(encoding="utf-8"), tag="CONFIG")
                _remove_companion(new_path)
                _log.ui(f"  Installed {label} over {target.name}")
                return "installed"
        elif ans == "b":
            return "aborted"


def cmd_config_merge(args) -> int:
    """Interactively adopt/clear .sfnew (and .pacnew/.pacsave) companions.

    Returns a process exit code: 0 on a clean run (including "nothing to do").
    """
    config_dir = _resolve_config_dir(args)
    if not config_dir.is_dir():
        _log.error(f"Config dir not found: {config_dir}")
        return 1

    pairs = _candidates(config_dir)
    if not pairs:
        _log.ui(f"No .sfnew/.pacnew companions in {config_dir}: nothing to merge.")
        return 0

    use_pager = not getattr(args, "no_pager", False)

    # Non-interactive listing for scripting / CI.
    if getattr(args, "dry_run", False):
        _log.ui(f"{len(pairs)} companion(s) in {config_dir}:")
        for new_path, target in pairs:
            state = "live present" if target.exists() else "no live target"
            print(f"  {new_path.name}  ->  {target.name}  ({state})")
        return 0

    counts: dict[str, int] = {}
    for new_path, target in pairs:
        outcome = _merge_one(target, new_path, use_pager)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == "aborted":
            _log.ui("Aborted.")
            break

    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    _log.ui(f"config merge: {summary}")
    return 0


# ---------------------------------------------------------------------------
# check / clean / reset (3.5.0-F18)
# ---------------------------------------------------------------------------

_HINTS = {
    "retired_key": "sysforge config clean",
    "retired_value": "sysforge config clean",
    "unknown_key": "check the spelling, or sysforge config clean --unknown",
    "unknown_section": "check the spelling, or sysforge config clean --unknown",
    "parse_error": "fix it by hand",
}


def cmd_config_check(args) -> int:
    """Report retired and unknown settings. 0 when there are none, else 1."""
    config_dir = _resolve_config_dir(args)
    issues = config_schema.inspect(config_dir)
    if json_out.requested(args):
        json_out.emit("config-check", {
            "config_dir": str(config_dir),
            "issues": [{"file": i.file, "line": i.line, "kind": i.kind,
                        "setting": i.where or None, "message": i.message,
                        "fix": _HINTS[i.kind]} for i in issues],
        })
        return 1 if issues else 0
    if not issues:
        _log.ui(f"config check: nothing retired or unknown in {config_dir}")
        return 0
    for name in config_schema.CONFIG_FILES:
        mine = [i for i in issues if i.file == name]
        if not mine:
            continue
        _log.ui(f"{config_dir / name}")
        for i in mine:
            where = f"{i.where}: " if i.where else ""
            _log.ui(f"  line {i.line}  {where}{i.message}  ({_HINTS[i.kind]})")
    kinds = {i.kind for i in issues}
    _log.ui(f"config check: {len(issues)} setting(s) to review"
            + ("; `sysforge config clean` fixes the retired ones"
               if kinds & {"retired_key", "retired_value"} else ""))
    return 1


def _show_diff(path: Path, old: str, new: str) -> None:
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     f"{path} (now)", f"{path} (after)", lineterm=""):
        _log.ui(line)


def _consent(args, question: str) -> bool | None:
    """True to write, False when declined, None when it cannot ask."""
    if getattr(args, "yes", False):
        return True
    if not prompt.is_interactive():
        return None
    return prompt.prompt_choice(f"{question} [y/N] ", ["y", "n"], default="n",
                                retry_on_invalid=False) == "y"


def _write_all(args, writes: list[tuple[Path, str, str]], question: str) -> int:
    """Show each (path, old, new) diff, then write them all with consent."""
    for path, old, new in writes:
        _show_diff(path, old, new)
    if getattr(args, "dry_run", False):
        _log.ui("Dry run: nothing written.")
        return 0
    answer = _consent(args, question)
    if answer is None:
        _log.error(f"refusing to edit config files ({prompt.no_prompt_reason()}); "
                   "pass --yes (or --dry-run to only look)")
        return 2
    if not answer:
        _log.ui("Aborted: nothing written.")
        return 2
    for path, _old, new in writes:
        written = atomic_write.replace_file(path, new, tag="CONFIG")
        _log.ui(f"Wrote {written}")
    return 0


def cmd_config_clean(args) -> int:
    config_dir = _resolve_config_dir(args)
    unknown = getattr(args, "unknown", False)
    writes, fixed, left = [], [], []
    for name in config_schema.CONFIG_FILES:
        path = config_dir / name
        if not path.is_file():
            continue
        old = path.read_text(encoding="utf-8")
        try:
            new, done = config_schema.clean_text(name, old, unknown=unknown)
        except config_schema.ConfigEditError as e:
            _log.error(f"{path}: {e}")
            return 1
        fixed += done
        left += config_schema.inspect_text(name, new)
        if new != old:
            writes.append((path, old, new))
    for i, action in fixed:
        _log.ui(f"{i.file} line {i.line}  {i.where}: {action} ({i.message})")
    if left:
        _log.ui(f"{len(left)} unknown setting(s) left in place; `sysforge config check` "
                "lists them, and --unknown removes them")
    if not writes:
        _log.ui("config clean: nothing to change")
        return 0
    return _write_all(args, writes, f"Apply these changes to {len(writes)} file(s)?")


def cmd_config_reset(args) -> int:
    config_dir = _resolve_config_dir(args)
    name = args.file
    path = config_dir / name
    keys = list(getattr(args, "keys", None) or [])
    shipped = config_schema.shipped_text(name)
    if shipped is None:
        _log.error(f"no shipped default for {name} is installed")
        return 1
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    if keys:
        try:
            new, changes = config_schema.reset_keys_text(name, old, keys)
        except config_schema.ConfigEditError as e:
            _log.error(str(e))
            return 2
        for line in changes:
            _log.ui(line)
        if new == old:
            return 0
        return _write_all(args, [(path, old, new)], f"Reset {len(keys)} key(s) in {path}?")
    if old == shipped:
        _log.ui(f"{path} is already the shipped default")
        return 0
    backup = path.with_name(f"{name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    _log.ui(f"{path} will be replaced by the shipped default; the current file is "
            f"kept as {backup}")
    if getattr(args, "dry_run", False):
        _show_diff(path, old, shipped)
        _log.ui("Dry run: nothing written.")
        return 0
    answer = _consent(args, f"Replace {path} with the shipped default?")
    if answer is None:
        _log.error(f"refusing to replace a config file ({prompt.no_prompt_reason()}); "
                   "pass --yes (or --dry-run to only look)")
        return 2
    if not answer:
        _log.ui("Aborted: nothing written.")
        return 2
    if old:
        atomic_write.replace_file(backup, old, tag="CONFIG")
    written = atomic_write.replace_file(path, shipped, tag="CONFIG")
    _log.ui(f"Wrote {written}" + (f" (previous copy: {backup})" if old else ""))
    return 0


def _add_config_dir(p) -> None:
    p.add_argument("--config-dir", metavar="DIR", dest="config_dir",
        help="Live config dir (default: $SYSFORGE_CONFIG_DIR, else /etc/sysforge).")


class ConfigCheckVerb(Verb):
    """Read-only: report retired and unknown settings in the live config."""

    name = "config-check"
    requires_sentinel = False

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("check",
            help="Report settings in the live config files that sysforge retired "
                 "(renamed keys, refused values) or does not know (misspelt or "
                 "no longer used). Exits 1 when it finds any.")
        _add_config_dir(p)
        p.add_argument("--json", action="store_true", dest="json",
            help="Print one JSON document (schema 1) on stdout instead of the "
                 "report; the exit code is unchanged.")
        p.set_defaults(verb_cls=cls)
        return p

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        return ExecResult(exit_code=cmd_config_check(args))


class ConfigCleanVerb(Verb):
    """Rewrite retired settings in place; reversible and never builds."""

    name = "config-clean"
    wants_run_log = True
    requires_sentinel = False

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("clean",
            help="Rewrite retired settings in the live config files: a renamed key "
                 "takes its new name (or is dropped when the new one is already "
                 "set), a retired value its replacement. Comments are kept. Shows "
                 "the diff and asks first.")
        _add_config_dir(p)
        p.add_argument("--unknown", action="store_true", dest="unknown",
            help="Also remove settings sysforge does not know (see `config check`).")
        p.add_argument("--dry-run", action="store_true", dest="dry_run",
            help="Show the diff without writing anything.")
        p.add_argument("--yes", action="store_true", dest="yes",
            help="Write without asking (needed without a terminal or with --unattended).")
        p.set_defaults(verb_cls=cls)
        return p

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        return ExecResult(exit_code=cmd_config_clean(args))


class ConfigResetVerb(Verb):
    """Put keys, or a whole file, back to the shipped default."""

    name = "config-reset"
    wants_run_log = True
    requires_sentinel = False

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("reset",
            help="Put settings back to the shipped default: each KEY given as "
                 "SECTION.KEY (or KEY for a top-level one), or with no KEY the whole "
                 "FILE, keeping the current one as FILE.bak-<time>. Shows the diff "
                 "and asks first.")
        p.add_argument("file", metavar="FILE", choices=config_schema.CONFIG_FILES,
            help="Config file to reset (sysforge.toml, profiles.toml, packages.toml, "
                 "kernel.toml or toolchain.toml).")
        p.add_argument("keys", nargs="*", metavar="KEY",
            help="Keys to reset, such as build.jobs; omit to reset the whole file.")
        _add_config_dir(p)
        p.add_argument("--dry-run", action="store_true", dest="dry_run",
            help="Show the diff without writing anything.")
        p.add_argument("--yes", action="store_true", dest="yes",
            help="Write without asking (needed without a terminal or with --unattended).")
        p.set_defaults(verb_cls=cls)
        return p

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        return ExecResult(exit_code=cmd_config_reset(args))


class ConfigMergeVerb(Verb):
    """Interactive .sfnew/.pacnew adoption. Edits config files in place but is
    fully reversible and never builds/installs, so it carries no sentinel."""

    name = "config-merge"
    wants_run_log = True
    requires_sentinel = False

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("merge",
            help="Interactively adopt or clear the companions a config upgrade "
                 "leaves beside sysforge's config files (pacman's .pacnew/.pacsave, "
                 "or .sfnew). "
                 "pacdiff-style: view diff, merge in a tool, skip, remove, or "
                 "overwrite. Merge tool: SYSFORGE_MERGE > sysforge.toml [ui].merge "
                 "> $DIFFPROG > vimdiff.")
        p.add_argument("--config-dir", metavar="DIR", dest="config_dir",
            help="Live config dir to scan (default: $SYSFORGE_CONFIG_DIR, else "
                 "/etc/sysforge).")
        p.add_argument("--dry-run", action="store_true", dest="dry_run",
            help="List companion files and their live targets without prompting "
                 "or merging (non-interactive; for scripting/CI).")
        p.add_argument("--no-pager", action="store_true", dest="no_pager",
            help="Don't pipe diffs through $PAGER (default: paginate when stdout is a TTY).")
        p.set_defaults(verb_cls=cls)
        return p

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        return ExecResult(exit_code=cmd_config_merge(args))


#: `sysforge config`: check, clean, reset, merge (subcommand required).
CONFIG_GROUP = VerbGroup(
    name="config",
    help="Check, clean or reset the live sysforge config files, or merge "
         "shipped-default changes into them.",
    dest="config_cmd",
    members=(ConfigCheckVerb, ConfigCleanVerb, ConfigResetVerb, ConfigMergeVerb),
)
