# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for primitives/kernel_fdo.py — kernel AutoFDO / Propeller orchestration."""
from pathlib import Path

import pytest

from sysforge.primitives import kernel_fdo


# ---------------------------------------------------------------------------
# Store resolution + build-mode
# ---------------------------------------------------------------------------

def test_store_is_method_subdir_namespaced_by_pkgname():
    autofdo = kernel_fdo.resolve_store("linux-custom", propeller=False, tcfg={})
    assert autofdo.name == "linux-custom"
    assert autofdo.parent.name == "autofdo"
    prop = kernel_fdo.resolve_store("linux-custom", propeller=True, tcfg={})
    assert prop.parent.name == "propeller"


def test_store_honours_profile_store_override(tmp_path):
    store = kernel_fdo.resolve_store(
        "linux", propeller=False, tcfg={"profile_store": str(tmp_path)}
    )
    assert store == tmp_path / "autofdo" / "linux"


def test_build_mode_maps_propeller():
    assert kernel_fdo.build_mode(propeller=False) == "autofdo_kernel"
    assert kernel_fdo.build_mode(propeller=True) == "propeller_kernel"


@pytest.mark.parametrize("pkg,record,fdo,prop", [
    ("linux-sysforge", "linux-sysforge-profiling", "linux-sysforge-fdo",
     "linux-sysforge-propeller"),
    ("linux-mine", "linux-mine-sysforge-profiling", "linux-mine-sysforge-fdo",
     "linux-mine-sysforge-propeller"),
])
def test_role_names(pkg, record, fdo, prop):
    assert kernel_fdo.record_pkgname(pkg) == record
    assert kernel_fdo.optimized_pkgname(pkg, propeller=False) == fdo
    assert kernel_fdo.optimized_pkgname(pkg, propeller=True) == prop


@pytest.mark.parametrize("name", [
    "linux-sysforge-profiling", "linux-sysforge-fdo", "linux-sysforge-propeller"])
def test_role_names_idempotent_and_never_stack(name):
    # Any role name maps back through its sysforge base, never stacking roles.
    assert kernel_fdo.record_pkgname(name) == "linux-sysforge-profiling"
    assert kernel_fdo.optimized_pkgname(name, propeller=False) == "linux-sysforge-fdo"
    assert kernel_fdo.optimized_pkgname(name, propeller=True) == "linux-sysforge-propeller"


@pytest.mark.parametrize("name,expected", [
    ("linux-sysforge-profiling", "linux-sysforge"),
    ("linux-sysforge-fdo", "linux-sysforge"),
    ("linux-sysforge-propeller", "linux-sysforge"),
    ("linux-sysforge", "linux-sysforge"),
    ("linux", "linux"),
    ("foo-fdo", "foo-fdo"),
])
def test_strip_role(name, expected):
    assert kernel_fdo.strip_role(name) == expected


def test_build_modes_are_optimized_and_coexist():
    from sysforge.primitives.profile import (
        is_optimized_build_mode,
        rename_mode_for_build_mode,
    )

    for mode in (kernel_fdo.BUILD_MODE_AUTOFDO, kernel_fdo.BUILD_MODE_PROPELLER):
        assert is_optimized_build_mode(mode)
        # Kernel FDO installs alongside the stock kernel for bootloader fallback.
        assert rename_mode_for_build_mode(mode) == "coexist"


# ---------------------------------------------------------------------------
# kconfig + use-env
# ---------------------------------------------------------------------------

def test_fdo_kconfig_base_and_propeller():
    assert kernel_fdo.fdo_kconfig(propeller=False) == {"CONFIG_AUTOFDO_CLANG": "y"}
    assert kernel_fdo.fdo_kconfig(propeller=True) == {
        "CONFIG_AUTOFDO_CLANG": "y",
        "CONFIG_PROPELLER_CLANG": "y",
    }


def test_use_env_autofdo_only(tmp_path):
    env = kernel_fdo.use_env(tmp_path, propeller=False)
    assert env == {"CLANG_AUTOFDO_PROFILE": str(tmp_path / "kernel.afdo")}


