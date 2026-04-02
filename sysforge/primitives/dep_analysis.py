"""
dep_analysis.py — pre-build dependency analysis

Checks .so entries in a package's declared depends against ldconfig's cache,
surfacing soname/ABI mismatches before the build starts rather than as cryptic
mid-build linker errors.

Version constraint checking (depends on pacman -Q / vercmp) is intentionally
omitted — makepkg already does this and any pre-check adds false-positive risk
without meaningful value.

Public API:
    run_dep_analysis(pkgmeta, config, ldconfig_fn=None)
"""
import re
import subprocess

from sysforge.primitives.failure import handle_failure
from sysforge import log
_log = log.get_logger("DEP")


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches soname entries in depends: libfoo.so  or  libfoo.so=2
# The =N form means soname major version N → libfoo.so.N in ldconfig.
_SONAME_RE = re.compile(r"^(?P<base>\S+\.so)(?:=(?P<major>\d+))?$")


# ---------------------------------------------------------------------------
# Default system call implementation
# ---------------------------------------------------------------------------

def _default_ldconfig_fn():
    """
    Run ldconfig -p and return its stdout as a string.
    Returns empty string on failure (e.g. ldconfig not found).
    """
    try:
        result = subprocess.run(
            ["ldconfig", "-p"],
            capture_output=True,
            text=True,
        )
        return result.stdout
    except FileNotFoundError:
        _log.warn("ldconfig not found — skipping soname checks")
        return ""


# ---------------------------------------------------------------------------
# Soname dependency check
# ---------------------------------------------------------------------------

def _parse_ldconfig(ldconfig_output):
    """
    Parse ldconfig -p output into a set of available soname filenames.

    ldconfig -p lines look like:
        \tlibcap.so.2 (libc6,x86-64) => /usr/lib/libcap.so.2

    Returns a set of bare soname names e.g. {"libcap.so.2", "libcap.so.2.69"}.
    """
    sonames = set()
    for line in ldconfig_output.splitlines():
        line = line.strip()
        if not line or "=>" not in line:
            continue
        sonames.add(line.split()[0])
    return sonames


def check_soname_deps(depends, config, pkgname="unknown", ldconfig_fn=None):
    """
    Check that all .so entries in depends are present in ldconfig's cache
    at the expected major version.

    Entries checked:
      libfoo.so      — any version of libfoo.so must be present
      libfoo.so=2    — libfoo.so.2 must be present specifically

    Non-.so entries are silently skipped.

    Calls handle_failure("abi_mismatch", ...) for each missing soname.
    Returns list of (soname_entry, issue_str) tuples for all findings.
    """
    if ldconfig_fn is None:
        ldconfig_fn = _default_ldconfig_fn

    available = _parse_ldconfig(ldconfig_fn())
    findings = []

    for entry in depends:
        m = _SONAME_RE.match(entry)
        if not m:
            continue

        base = m.group("base")
        major = m.group("major")

        if major is not None:
            expected = f"{base}.{major}"
            prefix = expected + "."
            if not any(s == expected or s.startswith(prefix) for s in available):
                issue = (
                    f"soname {expected!r} not found in ldconfig cache "
                    f"(required by depends entry {entry!r})"
                )
                _log.warn(f"[{pkgname}] ABI mismatch: {issue}")
                handle_failure("abi_mismatch", issue, config)
                findings.append((entry, issue))
            else:
                _log.info(f"[{pkgname}] soname ok: {expected} → found")
        else:
            prefix = base + "."
            if base not in available and not any(s.startswith(prefix) for s in available):
                issue = f"soname {base!r} (any version) not found in ldconfig cache"
                _log.warn(f"[{pkgname}] ABI mismatch: {issue}")
                handle_failure("abi_mismatch", issue, config)
                findings.append((entry, issue))
            else:
                _log.info(f"[{pkgname}] soname ok: {base} → found")

    return findings


# ---------------------------------------------------------------------------
# Makedepends runtime probes
# ---------------------------------------------------------------------------

_MAKEDEP_PROBE_TIMEOUT = 15  # seconds

