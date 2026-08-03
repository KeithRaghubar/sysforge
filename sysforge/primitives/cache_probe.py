# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
cache_probe.py — passive cache monitoring for [CACHE] log tag

Probes ccache/sccache stats before and after each build to compute per-build
hit/miss deltas. Also probes system-level caches (pacman, ld.so) and ThinLTO
cache directories for informational logging.

Passive: never enables, disables, or clears any cache. Read-only.

Public API:
    probe_ccache()                      → dict | None
    probe_sccache()                     → dict | None
    diff_ccache(before, after)          → dict
    diff_sccache(before, after)         → dict
    emit_build_stats(pkgname, cc, sc)   → None
    probe_pacman_cache(path)            → dict | None
    probe_ldso_mtime(path)              → str | None
    probe_thinlto_cache(ldflags)        → dict | None
    emit_system_probes(ldflags)         → None

    # Session accumulation for --cache-report:
    reset_session()
    record_build_result(pkgname, cc_delta, sc_delta)
    emit_session_report()
"""
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from sysforge import log
from sysforge.primitives.render import fmt_bytes as _fmt_bytes

_log = log.get_logger("CACHE")

_SESSION_RECORDS: list[dict] = []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_command(cmd: list[str]) -> str | None:
    """Run a command and return stdout, or None on failure/timeout."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# ccache
# ---------------------------------------------------------------------------

def probe_ccache() -> dict | None:
    """
    Return ccache stats as a dict, or None if ccache is not installed.

    Uses --print-stats --format=tab (ccache >= 4.0, standard on Arch).
    Keys: direct_hits, preprocessed_hits, misses, files, size_bytes.
    """
    if not shutil.which("ccache"):
        return None
    out = _run_command(["ccache", "--print-stats", "--format=tab"])
    if out is None:
        return None
    return _parse_ccache_tab(out)


def _parse_ccache_tab(text: str) -> dict:
    """Parse ccache --print-stats --format=tab output."""
    kv: dict[str, str] = {}
    for line in text.splitlines():
        if "\t" in line:
            k, _, v = line.partition("\t")
            kv[k.strip()] = v.strip()

    def _int(key: str) -> int:
        try:
            return int(kv.get(key, "0"))
        except ValueError:
            return 0

    return {
        "direct_hits": _int("direct_cache_hit"),
        "preprocessed_hits": _int("preprocessed_cache_hit"),
        "misses": _int("cache_miss"),
        "files": _int("files_in_cache"),
        "size_bytes": _int("cache_size_bytes"),
    }


def diff_ccache(before: dict, after: dict) -> dict:
    """Return per-build delta between two ccache snapshots."""
    return {
        "direct_hits": after["direct_hits"] - before["direct_hits"],
        "preprocessed_hits": after["preprocessed_hits"] - before["preprocessed_hits"],
        "misses": after["misses"] - before["misses"],
        # Post-build totals (not deltas — reflect current cache state)
        "files": after["files"],
        "size_bytes": after["size_bytes"],
    }


# ---------------------------------------------------------------------------
# sccache
# ---------------------------------------------------------------------------

def probe_sccache() -> dict | None:
    """
    Return sccache stats as a dict, or None if sccache is not installed.

    Parses sccache --show-stats text output.
    Keys: hits, misses, requests, size_str.
    """
    if not shutil.which("sccache"):
        return None
    out = _run_command(["sccache", "--show-stats"])
    if out is None:
        return None
    return _parse_sccache_text(out)


def _parse_sccache_text(text: str) -> dict:
    """Parse sccache --show-stats text output."""
    stats = {"hits": 0, "misses": 0, "requests": 0, "size_str": ""}
    for line in text.splitlines():
        s = line.strip()
        # Skip sub-category lines like "Cache hits (C++)" and rate lines like
        # "Cache hits rate" introduced in sccache 0.14 — the float value (e.g.
        # "86.09 %") is not parseable by _extract_last_int and would zero out
        # the hit count that was correctly parsed from the earlier "Cache hits" line.
        if s.startswith("Cache hits") and "(" not in s and "rate" not in s:
            stats["hits"] = _extract_last_int(s)
        elif s.startswith("Cache misses") and "(" not in s and "rate" not in s:
            stats["misses"] = _extract_last_int(s)
        elif s.startswith("Compile requests") and "executed" not in s:
            stats["requests"] = _extract_last_int(s)
        elif s.startswith("Cache size"):
            parts = s.split()
            if len(parts) >= 3:
                stats["size_str"] = parts[-2] + " " + parts[-1]
    return stats


