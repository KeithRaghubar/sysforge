# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""test_mesa_smoke.py — mesa-pinned EGL/Vulkan context probes (3.2.0-F10)."""
import subprocess

import pytest

from sysforge.primitives import diagnostics as diag
from sysforge.primitives import mesa_smoke as ms

_EGL_OK = """Surfaceless platform:
EGL vendor string: Mesa Project
OpenGL core profile renderer: llvmpipe (LLVM 23.1.1, 256 bits)
OpenGL ES profile renderer: llvmpipe (LLVM 23.1.1, 256 bits)
"""
_EGL_HW = "OpenGL core profile renderer: AMD Radeon RX 7900 XTX (radeonsi, navi31)\n"
_VK_OK = "Devices:\n========\nGPU0:\n\tdeviceName         = llvmpipe (LLVM 23.1.1, 256 bits)\n"


def _cp(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def probe(monkeypatch):
    calls = []

    def install(result):
        def fake(cmd, env_extra):
            calls.append((cmd, dict(env_extra)))
            return result
        monkeypatch.setattr(ms, "_probe", fake)
        return calls
    return install


@pytest.fixture
def vendor(monkeypatch, tmp_path):
    f = tmp_path / "50_mesa.json"
    f.write_text("{}")
    monkeypatch.setattr(ms, "mesa_vendor_file", lambda: f)
    return f


def test_egl_pass_pins_mesa_vendor(probe, vendor):
    calls = probe(_cp(_EGL_OK))
    assert ms.check_egl() is None
    cmd, env = calls[0]
    assert cmd == ["eglinfo", "-B", "-p", "surfaceless"]
    assert env == {"__EGL_VENDOR_LIBRARY_FILENAMES": str(vendor)}


def test_egl_software_forces_llvmpipe(probe, vendor):
    calls = probe(_cp(_EGL_OK))
    assert ms.check_egl(software=True) is None
    assert calls[0][1]["LIBGL_ALWAYS_SOFTWARE"] == "1"


def test_egl_software_wrong_renderer_fails(probe, vendor):
    probe(_cp(_EGL_HW))
    f = ms.check_egl(software=True)
    assert f.check_id == "mesa_egl_software" and f.severity == "error"
    assert "radeonsi" in f.message
    assert "doctor --graphics" in f.remediation


def test_egl_nonzero_exit_fails_with_stderr_tail(probe, vendor):
    probe(_cp("", 1, "MESA: error: failed to load driver\nEGL_NOT_INITIALIZED\n"))
    f = ms.check_egl()
    assert f.check_id == "mesa_egl"
    assert "exit 1" in f.message and "EGL_NOT_INITIALIZED" in f.message


def test_egl_missing_renderer_line_fails(probe, vendor):
    probe(_cp("Surfaceless platform:\nEGL vendor string: Mesa Project\n"))
    assert ms.check_egl().check_id == "mesa_egl"


def test_egl_timeout_fails(probe, vendor):
    probe(ms._TIMEOUT)
    f = ms.check_egl()
    assert f.check_id == "mesa_egl" and "did not finish" in f.message


def test_egl_skips_when_eglinfo_missing(probe, vendor):
    probe(None)
    r = ms.check_egl()
    assert isinstance(r, diag.Skip) and r.reason == "eglinfo not installed (mesa-utils)"


def test_egl_skips_without_vendor_file(monkeypatch, probe):
    monkeypatch.setattr(ms, "mesa_vendor_file", lambda: None)
    calls = probe(_cp(_EGL_OK))
    r = ms.check_egl()
    assert isinstance(r, diag.Skip) and r.reason == "no mesa EGL vendor file"
    assert calls == []


def test_vulkan_pass_restricts_to_mesa_icds(monkeypatch, probe, tmp_path):
    lvp = tmp_path / "lvp_icd.json"
    monkeypatch.setattr(ms, "mesa_icds", lambda: [lvp])
    calls = probe(_cp(_VK_OK))
    assert ms.check_vulkan() is None
    assert calls[0] == (["vulkaninfo", "--summary"], {"VK_DRIVER_FILES": str(lvp)})


def test_vulkan_zero_devices_without_lavapipe_skips(monkeypatch, probe, tmp_path):
    monkeypatch.setattr(ms, "mesa_icds", lambda: [tmp_path / "radeon_icd.json"])
    probe(_cp("ERROR_INCOMPATIBLE_DRIVER", 1))
    r = ms.check_vulkan()
    assert isinstance(r, diag.Skip)
    assert r.reason == "no installed mesa Vulkan driver matches this hardware"


def test_vulkan_zero_devices_with_lavapipe_fails(monkeypatch, probe, tmp_path):
    monkeypatch.setattr(ms, "mesa_icds", lambda: [tmp_path / "lvp_icd.json"])
    probe(_cp("ERROR_INCOMPATIBLE_DRIVER", 1))
    assert ms.check_vulkan().check_id == "mesa_vulkan"


def test_vulkan_crash_without_lavapipe_still_fails(monkeypatch, probe, tmp_path):
    monkeypatch.setattr(ms, "mesa_icds", lambda: [tmp_path / "radeon_icd.json"])
    probe(_cp("", -11))
    assert ms.check_vulkan().check_id == "mesa_vulkan"


def test_vulkan_skips_without_icds(monkeypatch, probe):
    monkeypatch.setattr(ms, "mesa_icds", lambda: [])
    r = ms.check_vulkan()
    assert isinstance(r, diag.Skip) and r.reason == "no mesa Vulkan ICDs installed"


def test_vulkan_skips_when_vulkaninfo_missing(monkeypatch, probe, tmp_path):
    monkeypatch.setattr(ms, "mesa_icds", lambda: [tmp_path / "lvp_icd.json"])
    probe(None)
    r = ms.check_vulkan()
    assert isinstance(r, diag.Skip) and r.reason == "vulkaninfo not installed (vulkan-tools)"


def test_mesa_icds_selects_by_owner_pkgbase(monkeypatch, tmp_path):
    for name in ("lvp_icd.json", "nvidia_icd.json", "radeon_icd.json"):
        (tmp_path / name).write_text("{}")
    monkeypatch.setattr(ms, "_ICD_DIR", tmp_path)
    owners = {tmp_path / "lvp_icd.json": "vulkan-swrast",
              tmp_path / "nvidia_icd.json": "nvidia-utils",
              tmp_path / "radeon_icd.json": "vulkan-radeon"}
    monkeypatch.setattr(ms.pacman, "owners_of", lambda paths: owners)
    monkeypatch.setattr(ms.pacman, "get_pkgbase",
                        lambda n: "mesa" if n.startswith("vulkan-") else None)
    assert ms.mesa_icds() == [tmp_path / "lvp_icd.json", tmp_path / "radeon_icd.json"]


def test_mesa_icds_empty_when_owner_lookup_fails(monkeypatch, tmp_path):
    (tmp_path / "lvp_icd.json").write_text("{}")
    monkeypatch.setattr(ms, "_ICD_DIR", tmp_path)
    monkeypatch.setattr(ms.pacman, "owners_of", lambda paths: {})
    assert ms.mesa_icds() == []


def test_probe_maps_timeout(monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(ms.run, "capture", boom)
    assert ms._probe(["eglinfo"], {}) is ms._TIMEOUT


def test_run_smoke_rosters_all_three(monkeypatch):
    monkeypatch.setattr(ms, "check_egl", lambda software=False: None)
    monkeypatch.setattr(ms, "check_vulkan", lambda: diag.Skip("no mesa Vulkan ICDs installed"))
    res = ms.run_smoke()
    assert res.findings == []
    assert res.roster.ran == ["mesa_egl", "mesa_egl_software"]
    assert res.roster.skipped == [("mesa_vulkan", "no mesa Vulkan ICDs installed")]


def _fields(**pkgs):
    """pkgs: name -> (base, depends, provides)."""
    return {n: {"%BASE%": [b] if b else [], "%DEPENDS%": list(d), "%PROVIDES%": list(p)}
            for n, (b, d, p) in pkgs.items()}


_STACK_DB = _fields(
    mesa=("mesa", ["llvm-libs>=23", "libdrm", "libgcc"], []),
    **{"vulkan-swrast": ("mesa", ["mesa=1:26.2.4"], [])},
    **{"llvm-libs": ("llvm", ["zstd", "libffi"], ["libLLVM.so=23.1-64"])},
    libdrm=(None, ["glibc"], []),
    **{"gcc-libs": ("gcc", ["glibc"], ["libgcc"])},
    zstd=(None, ["glibc"], []),
    libffi=(None, ["glibc"], []),
    glibc=(None, [], []),
    firefox=(None, ["glibc", "zstd"], []),
    **{"lib32-mesa": ("lib32-mesa", ["lib32-llvm-libs"], [])},
    **{"lib32-llvm-libs": ("lib32-llvm", [], [])},
)


def test_mesa_stack_closure_excludes_unrelated_and_lib32():
    stack = ms.mesa_stack(_STACK_DB)
    assert {"mesa", "vulkan-swrast", "llvm-libs", "libdrm", "zstd", "libffi",
            "glibc"} <= stack
    assert "firefox" not in stack
    assert not any(n.startswith("lib32-") for n in stack)


def test_mesa_stack_resolves_provides():
    assert "gcc-libs" in ms.mesa_stack(_STACK_DB)  # mesa depends on libgcc, provided by gcc-libs


def test_mesa_stack_tolerates_cycles():
    db = _fields(mesa=("mesa", ["a"], []), a=(None, ["b"], []), b=(None, ["a", "mesa"], []))
    assert ms.mesa_stack(db) == {"mesa", "a", "b"}


def test_mesa_stack_empty_without_mesa():
    assert ms.mesa_stack(_fields(glibc=(None, [], []))) == set()


@pytest.mark.parametrize("source_built,changed,expected", [
    (set(), {"mesa"}, False),                    # all-stock stack: what the distro shipped
    ({"llvm-libs"}, {"mesa"}, True),             # stock mesa landing on a custom LLVM
    ({"libdrm"}, {"firefox"}, False),            # custom stack, unrelated change
    ({"mesa"}, None, True),                      # unknown change set errs toward running
    ({"firefox"}, {"mesa"}, False),              # source-built package outside the stack
])
def test_should_check_truth_table(monkeypatch, source_built, changed, expected):
    monkeypatch.setattr(ms, "_source_built", lambda state_dir: source_built)
    stack = {"mesa", "llvm-libs", "libdrm", "glibc"}
    assert ms.should_check(changed, None, stack=stack) is expected


def test_should_check_false_without_stack(monkeypatch):
    monkeypatch.setattr(ms, "_source_built", lambda state_dir: {"mesa"})
    assert ms.should_check(None, None, stack=set()) is False


def test_post_install_reports_failure_and_hint(monkeypatch, capsys):
    from sysforge.primitives.graphics_probe import GraphicsFinding
    monkeypatch.setattr(ms, "_post_install_plan", lambda changed, state_dir: ["llvm-libs"])
    monkeypatch.setattr(ms, "run_smoke", lambda culprits=None: diag.AxisResult(
        [GraphicsFinding("error", "mesa_egl", "mesa could not create an EGL context",
                         "`sysforge revert-to-stock llvm-libs` while this session still works")],
        diag.Roster(ran=["mesa_egl"])))
    res = ms.post_install({"mesa"}, None)
    err = capsys.readouterr().err
    assert res is not None
    assert "mesa could not create an EGL context" in err
    assert "sysforge revert-to-stock llvm-libs" in err


def test_post_install_skips_when_trigger_false(monkeypatch):
    monkeypatch.setattr(ms, "_post_install_plan", lambda changed, state_dir: None)
    monkeypatch.setattr(ms, "run_smoke", lambda culprits=None: pytest.fail("must not probe"))
    assert ms.post_install({"firefox"}, None) is None


def test_post_install_swallows_exceptions(monkeypatch, capsys):
    from sysforge import log
    def boom(changed, state_dir):
        raise RuntimeError("db locked")
    monkeypatch.setattr(ms, "_post_install_plan", boom)
    saved = log.get_verbosity()
    try:
        log.set_verbosity(1)
        assert ms.post_install({"mesa"}, None) is None
    finally:
        log.set_verbosity(saved)
    assert "could not run: db locked" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Final-review fixes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("mesa", True), ("mesa-sysforge", True), ("mesa-git-sysforge", True),
    ("lib32-mesa-sysforge", True), ("firefox-sysforge", False), ("sysforge", False),
])
def test_is_mesa_family_sees_renamed_pgo_builds(name, expected):
    from sysforge.primitives.profile import is_mesa_family
    assert is_mesa_family(name) is expected