# Map of makedepend package names to (probe_cmd, description) tuples.
# Each probe_cmd is run with a timeout; non-zero exit or timeout = failure.
_MAKEDEP_PROBES = {
    "libguestfs": (
        ["guestfish", "add", "/dev/null", ":", "run"],
        "libguestfs appliance boot (requires compatible kernel modules: "
        "virtio, ext4, 9p, etc.)",
    ),
}


def check_makedep_runtime(makedepends, config, pkgname="unknown",
                          run_fn=None):
    """
    Probe makedepends that have known runtime requirements beyond package
    installation (e.g. libguestfs needs a bootable kernel appliance).

    Only probes packages that are actually in the PKGBUILD's makedepends.
    Each probe runs with a short timeout — failure produces a clear
    diagnostic instead of a silent mid-build hang.

    Returns list of (makedep, issue_str) tuples for failures.
    """
    if run_fn is None:
        run_fn = subprocess.run

    findings = []
    for dep in makedepends:
        # Strip version constraints: libguestfs>=1.50 → libguestfs
        bare = re.split(r"[><=]", dep)[0]
        if bare not in _MAKEDEP_PROBES:
            continue

        probe_cmd, description = _MAKEDEP_PROBES[bare]
        _log.info(f"[{pkgname}] probing makedep {bare!r}: {' '.join(probe_cmd)}")

        try:
            result = run_fn(
                probe_cmd,
                capture_output=True, text=True,
                timeout=_MAKEDEP_PROBE_TIMEOUT,
            )
            if result.returncode != 0:
                stderr_tail = (result.stderr or "").strip().splitlines()
                detail = stderr_tail[-1] if stderr_tail else f"exit code {result.returncode}"
                issue = (
                    f"makedep {bare!r} probe failed: {detail}\n"
                    f"  check: {description}"
                )
                _log.error(f"[{pkgname}] {issue}")
                handle_failure("makedep_probe_failed", issue, config)
                findings.append((bare, issue))
            else:
                _log.info(f"[{pkgname}] makedep probe ok: {bare}")
        except subprocess.TimeoutExpired:
            issue = (
                f"makedep {bare!r} probe timed out after {_MAKEDEP_PROBE_TIMEOUT}s — "
                f"appliance likely cannot boot\n"
                f"  check: {description}"
            )
            _log.error(f"[{pkgname}] {issue}")
            handle_failure("makedep_probe_failed", issue, config)
            findings.append((bare, issue))
        except FileNotFoundError:
            _log.info(f"[{pkgname}] makedep probe skipped ({bare!r} not installed)")

    return findings


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_dep_analysis(pkgmeta, config, ldconfig_fn=None, run_fn=None):
    """
    Run pre-build dependency checks against pkgmeta.

    Two check categories:
      1. Soname checks — .so entries in depends verified against ldconfig.
      2. Makedep runtime probes — makedepends with known runtime requirements
         (e.g. libguestfs appliance boot) are tested with a short timeout.

    Non-fatal by default — behaviour governed by [failure_handling]:
      abi_mismatch         (default: warn_and_fallback)
      makedep_probe_failed (default: warn_and_fallback)

    Returns list of (entry, issue_str) tuples. Empty list = all clear.
    """
    globals_ = pkgmeta.get("globals", {})
    pkgname = globals_.get("pkgname", "unknown")
    if isinstance(pkgname, list):
        pkgname = pkgname[0]

    findings = []

    # Soname checks
    depends = globals_.get("depends", [])
    soname_entries = [e for e in depends if _SONAME_RE.match(e)]
    if soname_entries:
        _log.info(f"[{pkgname}] checking {len(soname_entries)} soname dep(s)")
        findings.extend(
            check_soname_deps(depends, config, pkgname=pkgname, ldconfig_fn=ldconfig_fn)
        )
    else:
        _log.info(f"[{pkgname}] no soname entries in depends — skipping")

    # Makedep runtime probes
    makedepends = globals_.get("makedepends", [])
    if makedepends:
        findings.extend(
            check_makedep_runtime(makedepends, config, pkgname=pkgname, run_fn=run_fn)
        )

    if not findings:
        _log.info(f"[{pkgname}] all dependency checks passed")

    return findings
