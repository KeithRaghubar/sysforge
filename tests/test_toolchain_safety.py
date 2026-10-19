"""
test_toolchain_safety.py — unit tests for the pure toolchain-safety facts.

Mirrors test_kernel_safety.py: every check is exercised against a fixture tree
or monkeypatched subprocess/path constants, so nothing real is inspected. The
toolchain stage owns the abort/warn policy; this module owns the facts, and
these tests pin the fact shape (is_brick, check_id, message content).
"""
from pathlib import Path
from unittest.mock import MagicMock


from sysforge.primitives import toolchain_safety as ts


# ---------------------------------------------------------------------------
# detect_suite_skew
# ---------------------------------------------------------------------------

def test_detect_suite_skew_agreeing_versions_no_finding():
    versions = {"llvm": "22.0.0-1", "llvm-libs": "22.0.0-1", "clang": "22.0.0-1"}
    assert ts.detect_suite_skew(versions) is None


def test_detect_suite_skew_single_member_no_finding():
    assert ts.detect_suite_skew({"llvm": "22.0.0-1", "clang": None}) is None


def test_detect_suite_skew_disagreement_is_brick():
    versions = {
        "llvm": "22.0.0-1", "llvm-libs": "22.0.1-1", "clang": "22.0.0-1",
    }
    finding = ts.detect_suite_skew(versions)
    assert finding is not None
    assert finding.is_brick
    assert finding.check_id == "suite_skew"
    assert "llvm-libs=22.0.1-1" in finding.message
    assert "versions disagree" in finding.message


def test_detect_suite_skew_pkgrel_split_within_llvm_pair_is_brick():
    """llvm/llvm-libs share a pkgbase — a pkgrel split between them is a real
    interrupted install of the split package (libLLVM.so halves disagree)."""
    versions = {"llvm": "22.0.0-1", "llvm-libs": "22.0.0-2"}
    finding = ts.detect_suite_skew(versions)
    assert finding is not None
    assert finding.is_brick


def test_detect_suite_skew_pkgrel_bump_across_pkgbases_no_finding():
    """Independent pkgbases carry independent pkgrel counters — an llvm -2
    rebuild alongside clang/lld -1 is a legitimate state, not a brick.

    Regression: this pkgrel-only cross-pkgbase divergence used to false-trip
    Gate-3 and force an unnecessary rollback to the prior toolchain.
    """
    versions = {
        "llvm": "22.1.8-2", "llvm-libs": "22.1.8-2", "clang": "22.1.8-1",
        "lld": "22.1.8-1", "compiler-rt": "22.1.8-1", "polly": "22.1.8-1",
        "openmp": "22.1.8-1",
    }
    assert ts.detect_suite_skew(versions) is None


def test_detect_suite_skew_pkgver_split_across_pkgbases_is_brick():
    """A genuine interrupted install shows as a pkgver split — still a brick."""
    versions = {"llvm": "22.1.8-1", "clang": "22.1.7-1"}
    finding = ts.detect_suite_skew(versions)
    assert finding is not None
    assert finding.is_brick
    assert finding.check_id == "suite_skew"


# ---------------------------------------------------------------------------
# smoke_test_compilers
# ---------------------------------------------------------------------------