def _extract_last_int(line: str) -> int:
    """Extract the last integer token from a line."""
    for part in reversed(line.split()):
        if part.isdigit():
            return int(part)
    return 0


def diff_sccache(before: dict, after: dict) -> dict:
    """Return per-build delta between two sccache snapshots."""
    return {
        "hits": after["hits"] - before["hits"],
        "misses": after["misses"] - before["misses"],
        "requests": after["requests"] - before["requests"],
        "size_str": after.get("size_str", ""),
    }


# ---------------------------------------------------------------------------
# Emit per-build cache stats
# ---------------------------------------------------------------------------

def emit_build_stats(pkgname: str, cc_delta: dict | None, sc_delta: dict | None) -> None:
    """Emit [CACHE] INFO lines for a single package build's cache usage."""
    if cc_delta is not None:
        hits = cc_delta["direct_hits"] + cc_delta["preprocessed_hits"]
        misses = cc_delta["misses"]
        total = hits + misses
        size = _fmt_bytes(cc_delta["size_bytes"])
        if total > 0:
            pct = 100 * hits // total
            _log.info(f"{pkgname}: ccache hits={hits} misses={misses} "
                      f"({pct}% hit rate) cache={size} files={cc_delta['files']}")
        else:
            _log.info(f"{pkgname}: ccache — no compilations recorded "
                      f"(cache={size} files={cc_delta['files']})")

    if sc_delta is not None:
        hits = sc_delta["hits"]
        misses = sc_delta["misses"]
        total = hits + misses
        if total > 0:
            pct = 100 * hits // total
            _log.info(f"{pkgname}: sccache hits={hits} misses={misses} "
                      f"({pct}% hit rate) cache={sc_delta['size_str']}")
        else:
            _log.info(f"{pkgname}: sccache — no compilations recorded "
                      f"(cache={sc_delta['size_str']})")


# ---------------------------------------------------------------------------
# Cache readiness (2.2.0-F1 — doctor `cache` axis)
#
# Point-in-time "is the compile cache set up correctly *before* a build relies
# on it?" — the readiness lens, distinct from the per-build effectiveness
# tracked by --cache-report. Kept here (the one home for cache knowledge) so no
# cache subprocess logic leaks into doctor.py. Best-effort throughout: a missing
# tool or unreadable config degrades to a fact, never a hard failure.
# ---------------------------------------------------------------------------

# Leading numeric part of a size string ("5.0 GB", "10 GiB", "0"). We only need
# to know whether a configured max size is non-zero, so the unit is ignored.
_LEADING_NUM_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)")


def _size_is_nonzero(raw: str | None) -> bool:
    """True when a size string carries a numeric value greater than zero."""
    if not raw:
        return False
    m = _LEADING_NUM_RE.match(raw)
    if not m:
        return False
    try:
        return float(m.group(1)) > 0
    except ValueError:
        return False


def _dir_writable(path: str | None) -> bool:
    """True when ``path`` is an existing writable directory, or (when it does
    not exist yet) its nearest existing ancestor is writable — a cache dir that
    ccache/sccache will create on first use still counts as ready."""
    if not path:
        return False
    p = Path(path)
    while True:
        if p.is_dir():
            return os.access(p, os.W_OK | os.X_OK)
        parent = p.parent
        if parent == p:
            return False
        p = parent


def _ccache_readiness() -> dict:
    if not shutil.which("ccache"):
        return {"tool": "ccache", "installed": False, "state": "absent",
                "detail": "ccache is not installed", "remediation": None}
    cache_dir = _run_command(["ccache", "--get-config", "cache_dir"])
    max_size = _run_command(["ccache", "--get-config", "max_size"])
    cache_dir = cache_dir.strip() if cache_dir else None
    max_size = max_size.strip() if max_size else None

    problems: list[str] = []
    if not _dir_writable(cache_dir):
        problems.append(f"cache dir not writable ({cache_dir or 'unknown'})")
    if not _size_is_nonzero(max_size):
        problems.append(f"max cache size unset or zero ({max_size or 'unknown'})")

    if problems:
        return {"tool": "ccache", "installed": True, "state": "misconfigured",
                "detail": "; ".join(problems),
                "remediation": "set a cache size cap with `ccache -M <size>` "
                               "(e.g. `ccache -M 20G`) and ensure its cache dir "
                               "is writable"}
    return {"tool": "ccache", "installed": True, "state": "ok",
            "detail": f"cache dir {cache_dir}, max size {max_size}",
            "remediation": None}