# ---------------------------------------------------------------------------
# require_profile — the fail-fast guard for `--autofdo=use`
# ---------------------------------------------------------------------------

def test_require_profile_missing_autofdo_raises(tmp_path):
    with pytest.raises(kernel_fdo.KernelFdoError) as exc:
        kernel_fdo.require_profile(tmp_path, propeller=False)
    assert "--autofdo=record" in str(exc.value)


def test_require_profile_present_autofdo_ok(tmp_path):
    (tmp_path / "kernel.afdo").write_text("x")
    kernel_fdo.require_profile(tmp_path, propeller=False)  # no raise


# ---------------------------------------------------------------------------
# detect_branch_sampling — uarch event resolution (BRS vs LBR)
# ---------------------------------------------------------------------------

def test_detect_amd_zen3_brs_supported():
    bs = kernel_fdo.detect_branch_sampling(
        "vendor_id\t: AuthenticAMD\ncpu family\t: 25\n"
    )
    assert bs.vendor == "amd" and bs.supported
    assert "pfm" in bs.perf_event_args.lower()
    assert "experimental" in bs.note.lower()


def test_detect_amd_pre_zen3_unsupported():
    bs = kernel_fdo.detect_branch_sampling(
        "vendor_id\t: AuthenticAMD\ncpu family\t: 23\n"  # Zen/Zen2 → no BRS
    )
    assert bs.vendor == "amd" and not bs.supported


def test_detect_intel_lbr_supported():
    bs = kernel_fdo.detect_branch_sampling(
        "vendor_id\t: GenuineIntel\ncpu family\t: 6\n"
    )
    assert bs.vendor == "intel" and bs.supported
    assert "BR_INST_RETIRED" in bs.perf_event_args


def test_detect_unknown_vendor_unsupported():
    bs = kernel_fdo.detect_branch_sampling("model name\t: Something\n")
    assert bs.vendor == "unknown" and not bs.supported


# ---------------------------------------------------------------------------
# resolve_vmlinux
# ---------------------------------------------------------------------------

BANNER = "Linux version 7.2.7-arch1-1-sysforge-profiling (k@h) (clang 21) #1 SMP Mon Oct 5\n"


def _vmlinux(root, banner, *, debug=True):
    p = root / "src" / "linux-7.2.7" / "vmlinux"
    p.parent.mkdir(parents=True, exist_ok=True)
    shstrtab = b"\0.text\0.debug_info\0.debug_line\0" if debug else b"\0.text\0.symtab\0"
    p.write_bytes(b"\x7fELF...." + banner.encode() + b"\0tail" + shstrtab)
    return p


def test_vmlinux_banner_beats_newer_mismatch(tmp_path):
    import os
    good = _vmlinux(tmp_path / "rec", BANNER)
    newer = _vmlinux(tmp_path / "sys" / "linux-sysforge-profiling", BANNER.replace("#1", "#2"))
    os.utime(newer, (good.stat().st_mtime + 100,) * 2)
    got = kernel_fdo.resolve_vmlinux(
        "linux-sysforge-profiling", recorded_build_dir=tmp_path / "rec",
        builddir=tmp_path / "sys", proc_version=BANNER, running_release="x")
    assert got == good


def test_vmlinux_recorded_tree_found_when_system_builddir_differs(tmp_path):
    good = _vmlinux(tmp_path / "profile-builds" / "linux-sysforge-profiling", BANNER)
    got = kernel_fdo.resolve_vmlinux(
        "linux-sysforge-profiling",
        recorded_build_dir=tmp_path / "profile-builds" / "linux-sysforge-profiling",
        builddir=tmp_path / "tmp-makepkg", proc_version=BANNER, running_release="x")
    assert got == good


def test_vmlinux_not_booted_refuses(tmp_path):
    _vmlinux(tmp_path / "rec", BANNER)
    with pytest.raises(kernel_fdo.KernelFdoError, match="not booted into linux-sysforge-profiling"):
        kernel_fdo.resolve_vmlinux(
            "linux-sysforge-profiling", recorded_build_dir=tmp_path / "rec",
            builddir=tmp_path / "none", proc_version="Linux version other\n",
            running_release="7.2.7-arch1-1")