def test_smoke_test_clang_missing_is_brick(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_USR_BIN_CLANG", tmp_path / "absent")
    findings = ts.smoke_test_compilers()
    assert findings and findings[0].is_brick
    assert findings[0].check_id == "smoke:clang_missing"


def test_smoke_test_clang_broken_is_brick(tmp_path, monkeypatch):
    clang = tmp_path / "clang"
    clang.touch()
    monkeypatch.setattr(ts, "_USR_BIN_CLANG", clang)

    def fake_run(cmd, **kwargs):
        if "/dev/null" in cmd:
            return MagicMock(
                returncode=127, stdout="",
                stderr="symbol lookup error: libclang-cpp.so: undefined symbol",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ts, "_run", fake_run)
    monkeypatch.setattr(ts.shutil, "which", lambda b: "/usr/bin/lld")
    findings = ts.smoke_test_compilers()
    assert any(f.check_id == "smoke:clang_broken" and f.is_brick for f in findings)


def test_smoke_test_lld_missing_is_brick(tmp_path, monkeypatch):
    clang = tmp_path / "clang"
    clang.touch()
    monkeypatch.setattr(ts, "_USR_BIN_CLANG", clang)
    monkeypatch.setattr(ts, "_run",
                        lambda cmd, **k: MagicMock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(ts.shutil, "which", lambda b: None)  # lld absent
    findings = ts.smoke_test_compilers()
    assert any(f.check_id == "smoke:lld_missing" and f.is_brick for f in findings)


def test_smoke_test_all_healthy_no_findings(tmp_path, monkeypatch):
    clang = tmp_path / "clang"
    clang.touch()
    monkeypatch.setattr(ts, "_USR_BIN_CLANG", clang)
    monkeypatch.setattr(ts, "_run",
                        lambda cmd, **k: MagicMock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(ts.shutil, "which", lambda b: "/usr/bin/lld")
    assert ts.smoke_test_compilers() == []


# ---------------------------------------------------------------------------
# scan_abi_hazards
# ---------------------------------------------------------------------------

def test_scan_abi_hazards_flags_std_at_llvm(tmp_path, monkeypatch):
    pkg = tmp_path / "clang-22.1.5-1-x86_64.pkg.tar.zst"
    pkg.touch()
    fake_so = tmp_path / "extracted" / "usr/lib/libclang-cpp.so.22.1"
    fake_so.parent.mkdir(parents=True)
    fake_so.touch()
    hazard_sym = "_ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE9_M_assignERKS4_"

    import sysforge.primitives.abi_check as abi
    monkeypatch.setattr(abi, "_list_sos_in_pkg", lambda p: ["usr/lib/libclang-cpp.so.22.1"])
    monkeypatch.setattr(abi, "_extract_sos", lambda p, m, d: [fake_so])
    monkeypatch.setattr(abi, "_undefined_versioned", lambda so: {(hazard_sym, "LLVM_22.1")})

    findings = ts.scan_abi_hazards([pkg])
    assert len(findings) == 1
    assert findings[0].is_brick
    assert findings[0].check_id == "abi_hazard"
    assert hazard_sym in findings[0].message


def test_scan_abi_hazards_clean_glibcxx_no_finding(tmp_path, monkeypatch):
    pkg = tmp_path / "clang-22.1.5-1-x86_64.pkg.tar.zst"
    pkg.touch()
    fake_so = tmp_path / "extracted" / "usr/lib/libclang-cpp.so.22.1"
    fake_so.parent.mkdir(parents=True)
    fake_so.touch()
    sym = "_ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE9_M_assignERKS4_"

    import sysforge.primitives.abi_check as abi
    monkeypatch.setattr(abi, "_list_sos_in_pkg", lambda p: ["usr/lib/libclang-cpp.so.22.1"])
    monkeypatch.setattr(abi, "_extract_sos", lambda p, m, d: [fake_so])
    monkeypatch.setattr(abi, "_undefined_versioned", lambda so: {(sym, "GLIBCXX_3.4")})

    assert ts.scan_abi_hazards([pkg]) == []


# ---------------------------------------------------------------------------
# check_pkgver_lockstep
# ---------------------------------------------------------------------------

def test_check_pkgver_lockstep_agreeing_no_finding():
    pkgvers = {"llvm": "22.1.5", "clang": "22.1.5", "lld": "22.1.5"}
    assert ts.check_pkgver_lockstep(pkgvers) is None


def test_check_pkgver_lockstep_skew_is_brick():
    pkgvers = {"llvm": "22.1.5", "clang": "22.1.6"}
    finding = ts.check_pkgver_lockstep(pkgvers)
    assert finding is not None and finding.is_brick
    assert finding.check_id == "pkgver_lockstep"


def test_check_pkgver_lockstep_excludes_spirv():
    """spirv-llvm-translator's own version scheme must not raise a false skew."""
    pkgvers = {
        "llvm": "22.1.5", "clang": "22.1.5",
        "spirv-llvm-translator": "19.1.5",  # legitimately different lineage
    }
    assert ts.check_pkgver_lockstep(pkgvers) is None


def test_check_pkgver_lockstep_excludes_lib32():
    """lib32-* members aren't pkgver-locked to the suite (separate lineage/epoch)."""
    pkgvers = {
        "llvm": "22.1.5", "clang": "22.1.5",
        "lib32-llvm": "1:22.1.0",  # excluded — not a lockstep member
    }
    assert ts.check_pkgver_lockstep(pkgvers) is None


def test_check_pkgver_lockstep_fewer_than_two_no_finding():
    assert ts.check_pkgver_lockstep({"llvm": "22.1.5"}) is None


# ---------------------------------------------------------------------------
# check_build_space
# ---------------------------------------------------------------------------

def test_check_build_space_enough_no_finding(tmp_path, monkeypatch):
    usage = MagicMock(free=100 * 1024 ** 3)  # 100 GiB
    monkeypatch.setattr(ts.shutil, "disk_usage", lambda p: usage)
    assert ts.check_build_space([tmp_path], min_free_gb=40) is None


def test_check_build_space_shortfall_is_brick(tmp_path, monkeypatch):
    usage = MagicMock(free=5 * 1024 ** 3)  # 5 GiB
    monkeypatch.setattr(ts.shutil, "disk_usage", lambda p: usage)
    finding = ts.check_build_space([tmp_path], min_free_gb=40)
    assert finding is not None and finding.is_brick
    assert finding.check_id == "build_space"


def test_check_build_space_dedupes_by_device(tmp_path, monkeypatch):
    """Two paths on the same filesystem are checked once, not summed."""
    calls = []
    usage = MagicMock(free=50 * 1024 ** 3)

    def fake_usage(p):
        calls.append(p)
        return usage

    monkeypatch.setattr(ts.shutil, "disk_usage", fake_usage)
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    ts.check_build_space([a, b], min_free_gb=40)
    # a and b share tmp_path's st_dev → one disk_usage call.
    assert len(calls) == 1


def test_check_build_space_zero_min_skips(tmp_path):
    assert ts.check_build_space([tmp_path], min_free_gb=0) is None


# ---------------------------------------------------------------------------
# check_multilib_enabled
# ---------------------------------------------------------------------------

def test_check_multilib_not_in_scope_no_finding():
    assert ts.check_multilib_enabled(lib32_in_scope=False) is None


def test_check_multilib_enabled_present_no_finding(tmp_path, monkeypatch):
    conf = tmp_path / "pacman.conf"
    conf.write_text("[options]\n[core]\n[extra]\n[multilib]\nInclude = /etc/pacman.d/mirrorlist\n")
    monkeypatch.setattr(ts, "_PACMAN_CONF", conf)
    assert ts.check_multilib_enabled(lib32_in_scope=True) is None


def test_check_multilib_disabled_is_brick(tmp_path, monkeypatch):
    conf = tmp_path / "pacman.conf"
    conf.write_text(
        "[options]\n[core]\n[extra]\n#[multilib]\n#Include = /etc/pacman.d/mirrorlist\n"
    )
    monkeypatch.setattr(ts, "_PACMAN_CONF", conf)
    finding = ts.check_multilib_enabled(lib32_in_scope=True)
    assert finding is not None and finding.is_brick
    assert finding.check_id == "multilib_disabled"


# ---------------------------------------------------------------------------
# detect_residual_instrumentation
# ---------------------------------------------------------------------------

def test_detect_residual_instrumentation_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_USR_LIB", tmp_path)  # no libLLVM-*.so present
    monkeypatch.setattr(ts, "_system_llvm_static_is_instrumented", lambda: False)
    assert ts.detect_residual_instrumentation() == []


def test_detect_residual_instrumentation_instrumented_shared_lib_warns(tmp_path, monkeypatch):
    so = tmp_path / "libLLVM-22.so"
    so.touch()
    monkeypatch.setattr(ts, "_USR_LIB", tmp_path)
    monkeypatch.setattr(ts, "_system_llvm_static_is_instrumented", lambda: False)
    monkeypatch.setattr(
        ts, "_run",
        lambda cmd, **k: MagicMock(returncode=0, stdout="  [11] __llvm_prf_names\n", stderr=""),
    )
    findings = ts.detect_residual_instrumentation()
    assert len(findings) == 1
    assert not findings[0].is_brick  # advisory
    assert "instrumented" in findings[0].message


# ---------------------------------------------------------------------------
# assess_libllvm_soname_impact — pre-build reverse-dependency assessment
# ---------------------------------------------------------------------------

def _patch_pacman(monkeypatch, *, llvm_libs_files, installed, depends, pkgbase=None):
    """Wire the pacman DB readers assess_libllvm_soname_impact draws from."""
    from sysforge.primitives import pacman
    monkeypatch.setattr(
        pacman, "get_package_files",
        lambda name: llvm_libs_files if name == "llvm-libs" else [],
    )
    monkeypatch.setattr(pacman, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman, "get_package_depends", lambda name: depends.get(name, []))
    # The whole-system reverse-dep walk reads the DB in one pass (2.6.1-B22);
    # stub it too or these tests fall through to the real host pacman DB.
    monkeypatch.setattr(
        pacman, "get_all_package_depends",
        lambda root=None: {name: depends.get(name, []) for name in installed},
    )
    monkeypatch.setattr(pacman, "get_pkgbase", lambda name: (pkgbase or {}).get(name))


def test_soname_impact_same_version_no_impact(monkeypatch):
    # Installed libLLVM.so.22.1; target pkgver 22.1.5 → same major.minor.
    _patch_pacman(
        monkeypatch,
        llvm_libs_files=["usr/lib/libLLVM.so", "usr/lib/libLLVM.so.22.1"],
        installed={"mesa": "1"},
        depends={"mesa": ["libLLVM.so=22.1-64"]},
    )
    assert ts.assess_libllvm_soname_impact("22.1.5", exclude=set()) is None


def test_soname_impact_llvm_libs_absent_no_impact(monkeypatch):
    # No installed llvm-libs → nothing on the system to break.
    _patch_pacman(
        monkeypatch,
        llvm_libs_files=[],
        installed={"mesa": "1"},
        depends={"mesa": ["libLLVM.so=22.1-64"]},
    )
    assert ts.assess_libllvm_soname_impact("23.0.0", exclude=set()) is None


def test_soname_impact_unparseable_pkgver_no_impact(monkeypatch):
    _patch_pacman(
        monkeypatch,
        llvm_libs_files=["usr/lib/libLLVM.so.22.1"],
        installed={"mesa": "1"},
        depends={"mesa": ["libLLVM.so=22.1-64"]},
    )
    assert ts.assess_libllvm_soname_impact("git", exclude=set()) is None


def test_soname_impact_version_bump_lists_consumers(monkeypatch):
    # 22.1 → 23.0 soname bump. mesa links old soname; clang is a suite member
    # (excluded); libfoo links a *different* old soname and must not match.
    _patch_pacman(
        monkeypatch,
        llvm_libs_files=["usr/lib/libLLVM.so.22.1"],
        installed={"mesa": "1", "clang": "1", "llvm-libs": "1", "libfoo": "1"},
        depends={
            "mesa": ["libLLVM.so=22.1-64", "libdrm.so=2-64"],
            "clang": ["libLLVM.so=22.1-64"],
            "llvm-libs": [],
            "libfoo": ["libLLVM.so=21.1-64"],  # older soname — not the one bumping
        },
    )
    exclude = set(ts.LLVM_LOCKSTEP_SUITE)
    impact = ts.assess_libllvm_soname_impact("23.0.1", exclude=exclude)
    assert impact is not None
    assert impact.old_soname == "libLLVM.so.22.1"
    assert impact.new_soname == "libLLVM.so.23.0"
    assert impact.consumers == ["mesa"]  # clang excluded, libfoo wrong soname


def test_soname_impact_excludes_in_scope_build_names(monkeypatch):
    # A consumer that is itself in the build scope this run must be dropped.
    _patch_pacman(
        monkeypatch,
        llvm_libs_files=["usr/lib/libLLVM.so.22.1"],
        installed={"mesa": "1", "spirv-llvm-translator": "1"},
        depends={
            "mesa": ["libLLVM.so=22.1-64"],
            "spirv-llvm-translator": ["libLLVM.so=22.1-64"],
        },
    )
    exclude = set(ts.LLVM_LOCKSTEP_SUITE) | {"spirv-llvm-translator"}
    impact = ts.assess_libllvm_soname_impact("23.0.0", exclude=exclude)
    assert impact is not None
    assert impact.consumers == ["mesa"]


def test_soname_impact_collapses_split_subpkg_to_pkgbase(monkeypatch):
    # An installed split subpackage links the old soname; it collapses to its
    # pkgbase so the rebuild targets the base, deduped against a sibling.
    _patch_pacman(
        monkeypatch,
        llvm_libs_files=["usr/lib/libLLVM.so.22.1"],
        installed={"vulkan-radeon": "1", "libva-mesa-driver": "1"},
        depends={
            "vulkan-radeon": ["libLLVM.so=22.1-64"],
            "libva-mesa-driver": ["libLLVM.so=22.1-64"],
        },
        pkgbase={"vulkan-radeon": "mesa", "libva-mesa-driver": "mesa"},
    )
    impact = ts.assess_libllvm_soname_impact("23.0.0", exclude=set(ts.LLVM_LOCKSTEP_SUITE))
    assert impact is not None
    assert impact.consumers == ["mesa"]  # both subpkgs → one pkgbase


def test_soname_impact_no_consumers_after_exclude_no_impact(monkeypatch):
    # Soname bumps but the only linker is a suite member → no impact.
    _patch_pacman(
        monkeypatch,
        llvm_libs_files=["usr/lib/libLLVM.so.22.1"],
        installed={"clang": "1"},
        depends={"clang": ["libLLVM.so=22.1-64"]},
    )
    impact = ts.assess_libllvm_soname_impact("23.0.0", exclude=set(ts.LLVM_LOCKSTEP_SUITE))
    assert impact is None


def test_libllvm_abi_consumers_current_soname_not_gated_on_bump(monkeypatch):
    # Same-soname case: no version change, but libllvm_abi_consumers still walks
    # the reverse deps of the *installed* soname (the std:: re-export drift path).
    _patch_pacman(
        monkeypatch,
        llvm_libs_files=["usr/lib/libLLVM.so.22.1"],
        installed={"mesa": "1", "clang": "1"},
        depends={
            "mesa": ["libLLVM.so=22.1-64"],
            "clang": ["libLLVM.so=22.1-64"],
        },
    )
    out = ts.libllvm_abi_consumers(exclude=set(ts.LLVM_LOCKSTEP_SUITE))
    assert out == ["mesa"]  # clang excluded as a suite member


def test_libllvm_abi_consumers_llvm_libs_absent_empty(monkeypatch):
    _patch_pacman(
        monkeypatch, llvm_libs_files=[], installed={"mesa": "1"},
        depends={"mesa": ["libLLVM.so=22.1-64"]},
    )
    assert ts.libllvm_abi_consumers(exclude=set()) == []


def test_libllvm_soname_consumers_shared_walk(monkeypatch):
    # The factored helper both paths share: collapse to pkgbase, minus exclude.
    _patch_pacman(
        monkeypatch,
        llvm_libs_files=["usr/lib/libLLVM.so.22.1"],
        installed={"vulkan-radeon": "1", "clang": "1"},
        depends={
            "vulkan-radeon": ["libLLVM.so=22.1-64"],
            "clang": ["libLLVM.so=22.1-64"],
        },
        pkgbase={"vulkan-radeon": "mesa"},
    )
    out = ts.libllvm_soname_consumers("22.1", exclude=set(ts.LLVM_LOCKSTEP_SUITE))
    assert out == ["mesa"]


# ---------------------------------------------------------------------------
# System graphics-consumer symbol sufficiency
# (_llvm_init_target / _diff_consumers_against_libllvm /
#  check_system_consumer_symbols / check_installed_consumer_symbols)
#
# The abi_check seams (_exported_versioned/_undefined_versioned/needed_sonames/
# _list_sos_in_pkg/_extract_sos) are imported lazily inside the functions, so
# monkeypatching them on the abi_check module (string-target form) takes effect
# at call time. _USR_LIB is repointed at a fixture tree.
# ---------------------------------------------------------------------------

_AC = "sysforge.primitives.abi_check."


def test_llvm_init_target_extracts_backend():
    assert ts._llvm_init_target("LLVMInitializeAMDGPUTargetInfo") == "AMDGPU"
    assert ts._llvm_init_target("LLVMInitializeAMDGPUTargetMCA") == "AMDGPU"
    assert ts._llvm_init_target("LLVMInitializeX86Target") == "X86"
    assert ts._llvm_init_target("LLVMInitializeAArch64AsmPrinter") == "AArch64"
    assert ts._llvm_init_target("malloc") is None


def _mk(usr_lib: Path, name: str) -> Path:
    usr_lib.mkdir(parents=True, exist_ok=True)
    p = usr_lib / name
    p.write_bytes(b"\x7fELF")  # content irrelevant — the symbol seams are faked
    return p


def test_diff_consumers_missing_amdgpu_is_brick(tmp_path, monkeypatch):
    usr_lib = tmp_path / "usr/lib"
    _mk(usr_lib, "libgallium-26.1.2-arch1.1.so")
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(_AC + "needed_sonames", lambda p: ["libLLVM.so.22.1"])
    monkeypatch.setattr(_AC + "_undefined_versioned", lambda p: {
        ("LLVMInitializeAMDGPUTarget", "LLVM_22.1"),
        ("LLVMInitializeAMDGPUTargetInfo", "LLVM_22.1"),
        ("malloc", "GLIBC_2.2.5"),   # non-LLVM undef must be ignored
    })
    monkeypatch.setattr(_AC + "_exported_versioned",
                        lambda path, cache: {("LLVMInitializeX86Target", "LLVM_22.1")})
    findings = ts._diff_consumers_against_libllvm(
        Path("/staging/usr/lib/libLLVM.so.22.1"), source_label="installed",
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.is_brick
    assert not f.healable  # a dropped target backend is unhealable — hard block
    assert f.check_id == "libllvm_consumer_symbols"
    assert "AMDGPU" in f.message
    assert "libgallium-26.1.2-arch1.1.so" in f.message
    assert "malloc" not in f.message  # libc undef not attributed to libLLVM


def test_diff_consumers_stddrift_is_healable(tmp_path, monkeypatch):
    """A drop of *only* non-target-init LLVM_*-versioned symbols (libstdc++
    re-exports the PGO build inlined away) is the same-soname std:: drift:
    flagged is_brick but healable, with no target-centric remediation."""
    usr_lib = tmp_path / "usr/lib"
    _mk(usr_lib, "libgallium-26.1.3-arch1.2.so")
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(_AC + "needed_sonames", lambda p: ["libLLVM.so.22.1"])
    std_sym = ("_ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE"
               "9_M_assignERKS4_")
    monkeypatch.setattr(_AC + "_undefined_versioned", lambda p: {
        (std_sym, "LLVM_22.1"),
    })
    # The new libLLVM still exports the target-init node (no backend dropped),
    # just not the weak std:: copy.
    monkeypatch.setattr(_AC + "_exported_versioned", lambda path, cache: {
        ("LLVMInitializeAMDGPUTarget", "LLVM_22.1"),
    })
    findings = ts._diff_consumers_against_libllvm(
        Path("/x/libLLVM.so.22.1"), source_label="freshly-built",
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.is_brick
    assert f.healable
    assert "LLVM_TARGETS_TO_BUILD" not in f.remediation  # not target advice
    assert "rebuild" in f.remediation.lower()


def test_diff_consumers_mixed_drop_is_unhealable(tmp_path, monkeypatch):
    """If *any* miss is a target-init symbol the finding is unhealable even when
    a std:: symbol is also dropped — the backend loss governs."""
    usr_lib = tmp_path / "usr/lib"
    _mk(usr_lib, "libgallium-26.so")
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(_AC + "needed_sonames", lambda p: ["libLLVM.so.22.1"])
    monkeypatch.setattr(_AC + "_undefined_versioned", lambda p: {
        ("LLVMInitializeAMDGPUTarget", "LLVM_22.1"),
        ("_ZNSt7__cxx11_M_assign", "LLVM_22.1"),
    })
    monkeypatch.setattr(_AC + "_exported_versioned",
                        lambda path, cache: {("LLVMInitializeX86Target", "LLVM_22.1")})
    findings = ts._diff_consumers_against_libllvm(
        Path("/x/libLLVM.so.22.1"), source_label="installed",
    )
    assert len(findings) == 1
    assert not findings[0].healable


def test_diff_consumers_satisfied_no_findings(tmp_path, monkeypatch):
    usr_lib = tmp_path / "usr/lib"
    _mk(usr_lib, "libgallium-26.so")
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(_AC + "needed_sonames", lambda p: ["libLLVM.so.22.1"])
    monkeypatch.setattr(_AC + "_undefined_versioned",
                        lambda p: {("LLVMInitializeAMDGPUTarget", "LLVM_22.1")})
    monkeypatch.setattr(_AC + "_exported_versioned", lambda path, cache: {
        ("LLVMInitializeAMDGPUTarget", "LLVM_22.1"),
        ("LLVMInitializeX86Target", "LLVM_22.1"),
    })
    assert ts._diff_consumers_against_libllvm(
        Path("/x/libLLVM.so.22.1"), source_label="installed") == []


def test_diff_consumers_different_soname_skipped(tmp_path, monkeypatch):
    """Consumer links the OLD soname → owned by the soname-bump gate, not here."""
    usr_lib = tmp_path / "usr/lib"
    _mk(usr_lib, "libgallium-26.so")
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(_AC + "needed_sonames", lambda p: ["libLLVM.so.21.1"])
    monkeypatch.setattr(_AC + "_undefined_versioned",
                        lambda p: {("LLVMInitializeAMDGPUTarget", "LLVM_21.1")})
    monkeypatch.setattr(_AC + "_exported_versioned",
                        lambda path, cache: {("LLVMInitializeX86Target", "LLVM_22.1")})
    assert ts._diff_consumers_against_libllvm(
        Path("/x/libLLVM.so.22.1"), source_label="installed") == []


def test_diff_consumers_unreadable_exports_fail_open(tmp_path, monkeypatch):
    """nm can't read the new lib → return [] (don't block a build on a hiccup)."""
    usr_lib = tmp_path / "usr/lib"
    _mk(usr_lib, "libgallium-26.so")
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(_AC + "_exported_versioned", lambda path, cache: set())
    assert ts._diff_consumers_against_libllvm(
        Path("/x/libLLVM.so.22.1"), source_label="freshly-built") == []


def test_check_system_consumer_symbols_extracts_and_flags(tmp_path, monkeypatch):
    usr_lib = tmp_path / "usr/lib"
    _mk(usr_lib, "libgallium-26.so")
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    extracted = tmp_path / "extract/usr/lib/libLLVM.so.22.1"

    def fake_extract(pkg, members, dest):
        # lib32 member must have been filtered out before extraction.
        assert members == ["usr/lib/libLLVM.so.22.1"]
        extracted.parent.mkdir(parents=True, exist_ok=True)
        extracted.write_bytes(b"\x7fELF")
        return [extracted]

    monkeypatch.setattr(_AC + "_list_sos_in_pkg", lambda pkg: [
        "usr/lib/libLLVM.so.22.1", "usr/lib32/libLLVM.so.22.1",
    ])
    monkeypatch.setattr(_AC + "_extract_sos", fake_extract)
    monkeypatch.setattr(_AC + "needed_sonames", lambda p: ["libLLVM.so.22.1"])
    monkeypatch.setattr(_AC + "_undefined_versioned",
                        lambda p: {("LLVMInitializeAMDGPUTarget", "LLVM_22.1")})
    monkeypatch.setattr(_AC + "_exported_versioned",
                        lambda path, cache: {("LLVMInitializeX86Target", "LLVM_22.1")})
    findings = ts.check_system_consumer_symbols([tmp_path / "llvm-libs.pkg.tar.zst"])
    assert len(findings) == 1
    assert findings[0].is_brick
    assert "AMDGPU" in findings[0].message
    assert "freshly-built" in findings[0].message


def test_check_system_consumer_symbols_no_libllvm_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(_AC + "_list_sos_in_pkg", lambda pkg: ["usr/lib/libfoo.so.1"])
    assert ts.check_system_consumer_symbols([tmp_path / "foo.pkg.tar.zst"]) == []


def test_check_installed_consumer_symbols_no_libllvm_returns_empty(tmp_path, monkeypatch):
    usr_lib = tmp_path / "usr/lib"
    usr_lib.mkdir(parents=True)
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    assert ts.check_installed_consumer_symbols() == []


def test_check_installed_consumer_symbols_flags_broken(tmp_path, monkeypatch):
    usr_lib = tmp_path / "usr/lib"
    _mk(usr_lib, "libLLVM.so.22.1")        # the now-installed lib (real file)
    _mk(usr_lib, "libgallium-26.so")       # broken consumer
    (usr_lib / "libLLVM.so").symlink_to(usr_lib / "libLLVM.so.22.1")  # dev symlink ignored
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(_AC + "needed_sonames", lambda p: ["libLLVM.so.22.1"])
    monkeypatch.setattr(_AC + "_undefined_versioned",
                        lambda p: {("LLVMInitializeAMDGPUTarget", "LLVM_22.1")})
    monkeypatch.setattr(_AC + "_exported_versioned",
                        lambda path, cache: {("LLVMInitializeX86Target", "LLVM_22.1")})
    findings = ts.check_installed_consumer_symbols()
    assert len(findings) == 1
    assert findings[0].is_brick
    assert "installed" in findings[0].message


# ---------------------------------------------------------------------------
# libllvm_soname_consumers — local-DB walk cost (2.6.1-B22)
# ---------------------------------------------------------------------------

def _mk_local_db(root: Path, names: dict[str, list[str]]) -> None:
    """Build a fake /var/lib/pacman/local tree: <pkg>-1.0-1/desc with %DEPENDS%."""
    root.mkdir(parents=True, exist_ok=True)
    for name, deps in names.items():
        entry = root / f"{name}-1.0-1"
        entry.mkdir()
        body = f"%NAME%\n{name}\n\n%VERSION%\n1.0-1\n"
        if deps:
            body += "\n%DEPENDS%\n" + "\n".join(deps) + "\n"
        (entry / "desc").write_text(body)


def _count_db_enumerations(monkeypatch, db_root: Path) -> dict[str, int]:
    """Count directory enumerations of db_root, whichever pathlib route is used.

    Path.iterdir() goes through os.listdir; Path.glob() through os.scandir.
    Counting both keeps this test blind to the implementation choice.
    """
    import os

    counter = {"n": 0}
    real_listdir, real_scandir = os.listdir, os.scandir

    def _bump(path):
        if Path(path) == db_root:
            counter["n"] += 1

    def fake_listdir(path=".", *a, **k):
        _bump(path)
        return real_listdir(path, *a, **k)

    def fake_scandir(path=".", *a, **k):
        _bump(path)
        return real_scandir(path, *a, **k)

    monkeypatch.setattr(os, "listdir", fake_listdir)
    monkeypatch.setattr(os, "scandir", fake_scandir)
    return counter


def _run_soname_walk(monkeypatch, tmp_path, n_packages: int) -> int:
    """Run the walk against an n-package fake DB; return db-root enumerations."""
    from sysforge.primitives import pacman

    db_root = tmp_path / f"db{n_packages}"
    names = {f"filler{i}": [] for i in range(n_packages - 1)}
    names["vulkan-radeon"] = ["libLLVM.so=22.1-64"]
    _mk_local_db(db_root, names)

    monkeypatch.setattr(pacman, "_LOCAL_DB_ROOT", db_root)
    monkeypatch.setattr(pacman, "get_all_installed_packages", lambda: dict.fromkeys(names, "1.0-1"))
    counter = _count_db_enumerations(monkeypatch, db_root)
    ts.libllvm_soname_consumers("22.1", exclude=set())
    return counter["n"]


def test_libllvm_soname_consumers_does_not_rescan_db_per_package(monkeypatch, tmp_path):
    """The reverse-dep walk must not re-enumerate the local DB once per package.

    Regression guard for 2.6.1-B22: resolving <pkg>-<ver>-<rel> per package
    re-read the whole DB directory each time, making the walk O(N^2) in
    installed packages (~5.5M stats on a 2,349-package host). Enumeration
    count must stay flat as the DB grows, not track its size.
    """
    small = _run_soname_walk(monkeypatch, tmp_path, 20)
    large = _run_soname_walk(monkeypatch, tmp_path, 200)
    assert small == large, (
        f"DB enumerations scale with package count: {small} at N=20, "
        f"{large} at N=200 — the walk re-resolves the DB per package"
    )


# ---------------------------------------------------------------------------
# 3.2.0-B22 — the std:: drift remediation names which of two causes happened
# ---------------------------------------------------------------------------

def _stddrift_setup(tmp_path, monkeypatch):
    usr_lib = tmp_path / "usr/lib"
    _mk(usr_lib, "libgallium-26.2.2-arch1.1.so")
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(_AC + "needed_sonames", lambda p: ["libLLVM.so.22.1"])
    monkeypatch.setattr(_AC + "_undefined_versioned", lambda p: {
        ("_ZNSt6vectorIiSaIiEE17_M_realloc_insertIJRKiEEEvN9__gnu_cxx", "LLVM_22.1"),
    })
    monkeypatch.setattr(_AC + "_exported_versioned", lambda path, cache: {
        ("LLVMInitializeAMDGPUTarget", "LLVM_22.1"),
    })
    monkeypatch.setattr("sysforge.primitives.pacman.owners_of", lambda paths: {
        p: ("mesa" if "gallium" in p.name else "llvm-libs") for p in paths
    })


def _dates(consumer_built, libllvm_installed):
    return lambda name, root=None: (
        (consumer_built, 1) if name == "mesa" else (1, libllvm_installed)
    )


def test_stddrift_consumer_built_after_libllvm_names_the_wrong_link(
        tmp_path, monkeypatch):
    """2026-09-13: mesa was *built* against the repo libLLVM in a sandbox —
    "rebuild the consumer" would reproduce the identical broken package."""
    _stddrift_setup(tmp_path, monkeypatch)
    monkeypatch.setattr("sysforge.primitives.pacman.get_package_dates",
                        _dates(consumer_built=2000, libllvm_installed=1000))
    [f] = ts._diff_consumers_against_libllvm(
        Path("/usr/lib/libLLVM.so.22.1"), source_label="installed",
    )
    assert "built after" in f.message and "different build of libLLVM" in f.message
    assert "sandbox" in f.remediation
    assert "sysforge build <consumer" not in f.remediation


def test_stddrift_libllvm_changed_under_consumer_keeps_rebuild_advice(
        tmp_path, monkeypatch):
    _stddrift_setup(tmp_path, monkeypatch)
    monkeypatch.setattr("sysforge.primitives.pacman.get_package_dates",
                        _dates(consumer_built=1000, libllvm_installed=2000))
    [f] = ts._diff_consumers_against_libllvm(
        Path("/usr/lib/libLLVM.so.22.1"), source_label="installed",
    )
    assert "inlined away" in f.message
    assert "sysforge build <consumer" in f.remediation


# ---------------------------------------------------------------------------
# 3.5.0-B14: consumer discovery beyond the curated graphics globs
# ---------------------------------------------------------------------------

def test_libllvm_soname_consumers_counts_plain_llvm_libs_dependents(monkeypatch):
    """Arch consumers depend on bare `llvm-libs`, never on the
    `libLLVM.so=<mm>-64` soname it provides; the walk must still see them."""
    _patch_pacman(
        monkeypatch,
        llvm_libs_files=["usr/lib/libLLVM.so.23.1"],
        installed={"lldb": "1", "mesa": "1", "zlib": "1", "clang": "1"},
        depends={
            "lldb": ["llvm-libs", "clang"],
            "mesa": ["llvm-libs>=23"],
            "zlib": ["glibc"],
            "clang": ["llvm-libs"],
        },
    )
    out = ts.libllvm_soname_consumers("23.1", exclude=set(ts.LLVM_LOCKSTEP_SUITE))
    assert out == ["lldb", "mesa"]


def _consumer_tree(tmp_path, monkeypatch, files, imports):
    """Fake installed consumers: `files` {pkg: [relpath]}, ELF ones get the
    magic; `imports` {relpath: {(sym, ver)}} feeds the nm seam."""
    monkeypatch.setattr(ts, "_ROOT", tmp_path)
    for rels in files.values():
        for rel in rels:
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x7fELF" if rel in imports else b"#!/bin/sh\n")
    from sysforge.primitives import pacman
    monkeypatch.setattr(pacman, "get_package_files", lambda name: files.get(name, []))
    monkeypatch.setattr(
        _AC + "_undefined_versioned",
        lambda path: imports.get(str(path.relative_to(tmp_path)), set()),
    )


def test_consumer_required_targets_maps_backends_to_packages(tmp_path, monkeypatch):
    _patch_pacman(
        monkeypatch, llvm_libs_files=["usr/lib/libLLVM.so.23.1"],
        installed={"lldb": "1", "bpf": "1", "clang": "1"},
        depends={"lldb": ["llvm-libs"], "bpf": ["llvm-libs"], "clang": ["llvm-libs"]},
    )
    _consumer_tree(tmp_path, monkeypatch, files={
        "lldb": ["usr/lib/liblldb.so.23.1", "usr/share/lldb/README"],
        "bpf": ["usr/bin/bpftool"],
        "clang": ["usr/lib/libclang-cpp.so.23.1"],
    }, imports={
        "usr/lib/liblldb.so.23.1": {
            ("LLVMInitializeWebAssemblyAsmPrinter", "LLVM_23.1"),
            ("LLVMInitializeHexagonTarget", "LLVM_23.1"),
            ("LLVMInitializeX86TargetInfo", "LLVM_23.1"),
            ("_ZNSt6vectorIiSaIiEE9push_backEOi", "LLVM_23.1"),  # not a target init
            ("malloc", "GLIBC_2.2.5"),
        },
        "usr/bin/bpftool": {("LLVMInitializeWebAssemblyTarget", "LLVM_23.1")},
        # lockstep suite member: rebuilt with the same targets, never a consumer
        "usr/lib/libclang-cpp.so.23.1": {("LLVMInitializeSparcTarget", "LLVM_23.1")},
    })
    out = ts.consumer_required_targets(exclude=set(ts.LLVM_LOCKSTEP_SUITE))
    assert out == {
        "Hexagon": ["lldb"],
        "WebAssembly": ["bpf", "lldb"],
        "X86": ["lldb"],
    }


def test_consumer_required_targets_nothing_installed_is_empty():
    # The autouse isolated pacman DB has no consumers.
    assert ts.consumer_required_targets(exclude=set()) == {}


def test_consumer_required_targets_excludes_by_pkgname_not_pkgbase(tmp_path, monkeypatch):
    """lldb's pkgbase is `llvm` since upstream merged the suite, but the
    toolchain does not rebuild lldb, so it is still a consumer."""
    _patch_pacman(
        monkeypatch, llvm_libs_files=["usr/lib/libLLVM.so.23.1"],
        installed={"lldb": "1"}, depends={"lldb": ["llvm-libs"]},
        pkgbase={"lldb": "llvm"},
    )
    _consumer_tree(tmp_path, monkeypatch, files={"lldb": ["usr/lib/liblldb.so.23.1"]},
                   imports={"usr/lib/liblldb.so.23.1": {("LLVMInitializeBPFTarget", "LLVM_23.1")}})
    assert ts.consumer_required_targets(exclude=set(ts.LLVM_LOCKSTEP_SUITE)) == {"BPF": ["lldb"]}


def _gate_seams(monkeypatch, *, undefined, exported):
    monkeypatch.setattr(_AC + "needed_sonames", lambda p: ["libLLVM.so.23.1"])
    monkeypatch.setattr(_AC + "_undefined_versioned", lambda p: undefined)
    monkeypatch.setattr(_AC + "_exported_versioned", lambda path, cache: exported)


def test_diff_consumers_flags_discovered_non_graphics_consumer(tmp_path, monkeypatch):
    """3.5.0-B14: lldb is no graphics glob, but it is a libLLVM consumer."""
    usr_lib = tmp_path / "usr/lib"
    lldb = _mk(usr_lib, "liblldb.so.23.1")
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(ts, "libllvm_consumer_files", lambda *, exclude: {"lldb": [lldb]})
    _gate_seams(monkeypatch,
                undefined={("LLVMInitializeWebAssemblyTarget", "LLVM_23.1")},
                exported={("LLVMInitializeX86Target", "LLVM_23.1")})
    findings = ts._diff_consumers_against_libllvm(
        Path("/staging/usr/lib/libLLVM.so.23.1"), source_label="installed")
    assert len(findings) == 1
    f = findings[0]
    assert f.check_id == "libllvm_consumer_symbols"
    assert not f.is_brick and not f.healable
    assert "lldb" in f.message and "WebAssembly" in f.message
    assert "desktop" not in f.message


def test_diff_consumers_graphics_file_also_discovered_is_reported_once(tmp_path, monkeypatch):
    usr_lib = tmp_path / "usr/lib"
    gallium = _mk(usr_lib, "libgallium-26.2.4-arch1.1.so")
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(ts, "libllvm_consumer_files", lambda *, exclude: {"mesa": [gallium]})
    _gate_seams(monkeypatch,
                undefined={("LLVMInitializeAMDGPUTarget", "LLVM_23.1")},
                exported={("LLVMInitializeX86Target", "LLVM_23.1")})
    findings = ts._diff_consumers_against_libllvm(
        Path("/staging/usr/lib/libLLVM.so.23.1"), source_label="installed")
    assert len(findings) == 1 and findings[0].is_brick


def test_diff_consumers_ignores_imports_another_needed_lib_exports(tmp_path, monkeypatch):
    """libclang, libclang-cpp and liblldb export under ``LLVM_*`` nodes of their
    own: doxygen's ``clang_createIndex@LLVM_13`` is libclang's, never owed by
    libLLVM. Only what no other NEEDED lib provides is a libLLVM miss."""
    usr_lib = tmp_path / "usr/lib"
    doxygen = _mk(usr_lib, "doxygen")
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(ts, "libllvm_consumer_files", lambda *, exclude: {"doxygen": [doxygen]})
    monkeypatch.setattr(_AC + "needed_sonames",
                        lambda p: ["libclang.so.23.1", "libLLVM.so.23.1"])
    monkeypatch.setattr(_AC + "_undefined_versioned", lambda p: {
        ("clang_createIndex", "LLVM_13"),
        ("LLVMInitializeAMDGPUTarget", "LLVM_23.1"),
    })
    monkeypatch.setattr(_AC + "_build_ldconfig_map", lambda: {
        ("libclang.so.23.1", "ELF64"): "/usr/lib/libclang.so.23.1"})
    monkeypatch.setattr(_AC + "_elf_class", lambda p: "ELF64")
    exports = {"/usr/lib/libclang.so.23.1": {("clang_createIndex", "LLVM_13")}}
    monkeypatch.setattr(_AC + "_exported_versioned",
                        lambda path, cache: exports.get(path, {("LLVMInitializeX86Target",
                                                                "LLVM_23.1")}))
    findings = ts._diff_consumers_against_libllvm(
        Path("/staging/usr/lib/libLLVM.so.23.1"), source_label="installed")
    assert len(findings) == 1
    assert "AMDGPU" in findings[0].message
    assert "clang_createIndex" not in findings[0].message


# ---------------------------------------------------------------------------
#  3.5.0-B20: the gate skips packages the same transaction replaces
# ---------------------------------------------------------------------------

def _two_consumers(tmp_path, monkeypatch):
    """An installed lldb and doxygen, both missing a libLLVM target symbol."""
    usr_lib = tmp_path / "usr/lib"
    files = {"lldb": [_mk(usr_lib, "liblldb.so.23.1")],
             "doxygen": [_mk(usr_lib, "doxygen")]}
    monkeypatch.setattr(ts, "_USR_LIB", usr_lib)
    monkeypatch.setattr(ts, "libllvm_consumer_files", lambda *, exclude: {
        k: v for k, v in files.items() if k not in exclude})
    _gate_seams(monkeypatch,
                undefined={("LLVMInitializeWebAssemblyTarget", "LLVM_23.1")},
                exported={("LLVMInitializeX86Target", "LLVM_23.1")})
    return usr_lib


def test_pre_install_gate_skips_pkgnames_the_batch_ships(tmp_path, monkeypatch):
    _two_consumers(tmp_path, monkeypatch)
    built = {tmp_path / "llvm-libs.pkg.tar.zst": "llvm-libs",
             tmp_path / "lldb.pkg.tar.zst": "lldb"}
    monkeypatch.setattr("sysforge.primitives.pacman.read_pkgname_from_file",
                        lambda p: built[p])
    extracted = tmp_path / "extract/usr/lib/libLLVM.so.23.1"

    def fake_extract(pkg, members, dest):
        extracted.parent.mkdir(parents=True, exist_ok=True)
        extracted.write_bytes(b"\x7fELF")
        return [extracted]

    monkeypatch.setattr(_AC + "_list_sos_in_pkg", lambda pkg: (
        ["usr/lib/libLLVM.so.23.1"] if pkg.name.startswith("llvm-libs") else []))
    monkeypatch.setattr(_AC + "_extract_sos", fake_extract)
    findings = ts.check_system_consumer_symbols(list(built))
    assert [f.message.split()[0] for f in findings] == ["doxygen"]


def test_post_install_gate_skips_excluded_pkgnames(tmp_path, monkeypatch):
    usr_lib = _two_consumers(tmp_path, monkeypatch)
    _mk(usr_lib, "libLLVM.so.23.1")
    findings = ts.check_installed_consumer_symbols(exclude={"lldb"})
    assert [f.message.split()[0] for f in findings] == ["doxygen"]


def test_post_install_gate_excludes_nothing_by_default(tmp_path, monkeypatch):
    usr_lib = _two_consumers(tmp_path, monkeypatch)
    _mk(usr_lib, "libLLVM.so.23.1")
    names = sorted(f.message.split()[0] for f in ts.check_installed_consumer_symbols())
    assert names == ["doxygen", "liblldb.so.23.1"]


def test_shipped_pkgnames_skips_unreadable_archives(tmp_path, monkeypatch):
    names = {tmp_path / "a.pkg.tar.zst": "lldb", tmp_path / "b.pkg.tar.zst": None}
    monkeypatch.setattr("sysforge.primitives.pacman.read_pkgname_from_file",
                        lambda p: names[p])
    assert ts.shipped_pkgnames(list(names)) == {"lldb"}


# ---------------------------------------------------------------------------
#  3.5.0-F4: std:: export parity against stock libLLVM
# ---------------------------------------------------------------------------

_V = "LLVM_23.1"
_STR_REPLACE = "_ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE10_M_replaceEmmPKcm"
_LLVM_TYPED = "_ZNSt6vectorIN4llvm5ValueESaIS2_EE7reserveEm"
_ANON_TYPED = "_ZNSt6vectorIN12_GLOBAL__N_13FooESaIS1_EE7reserveEm"
_LLVM_PREFIX_NAME = "_ZNSt6vectorI14llvmFooBarBazQSaIS0_EE7reserveEm"
_TEMP_BUF = "_ZNSt17_Temporary_bufferIPSt4pairIiiES1_EC1ES2_l"
# What the system libstdc++ exports, for tests that are not about that filter.
_ALL = {_STR_REPLACE, _LLVM_TYPED, _ANON_TYPED, _LLVM_PREFIX_NAME,
        "_ZSt4swapIiEvRT_S1_", "_ZN4llvm5Value4dumpEv"}


def test_parity_gap_reports_missing_pure_std():
    stock = {(_STR_REPLACE, _V), ("_ZSt4swapIiEvRT_S1_", _V)}
    assert ts.std_export_parity_gap(set(), stock, libstdcxx=_ALL) == sorted(
        [_STR_REPLACE, "_ZSt4swapIiEvRT_S1_"])


def test_parity_gap_drops_what_libstdcxx_does_not_export():
    """3.5.0-B23: an implicit instantiation (every consumer emits its own) is
    not a hazard; only the extern-template set libstdc++ exports is."""
    stock = {(_STR_REPLACE, _V), (_TEMP_BUF, _V)}
    assert ts.std_export_parity_gap(set(), stock, libstdcxx={_STR_REPLACE}) == [_STR_REPLACE]


def test_parity_gap_ignores_llvm_and_anonymous_types():
    stock = {(_LLVM_TYPED, _V), (_ANON_TYPED, _V)}
    assert ts.std_export_parity_gap(set(), stock, libstdcxx=_ALL) == []


def test_parity_gap_keeps_an_identifier_that_merely_starts_with_llvm():
    """`14llvmFooBarBazQ` is a 14-character name, not the llvm namespace."""
    gap = ts.std_export_parity_gap(set(), {(_LLVM_PREFIX_NAME, _V)}, libstdcxx=_ALL)
    assert gap == [_LLVM_PREFIX_NAME]


def test_parity_gap_ignores_present_non_std_and_non_llvm_versions():
    stock = {(_STR_REPLACE, _V), ("_ZN4llvm5Value4dumpEv", _V), (_STR_REPLACE, "GLIBCXX_3.4.21")}
    assert ts.std_export_parity_gap({(_STR_REPLACE, _V)}, stock, libstdcxx=_ALL) == []


def test_parity_gap_counts_a_non_default_export_as_present():
    """The thunks export sym@LLVM_23.1 (one @); nm parsing yields the same pair."""
    from sysforge.primitives.abi_check import _parse_nm_exports
    built = _parse_nm_exports(f"0000000000001180 T {_STR_REPLACE}@{_V}\n")
    assert ts.std_export_parity_gap(built, {(_STR_REPLACE, _V)}, libstdcxx=_ALL) == []


def _parity_seams(monkeypatch, tmp_path, *, stock_pkg, exports):
    built_pkg = tmp_path / "llvm-libs-23.1.3-3-x86_64.pkg.tar.zst"
    libs = {"built": tmp_path / "built/libLLVM.so.23.1",
            "stock": tmp_path / "stock/libLLVM.so.23.1"}

    def fake_extract(pkg_files, dest):
        return (pkg_files[0], libs[dest.name])

    monkeypatch.setattr(ts, "_extract_system_libllvm", fake_extract)
    monkeypatch.setattr("sysforge.primitives.pacman.read_pkgname_from_file",
                        lambda p: "llvm-libs")
    asked = {}

    def fake_cached(name, version):
        asked["key"] = (name, version)
        return stock_pkg

    monkeypatch.setattr("sysforge.primitives.pacman.cached_repo_pkg_file", fake_cached)
    monkeypatch.setattr(ts, "_LIBSTDCXX", tmp_path / "sys/libstdc++.so.6")
    exports = {"sys": {(s, "GLIBCXX_3.4.21") for s in _ALL}, **exports}
    monkeypatch.setattr(_AC + "_exported_versioned",
                        lambda path, cache: exports[Path(path).parent.name])
    return built_pkg, asked


def test_check_std_export_parity_reports_gap(tmp_path, monkeypatch):
    stock_pkg = tmp_path / "cache/llvm-libs-23.1.3-3-x86_64.pkg.tar.zst"
    built_pkg, asked = _parity_seams(monkeypatch, tmp_path, stock_pkg=stock_pkg, exports={
        "built": {("_ZN4llvm5Value4dumpEv", _V)},
        "stock": {("_ZN4llvm5Value4dumpEv", _V), (_STR_REPLACE, _V)},
    })
    result = ts.check_std_export_parity([built_pkg])
    assert asked["key"] == ("llvm-libs", "23.1.3-3")
    assert result.gap == (_STR_REPLACE,)
    assert stock_pkg.name in result.detail


def test_check_std_export_parity_skips_without_stock(tmp_path, monkeypatch):
    built_pkg, _ = _parity_seams(monkeypatch, tmp_path, stock_pkg=None, exports={})
    result = ts.check_std_export_parity([built_pkg])
    assert result.gap is None
    assert "llvm-libs-23.1.3-3" in result.detail


def test_check_std_export_parity_unreadable_exports_skip(tmp_path, monkeypatch):
    """An nm failure reads as an empty export set; that is a skip, not a gap of everything."""
    built_pkg, _ = _parity_seams(
        monkeypatch, tmp_path, stock_pkg=tmp_path / "s.pkg.tar.zst",
        exports={"built": set(), "stock": {(_STR_REPLACE, _V)}})
    assert ts.check_std_export_parity([built_pkg]).gap is None


def test_check_std_export_parity_filters_by_system_libstdcxx(tmp_path, monkeypatch):
    """3.5.0-B23: a gap symbol the system libstdc++ does not export is dropped."""
    built_pkg, _ = _parity_seams(
        monkeypatch, tmp_path, stock_pkg=tmp_path / "s.pkg.tar.zst", exports={
            "built": {("_ZN4llvm5Value4dumpEv", _V)},
            "stock": {("_ZN4llvm5Value4dumpEv", _V), (_STR_REPLACE, _V), (_TEMP_BUF, _V)},
        })
    assert ts.check_std_export_parity([built_pkg]).gap == (_STR_REPLACE,)


def test_check_std_export_parity_unreadable_libstdcxx_skips(tmp_path, monkeypatch):
    built_pkg, _ = _parity_seams(
        monkeypatch, tmp_path, stock_pkg=tmp_path / "s.pkg.tar.zst", exports={
            "sys": set(),
            "built": {("_ZN4llvm5Value4dumpEv", _V)},
            "stock": {("_ZN4llvm5Value4dumpEv", _V), (_STR_REPLACE, _V)},
        })
    result = ts.check_std_export_parity([built_pkg])
    assert result.gap is None and "libstdc++" in result.detail


def test_check_std_export_parity_failure_is_a_skip(tmp_path, monkeypatch):
    def boom(pkg_files, dest):
        raise OSError("bsdtar exploded")

    monkeypatch.setattr(ts, "_extract_system_libllvm", boom)
    result = ts.check_std_export_parity([tmp_path / "llvm-libs-1-1-x86_64.pkg.tar.zst"])
    assert result.gap is None and "bsdtar exploded" in result.detail


def test_check_std_export_parity_no_libllvm_is_a_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_extract_system_libllvm", lambda pkg_files, dest: None)
    assert ts.check_std_export_parity([tmp_path / "foo.pkg.tar.zst"]).gap is None


def test_installed_consumer_check_uses_the_newest_libllvm(tmp_path, monkeypatch):
    """3.5.0-B58: with a compat package such as llvm21-libs installed, the
    lexically first soname is the old library; the newest one is checked."""
    for name in ("libLLVM.so.21.1", "libLLVM.so.22.1"):
        (tmp_path / name).write_bytes(b"\x7fELF")
    monkeypatch.setattr(ts, "_USR_LIB", tmp_path)
    seen = []
    monkeypatch.setattr(ts, "_diff_consumers_against_libllvm",
                        lambda lib, **k: seen.append(lib.name) or [])
    ts.check_installed_consumer_symbols()
    assert seen == ["libLLVM.so.22.1"]


def test_so_version_is_numeric():
    assert ts.so_version(Path("libLLVM.so.9.0")) < ts.so_version(Path("libLLVM.so.21.1"))