def test_mesa_stack_includes_renamed_pgo_mesa():
    db = _fields(**{
        "mesa-sysforge": ("mesa-sysforge", ["llvm-libs"], []),
        "vulkan-swrast-sysforge": ("mesa-sysforge", [], []),
        "llvm-libs": ("llvm", [], []),
    })
    assert ms.mesa_stack(db) == {"mesa-sysforge", "vulkan-swrast-sysforge", "llvm-libs"}


def test_mesa_icds_keeps_renamed_pgo_owner(monkeypatch, tmp_path):
    (tmp_path / "lvp_icd.json").write_text("{}")
    monkeypatch.setattr(ms, "_ICD_DIR", tmp_path)
    monkeypatch.setattr(ms.pacman, "owners_of",
                        lambda paths: {tmp_path / "lvp_icd.json": "vulkan-swrast-sysforge"})
    monkeypatch.setattr(ms.pacman, "get_pkgbase", lambda n: "mesa-sysforge")
    assert ms.mesa_icds() == [tmp_path / "lvp_icd.json"]


def _failing_egl(monkeypatch):
    monkeypatch.setattr(ms, "check_egl", lambda software=False: ms._failure("mesa_egl", "boom"))
    monkeypatch.setattr(ms, "check_vulkan", lambda: None)