def test_vmlinux_no_candidates_refuses(tmp_path):
    with pytest.raises(kernel_fdo.KernelFdoError, match="build tree is gone"):
        kernel_fdo.resolve_vmlinux(
            "linux-sysforge-profiling", recorded_build_dir=None,
            builddir=tmp_path / "none", proc_version=BANNER, running_release="x")


def test_vmlinux_empty_or_unreadable_candidate_skipped(tmp_path):
    empty = tmp_path / "rec" / "src" / "a" / "vmlinux"
    empty.parent.mkdir(parents=True)
    empty.write_bytes(b"")
    good = _vmlinux(tmp_path / "rec", BANNER)
    assert kernel_fdo.resolve_vmlinux(
        "linux-sysforge-profiling", recorded_build_dir=tmp_path / "rec",
        builddir=tmp_path / "none", proc_version=BANNER, running_release="x") == good


# ---------------------------------------------------------------------------
# capture_commands / missing_tools
# ---------------------------------------------------------------------------

_SAMPLING = kernel_fdo.BranchSampling("amd", True, "--pfm-events EV:k", "n")


def test_capture_round1_lines(tmp_path):
    lines = kernel_fdo.capture_commands(
        tmp_path, sampling=_SAMPLING, vmlinux=Path("/b/vmlinux"), propeller=False)
    assert lines == [
        "# 1. Start your normal workload(s) now.",
        "# 2. While they run, sample the whole system for 5 minutes "
        "(Ctrl-C ends early; data is kept):",
        f"sudo perf record --pfm-events EV:k -a -N -b -c 500009 -o {tmp_path}/perf.data"
        " -- sleep 300",
        f'sudo chown "$(id -un)": {tmp_path}/perf.data',
        "# 3. Convert (any time later, on any kernel, while the build tree still exists):",
        f"llvm-profgen --kernel --binary=/b/vmlinux --perfdata={tmp_path}/perf.data "
        f"--format=extbinary --output={tmp_path}/kernel.afdo",
    ]


def test_capture_round2_prints_only_propeller_conversion(tmp_path):
    lines = kernel_fdo.capture_commands(
        tmp_path, sampling=_SAMPLING, vmlinux=Path("/b/vmlinux"), propeller=True)
    assert lines[-1] == (
        f"generate_propeller_profiles --binary=/b/vmlinux --profile={tmp_path}/perf.data "
        f"--format=propeller --propeller_output_module_name "
        f"--out={tmp_path}/propeller_cc_profile.txt "
        f"--propeller_symorder={tmp_path}/propeller_ld_profile.txt")
    assert not any("llvm-profgen" in ln or "create_llvm_prof" in ln for ln in lines)


@pytest.mark.parametrize("propeller", [False, True])
def test_capture_quotes_paths_with_spaces(tmp_path, propeller):
    import shlex
    store = tmp_path / "my store"
    vm = Path("/b/my build/vmlinux")
    lines = kernel_fdo.capture_commands(
        store, sampling=_SAMPLING, vmlinux=vm, propeller=propeller)
    perf = shlex.split(next(ln for ln in lines if ln.startswith("sudo perf")))
    assert perf[perf.index("-o") + 1] == f"{store}/perf.data"
    assert shlex.quote(f"{store}/perf.data") in "\n".join(lines)
    chown = shlex.split(next(ln for ln in lines if "chown" in ln))
    assert chown[:2] == ["sudo", "chown"] and chown[-1] == f"{store}/perf.data"
    conv = shlex.split(lines[-1])
    assert f"--binary={vm}" in conv
    if propeller:
        assert f"--profile={store}/perf.data" in conv
        assert f"--out={store}/propeller_cc_profile.txt" in conv
        assert f"--propeller_symorder={store}/propeller_ld_profile.txt" in conv
    else:
        assert f"--perfdata={store}/perf.data" in conv
        assert f"--output={store}/kernel.afdo" in conv


