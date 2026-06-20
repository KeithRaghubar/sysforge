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


def test_detect_suite_skew_pkgrel_only_bump_still_brick():
    """The install verifier wants exact lockstep — a pkgrel bump is a skew."""
    versions = {"llvm": "22.0.0-1", "llvm-libs": "22.0.0-2"}
    assert ts.detect_suite_skew(versions) is not None


# ---------------------------------------------------------------------------
# check_link_resolution
# ---------------------------------------------------------------------------

def _fake_ldd(mapping):
    """side_effect: returns ldd-style output for clang/lld, success otherwise."""
    def run(cmd, **kwargs):
        if cmd and cmd[0] == "ldd":
            binary = cmd[1]
            return MagicMock(returncode=0, stdout=mapping.get(binary, ""), stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")
    return run


def test_check_link_resolution_clean(tmp_path, monkeypatch):
    clang = tmp_path / "clang"
    clang.touch()
    monkeypatch.setattr(ts, "_USR_BIN_CLANG", clang)
    monkeypatch.setattr(ts, "_USR_LIB", Path("/usr/lib"))
    monkeypatch.setattr(ts, "_run", _fake_ldd({
        str(clang): "\tlibLLVM-22.so => /usr/lib/libLLVM-22.so (0x7f01)\n",
    }))
    assert ts.check_link_resolution() == []


def test_check_link_resolution_staging_prefix_is_brick(tmp_path, monkeypatch):
    clang = tmp_path / "clang"
    clang.touch()
    monkeypatch.setattr(ts, "_USR_BIN_CLANG", clang)
    monkeypatch.setattr(ts, "_run", _fake_ldd({
        str(clang):
            "\tlibLLVM-22.so => /var/tmp/sysforge-llvm-stage2/usr/lib/libLLVM-22.so (0x7f01)\n",
    }))
    findings = ts.check_link_resolution()
    assert len(findings) == 1
    assert findings[0].is_brick
    assert "staging prefix" in findings[0].message


def test_check_link_resolution_shadow_outside_usrlib_is_brick(tmp_path, monkeypatch):
    clang = tmp_path / "clang"
    clang.touch()
    monkeypatch.setattr(ts, "_USR_BIN_CLANG", clang)
    monkeypatch.setattr(ts, "_run", _fake_ldd({
        str(clang): "\tlibLLVM-22.so => /home/u/.local/lib/libLLVM-22.so (0x7f01)\n",
    }))
    findings = ts.check_link_resolution()
    assert len(findings) == 1
    assert findings[0].is_brick
    assert "outside /usr/lib" in findings[0].message


def test_check_link_resolution_ldd_missing_warns(tmp_path, monkeypatch):
    clang = tmp_path / "clang"
    clang.touch()
    monkeypatch.setattr(ts, "_USR_BIN_CLANG", clang)
    monkeypatch.setattr(ts, "_run", lambda cmd, **k: None)  # ldd absent
    findings = ts.check_link_resolution()
    assert len(findings) == 1
    assert not findings[0].is_brick


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
    conf.write_text("[options]\n[core]\n[extra]\n#[multilib]\n#Include = /etc/pacman.d/mirrorlist\n")
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
