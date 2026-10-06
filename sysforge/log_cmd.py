# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
log_cmd.py — `sysforge log [PKG]` verb.

Pages a sysforge log file through ``$PAGER``:
  • no PKG: unified log at ``<state_dir>/sysforge.log``
  • with PKG: per-package log at ``<pkgbuild_src_dir>/<pkg>/sysforge_<pkg>.log``

Read-only. Refuses with a non-zero exit when the target file is absent;
does not fall back to the unified log and does not trigger an AUR clone
for unknown package names (we deliberately avoid ``find_pkgbuild`` here).

Public API:
    cmd_log(args)
    LogVerb
"""
from pathlib import Path

from sysforge import log
from sysforge.primitives.pager import maybe_pager
from sysforge.verbs import ExecResult, PreCheckResult, Verb

_log = log.get_logger("LOG")


def _resolve_log_path(args) -> tuple[Path, str]:
    """Return (path, label) for the log the verb should page.

    ``label`` is a short human description used in error messages
    (e.g. ``unified log`` or ``log for foo``).
    """
    pkg = getattr(args, "pkg", None)
    if pkg:
        from sysforge.primitives.config import load_config

        config = load_config() or {}
        raw = config.get("paths", {}).get("pkgbuild_src_dir")
        if not raw:
            raise RuntimeError(
                "[paths] pkgbuild_src_dir is not set in profiles.toml; "
                "cannot locate per-package logs."
            )
        pkg_dir = Path(raw).expanduser() / pkg
        return pkg_dir / f"sysforge_{pkg}.log", f"log for {pkg}"

    from sysforge.pipeline.state import resolve_state_dir

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    return state_dir / "sysforge.log", "unified log"


def cmd_log(args) -> int:
    """Page the resolved sysforge log. Returns the process exit code."""
    try:
        path, label = _resolve_log_path(args)
    except RuntimeError as e:
        _log.error(str(e))
        return 1

    if not path.exists():
        _log.error(f"No sysforge {label} found at {path}")
        return 1

    use_pager = not getattr(args, "no_pager", False)
    with maybe_pager(use_pager), path.open(encoding="utf-8") as f:
        for line in f:
            print(line, end="")
    return 0


class LogVerb(Verb):
    """Read-only: page the unified or per-package sysforge log."""

    name = "log"
    requires_sentinel = False

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("log",
            help="Page the unified or per-package sysforge log through $PAGER.")
        p.add_argument("pkg", nargs="?", metavar="PKG",
            help="Package name (resolves to <pkgbuild_src_dir>/<pkg>/sysforge_<pkg>.log). "
                 "Omit to page the unified log.")
        p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
            help="Override state directory (used to locate the unified log).")
        p.add_argument("--no-pager", action="store_true", dest="no_pager",
            help="Don't pipe output through $PAGER (default: paginate when stdout is a TTY).")
        p.set_defaults(verb_cls=cls)
        return p

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        return ExecResult(exit_code=cmd_log(args))