def test_vmlinux_unreadable_proc_version_is_distinct_error(tmp_path):
    _vmlinux(tmp_path / "rec", BANNER)
    with pytest.raises(kernel_fdo.KernelFdoError, match="cannot read /proc/version"):
        kernel_fdo.resolve_vmlinux(
            "linux-sysforge-profiling", recorded_build_dir=tmp_path / "rec",
            builddir=tmp_path / "none", proc_version="", running_release="x")


@pytest.mark.parametrize("propeller,absent,expect", [
    (False, {"perf"}, "perf"),
    (False, {"llvm-profgen"}, "llvm-profgen"),
    (True, {"generate_propeller_profiles"}, "generate_propeller_profiles"),
])
def test_missing_tools(propeller, absent, expect):
    def which(t):
        return None if t in absent else f"/usr/bin/{t}"
    missing = kernel_fdo.missing_tools(propeller=propeller, which=which)
    assert len(missing) == 1 and missing[0].startswith(expect)


def test_missing_tools_round1_does_not_need_propeller_tool():
    def which(t):
        return None if t == "generate_propeller_profiles" else "/x"
    assert kernel_fdo.missing_tools(propeller=False, which=which) == []


# ---------------------------------------------------------------------------
# Round sidecar, pinning, version helpers (3.3.0-B14)
# ---------------------------------------------------------------------------

def test_round_roundtrip(tmp_path):
    kernel_fdo.write_round(tmp_path, pkgver="7.2.7.arch1-1",
                           build_dir=tmp_path / 'b "q"', afdo_sha256="ab" * 32)
    info = kernel_fdo.read_round(tmp_path)
    assert info == kernel_fdo.RoundInfo("7.2.7.arch1-1", tmp_path / 'b "q"', "ab" * 32)


def test_round_optional_keys_omitted(tmp_path):
    kernel_fdo.write_round(tmp_path, pkgver="7.2.7.arch1-1", build_dir=None)
    assert kernel_fdo.read_round(tmp_path) == kernel_fdo.RoundInfo("7.2.7.arch1-1", None, None)


@pytest.mark.parametrize("text", ["not = [toml", 'pkgver = 7', "afdo_sha256 = []"])
def test_round_malformed_reads_as_missing(tmp_path, text):
    (tmp_path / "round.toml").write_text(text)
    assert kernel_fdo.read_round(tmp_path) is None


def test_round_non_utf8_reads_as_missing(tmp_path):
    (tmp_path / "round.toml").write_bytes(b'pkgver = "\xff\xfe"\n')
    assert kernel_fdo.read_round(tmp_path) is None


def test_round_absent_is_none(tmp_path):
    assert kernel_fdo.read_round(tmp_path) is None


@pytest.mark.parametrize("ver,mm", [
    ("7.2.7.arch1-1", (7, 2)), ("7.3rc1.r12.gabc-1", (7, 3)), ("6.12-1", (6, 12)),
    ("garbage", None), (None, None)])
def test_kernel_major_minor(ver, mm):
    assert kernel_fdo.kernel_major_minor(ver) == mm


def _stores(tmp_path):
    tcfg = {"profile_store": str(tmp_path)}
    afdo = kernel_fdo.resolve_store("linux", propeller=False, tcfg=tcfg)
    prop = kernel_fdo.resolve_store("linux", propeller=True, tcfg=tcfg)
    return tcfg, afdo, prop


def test_pin_copies_and_hashes(tmp_path):
    tcfg, afdo, prop = _stores(tmp_path)
    afdo.mkdir(parents=True)
    (afdo / "kernel.afdo").write_bytes(b"profile-A")
    pinned, sha = kernel_fdo.pin_autofdo_profile("linux", tcfg=tcfg)
    assert pinned == prop / "kernel.afdo"
    assert pinned.read_bytes() == b"profile-A"
    assert sha == kernel_fdo.file_sha256(afdo / "kernel.afdo")


