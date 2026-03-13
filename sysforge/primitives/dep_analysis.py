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
        print("[DEP] ldconfig not found — skipping soname checks")
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
                print(f"[DEP][{pkgname}] ABI mismatch: {issue}")
                handle_failure("abi_mismatch", issue, config)
                findings.append((entry, issue))
            else:
                print(f"[DEP][{pkgname}] soname ok: {expected} → found")
        else:
            prefix = base + "."
            if base not in available and not any(s.startswith(prefix) for s in available):
                issue = f"soname {base!r} (any version) not found in ldconfig cache"
                print(f"[DEP][{pkgname}] ABI mismatch: {issue}")
                handle_failure("abi_mismatch", issue, config)
                findings.append((entry, issue))
            else:
                print(f"[DEP][{pkgname}] soname ok: {base} → found")

    return findings


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_dep_analysis(pkgmeta, config, ldconfig_fn=None):
    """
    Run pre-build soname dependency checks against pkgmeta.

    Extracts depends from pkgmeta globals and checks all .so entries against
    ldconfig's cache. makedepends are not checked — soname entries only
    appear in runtime depends, not build deps.

    Non-fatal by default — behaviour governed by [failure_handling]:
      abi_mismatch  (default: warn_and_fallback)

    Returns list of (entry, issue_str) tuples. Empty list = all clear.
    """
    globals_ = pkgmeta.get("globals", {})
    pkgname = globals_.get("pkgname", "unknown")
    if isinstance(pkgname, list):
        pkgname = pkgname[0]

    depends = globals_.get("depends", [])

    soname_entries = [e for e in depends if _SONAME_RE.match(e)]
    if not soname_entries:
        print(f"[DEP][{pkgname}] no soname entries in depends — skipping")
        return []

    print(f"[DEP][{pkgname}] checking {len(soname_entries)} soname dep(s)")

    findings = check_soname_deps(depends, config, pkgname=pkgname, ldconfig_fn=ldconfig_fn)

    if not findings:
        print(f"[DEP][{pkgname}] all soname checks passed")

    return findings