def _sccache_readiness() -> dict:
    if not shutil.which("sccache"):
        return {"tool": "sccache", "installed": False, "state": "absent",
                "detail": "sccache is not installed", "remediation": None}

    stats = _run_command(["sccache", "--show-stats"]) or ""
    # Max size: SCCACHE_CACHE_SIZE env wins, else the "Max cache size" stat line.
    max_size = os.environ.get("SCCACHE_CACHE_SIZE")
    if not max_size:
        for line in stats.splitlines():
            if line.strip().startswith("Max cache size"):
                max_size = line.split("size", 1)[1].strip()
                break
    # Local disk dir: SCCACHE_DIR env, else the quoted path on the location line.
    cache_dir = os.environ.get("SCCACHE_DIR")
    if not cache_dir:
        for line in stats.splitlines():
            if "Local disk:" in line:
                m = re.search(r'Local disk:\s*"?([^"]+)"?', line)
                if m:
                    cache_dir = m.group(1).strip()
                break

    problems: list[str] = []
    # A cloud-backed sccache has no local disk dir; only fault an *unwritable*
    # local dir, not an absent one.
    if cache_dir and not _dir_writable(cache_dir):
        problems.append(f"cache dir not writable ({cache_dir})")
    if not _size_is_nonzero(max_size):
        problems.append(f"max cache size unset or zero ({max_size or 'unknown'})")

    if problems:
        return {"tool": "sccache", "installed": True, "state": "misconfigured",
                "detail": "; ".join(problems),
                "remediation": "set a non-zero `SCCACHE_CACHE_SIZE` (e.g. "
                               "`SCCACHE_CACHE_SIZE=20G`) and ensure its cache "
                               "dir is writable"}
    return {"tool": "sccache", "installed": True, "state": "ok",
            "detail": f"cache dir {cache_dir or 'default/remote'}, "
                      f"max size {max_size}",
            "remediation": None}


def check_cache_readiness() -> list[dict]:
    """Report per-tool readiness of the compile caches (ccache, sccache).

    Each entry is ``{"tool", "installed", "state", "detail", "remediation"}``
    where ``state`` is ``"absent"`` (not installed), ``"ok"`` (installed with a
    writable cache dir and a non-zero size cap), or ``"misconfigured"`` (a fault
    the user can fix). Best-effort; never raises. Consumed by doctor's ``cache``
    axis (:func:`sysforge.doctor._collect_cache_findings`)."""
    return [_ccache_readiness(), _sccache_readiness()]


# ---------------------------------------------------------------------------
# System-level probes (emit once per run)
# ---------------------------------------------------------------------------

def probe_pacman_cache(path: str = "/var/cache/pacman/pkg") -> dict | None:
    """
    Return pacman cache file count and total size, or None if inaccessible.
    Counts both compressed (*.pkg.tar.zst, *.pkg.tar.xz) and uncompressed
    (*.pkg.tar) packages — the latter is produced when PKGEXT='.pkg.tar'.
    """
    p = Path(path)
    if not p.is_dir():
        return None
    try:
        files = list(p.glob("*.pkg.tar*"))
        total = sum(f.stat().st_size for f in files)
        return {"count": len(files), "size_bytes": total, "path": str(p)}
    except PermissionError:
        return None


