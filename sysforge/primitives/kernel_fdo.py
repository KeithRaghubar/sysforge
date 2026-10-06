# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kernel_fdo.py — kernel AutoFDO / Propeller orchestration (sample-based FDO)

The one home for sysforge's *sample*-based kernel optimization (`run kernel
--autofdo=…` / `--propeller`). It is the third optimization method on the shared
profile-store rails, and the first that is **sample**-based rather than
*instrumentation*-based:

  * The LLVM toolchain PGO and mesa PGO (``mesa_pgo``) instrument the binary
    (``-fprofile-generate``) — the program counts its own executions.
  * Kernel AutoFDO never instruments. You build a kernel with
    ``CONFIG_AUTOFDO_CLANG=y`` (which only adds debug info for profiling), boot
    it, sample its branches with ``perf record -b`` against a real workload, and
    convert the samples to a profile with ``llvm-profgen``. The optimized
    rebuild consumes that profile via the kernel build's ``CLANG_AUTOFDO_PROFILE``
    make-variable (``-fprofile-sample-use`` under the hood).

Because the sample-collection step runs on the *booted* profiling kernel, it
necessarily spans a reboot — sysforge cannot capture in the same invocation that
built the profiling kernel. The flow is therefore three steps, run twice when
Propeller is wanted (round 1 AutoFDO, round 2 Propeller on the AutoFDO kernel):

  1. ``--autofdo=record`` — build+install a profiling kernel under the
     ``-profiling`` role name. The kconfig fragment gains :data:`CONFIG_AUTOFDO`
     (+ :data:`CONFIG_PROPELLER`).
  2. ``--autofdo=capture`` — a read-only step: refuse unless the needed tools are
     on PATH and the running kernel is the profiling kernel, then print the exact
     ``perf record`` command plus the converter (``llvm-profgen`` for AutoFDO,
     ``generate_propeller_profiles`` for Propeller) for the operator to run while
     exercising the machine. sysforge does not run ``perf`` itself.
  3. ``--autofdo=use`` — rebuild with the collected profile injected through the
     kernel build's make-variables (the existing ``extra_env`` seam — ``make``
     imports env vars as make-variables, exactly as the kernel build already
     injects ``LLVM=1``). The optimized kernel earns the ``-fdo`` / ``-propeller``
     role name so it installs alongside the stock kernel for bootloader fallback.

Store resolution defers to :func:`makepkg_pgo.resolve_method_store` (methods
``"autofdo"`` / ``"propeller"``, namespaced per kernel pkgname). LLVM-only — the
whole feature is gated on the Clang toolchain by the kernel stage; Propeller and
the kernel's Clang AutoFDO have no GCC equivalent. Pure except for the
filesystem existence checks in :func:`require_profile` / :func:`resolve_vmlinux`
and the ``/proc/cpuinfo`` read in :func:`detect_branch_sampling` (injectable for
tests).
"""
import hashlib
import mmap
import os
import re
import shlex
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from sysforge.primitives.artifacts import toml_escape
from sysforge.primitives.makepkg_pgo import ROUND_SIDECAR, resolve_method_store
from sysforge.primitives.paths import TOOLCHAIN_PATH
from sysforge.primitives.pkgbuild_patcher import RENAME_SUFFIX

# kconfig options that enable the kernel's Clang FDO machinery. Both the
# `record` and the `use` build need CONFIG_AUTOFDO_CLANG=y — `record` to emit the
# profiling debug info, `use` because the same option is what wires
# CLANG_AUTOFDO_PROFILE into -fprofile-sample-use. CONFIG_PROPELLER_CLANG layers
# basic-block ordering on top.
CONFIG_AUTOFDO = "CONFIG_AUTOFDO_CLANG"
CONFIG_PROPELLER = "CONFIG_PROPELLER_CLANG"

# Make-variables the kernel build reads (passed via the extra_env seam — `make`
# imports the environment as make-variables). CLANG_AUTOFDO_PROFILE points at the
# merged AutoFDO profile; CLANG_PROPELLER_PROFILE_PREFIX is the basename prefix of
# the Propeller `<prefix>_cc_profile.txt` / `<prefix>_ld_profile.txt` pair.
ENV_AUTOFDO = "CLANG_AUTOFDO_PROFILE"
ENV_PROPELLER = "CLANG_PROPELLER_PROFILE_PREFIX"

# Artifact names inside the per-kernel store.
AUTOFDO_PROFILE_NAME = "kernel.afdo"   # llvm-profgen --format=extbinary output
PERF_DATA_NAME = "perf.data"           # raw perf sample buffer
PROPELLER_PREFIX_NAME = "propeller"    # → propeller_cc_profile.txt / propeller_ld_profile.txt

# build_mode values these flows record (see profile.is_optimized_build_mode and
# profile.rename_mode_for_build_mode — both are "coexist" kernel modes). The
# -sysforge rename they trigger is applied generically in makepkg_wrapper, not here.
BUILD_MODE_AUTOFDO = "autofdo_kernel"
BUILD_MODE_PROPELLER = "propeller_kernel"

VALID_MODES = ("record", "capture", "use")

# perf sampling period (branches between samples). The kernel AutoFDO docs use
# this order of magnitude; larger = lower overhead, sparser profile.
_PERF_PERIOD = 500009
# Capture window for the printed `perf record -- sleep N` command.
_CAPTURE_SECONDS = 300


class KernelFdoError(Exception):
    """A kernel-FDO step cannot proceed (no collected profile for ``use``, an
    invalid mode). Raised so the kernel stage aborts cleanly *before* a multi-hour
    build, with an actionable hint, rather than silently building an unprofiled
    kernel."""


# Per-role coexist names (3.3.0-F6). Every FDO kernel installs beside the plain
# one: `<pkgname>`, then `-sysforge` if absent, then exactly one role suffix.
# The coexist name is itself the ownership gate (F26) — a reinstall only ever
# replaces the prior kernel of the same role.
_SYSFORGE = f"-{RENAME_SUFFIX}"  # the coexist rename infix, one home
ROLE_PROFILING = "-profiling"
ROLE_FDO = "-fdo"
ROLE_PROPELLER = "-propeller"
_ROLES = (ROLE_PROFILING, ROLE_FDO, ROLE_PROPELLER)


def strip_role(name: str) -> str:
    """``linux-sysforge-fdo`` → ``linux-sysforge``; any non-role name unchanged."""
    for r in _ROLES:
        if name.endswith(_SYSFORGE + r):
            return name[: -len(r)]
    return name


def _role_name(pkgname: str, role: str) -> str:
    base = strip_role(pkgname)
    if not base.endswith(_SYSFORGE):
        base += _SYSFORGE
    return base + role


def build_mode(*, propeller: bool) -> str:
    """The optimization build_mode this run records (drives the coexist rename policy)."""
    return BUILD_MODE_PROPELLER if propeller else BUILD_MODE_AUTOFDO


def record_pkgname(pkgname: str) -> str:
    """The profiling (``--autofdo=record``, both rounds) kernel's name.

    Kept in one home so the record dispatch and the ``capture`` step derive the
    same name. Idempotent across every role name.
    """
    return _role_name(pkgname, ROLE_PROFILING)


def optimized_pkgname(pkgname: str, *, propeller: bool) -> str:
    """The ``--autofdo=use`` kernel's name: ``-fdo`` or ``-propeller``.

    Always reflects what was *applied* — a Propeller→AutoFDO fallback is ``-fdo``.
    """
    return _role_name(pkgname, ROLE_PROPELLER if propeller else ROLE_FDO)


def _load_tcfg() -> dict | None:
    """Best-effort load of toolchain.toml for store-path overrides (pure)."""
    if not TOOLCHAIN_PATH.exists():
        return None
    try:
        with TOOLCHAIN_PATH.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def resolve_store(pkgname: str, *, propeller: bool, tcfg: dict | None = None) -> Path:
    """Resolve the per-kernel FDO store dir.

    AutoFDO and Propeller keep separate sibling subdirs under the shared profile
    root (``<root>/autofdo/<pkgname>`` and ``<root>/propeller/<pkgname>``), each
    namespaced by the kernel ``pkgname`` so two tracked kernels never collide.
    Pure path math — the directory is provisioned by the caller via
    ``fs_provision.ensure_writable_dir``.
    """
    method = "propeller" if propeller else "autofdo"
    return resolve_method_store(
        tcfg if tcfg is not None else _load_tcfg(), method, target=pkgname
    )


def autofdo_profile_path(store: Path) -> Path:
    """The merged AutoFDO profile ``-fprofile-sample-use`` consumes."""
    return store / AUTOFDO_PROFILE_NAME


def perf_data_path(store: Path) -> Path:
    """The raw ``perf.data`` the capture step writes and the converter reads."""
    return store / PERF_DATA_NAME


def propeller_prefix(store: Path) -> Path:
    """The Propeller profile prefix (``<store>/propeller``).

    ``generate_propeller_profiles`` writes ``<prefix>_cc_profile.txt`` and
    ``<prefix>_ld_profile.txt``; the kernel build reads the same pair via
    ``CLANG_PROPELLER_PROFILE_PREFIX``.
    """
    return store / PROPELLER_PREFIX_NAME


def _propeller_files(store: Path) -> tuple[Path, Path]:
    prefix = propeller_prefix(store)
    return (
        Path(f"{prefix}_cc_profile.txt"),
        Path(f"{prefix}_ld_profile.txt"),
    )


def fdo_kconfig(*, propeller: bool) -> dict[str, str]:
    """The kconfig entries that enable the kernel's Clang FDO machinery.

    Merged into the build's ``sysforge.config`` fragment for both ``record`` and
    ``use``. ``CONFIG_AUTOFDO_CLANG=y`` is the base; Propeller adds
    ``CONFIG_PROPELLER_CLANG=y`` on top.
    """
    cfg = {CONFIG_AUTOFDO: "y"}
    if propeller:
        cfg[CONFIG_PROPELLER] = "y"
    return cfg


@dataclass(frozen=True)
class RoundInfo:
    """``round.toml`` — what a record round profiled (3.3.0-B14)."""

    pkgver: str | None       # full pkgver-pkgrel of the profiled source
    build_dir: Path | None   # the record build tree capture searches for vmlinux
    afdo_sha256: str | None  # round 2: hash of the pinned AutoFDO profile


def _write_sidecar(store: Path, name: str, lines: list[str]) -> Path:
    """Write ``<store>/<name>`` atomically: a sibling ``<name>.tmp`` then
    ``os.replace``, so a reader never sees a half-written sidecar."""
    store.mkdir(parents=True, exist_ok=True)
    path = store / name
    tmp = store / f"{name}.tmp"
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)  # os.replace: atomic within one filesystem
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def write_round(store: Path, *, pkgver, build_dir, afdo_sha256=None) -> Path:
    lines = []
    if pkgver:
        lines.append(f'pkgver = "{toml_escape(pkgver)}"')
    if build_dir:
        lines.append(f'build_dir = "{toml_escape(str(build_dir))}"')
    if afdo_sha256:
        lines.append(f'afdo_sha256 = "{toml_escape(afdo_sha256)}"')
    return _write_sidecar(store, ROUND_SIDECAR, lines)


def read_round(store: Path) -> RoundInfo | None:
    """The store's round sidecar, or ``None`` when missing *or malformed* — a
    hand-edited file is treated as absent, never a traceback."""
    try:
        with (store / ROUND_SIDECAR).open("rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):  # ValueError covers TOMLDecodeError + UnicodeDecodeError
        return None
    vals = {k: data.get(k) for k in ("pkgver", "build_dir", "afdo_sha256")}
    if any(v is not None and not isinstance(v, str) for v in vals.values()):
        return None
    return RoundInfo(vals["pkgver"] or None,
                     Path(vals["build_dir"]) if vals["build_dir"] else None,
                     vals["afdo_sha256"] or None)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Applied-profile sidecar (R21): the fingerprint of the profile files the last
# successful `use` install of this store's role consumed. Kernel-only, so it
# lives here; ROUND_SIDECAR stays in makepkg_pgo because `state profiles` reads it.
APPLIED_SIDECAR = "applied.toml"


def _applied_inputs(store: Path, *, propeller: bool) -> list[Path]:
    files = [autofdo_profile_path(store)]
    if propeller:
        files.extend(_propeller_files(store))
    return files


def applied_fingerprint(store: Path, *, propeller: bool) -> str:
    """sha256 over the profile files a ``use`` build of ``store`` consumes.

    In a fixed order: the AutoFDO profile (for Propeller, the pinned copy in the
    Propeller store), then for Propeller the cc and ld profiles. Each file's
    content digest is combined with its name, so a change to any input (or a
    switch between AutoFDO and Propeller) changes the result. Raises
    ``OSError`` when an input is unreadable.
    """
    h = hashlib.sha256()
    for p in _applied_inputs(store, propeller=propeller):
        h.update(f"{p.name}\0{file_sha256(p)}\n".encode())
    return h.hexdigest()


def write_applied(store: Path, fingerprint: str) -> Path:
    """Record the fingerprint a successful ``use`` install applied (atomic)."""
    return _write_sidecar(store, APPLIED_SIDECAR,
                          [f'fingerprint = "{toml_escape(fingerprint)}"'])


def read_applied(store: Path) -> str | None:
    """The store's applied fingerprint, or ``None`` when missing *or malformed*."""
    try:
        with (store / APPLIED_SIDECAR).open("rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):  # ValueError covers TOMLDecodeError + UnicodeDecodeError
        return None
    fp = data.get("fingerprint")
    return fp if isinstance(fp, str) and fp else None


_MAJOR_MINOR = re.compile(r"^(\d+)\.(\d+)")


def kernel_major_minor(version: str | None) -> tuple[int, int] | None:
    """``(major, minor)`` from a kernel pkgver (``7.2.7.arch1-1`` -> ``(7, 2)``)."""
    m = _MAJOR_MINOR.match(version or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def pin_autofdo_profile(pkgname, *, tcfg=None, dry_run=False) -> tuple[Path, str]:
    """Copy round 1's AutoFDO profile into the Propeller store (round 2).

    Round 2's profiling kernel and its final build must apply the *same*
    AutoFDO profile or the Propeller basic-block IDs stop matching; the pinned
    copy is what both read, and its hash goes into ``round.toml``.
    """
    src = autofdo_profile_path(resolve_store(pkgname, propeller=False, tcfg=tcfg))
    if not src.is_file():
        raise KernelFdoError(
            f"--propeller is round 2 and needs round 1's AutoFDO profile at {src}. "
            "Complete round 1 first: `sysforge run kernel --autofdo=record`, reboot into "
            "the profiling kernel, run `--autofdo=capture` and the commands it prints "
            "(the last one writes kernel.afdo). Round 1's `--autofdo=use` is optional here."
        )
    pinned = autofdo_profile_path(resolve_store(pkgname, propeller=True, tcfg=tcfg))
    sha = file_sha256(src)
    if not dry_run:
        pinned.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, pinned)
    return pinned, sha


def require_profile(store: Path, *, propeller: bool) -> RoundInfo | None:
    """Raise :class:`KernelFdoError` when the ``use`` rebuild cannot proceed.

    AutoFDO: the profile must exist. Propeller (round 2, ``store`` is the
    Propeller store): ``round.toml`` must exist, the pinned AutoFDO copy must
    still hash to its ``afdo_sha256``, and the cc/ld pair must be present.
    Returns the store's round info (``None`` for a pre-rework AutoFDO store).
    """
    afdo = autofdo_profile_path(store)
    info = read_round(store)
    if not propeller:
        if not afdo.is_file():
            raise KernelFdoError(
                f"no AutoFDO profile at {afdo}. Build the profiling kernel with "
                "`sysforge run kernel --autofdo=record`, reboot into it, run "
                "`--autofdo=capture` and the commands it prints, then re-run "
                "`--autofdo=use`."
            )
        return info
    if info is None or not info.afdo_sha256:
        raise KernelFdoError(
            f"no round-2 record in {store / ROUND_SIDECAR} (a pre-rework single-round "
            "Propeller store?). Redo round 2: `--autofdo=record --propeller`, reboot, "
            "`--autofdo=capture --propeller`, then `--autofdo=use --propeller`."
        )
    if not afdo.is_file() or file_sha256(afdo) != info.afdo_sha256:
        raise KernelFdoError(
            f"the pinned AutoFDO profile {afdo} changed since round 2 profiled with it. "
            "Redo round 2 (`--autofdo=record --propeller`)."
        )
    cc, ld = _propeller_files(store)
    missing = [str(p) for p in (cc, ld) if not p.is_file()]
    if missing:
        raise KernelFdoError(
            f"Propeller profile incomplete: missing {', '.join(missing)}. Re-run "
            "`sysforge run kernel --autofdo=capture --propeller` and the commands it prints."
        )
    return info


def use_env(store: Path, *, propeller: bool, afdo: Path | None = None) -> dict[str, str]:
    """Make-variables pointing the build at its profile(s).

    Propeller reads the pinned AutoFDO copy in its own store, never the live
    AutoFDO store. ``afdo`` overrides the AutoFDO path (the round-2 *record*
    build applies the pinned copy without a Propeller pair yet).
    """
    env = {ENV_AUTOFDO: str(afdo or autofdo_profile_path(store))}
    if propeller:
        env[ENV_PROPELLER] = str(propeller_prefix(store))
    return env


# ---------------------------------------------------------------------------
# Capture preflight — branch-sampling capability + vmlinux + printed commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchSampling:
    """How (and whether) this host can branch-sample for AutoFDO via ``perf -b``."""

    vendor: str            # "intel" | "amd" | "unknown"
    supported: bool        # branch sampling usable for AutoFDO on this CPU
    perf_event_args: str   # the `perf record` event-selection fragment
    note: str              # operator-facing caveat (uarch specifics, verification)


def _read_cpuinfo() -> str:
    try:
        return Path("/proc/cpuinfo").read_text(encoding="utf-8")
    except OSError:
        return ""


def detect_branch_sampling(cpuinfo_text: str | None = None) -> BranchSampling:
    """Resolve the branch-sampling ``perf`` event for AutoFDO on this CPU.

    AutoFDO needs taken-branch samples with a branch stack (``perf record -b``).
    The mechanism is uarch-specific: Intel uses **LBR**; AMD uses **BRS** (some
    Zen 3 parts, cpuinfo flag ``brs``) or **LbrExtV2** (Zen 4+, flag
    ``amd_lbr_v2``). Family is not a proxy — client Zen 3 is family 0x19 yet
    lacks the BRS CPUID bit, so ``perf -b`` is refused (``3.3.0-B22``); the AMD
    verdict keys on the kernel-exported flags. ``cpuinfo_text`` is injectable
    for tests; production reads ``/proc/cpuinfo``.
    """
    text = cpuinfo_text if cpuinfo_text is not None else _read_cpuinfo()
    vendor_id = ""
    family = -1
    flags: set[str] | None = None
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "vendor_id" and not vendor_id:
            vendor_id = val
        elif key == "cpu family" and family < 0:
            try:
                family = int(val)
            except ValueError:
                family = -1
        elif key == "flags" and flags is None:
            flags = set(val.split())
        if vendor_id and family >= 0 and flags is not None:
            break
    flags = flags or set()

    if vendor_id == "GenuineIntel":
        return BranchSampling(
            vendor="intel",
            supported=True,
            perf_event_args="-e BR_INST_RETIRED.NEAR_TAKEN:k",
            note="Intel LBR branch sampling.",
        )
    if vendor_id == "AuthenticAMD":
        mech = ("LbrExtV2" if "amd_lbr_v2" in flags
                else "BRS" if "brs" in flags else "")
        if mech:
            return BranchSampling(
                vendor="amd",
                supported=True,
                perf_event_args="--pfm-events RETIRED_TAKEN_BRANCH_INSTRUCTIONS:k",
                note=(
                    f"AMD {mech} branch sampling (family "
                    f"0x{family:x}). EXPERIMENTAL for AutoFDO and less "
                    "battle-tested than Intel LBR; the event uses libpfm "
                    "(`--pfm-events`) — verify your perf build supports it "
                    "(`perf record --pfm-events ... ` / `perf list`) and "
                    "cross-check the kernel's Documentation/dev-tools/autofdo.rst."
                ),
            )
        return BranchSampling(
            vendor="amd",
            supported=False,
            perf_event_args="-e ex_ret_brn_tkn:k",
            note=(
                f"AMD family 0x{family:x} exposes neither Branch Sampling (BRS, "
                "cpuinfo flag `brs`) nor LbrExtV2 (`amd_lbr_v2`), so `perf -b` "
                "branch-stack collection for AutoFDO is not supported on this CPU."
            ),
        )
    return BranchSampling(
        vendor="unknown",
        supported=False,
        perf_event_args="-e branches:k",
        note=(
            "Could not identify the CPU vendor from /proc/cpuinfo — pick a "
            "taken-branch event that supports `-b` (branch stack) on your "
            "hardware and verify with `perf list`."
        ),
    )


# Converters per round, with an install hint for the tool preflight.
_TOOLS_ROUND1 = (
    ("perf", "install it with `pacman -S perf`"),
    ("llvm-profgen", "ships with the `llvm` package, version-matched to the clang "
                     "that consumes the profile"),
)
_TOOLS_ROUND2 = (
    ("perf", "install it with `pacman -S perf`"),
    ("generate_propeller_profiles",
     "build it from the AutoFDO tools (github.com/google/autofdo)"),
)


def missing_tools(*, propeller: bool, which=shutil.which) -> list[str]:
    """Hint lines for each tool this round's printed commands need but PATH lacks."""
    return [f"{t}: {hint}" for t, hint in (_TOOLS_ROUND2 if propeller else _TOOLS_ROUND1)
            if not which(t)]


def _read_proc_version() -> str:
    try:
        return Path("/proc/version").read_text(encoding="utf-8")
    except OSError:
        return ""


# Section name in the ELF section-header string table of a vmlinux that still
# carries DWARF. The converters need it; the -headers package's copy under
# /usr/lib/modules/<release>/build is stripped of it.
_DEBUG_INFO = b".debug_info"


def _scan_vmlinux(path: Path, banner: bytes) -> tuple[bool, bool]:
    """``(banner matches, has debug info)`` for one candidate, in one mmap."""
    try:
        with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            if m.find(banner) == -1:
                return False, False
            return True, m.find(_DEBUG_INFO) != -1
    except (OSError, ValueError):  # ValueError: mmap of an empty file
        return False, False


def resolve_vmlinux(pkgname: str, *, recorded_build_dir: Path | None = None,
                    builddir: Path | None = None, proc_version: str | None = None,
                    running_release: str | None = None) -> Path:
    """The ``vmlinux`` of the kernel that is running *now*, for the converter.

    Candidates: the record tree recorded in ``round.toml`` (the profile's
    ``BUILDDIR``, post-rename pkgbase), the system ``BUILDDIR`` under the
    profiling name, then ``/usr/lib/modules/<running>/build/vmlinux``. The one
    whose ``Linux version ...`` banner equals ``/proc/version`` byte-for-byte wins:
    the banner carries host, compiler and timestamp, so it separates two builds
    of one release. A match must also carry debug info (a ``.debug_info``
    section name): the converters cannot use a stripped image, such as the
    -headers package's copy. Raises :class:`KernelFdoError` rather than guessing.
    """
    from sysforge.primitives.pacman import get_builddir

    running = running_release if running_release is not None else os.uname().release
    banner = (proc_version if proc_version is not None else _read_proc_version()).strip()
    roots: list[Path] = []
    if recorded_build_dir is not None:
        roots.append(Path(recorded_build_dir))      # already $BUILDDIR/<pkgbase>
    sys_bd = builddir if builddir is not None else get_builddir()
    if sys_bd:
        roots.append(Path(sys_bd) / pkgname)
    candidates: list[Path] = []
    for root in roots:
        src = root / "src"
        if src.is_dir():
            candidates.extend(p for p in src.glob("**/vmlinux") if p.is_file())
    mod = Path("/usr/lib/modules") / running / "build" / "vmlinux"
    if mod.is_file():
        candidates.append(mod)
    if not candidates:
        raise KernelFdoError(
            f"the profiling kernel's build tree is gone (was ~/builds wiped?), so "
            f"there is no vmlinux for {pkgname}. Re-run `sysforge run kernel "
            "--autofdo=record`."
        )
    if not banner:
        raise KernelFdoError(
            "cannot read /proc/version to identify the running kernel, so the "
            "matching vmlinux cannot be chosen."
        )
    stripped = None
    for p in candidates:
        matches, has_debug = _scan_vmlinux(p, banner.encode())
        if matches and has_debug:
            return p
        if matches and stripped is None:
            stripped = p
    if stripped is not None:
        # Every kernel's -headers copy matches its own banner, so this is also
        # what a run on the wrong kernel sees: name both readings.
        raise KernelFdoError(
            f"the running kernel ({running}) matches only {stripped}, which has no "
            "debug info (the -headers copy is stripped), so the converter cannot use "
            f"it. If this is {pkgname}, its build tree is gone (was ~/builds wiped?): "
            "re-run `sysforge run kernel --autofdo=record`. Otherwise reboot into "
            f"{pkgname} and re-run capture.")
    raise KernelFdoError(
        f"not booted into {pkgname} (running: {running}). Reboot into the "
        "profiling kernel, then re-run capture."
    )