def test_pin_dry_run_writes_nothing(tmp_path):
    tcfg, afdo, prop = _stores(tmp_path)
    afdo.mkdir(parents=True)
    (afdo / "kernel.afdo").write_bytes(b"profile-A")
    pinned, sha = kernel_fdo.pin_autofdo_profile("linux", tcfg=tcfg, dry_run=True)
    assert pinned == prop / "kernel.afdo" and not prop.exists()
    assert len(sha) == 64


def test_pin_without_round1_profile_refuses(tmp_path):
    tcfg, _, _ = _stores(tmp_path)
    with pytest.raises(kernel_fdo.KernelFdoError, match="round 1"):
        kernel_fdo.pin_autofdo_profile("linux", tcfg=tcfg)


def _round2_store(prop, *, sha_ok=True, pair=True, sidecar=True):
    prop.mkdir(parents=True, exist_ok=True)
    (prop / "kernel.afdo").write_bytes(b"pinned")
    if pair:
        (prop / "propeller_cc_profile.txt").write_text("cc")
        (prop / "propeller_ld_profile.txt").write_text("ld")
    if sidecar:
        sha = kernel_fdo.file_sha256(prop / "kernel.afdo") if sha_ok else "0" * 64
        kernel_fdo.write_round(prop, pkgver="7.2.7.arch1-1", build_dir=None, afdo_sha256=sha)


def test_require_round2_ok_returns_round(tmp_path):
    _, _, prop = _stores(tmp_path)
    _round2_store(prop)
    assert kernel_fdo.require_profile(prop, propeller=True).pkgver == "7.2.7.arch1-1"


@pytest.mark.parametrize("kw,match", [
    ({"sidecar": False}, "Redo round 2"),
    ({"sha_ok": False}, "changed since round 2"),
    ({"pair": False}, "incomplete"),
])
def test_require_round2_refusals(tmp_path, kw, match):
    _, _, prop = _stores(tmp_path)
    _round2_store(prop, **kw)
    with pytest.raises(kernel_fdo.KernelFdoError, match=match):
        kernel_fdo.require_profile(prop, propeller=True)


def test_require_round2_pinned_afdo_absent(tmp_path):
    _, _, prop = _stores(tmp_path)
    _round2_store(prop)
    (prop / "kernel.afdo").unlink()
    with pytest.raises(kernel_fdo.KernelFdoError, match="changed since round 2"):
        kernel_fdo.require_profile(prop, propeller=True)


def test_use_env_propeller_points_at_pinned_copy(tmp_path):
    _, _, prop = _stores(tmp_path)
    env = kernel_fdo.use_env(prop, propeller=True)
    assert env[kernel_fdo.ENV_AUTOFDO] == str(prop / "kernel.afdo")
    assert env[kernel_fdo.ENV_PROPELLER] == str(prop / "propeller")


def test_use_env_afdo_override(tmp_path):
    env = kernel_fdo.use_env(tmp_path, propeller=False, afdo=tmp_path / "x.afdo")
    assert env == {kernel_fdo.ENV_AUTOFDO: str(tmp_path / "x.afdo")}


def test_require_autofdo_returns_round_or_none(tmp_path):
    (tmp_path / "kernel.afdo").write_text("x")
    assert kernel_fdo.require_profile(tmp_path, propeller=False) is None
    kernel_fdo.write_round(tmp_path, pkgver="6.12-1", build_dir=None)
    assert kernel_fdo.require_profile(tmp_path, propeller=False).pkgver == "6.12-1"


# ---------------------------------------------------------------------------
# Applied-profile fingerprint + sidecar (3.3.0-B14, R21)
# ---------------------------------------------------------------------------

def test_applied_fingerprint_autofdo_tracks_profile(tmp_path):
    (tmp_path / "kernel.afdo").write_bytes(b"A")
    a = kernel_fdo.applied_fingerprint(tmp_path, propeller=False)
    assert a == kernel_fdo.applied_fingerprint(tmp_path, propeller=False)
    (tmp_path / "kernel.afdo").write_bytes(b"B")
    assert kernel_fdo.applied_fingerprint(tmp_path, propeller=False) != a