def test_remediation_names_real_verb_and_source_built_culprits(monkeypatch):
    _failing_egl(monkeypatch)
    res = ms.run_smoke(culprits=["llvm-libs", "libdrm"])
    rem = res.findings[0].remediation
    assert "sysforge revert-to-stock libdrm llvm-libs" in rem
    assert "sysforge revert " not in rem


def test_remediation_without_source_built_culprit_says_reinstall(monkeypatch):
    _failing_egl(monkeypatch)
    rem = ms.run_smoke(culprits=[]).findings[0].remediation
    assert "revert-to-stock" not in rem
    assert "reinstall" in rem


def test_post_install_passes_source_built_stack_members(monkeypatch):
    seen = {}
    monkeypatch.setattr(ms, "mesa_stack", lambda fields=None: {"mesa", "llvm-libs", "glibc"})
    monkeypatch.setattr(ms, "_source_built", lambda state_dir: {"llvm-libs", "firefox"})
    monkeypatch.setattr(ms, "run_smoke", lambda culprits=None: (
        seen.update(culprits=culprits) or diag.AxisResult([], diag.Roster(ran=["mesa_egl"]))))
    ms.post_install({"mesa"}, None)
    assert seen["culprits"] == ["llvm-libs"]


def test_vulkan_zero_devices_on_matching_gpu_fails(monkeypatch, probe, tmp_path):
    monkeypatch.setattr(ms, "mesa_icds", lambda: [tmp_path / "radeon_icd.json"])
    monkeypatch.setattr(ms, "_gpu_vendors", lambda: ["amd"])
    probe(_cp("ERROR_INITIALIZATION_FAILED", 1))
    assert ms.check_vulkan().check_id == "mesa_vulkan"


def test_vulkan_zero_devices_on_other_gpu_still_skips(monkeypatch, probe, tmp_path):
    monkeypatch.setattr(ms, "mesa_icds", lambda: [tmp_path / "radeon_icd.json",
                                                   tmp_path / "nouveau_icd.json"])
    monkeypatch.setattr(ms, "_gpu_vendors", lambda: ["nvidia"])
    probe(_cp("ERROR_INCOMPATIBLE_DRIVER", 1))
    assert isinstance(ms.check_vulkan(), diag.Skip)
