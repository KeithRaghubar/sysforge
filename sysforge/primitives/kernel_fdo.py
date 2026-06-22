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
    convert the samples to a profile with ``create_llvm_prof``. The optimized
    rebuild consumes that profile via the kernel build's ``CLANG_AUTOFDO_PROFILE``
    make-variable (``-fprofile-sample-use`` under the hood).

Because the sample-collection step runs on the *booted* profiling kernel, it
necessarily spans a reboot — sysforge cannot capture in the same invocation that
built the profiling kernel. The flow is therefore three steps:

  1. ``--autofdo=record`` — build+install a profiling kernel (stock name). The
     kconfig fragment gains :data:`CONFIG_AUTOFDO` (+ :data:`CONFIG_PROPELLER`).
  2. ``--autofdo=capture`` — a read-only step: resolve this host's branch-sampling
     ``perf`` event, the matching ``vmlinux``, and the store paths, then print the
     exact ``perf record`` + ``create_llvm_prof`` commands for the operator to run
     while exercising the machine. sysforge does not run ``perf`` itself.
  3. ``--autofdo=use`` — rebuild with the collected profile injected through the
     kernel build's make-variables (the existing ``extra_env`` seam — ``make``
     imports env vars as make-variables, exactly as the kernel build already
     injects ``LLVM=1``). The optimized kernel earns the ``-sysforge``
     **coexist** rename (``build_mode = autofdo_kernel`` / ``propeller_kernel``)
     so it installs alongside the stock kernel for bootloader fallback.

Store resolution defers to :func:`makepkg_pgo.resolve_method_store` (methods
``"autofdo"`` / ``"propeller"``, namespaced per kernel pkgname). LLVM-only — the
whole feature is gated on the Clang toolchain by the kernel stage; Propeller and
the kernel's Clang AutoFDO have no GCC equivalent. Pure except for the
filesystem existence checks in :func:`require_profile` / :func:`resolve_vmlinux`
and the ``/proc/cpuinfo`` read in :func:`detect_branch_sampling` (injectable for
tests).
"""
import tomllib
from dataclasses import dataclass
from pathlib import Path

from sysforge.primitives.makepkg_pgo import resolve_method_store
from sysforge.primitives.paths import TOOLCHAIN_PATH

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
AUTOFDO_PROFILE_NAME = "kernel.afdo"   # create_llvm_prof --format=extbinary output
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
# Default capture window for the printed `perf record -- sleep N` template.
_CAPTURE_SECONDS = 120


class KernelFdoError(Exception):
    """A kernel-FDO step cannot proceed (no collected profile for ``use``, an
    invalid mode). Raised so the kernel stage aborts cleanly *before* a multi-hour
    build, with an actionable hint, rather than silently building an unprofiled
    kernel."""


def build_mode(*, propeller: bool) -> str:
    """The optimization build_mode this run records (drives the -sysforge rename)."""
    return BUILD_MODE_PROPELLER if propeller else BUILD_MODE_AUTOFDO


def _load_tcfg() -> dict | None:
    """Best-effort load of toolchain.toml for store-path overrides (pure)."""
    if not TOOLCHAIN_PATH.exists():
        return None
    try:
        with open(TOOLCHAIN_PATH, "rb") as f:
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
    """The raw ``perf.data`` the capture step writes and create_llvm_prof reads."""
    return store / PERF_DATA_NAME


def propeller_prefix(store: Path) -> Path:
    """The Propeller profile prefix (``<store>/propeller``).

    ``create_llvm_prof --format=propeller`` writes ``<prefix>_cc_profile.txt`` and
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


def require_profile(store: Path, *, propeller: bool) -> None:
    """Raise :class:`KernelFdoError` when the ``use`` rebuild has no profile to
    consume — the operator skipped (or never finished) the record→capture steps.

    A clean pre-build abort with the recovery path, never a silent unprofiled
    rebuild. In Propeller mode both the AutoFDO profile *and* the Propeller
    ``cc``/``ld`` profile pair must be present.
    """
    afdo = autofdo_profile_path(store)
    if not afdo.is_file():
        raise KernelFdoError(
            f"no AutoFDO profile at {afdo} — build the profiling kernel with "
            "`sysforge run kernel --autofdo=record`, reboot into it, collect a "
            "profile with `sysforge run kernel --autofdo=capture` (it prints the "
            "perf + create_llvm_prof commands), then re-run `--autofdo=use`."
        )
    if propeller:
        cc, ld = _propeller_files(store)
        missing = [str(p) for p in (cc, ld) if not p.is_file()]
        if missing:
            raise KernelFdoError(
                f"Propeller profile incomplete — missing {', '.join(missing)}. "
                "Re-run `sysforge run kernel --autofdo=capture --propeller` and "
                "the printed `create_llvm_prof --format=propeller` command."
            )


