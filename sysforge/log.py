"""
log.py — SysForge structured logging

All output goes to stderr. Verbosity controls which levels are shown:
  0 (default) — ERROR only
  1 (-v)       — ERROR + WARN
  2 (-vv)      — ERROR + WARN + INFO

Format: [SYSFORGE][LEVEL][TAG] message

Usage:
    from sysforge import log

    log.info("[CONF]", f"Wrote temp conf: {path}")
    log.warn("[DEP]", "soname mismatch — continuing")
    log.error("[FAILURE]", "aborting build")

    # Set once at CLI entry point:
    log.set_verbosity(args.verbose)  # 0, 1, or 2
"""
import sys

_VERBOSITY = 0


def set_verbosity(level: int) -> None:
    global _VERBOSITY
    _VERBOSITY = max(0, int(level))


def get_verbosity() -> int:
    return _VERBOSITY


def error(tag: str, message: str) -> None:
    """Always printed regardless of verbosity."""
    print(f"[SYSFORGE][ERROR]{tag} {message}", file=sys.stderr)


def warn(tag: str, message: str) -> None:
    """Printed at verbosity >= 1 (-v)."""
    if _VERBOSITY >= 1:
        print(f"[SYSFORGE][WARN]{tag} {message}", file=sys.stderr)


def info(tag: str, message: str) -> None:
    """Printed at verbosity >= 2 (-vv)."""
    if _VERBOSITY >= 2:
        print(f"[SYSFORGE][INFO]{tag} {message}", file=sys.stderr)
