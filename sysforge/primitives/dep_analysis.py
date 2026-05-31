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
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from sysforge.primitives.failure import handle_failure
from sysforge import log
_log = log.get_logger("DEP")


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches soname entries in depends. Forms emitted by makepkg's soname
# reduction:
#   libfoo.so             — any version
#   libfoo.so=2           — soname suffix "2"           → libfoo.so.2
#   libfoo.so=22.1        — soname suffix "22.1"        → libfoo.so.22.1
#   libfoo.so=22.1-64     — suffix "22.1", arch 64       (arch suffix ignored
#                                                         for ldconfig lookup)
_SONAME_RE = re.compile(
    r"^(?P<base>\S+\.so)(?:=(?P<ver>[^-=\s]+))?(?:-(?P<arch>\d+))?$"
)


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


def soname_satisfied(entry, available):
    """
    Return True if the soname depends entry is present in the ldconfig set.

    Entry forms:
      libfoo.so         — any version of libfoo.so matches
      libfoo.so=N       — libfoo.so.N must be present (or longer suffix)
      libfoo.so=N.M     — libfoo.so.N.M must be present (or longer suffix)
      libfoo.so=N-ARCH  — same as above; the ARCH suffix (32/64) is ignored
                          for the ldconfig match — ldconfig's own lookup
                          already selects the architecturally-correct lib
                          based on the runtime search path.

    Returns False for non-soname entries (no .so). Safe to pass arbitrary
    depends strings — non-matches are treated as not-applicable.
    """
    m = _SONAME_RE.match(entry)
    if not m:
        return False
    base = m.group("base")
    ver = m.group("ver")
    if ver is not None:
        expected = f"{base}.{ver}"
        prefix = expected + "."
        return any(s == expected or s.startswith(prefix) for s in available)
    prefix = base + "."
    return base in available or any(s.startswith(prefix) for s in available)


# ---------------------------------------------------------------------------
# Filesystem fallback for stale ldconfig cache
# ---------------------------------------------------------------------------
#
# /etc/ld.so.cache is only refreshed when ldconfig runs as root (typically
# from a package install hook). Between an install and the cache refresh —
# or when the install hook is missing — `ldconfig -p` reports a soname as
# absent even though the .so file is on disk and is dlopen-able. To stop
# doctor from flagging these as "soname not in ldconfig", consult the
# library directories directly when the cache check misses.

@lru_cache(maxsize=2)
def _resolve_lib_dirs(lib32: bool) -> tuple[Path, ...]:
    """
    Library directories the dynamic linker actually searches.

    For lib32 we only consult /usr/lib32 (Arch's multilib convention).
    For the default case we start from /usr/lib + /lib and merge any
    absolute dirs declared in /etc/ld.so.conf.d/*.conf so vendor drops
    (e.g. nvidia-utils) are picked up. Cached per-process — the conf
    set doesn't change during a doctor run.
    """
    if lib32:
        return (Path("/usr/lib32"),)
    dirs: list[Path] = [Path("/usr/lib"), Path("/lib")]
    conf_d = Path("/etc/ld.so.conf.d")
    if conf_d.is_dir():
        for conf in sorted(conf_d.glob("*.conf")):
            try:
                content = conf.read_text()
            except OSError:
                continue
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("include"):
                    continue
                p = Path(line)
                if p.is_absolute() and p.is_dir():
                    dirs.append(p)
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        if d in seen:
            continue
        seen.add(d)
        out.append(d)
    return tuple(out)


@lru_cache(maxsize=2)
def _filesystem_soname_set(lib32: bool) -> frozenset[str]:
    """All .so* basenames present in the resolved lib dirs (cached)."""
    names: set[str] = set()
    for d in _resolve_lib_dirs(lib32):
        try:
            for entry in d.iterdir():
                if ".so" in entry.name:
                    names.add(entry.name)
        except OSError:
            continue
    return frozenset(names)


def _reset_soname_caches() -> None:
    """Clear the lib-dir and filesystem-soname caches. For tests."""
    _resolve_lib_dirs.cache_clear()
    _filesystem_soname_set.cache_clear()


def soname_available(entry, ldconfig_set, *, lib32=False):
    """
    True if the soname is satisfied by ``ldconfig_set`` OR present on disk
    in the standard library directories. The filesystem fallback masks
    stale /etc/ld.so.cache state — common immediately after an install
    when ldconfig hasn't been re-run.
    """
    if soname_satisfied(entry, ldconfig_set):
        return True
    return soname_satisfied(entry, _filesystem_soname_set(lib32))


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
    lib32 = pkgname.startswith("lib32-")
    findings = []

    for entry in depends:
        m = _SONAME_RE.match(entry)
        if not m:
            continue

        if soname_available(entry, available, lib32=lib32):
            display = f"{m.group('base')}.{m.group('ver')}" if m.group("ver") else m.group("base")
            _log.info(f"[{pkgname}] soname ok: {display} → found")
            continue

        if m.group("ver") is not None:
            expected = f"{m.group('base')}.{m.group('ver')}"
            issue = (
                f"soname {expected!r} not found in ldconfig cache "
                f"(required by depends entry {entry!r})"
            )
        else:
            issue = f"soname {m.group('base')!r} (any version) not found in ldconfig cache"
        _log.warn(f"[{pkgname}] ABI mismatch: {issue}")
        handle_failure("abi_mismatch", issue, config)
        findings.append((entry, issue))

    return findings