def use_env(store: Path, *, propeller: bool) -> dict[str, str]:
    """The make-variables (as env) that point the ``use`` rebuild at the profile.

    Injected through the kernel build's ``extra_env`` seam; ``make`` imports them
    as make-variables, and the kernel Makefile turns them into
    ``-fprofile-sample-use`` (+ Propeller basic-block-sections flags). Call
    :func:`require_profile` first so a missing profile aborts before the build.
    """
    env = {ENV_AUTOFDO: str(autofdo_profile_path(store))}
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
    The mechanism is uarch-specific: Intel uses **LBR**; AMD uses **BRS**, which
    exists only on **Zen 3 and later** (family ≥ 0x19). Pre-Zen3 AMD has no
    usable branch-sampling path for AutoFDO. ``cpuinfo_text`` is injectable for
    tests; production reads ``/proc/cpuinfo``.
    """
    text = cpuinfo_text if cpuinfo_text is not None else _read_cpuinfo()
    vendor_id = ""
    family = -1
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
        if vendor_id and family >= 0:
            break

    if vendor_id == "GenuineIntel":
        return BranchSampling(
            vendor="intel",
            supported=True,
            perf_event_args="-e BR_INST_RETIRED.NEAR_TAKEN:k",
            note="Intel LBR branch sampling.",
        )
    if vendor_id == "AuthenticAMD":
        if family >= 0x19:  # Zen 3+ — BRS available
            return BranchSampling(
                vendor="amd",
                supported=True,
                perf_event_args="--pfm-events RETIRED_TAKEN_BRANCH_INSTRUCTIONS:k",
                note=(
                    "AMD BRS branch sampling (Zen 3+, family "
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
                f"AMD family 0x{family:x} predates Zen 3 — Branch Sampling (BRS) "
                "is unavailable, so AutoFDO branch-stack collection is not "
                "supported on this CPU."
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


def resolve_vmlinux(pkgname: str, *, builddir: Path | None = None) -> Path | None:
    """Locate an uncompressed ``vmlinux`` (with symbols) for create_llvm_prof.

    Arch kernel *packages* strip ``vmlinux``, so the usable copy is the one left
    in the build tree by the ``record`` build (``<builddir>/<pkgname>/src/**/
    vmlinux``) — it also exactly matches the kernel you booted to profile. Falls
    back to the running kernel's kbuild tree. Returns the newest match, or
    ``None`` (the capture printout then shows a ``<path-to-vmlinux>`` placeholder).
    """
    from sysforge.primitives.pacman import get_builddir

    roots: list[Path] = []
    bd = builddir if builddir is not None else get_builddir()
    if bd:
        roots.append(Path(bd) / pkgname)

    candidates: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if str(root) in seen:
            continue
        seen.add(str(root))
        src = root / "src"
        if src.is_dir():
            candidates.extend(src.glob("**/vmlinux"))

    # Running kernel's build tree (DKMS symlink target) as a last resort.
    try:
        import os

        running = Path("/usr/lib/modules") / os.uname().release / "build" / "vmlinux"
        if running.is_file():
            candidates.append(running)
    except OSError:
        pass

    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def capture_commands(
    store: Path,
    *,
    sampling: BranchSampling,
    vmlinux: Path | None,
    propeller: bool,
    seconds: int = _CAPTURE_SECONDS,
) -> list[str]:
    """Assemble the exact ``perf record`` + ``create_llvm_prof`` command lines.

    Returned as a list of display lines (commands + inline guidance) for the
    ``--autofdo=capture`` step to print. The operator runs them while exercising
    the machine; ``--autofdo=use`` then consumes whatever profile landed in
    ``store``. Pure string assembly — nothing is executed here.
    """
    perf_data = perf_data_path(store)
    afdo = autofdo_profile_path(store)
    vmlinux_disp = str(vmlinux) if vmlinux else "<path-to-vmlinux>"

    lines = [
        f"# 1. Sample branches while running a representative workload (~{seconds}s):",
        (
            f"sudo perf record {sampling.perf_event_args} -a -N -b "
            f"-c {_PERF_PERIOD} -o {perf_data} -- sleep {seconds}"
        ),
        "#    (replace `sleep N` with a real workload, or Ctrl-C when done)",
        "",
        "# 2. Convert the samples into an AutoFDO profile:",
        (
            f"create_llvm_prof --binary={vmlinux_disp} --profile={perf_data} "
            f"--format=extbinary --out={afdo}"
        ),
    ]
    if propeller:
        cc, ld = _propeller_files(store)
        lines += [
            "",
            "# 3. Also derive the Propeller cluster/order profile from the same samples:",
            (
                f"create_llvm_prof --binary={vmlinux_disp} --profile={perf_data} "
                f"--format=propeller --propeller_output_module_name "
                f"--out={cc} --propeller_symorder={ld}"
            ),
            "#    (for best results, profile the AutoFDO-optimized kernel in a",
            "#     second round before generating the Propeller profile)",
        ]
    return lines