def capture_commands(store: Path, *, sampling: BranchSampling, vmlinux: Path,
                     propeller: bool, seconds: int = _CAPTURE_SECONDS) -> list[str]:
    """The printed capture block. Pure string assembly; nothing runs here."""
    perf_data = perf_data_path(store)
    q = shlex.quote
    minutes = seconds // 60
    lines = [
        "# 1. Start your normal workload(s) now.",
        f"# 2. While they run, sample the whole system for {minutes} minutes "
        "(Ctrl-C ends early; data is kept):",
        f"sudo perf record {sampling.perf_event_args} -a -N -b -c {_PERF_PERIOD} "
        f"-o {q(str(perf_data))} -- sleep {seconds}",
        # perf.data is created 0600 by the recording user (root under sudo), so
        # the unprivileged converter could not read it.
        f'sudo chown "$(id -un)": {q(str(perf_data))}',
        "# 3. Convert (any time later, on any kernel, while the build tree still exists):",
    ]
    if propeller:
        cc, ld = _propeller_files(store)
        lines.append(
            f"generate_propeller_profiles {q(f'--binary={vmlinux}')} "
            f"{q(f'--profile={perf_data}')} --format=propeller "
            f"--propeller_output_module_name {q(f'--out={cc}')} "
            f"{q(f'--propeller_symorder={ld}')}")
    else:
        lines.append(
            f"llvm-profgen --kernel {q(f'--binary={vmlinux}')} "
            f"{q(f'--perfdata={perf_data}')} --format=extbinary "
            f"{q('--output=' + str(autofdo_profile_path(store)))}")
    return lines
