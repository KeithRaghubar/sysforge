"""
log.py — SysForge structured logging

All output goes to stderr. Verbosity controls which levels are shown on stderr:
  0 (default) — ERROR only
  1 (-v)       — ERROR + WARN
  2 (-vv)      — ERROR + WARN + INFO
  3 (-vvv)     — ERROR + WARN + INFO + DEBUG (full config/profile/conf dumps)

File logging (always full verbosity, all levels):
  Unified log — one file for the entire run, managed by the pipeline runner.
                Default: <state_dir>/sysforge.log
                Persists across runs until a successful pipeline completion,
                then truncated. --purge-log truncates before the run starts.
                --persist-log suppresses truncation on success.
  Per-package log — one file per package build, written alongside the PKGBUILD.
                    Path: <pkgbuild_dir>/<pkgname>/sysforge_<pkgname>.log
                    Same lifecycle as the unified log.

Format: [SYSFORGE][LEVEL][TAG] message

Usage:
    from sysforge import log

    log.info("[CONF]", f"Wrote temp conf: {path}")
    log.warn("[DEP]", "soname mismatch — continuing")
    log.error("[FAILURE]", "aborting build")

    # Set once at CLI entry point:
    log.set_verbosity(args.verbose)  # 0, 1, 2, or 3

    # Pipeline runner manages the unified log:
    log.open_unified_log(path, purge=False)
    log.close_unified_log(success=True, persist=False)

    # makepkg_wrapper manages per-package logs:
    log.open_pkg_log(path)
    log.close_pkg_log(success=True, persist=False)
"""
import sys
from datetime import datetime
from pathlib import Path

_VERBOSITY = 0
_unified_log_fh = None
_pkg_log_fh = None

_CLEARED_MARKER = "# log cleared after successful run\n"
_SEP = "# " + "─" * 60 + "\n"


def _session_header(label: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{_SEP}# {label} — {ts}\n{_SEP}"


def set_verbosity(level: int) -> None:
    global _VERBOSITY
    _VERBOSITY = max(0, int(level))


def get_verbosity() -> int:
    return _VERBOSITY


# ---------------------------------------------------------------------------
# File log management
# ---------------------------------------------------------------------------

def open_unified_log(path, purge: bool = False) -> None:
    """
    Open (or create) the unified log file.
    purge=True truncates the file before writing, regardless of prior content.
    """
    global _unified_log_fh
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if purge else "a"
    _unified_log_fh = open(path, mode, buffering=1)  # line-buffered
    _write_to_files(_session_header("sysforge install"), raw=True)


def close_unified_log(success: bool = True, persist: bool = False) -> None:
    """
    Close the unified log. Truncates on success unless persist=True.
    """
    global _unified_log_fh
    if _unified_log_fh is None:
        return
    if success and not persist:
        _unified_log_fh.seek(0)
        _unified_log_fh.truncate()
        _unified_log_fh.write(_CLEARED_MARKER)
    _unified_log_fh.close()
    _unified_log_fh = None


def open_pkg_log(path) -> None:
    """Open (or create) the per-package log file, appending."""
    global _pkg_log_fh
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _pkg_log_fh = open(path, "a", buffering=1)
    _write_to_files(_session_header(f"sysforge build {path.parent.name}"), raw=True)


def close_pkg_log(success: bool = True, persist: bool = False) -> None:
    """
    Close the per-package log. Truncates on success unless persist=True.
    """
    global _pkg_log_fh
    if _pkg_log_fh is None:
        return
    if success and not persist:
        _pkg_log_fh.seek(0)
        _pkg_log_fh.truncate()
        _pkg_log_fh.write(_CLEARED_MARKER)
    _pkg_log_fh.close()
    _pkg_log_fh = None


def _write_to_files(line: str, raw: bool = False) -> None:
    """Write a line to all open file handles. raw=True skips formatting."""
    for fh in (_unified_log_fh, _pkg_log_fh):
        if fh is not None:
            try:
                fh.write(line)
            except Exception:
                pass  # never let file I/O break the build


# ---------------------------------------------------------------------------
# Log functions
# ---------------------------------------------------------------------------

def error(tag: str, message: str) -> None:
    """Always printed regardless of verbosity. Always written to log files."""
    line = f"[SYSFORGE][ERROR]{tag} {message}\n"
    print(line, end="", file=sys.stderr)
    _write_to_files(line)


def warn(tag: str, message: str) -> None:
    """Printed at verbosity >= 1 (-v). Always written to log files."""
    line = f"[SYSFORGE][WARN]{tag} {message}\n"
    if _VERBOSITY >= 1:
        print(line, end="", file=sys.stderr)
    _write_to_files(line)


def info(tag: str, message: str) -> None:
    """Printed at verbosity >= 2 (-vv). Always written to log files."""
    line = f"[SYSFORGE][INFO]{tag} {message}\n"
    if _VERBOSITY >= 2:
        print(line, end="", file=sys.stderr)
    _write_to_files(line)


def debug(tag: str, message: str) -> None:
    """
    Printed at verbosity >= 3 (-vvv). Always written to log files.
    Multi-line messages are split and each line is emitted with its own prefix.
    Use for full config/profile/conf body dumps.
    """
    for part in (message.splitlines() or [""]):
        entry = f"[SYSFORGE][DEBUG]{tag} {part}\n"
        if _VERBOSITY >= 3:
            print(entry, end="", file=sys.stderr)
        _write_to_files(entry)