def probe_ldso_mtime(path: str = "/etc/ld.so.cache") -> str | None:
    """Return the mtime of ld.so.cache as a formatted string, or None if missing."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        mtime = p.stat().st_mtime
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return None


def probe_thinlto_cache(ldflags: str) -> dict | None:
    """
    Extract --thinlto-cache-dir=PATH from an LDFLAGS string and report its size.

    Handles both bare tokens (--thinlto-cache-dir=PATH) and -Wl,--thinlto-cache-dir=PATH.
    Returns None if not configured. Returns dict with exists=False if configured but not
    yet created (first build).
    """
    cache_dir = None
    for token in ldflags.split():
        inner = token[4:] if token.startswith("-Wl,") else token
        for sub in inner.split(","):
            if sub.startswith("--thinlto-cache-dir="):
                cache_dir = sub[len("--thinlto-cache-dir="):]
                break
        if cache_dir:
            break

    if not cache_dir:
        return None

    p = Path(cache_dir)
    if not p.is_dir():
        return {"path": cache_dir, "exists": False, "size_bytes": 0, "files": 0}

    try:
        all_files = [f for f in p.rglob("*") if f.is_file()]
        total = sum(f.stat().st_size for f in all_files)
        return {"path": cache_dir, "exists": True, "size_bytes": total, "files": len(all_files)}
    except OSError:
        return {"path": cache_dir, "exists": True, "size_bytes": 0, "files": 0}


def report_thinlto_cache(ldflags: str) -> None:
    """Emit a one-line ``[CACHE]`` INFO summary of the ThinLTO cache configured
    in ``ldflags`` — its size if the dir exists, a not-yet-created note
    otherwise.  No-op when no ThinLTO cache dir is configured.  Called once per
    build by the build orchestrator and once per run by
    :func:`emit_system_probes`, so the cache-reporting emission has a single
    home in this module rather than being duplicated at the call sites.
    """
    if not ldflags:
        return
    thinlto = probe_thinlto_cache(ldflags)
    if not thinlto:
        return
    if thinlto["exists"]:
        _log.info(f"ThinLTO cache: {_fmt_bytes(thinlto['size_bytes'])} "
                  f"in {thinlto['files']} files ({thinlto['path']})")
    else:
        _log.info(f"ThinLTO cache dir configured but not yet created: {thinlto['path']}")


def emit_system_probes(ldflags: str = "") -> None:
    """
    Emit [CACHE] INFO lines for system-level caches.
    Call once per run before builds start.
    """
    mtime = probe_ldso_mtime()
    if mtime:
        _log.info(f"ld.so.cache mtime: {mtime}")

    pacman = probe_pacman_cache()
    if pacman:
        _log.info(f"pacman cache: {pacman['count']} packages, "
                  f"{_fmt_bytes(pacman['size_bytes'])} ({pacman['path']})")

    report_thinlto_cache(ldflags)


# ---------------------------------------------------------------------------
# Session accumulation for --cache-report
# ---------------------------------------------------------------------------

def reset_session() -> None:
    """Clear accumulated build records. Call at start of a new run."""
    _SESSION_RECORDS.clear()


def record_build_result(pkgname: str, cc_delta: dict | None, sc_delta: dict | None) -> None:
    """Accumulate per-build cache stats for the end-of-run report."""
    _SESSION_RECORDS.append({
        "pkgname": pkgname,
        "ccache": cc_delta,
        "sccache": sc_delta,
    })


def emit_session_report() -> None:
    """
    Print a structured cache summary to stderr.
    Always shown regardless of verbosity level.
    Called at end of run when --cache-report is set.
    """
    divider = "─" * 46
    _log.ui(divider)
    _log.ui("Cache Report")
    _log.ui(divider)

    if not _SESSION_RECORDS:
        _log.ui("  No cache data recorded.")
        _log.ui(divider)
        return

    total_cc_hits = total_cc_misses = 0
    total_sc_hits = total_sc_misses = 0
    has_cc = has_sc = False

    for rec in _SESSION_RECORDS:
        pkgname = rec["pkgname"]
        cc = rec["ccache"]
        sc = rec["sccache"]

        if cc is not None:
            has_cc = True
            hits = cc["direct_hits"] + cc["preprocessed_hits"]
            misses = cc["misses"]
            total_cc_hits += hits
            total_cc_misses += misses
            total = hits + misses
            pct = f"{100 * hits // total}%" if total > 0 else "n/a"
            size = _fmt_bytes(cc["size_bytes"])
            _log.ui(
                f"  {pkgname}: ccache {hits}/{hits + misses} hits "
                f"({pct}) cache={size}"
            )

        if sc is not None:
            has_sc = True
            hits = sc["hits"]
            misses = sc["misses"]
            total_sc_hits += hits
            total_sc_misses += misses
            total = hits + misses
            pct = f"{100 * hits // total}%" if total > 0 else "n/a"
            _log.ui(
                f"  {pkgname}: sccache {hits}/{hits + misses} hits "
                f"({pct}) cache={sc['size_str']}"
            )

    _log.ui(divider)

    if has_cc:
        total = total_cc_hits + total_cc_misses
        pct = f"{100 * total_cc_hits // total}%" if total > 0 else "n/a"
        _log.ui(f"  ccache total: {total_cc_hits}/{total} hits ({pct})")

    if has_sc:
        total = total_sc_hits + total_sc_misses
        pct = f"{100 * total_sc_hits // total}%" if total > 0 else "n/a"
        _log.ui(f"  sccache total: {total_sc_hits}/{total} hits ({pct})")

    if not has_cc and not has_sc:
        _log.ui("  ccache and sccache not installed.")

    _log.ui(divider)
