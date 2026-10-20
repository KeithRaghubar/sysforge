"""
test_build_inputs.py — out-of-profile build inputs and their drift sub-axis
(3.2.0-F18).

``build_inputs.resolve_build_inputs`` is the one resolver the record site
(``makepkg_wrapper._record_build_state``) and flag drift both call. These tests
pin each input's applicability, python ``major.minor`` normalisation, the
"missing field is unknown, not drift" upgrade path, the PGO-mode inputs-only
judging, and record/re-derive parity through the real record site.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from sysforge.primitives import build_inputs as bi
from sysforge.primitives.flag_drift import (
    STATUS_DRIFTED,
    STATUS_IN_SYNC,
    STATUS_NOT_PROFILED,
    drift_candidate,
    resolve_flag_drift,
)
from sysforge.primitives.profile import serialize_flags

_CONFIG = {
    "rules": [],
    "defaults": {"profile": "bare"},
    "profiles": {"bare": {"CFLAGS": "-O2"}},
    "conflict_groups": {},
}
_FLAGS = serialize_flags({"CFLAGS": "-O2"})


@pytest.fixture(autouse=True)
def _fresh_python_cache():
    bi._python_minor.cache_clear()
    yield
    bi._python_minor.cache_clear()


def _interp(tmp_path, name):
    p = tmp_path / "bin" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


def _pkgbuild(tmp_path, name="python-foo", depends="python"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    deps = f"depends=({depends})\n" if depends else ""
    (d / "PKGBUILD").write_text(f"pkgname={name}\npkgver=1.0\npkgrel=1\n{deps}")
    return d


def _meta(name, depends=()):
    return {"globals": {"pkgname": name, "pkgbase": name, "depends": list(depends)}}


def _drift(entry, state_dir):
    return resolve_flag_drift(entry, _CONFIG, {}, preserved_system_tokens={},
                              state_dir=state_dir)


# ---------------------------------------------------------------------------
# python: applicability and major.minor normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("deps,expected", [
    (["python"], True),
    (["python>=3.12"], True),
    (["python-requests", "glibc"], True),
    (["glibc"], False),
    ([], False),
])
def test_is_python_package_reads_runtime_depends(deps, expected):
    assert bi.is_python_package(_meta("x", deps)) is expected


def test_makedepends_only_python_is_not_a_python_package():
    meta = {"globals": {"pkgname": "x", "makedepends": ["python", "meson"]}}
    assert bi.is_python_package(meta) is False


def test_python_minor_comes_from_the_resolved_interpreter_name(tmp_path):
    real = _interp(tmp_path, "python3.13")
    link = tmp_path / "bin" / "python"
    link.symlink_to(real)
    # A patch-level update keeps the same file name, so it never drifts.
    assert bi._python_minor(str(link)) == "3.13"


def test_python_minor_probes_an_unversioned_interpreter(tmp_path, monkeypatch):
    interp = _interp(tmp_path, "python")
    import sysforge.primitives.run as run_mod
    monkeypatch.setattr(run_mod, "capture", lambda cmd, **k: SimpleNamespace(
        returncode=0, stdout="3.12\n"))
    assert bi._python_minor(str(interp)) == "3.12"


def test_python_input_follows_the_build_python_pin(tmp_path):
    interp = _interp(tmp_path, "python3.12")
    cfg = {"build": {"python": str(interp)}}
    got = bi.resolve_build_inputs(_meta("python-foo", ["python"]),
                                  state_dir=tmp_path, cc="gcc", sysforge_cfg=cfg)
    assert got == {"python": "3.12"}


def test_non_python_package_has_no_python_input(tmp_path):
    interp = _interp(tmp_path, "python3.12")
    got = bi.resolve_build_inputs(_meta("htop", ["ncurses"]), state_dir=tmp_path,
                                  cc="gcc", sysforge_cfg={"build": {"python": str(interp)}})
    assert got == {}


# ---------------------------------------------------------------------------
# mesa / llvm: applicability through their resolvers
# ---------------------------------------------------------------------------

def test_mesa_records_both_driver_lists(tmp_path, monkeypatch):
    import sysforge.primitives.mesa_drivers as md
    monkeypatch.setattr(md, "resolve_or_detect_mesa_drivers", lambda *a: {
        "gallium": ["radeonsi", "llvmpipe"], "vulkan": ["amd", "swrast"]})
    got = bi.resolve_build_inputs(_meta("mesa"), state_dir=tmp_path, cc="gcc")
    assert got == {"mesa_gallium": "radeonsi,llvmpipe", "mesa_vulkan": "amd,swrast"}


def test_unfiltered_mesa_has_no_driver_input(tmp_path, monkeypatch):
    import sysforge.primitives.mesa_drivers as md
    monkeypatch.setattr(md, "resolve_or_detect_mesa_drivers", lambda *a: None)
    assert bi.resolve_build_inputs(_meta("mesa"), state_dir=tmp_path, cc="gcc") == {}


def test_llvm_records_targets_but_lib32_never_does(tmp_path, monkeypatch):
    import sysforge.primitives.llvm_targets as lt
    monkeypatch.setattr(lt, "resolve_or_detect_llvm_targets",
                        lambda *a: ["AMDGPU", "X86"])
    assert bi.resolve_build_inputs(_meta("llvm"), state_dir=tmp_path,
                                   cc="gcc") == {"llvm_targets": "AMDGPU;X86"}
    assert bi.resolve_build_inputs(_meta("lib32-llvm"), state_dir=tmp_path,
                                   cc="gcc") == {}


# ---------------------------------------------------------------------------
# pgo_profile: one llvm-path and one gcc-path case (dual-toolchain parity)
# ---------------------------------------------------------------------------

@pytest.fixture
def _store_has_profile(tmp_path, monkeypatch):
    from sysforge.primitives import mesa_pgo
    prof = tmp_path / "foo.profdata"
    prof.write_text("")
    monkeypatch.setattr(mesa_pgo, "reuse_profdata",
                        lambda tcfg=None, pkgbase=None: prof)
    return prof


def test_pgo_profile_recorded_for_an_llvm_build(tmp_path, _store_has_profile):
    got = bi.resolve_build_inputs(_meta("foo"), state_dir=tmp_path, cc="clang")
    assert got == {"pgo_profile": str(_store_has_profile)}


def test_pgo_profile_absent_for_a_gcc_build(tmp_path, _store_has_profile):
    # A clang .profdata never feeds a gcc build, so it is not an input.
    assert bi.resolve_build_inputs(_meta("foo"), state_dir=tmp_path, cc="gcc") == {}


def test_pgo_profile_absent_for_a_stage_owned_build(tmp_path, _store_has_profile):
    got = bi.resolve_build_inputs(_meta("foo"), state_dir=tmp_path, cc="clang",
                                  owner_stage="toolchain")
    assert got == {}


# ---------------------------------------------------------------------------
# Drift sub-axis
# ---------------------------------------------------------------------------

def _entry(d, **kw):
    return {"build_mode": "source_built", "pkgbuild_dir": str(d),
            "flags_string": _FLAGS, **kw}


def _pin_python(monkeypatch, minor):
    monkeypatch.setattr(bi, "_resolve_python", lambda cfg: minor)


def test_python_bump_drifts_with_a_per_key_diff(tmp_path, monkeypatch):
    _pin_python(monkeypatch, "3.13")
    r = _drift(_entry(_pkgbuild(tmp_path), build_inputs="python=3.12\n"), tmp_path)
    assert r.status == STATUS_DRIFTED
    assert len(r.diffs) == 1
    assert "python" in r.diffs[0] and "'3.12'" in r.diffs[0] and "'3.13'" in r.diffs[0]


def test_matching_inputs_are_in_sync(tmp_path, monkeypatch):
    _pin_python(monkeypatch, "3.13")
    r = _drift(_entry(_pkgbuild(tmp_path), build_inputs="python=3.13\n"), tmp_path)
    assert r.status == STATUS_IN_SYNC


def test_missing_field_is_unknown_not_drifted(tmp_path, monkeypatch):
    # The upgrade path: entries recorded before the field existed.
    _pin_python(monkeypatch, "3.13")
    r = _drift(_entry(_pkgbuild(tmp_path)), tmp_path)
    assert r.status == STATUS_IN_SYNC


def test_new_input_reads_as_added(tmp_path, monkeypatch):
    # Recorded with no inputs; the package now gains one.
    _pin_python(monkeypatch, "3.13")
    r = _drift(_entry(_pkgbuild(tmp_path), build_inputs=""), tmp_path)
    assert r.status == STATUS_DRIFTED
    assert r.diffs == ["  +python: '3.13'"]


def test_axis_off_without_a_state_dir(tmp_path, monkeypatch):
    _pin_python(monkeypatch, "3.13")
    r = _drift(_entry(_pkgbuild(tmp_path), build_inputs="python=3.12\n"), None)
    assert r.status == STATUS_IN_SYNC


def test_buildflags_ignored_package_still_drifts_on_inputs(tmp_path, monkeypatch):
    _pin_python(monkeypatch, "3.13")
    d = _pkgbuild(tmp_path)
    (d / "PKGBUILD").write_text((d / "PKGBUILD").read_text() + "options=('!buildflags')\n")
    r = _drift(_entry(d, build_inputs="python=3.12\n"), tmp_path)
    assert r.status == STATUS_DRIFTED


def test_pgo_mode_entry_is_judged_on_inputs_only(tmp_path, monkeypatch):
    from sysforge.primitives import mesa_pgo
    monkeypatch.setattr(mesa_pgo, "reuse_profdata", lambda tcfg=None, pkgbase=None: None)
    monkeypatch.setenv("CC", "clang")
    d = _pkgbuild(tmp_path, "foo", depends=None)
    # Stale flags_string must NOT drift a PGO entry: only inputs are judged.
    entry = {"build_mode": "pgo", "pkgbuild_dir": str(d),
             "flags_string": "CFLAGS=-O0\n", "build_inputs": "pgo_profile=/gone.profdata\n"}
    assert drift_candidate(entry)
    r = _drift(entry, tmp_path)
    assert r.status == STATUS_DRIFTED
    assert r.diffs == ["  -pgo_profile: '/gone.profdata'"]


def test_pgo_mode_entry_without_the_field_is_not_judged(tmp_path):
    d = _pkgbuild(tmp_path, "foo", depends=None)
    entry = {"build_mode": "pgo", "pkgbuild_dir": str(d), "flags_string": "CFLAGS=-O0\n"}
    assert not drift_candidate(entry)
    assert _drift(entry, tmp_path).status == STATUS_NOT_PROFILED


# ---------------------------------------------------------------------------
# Record/re-derive parity through the real record site
# ---------------------------------------------------------------------------

def test_record_site_value_rederives_in_sync(tmp_path, monkeypatch):
    from sysforge.build import makepkg_wrapper
    from sysforge.primitives.build_state import BuildState
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    _pin_python(monkeypatch, "3.13")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    d = _pkgbuild(tmp_path)
    pkgbuild = d / "PKGBUILD"
    options = SimpleNamespace(
        state_dir=state_dir, source=None, owner_stage=None, cc_override=None,
        toolchain_variant=None, toolchain_fingerprint=None,
        record_toolchain_variant=None)
    makepkg_wrapper._record_build_state(
        # No resolved profile: no flags_string, so only the inputs axis judges.
        pkgbuild, parse_pkgbuild(pkgbuild), None, options,
        rename=None, record_build_mode=None, build_elapsed=1)

    entry = BuildState(state_dir).get("python-foo")
    assert entry["build_inputs"] == "python=3.13\n"
    r = resolve_flag_drift(entry, _CONFIG, {}, preserved_system_tokens={},
                           state_dir=state_dir)
    assert not r.drifted, r.diffs

    _pin_python(monkeypatch, "3.14")
    r = resolve_flag_drift(entry, _CONFIG, {}, preserved_system_tokens={},
                           state_dir=state_dir)
    assert r.status == STATUS_DRIFTED
    assert len(r.diffs) == 1 and "'3.13'" in r.diffs[0] and "'3.14'" in r.diffs[0]


def test_build_inputs_round_trip_through_build_state(tmp_path):
    from sysforge.primitives.build_state import BuildState
    bs = BuildState(tmp_path)
    bs.record("python-foo", "1", "1", "0", "python-foo", Path("/x"),
              build_inputs="mesa_gallium=iris\npython=3.13\n")
    bs.record("empty", "1", "1", "0", "empty", Path("/y"), build_inputs="")
    bs.save()
    again = BuildState(tmp_path)
    assert again.get("python-foo")["build_inputs"] == "mesa_gallium=iris\npython=3.13\n"
    # "" (recorded, nothing applies) stays distinct from absent (unknown).
    assert again.get("empty")["build_inputs"] == ""


def test_build_inputs_is_sticky_when_a_caller_does_not_know(tmp_path):
    from sysforge.primitives.build_state import BuildState
    bs = BuildState(tmp_path)
    bs.record("p", "1", "1", "0", "p", Path("/x"), build_inputs="python=3.13\n")
    bs.record("p", "2", "1", "0", "p", Path("/x"))
    assert bs.get("p")["build_inputs"] == "python=3.13\n"


def test_compiles_round_trips_as_bool_and_is_sticky(tmp_path):
    """3.5.0-F11: ``compiles`` is a native TOML bool and survives a caller
    that does not know it (repair/backfill)."""
    from sysforge.primitives.build_state import BuildState
    bs = BuildState(tmp_path)
    bs.record("foo-bin", "1", "1", "0", "foo-bin", tmp_path, compiles=False)
    bs.save()
    bs = BuildState(tmp_path)
    assert bs.get("foo-bin")["compiles"] is False
    bs.record("foo-bin", "2", "1", "0", "foo-bin", tmp_path)
    assert bs.get("foo-bin")["compiles"] is False