@pytest.mark.parametrize("changed", ["kernel.afdo", "propeller_cc_profile.txt",
                                     "propeller_ld_profile.txt"])
def test_applied_fingerprint_propeller_tracks_every_input(tmp_path, changed):
    _, _, prop = _stores(tmp_path)
    _round2_store(prop)
    before = kernel_fdo.applied_fingerprint(prop, propeller=True)
    assert before != kernel_fdo.applied_fingerprint(prop, propeller=False)
    (prop / changed).write_bytes(b"changed")
    assert kernel_fdo.applied_fingerprint(prop, propeller=True) != before


def test_applied_roundtrip(tmp_path):
    kernel_fdo.write_applied(tmp_path, "ab" * 32)
    assert kernel_fdo.read_applied(tmp_path) == "ab" * 32
    assert (tmp_path / kernel_fdo.APPLIED_SIDECAR).is_file()


@pytest.mark.parametrize("content", [b"not = [toml", b"fingerprint = 7",
                                     b'fingerprint = "\xff\xfe"\n', b"other = \"x\"\n"])
def test_applied_malformed_reads_as_missing(tmp_path, content):
    (tmp_path / kernel_fdo.APPLIED_SIDECAR).write_bytes(content)
    assert kernel_fdo.read_applied(tmp_path) is None


def test_applied_absent_is_none(tmp_path):
    assert kernel_fdo.read_applied(tmp_path) is None


def test_write_round_is_atomic_and_replaces_whole(tmp_path, monkeypatch):
    import os
    kernel_fdo.write_round(tmp_path, pkgver="7.2.6.arch1-1", build_dir=tmp_path / "old",
                           afdo_sha256="aa" * 32)
    replaced = []
    real = os.replace
    monkeypatch.setattr(kernel_fdo.os, "replace",
                        lambda a, b: replaced.append((Path(a), Path(b))) or real(a, b))
    kernel_fdo.write_round(tmp_path, pkgver="7.2.7.arch1-1", build_dir=None)
    assert replaced == [(tmp_path / "round.toml.tmp", tmp_path / "round.toml")]
    assert (tmp_path / "round.toml").read_text() == 'pkgver = "7.2.7.arch1-1"\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["round.toml"]


def test_write_applied_is_atomic(tmp_path, monkeypatch):
    import os
    replaced = []
    real = os.replace
    monkeypatch.setattr(kernel_fdo.os, "replace",
                        lambda a, b: replaced.append(Path(b).name) or real(a, b))
    kernel_fdo.write_applied(tmp_path, "ab" * 32)
    assert replaced == ["applied.toml"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["applied.toml"]


def test_sidecar_write_failure_leaves_no_tmp(tmp_path, monkeypatch):
    def boom(a, b):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(kernel_fdo.os, "replace", boom)
    with pytest.raises(PermissionError):
        kernel_fdo.write_round(tmp_path, pkgver="1-1", build_dir=None)
    assert list(tmp_path.iterdir()) == []


def test_vmlinux_stripped_banner_match_is_skipped(tmp_path):
    _vmlinux(tmp_path / "rec", BANNER, debug=False)
    good = _vmlinux(tmp_path / "sys" / "linux-sysforge-profiling", BANNER)
    assert kernel_fdo.resolve_vmlinux(
        "linux-sysforge-profiling", recorded_build_dir=tmp_path / "rec",
        builddir=tmp_path / "sys", proc_version=BANNER, running_release="x") == good


def test_vmlinux_only_stripped_match_refuses_specifically(tmp_path):
    _vmlinux(tmp_path / "rec", BANNER, debug=False)
    with pytest.raises(kernel_fdo.KernelFdoError) as ei:
        kernel_fdo.resolve_vmlinux(
            "linux-sysforge-profiling", recorded_build_dir=tmp_path / "rec",
            builddir=tmp_path / "none", proc_version=BANNER, running_release="x")
    msg = str(ei.value)
    assert "no debug info" in msg and "--autofdo=record" in msg
    assert "not booted" not in msg