# ---------------------------------------------------------------------------
# Makedepends runtime probes
# ---------------------------------------------------------------------------

_MAKEDEP_PROBE_TIMEOUT = 15  # seconds

# Modules the libguestfs appliance requires to boot.  The appliance uses
# virtio-scsi for disk access (QEMU -device scsi-hd) and ext4 for the root
# filesystem.  If any of these are missing from the running kernel (neither
# built-in nor loadable as a module), the appliance hangs waiting for root.
_GUESTFS_REQUIRED_MODULES = {
    "CONFIG_VIRTIO":       "virtio core",
    "CONFIG_VIRTIO_PCI":   "virtio PCI transport",
    "CONFIG_SCSI_VIRTIO":  "virtio-scsi (appliance disk access)",
    "CONFIG_EXT4_FS":      "ext4 (appliance root filesystem)",
    "CONFIG_VIRTIO_NET":   "virtio-net (appliance networking)",
}

# Set of makedepend names that have custom probes.
_PROBED_MAKEDEPS = frozenset({"libguestfs"})


def read_running_kconfig_text() -> str | None:
    """Return the running kernel's raw ``.config`` text, or None if unavailable.

    Source is ``/proc/config.gz`` (preferred) then ``/boot/config-$(uname -r)``.
    Single source of truth for *locating* the running config — both the parsed
    reader below and the kernel stage's ``base_config = "running"`` seeding share
    it so the lookup order can't drift.
    """
    import gzip

    config_path = Path("/proc/config.gz")
    boot_config = Path(f"/boot/config-{os.uname().release}")

    if config_path.exists():
        with gzip.open(config_path, "rt") as f:
            return f.read()
    if boot_config.exists():
        return boot_config.read_text()
    return None


def _parse_kernel_config():
    """
    Parse the running kernel's config from /proc/config.gz or
    /boot/config-$(uname -r).  Returns dict of CONFIG_KEY → value
    ('y', 'm', or 'n' for unset).  Returns None if config is unavailable.
    """
    from sysforge.primitives.kernel_safety import parse_kconfig_text

    text = read_running_kconfig_text()
    if text is None:
        return None

    # Shared .config line parser — see kernel_safety.parse_kconfig_text.
    return parse_kconfig_text(text)


def _diagnose_guestfs(output, kernel_config):
    """
    Parse libguestfs debug output and kernel config to produce a specific
    diagnostic.  Returns a list of human-readable issue strings.
    """
    issues = []

    # Check for "waiting for root UUID" — appliance can't find its root disk
    if "waiting" in output and "root UUID" in output:
        missing = []
        for key, desc in _GUESTFS_REQUIRED_MODULES.items():
            val = kernel_config.get(key, "n") if kernel_config else None
            if val == "n":
                missing.append(f"  {key}=m  ({desc})")
            elif val is None:
                missing.append(f"  {key}=m  ({desc}) [cannot verify — /proc/config.gz unavailable]")
        if missing:
            issues.append(
                "appliance cannot find root disk — missing kernel config options:\n"
                + "\n".join(missing)
            )
        else:
            issues.append(
                "appliance cannot find root disk — required modules appear enabled "
                "but the appliance still failed to boot (stale appliance cache?)"
            )
    return issues


def _probe_libguestfs(pkgname, config, run_fn):
    """
    Probe libguestfs by booting its appliance with debug output enabled.
    Parses the output on failure to identify missing kernel modules.

    Returns list of (makedep, issue_str) tuples.
    """
    probe_cmd = ["guestfish", "add", "/dev/null", ":", "run"]
    probe_env = {**os.environ, "LIBGUESTFS_DEBUG": "1"}
    _log.info(f"[{pkgname}] probing libguestfs appliance: {' '.join(probe_cmd)}")

    try:
        result = run_fn(
            probe_cmd,
            capture_output=True, text=True,
            timeout=_MAKEDEP_PROBE_TIMEOUT,
            env=probe_env,
        )
        if result.returncode == 0:
            _log.info(f"[{pkgname}] libguestfs appliance boot ok")
            return []
        combined = (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired as e:
        parts = []
        for s in (e.stdout, e.stderr):
            if isinstance(s, bytes):
                parts.append(s.decode(errors="replace"))
            elif isinstance(s, str):
                parts.append(s)
        combined = "".join(parts)
    except FileNotFoundError:
        _log.info(f"[{pkgname}] libguestfs probe skipped (guestfish not installed)")
        return []

    # Probe failed or timed out — diagnose
    kernel_config = _parse_kernel_config()
    diagnostics = _diagnose_guestfs(combined, kernel_config)

    if diagnostics:
        detail = "; ".join(diagnostics)
    else:
        detail = "appliance failed to boot (run LIBGUESTFS_DEBUG=1 guestfish add /dev/null : run for details)"

    issue = f"makedep 'libguestfs' probe failed: {detail}"
    _log.error(f"[{pkgname}] {issue}")
    handle_failure("makedep_probe_failed", issue, config)
    return [("libguestfs", issue)]


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
        bare = re.split(r"[><=]", dep)[0]
        if bare not in _PROBED_MAKEDEPS:
            continue

        if bare == "libguestfs":
            findings.extend(_probe_libguestfs(pkgname, config, run_fn))

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
