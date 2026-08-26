# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
config_cmd.py — `sysforge config merge` verb: interactive .sfnew adoption.

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
    cmd_config_merge(args)
    ConfigMergeVerb
"""
from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path

from sysforge import log
from sysforge.primitives import paths
from sysforge.primitives.editor import resolve_merge_tool, run_tty_argv
from sysforge.primitives.pager import maybe_pager
from sysforge.primitives.prompt import prompt_choice, prompt_key
from sysforge.verbs import ExecResult, PreCheckResult, Verb

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
            "# offsets — the -/+ pairs below are moves, not deletions:\n"
            + "".join(f"#   {h}\n" for h in moved)
            + "#\n"
        )
        return banner + diff
    return diff


def _confirm(msg: str) -> bool:
    """y/N confirmation; any non-y input (incl. EOF) is a decline."""
    return prompt_choice(msg, ["y", "yes"], default="",
                         retry_on_invalid=False) in {"y", "yes"}


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
                _log.warn("No merge tool found — set SYSFORGE_MERGE, "
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
                new_path.unlink()
                _log.ui(f"  Removed {label}")
                return "removed"
        elif ans == "o":
            if _confirm(f"  Overwrite {target.name} with {label} verbatim? "
                        "This discards your local values. [y/N] "):
                shutil.copy2(new_path, target)
                new_path.unlink()
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
        _log.ui(f"No .sfnew/.pacnew companions in {config_dir} — nothing to merge.")
        return 0

    use_pager = not getattr(args, "no_pager", False)

    # Non-interactive listing for scripting / CI.
    if getattr(args, "list", False) or getattr(args, "dry_run", False):
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


class ConfigMergeVerb(Verb):
    """Interactive .sfnew/.pacnew adoption. Edits config files in place but is
    fully reversible and never builds/installs, so it carries no sentinel."""

    name = "config-merge"
    wants_run_log = True
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        return ExecResult(exit_code=cmd_config_merge(args))
