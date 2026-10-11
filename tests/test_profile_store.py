# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_profile_store.py — inspection, reclamation and orphan notice for the
shared profile store (3.2.0-F11).
"""
from types import SimpleNamespace

import pytest

from sysforge.primitives import makepkg_pgo as mp


def _tcfg(tmp_path):
    return {"profile_store": str(tmp_path / "root"),
            "pgo_store": str(tmp_path / "root" / "llvm-pgo")}


def _file(path, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_list_profile_stores_reports_method_target_size(tmp_path):
    tc = _tcfg(tmp_path)
    root = tmp_path / "root"
    _file(root / "llvm-pgo" / "clang.profdata", 100)
    _file(root / "pgo-mesa" / "mesa.profdata", 50)
    (root / "pgo-mesa" / "mesa.profdata.version").write_text("1:26.1.3-2\n")
    _file(root / "pgo" / "htop" / "htop.profdata", 7)
    (root / "autofdo").mkdir(parents=True)  # seeded empty → not listed

    rows = {(r.method, r.target): r for r in mp.list_profile_stores(tc)}
    assert set(rows) == {("instr-pgo", None), ("pgo-mesa", None), ("pgo", "htop")}
    assert rows[("instr-pgo", None)].size_bytes == 100
    assert rows[("pgo-mesa", None)].collected_version == "1:26.1.3-2"
    assert rows[("pgo", "htop")].path == root / "pgo" / "htop"


def test_list_profile_stores_reads_round_sidecar_version(tmp_path):
    tc = _tcfg(tmp_path)
    store = tmp_path / "root" / "autofdo" / "linux"
    _file(store / "kernel.afdo")
    (store / "round.toml").write_text('pkgver = "7.2.7.arch1-1"\n')
    rows = {(r.method, r.target): r for r in mp.list_profile_stores(tc)}
    assert rows[("autofdo", "linux")].collected_version == "7.2.7.arch1-1"


def test_list_profile_stores_reads_round_through_kernel_fdo(tmp_path, monkeypatch):
    """One reader for round.toml: kernel_fdo.read_round (no inline parse)."""
    from sysforge.primitives import kernel_fdo
    store = tmp_path / "root" / "autofdo" / "linux"
    _file(store / "kernel.afdo")
    (store / "round.toml").write_text('pkgver = "on-disk"\n')
    monkeypatch.setattr(kernel_fdo, "read_round",
                        lambda s: kernel_fdo.RoundInfo("via-read-round", None, None))
    rows = {(r.method, r.target): r for r in mp.list_profile_stores(_tcfg(tmp_path))}
    assert rows[("autofdo", "linux")].collected_version == "via-read-round"


@pytest.mark.parametrize("content", [b"not = [toml", b"pkgver = 7", b'pkgver = "\xff\xfe"\n'])
def test_list_profile_stores_malformed_sidecar_has_no_version(tmp_path, content):
    store = tmp_path / "root" / "autofdo" / "linux"
    _file(store / "kernel.afdo")
    (store / "round.toml").write_bytes(content)
    rows = {(r.method, r.target): r for r in mp.list_profile_stores(_tcfg(tmp_path))}
    assert rows[("autofdo", "linux")].collected_version is None


@pytest.mark.parametrize("sidecar", ["round.toml", "applied.toml"])
def test_list_profile_stores_age_ignores_sidecar_mtime(tmp_path, sidecar):
    """3.3.0-B16: a bookkeeping rewrite must not make an old profile read as new."""
    import os
    store = tmp_path / "root" / "autofdo" / "linux"
    profile = _file(store / "kernel.afdo")
    os.utime(profile, (1_000_000, 1_000_000))
    side = _file(store / sidecar)
    os.utime(side, (2_000_000, 2_000_000))
    rows = {(r.method, r.target): r for r in mp.list_profile_stores(_tcfg(tmp_path))}
    assert rows[("autofdo", "linux")].mtime == 1_000_000


def test_list_profile_stores_sidecar_only_store_still_dated(tmp_path):
    import os
    store = tmp_path / "root" / "autofdo" / "linux"
    side = _file(store / "round.toml")
    os.utime(side, (2_000_000, 2_000_000))
    rows = {(r.method, r.target): r for r in mp.list_profile_stores(_tcfg(tmp_path))}
    assert rows[("autofdo", "linux")].mtime == 2_000_000


def test_list_profile_stores_empty_when_nothing_collected(tmp_path):
    assert mp.list_profile_stores(_tcfg(tmp_path)) == []


def test_resolve_purge_target_parses_method_and_target(tmp_path):
    tc = _tcfg(tmp_path)
    assert mp.resolve_purge_target(tc, "pgo/htop") == tmp_path / "root" / "pgo" / "htop"
    assert mp.resolve_purge_target(tc, "pgo-mesa") == tmp_path / "root" / "pgo-mesa"


def test_resolve_purge_target_refuses_an_unknown_method(tmp_path):
    with pytest.raises(ValueError, match="unknown profile method"):
        mp.resolve_purge_target(_tcfg(tmp_path), "llvm-pgo")


def test_resolve_purge_target_refuses_path_escapes(tmp_path):
    with pytest.raises(ValueError):
        mp.resolve_purge_target(_tcfg(tmp_path), "pgo/../../etc")


def test_stores_for_package_finds_non_empty_stores(tmp_path):
    tc = _tcfg(tmp_path)
    root = tmp_path / "root"
    _file(root / "pgo" / "htop" / "htop.profdata")
    _file(root / "autofdo" / "linux-sysforge" / "kernel.afdo")
    (root / "propeller" / "linux-sysforge").mkdir(parents=True)  # empty
    assert mp.stores_for_package(tc, ["htop"]) == [root / "pgo" / "htop"]
    assert mp.stores_for_package(tc, ["linux-sysforge"]) == [
        root / "autofdo" / "linux-sysforge"]


def test_stores_for_package_maps_mesa_to_its_back_compat_store(tmp_path):
    tc = _tcfg(tmp_path)
    _file(tmp_path / "root" / "pgo-mesa" / "mesa.profdata")
    assert mp.stores_for_package(tc, ["mesa"]) == [tmp_path / "root" / "pgo-mesa"]


# ---------------------------------------------------------------------------
# `sysforge state profiles`
# ---------------------------------------------------------------------------

def _args(**kw):
    base = dict(purge=None, no_pager=True)
    base.update(kw)
    return SimpleNamespace(**base)


def test_state_profiles_lists_the_store(tmp_path, monkeypatch, capsys):
    from sysforge import state_cmd

    tc = _tcfg(tmp_path)
    _file(tmp_path / "root" / "pgo" / "htop" / "htop.profdata", 2048)
    monkeypatch.setattr(state_cmd, "_load_toolchain_cfg", lambda: tc)
    assert state_cmd.cmd_state_profiles(_args()) == 0
    out = capsys.readouterr().out
    assert "pgo" in out and "htop" in out and "2.0 KiB" in out


def test_state_profiles_purge_needs_confirmation(tmp_path, monkeypatch):
    from sysforge import state_cmd

    tc = _tcfg(tmp_path)
    store = _file(tmp_path / "root" / "pgo" / "htop" / "htop.profdata").parent
    monkeypatch.setattr(state_cmd, "_load_toolchain_cfg", lambda: tc)
    monkeypatch.setattr("sysforge.primitives.prompt.is_interactive", lambda: True)
    monkeypatch.setattr("sysforge.primitives.prompt.prompt_choice", lambda *a, **k: "n")
    assert state_cmd.cmd_state_profiles(_args(purge="pgo/htop")) == 2
    assert store.is_dir()

    monkeypatch.setattr("sysforge.primitives.prompt.prompt_choice", lambda *a, **k: "y")
    assert state_cmd.cmd_state_profiles(_args(purge="pgo/htop")) == 0
    assert not store.exists()


def test_state_profiles_purge_refuses_without_a_tty(tmp_path, monkeypatch):
    from sysforge import state_cmd

    tc = _tcfg(tmp_path)
    store = _file(tmp_path / "root" / "pgo-mesa" / "mesa.profdata").parent
    monkeypatch.setattr(state_cmd, "_load_toolchain_cfg", lambda: tc)
    monkeypatch.setattr("sysforge.primitives.prompt.is_interactive", lambda: False)
    assert state_cmd.cmd_state_profiles(_args(purge="pgo-mesa")) == 2
    assert store.is_dir()


def test_state_profiles_purge_rejects_unknown_method(tmp_path, monkeypatch):
    from sysforge import state_cmd

    monkeypatch.setattr(state_cmd, "_load_toolchain_cfg", lambda: _tcfg(tmp_path))
    assert state_cmd.cmd_state_profiles(_args(purge="bogus")) == 2


def test_cli_parses_state_profiles():
    from sysforge.cli import _build_parser

    args = _build_parser().parse_args(["state", "profiles", "--purge", "pgo/htop"])
    assert args.purge == "pgo/htop"


# ---------------------------------------------------------------------------
# Orphan notice on revert / uninstall
# ---------------------------------------------------------------------------

def test_orphaned_profile_notice_names_the_path(tmp_path, monkeypatch):
    from sysforge import state_cmd

    tc = _tcfg(tmp_path)
    store = _file(tmp_path / "root" / "pgo" / "htop" / "htop.profdata").parent
    monkeypatch.setattr(state_cmd, "_load_toolchain_cfg", lambda: tc)
    lines = state_cmd.orphaned_profile_lines(["htop"])
    assert len(lines) == 1
    assert str(store) in lines[0] and "state profiles --purge pgo/htop" in lines[0]
    assert state_cmd.orphaned_profile_lines(["ripgrep"]) == []


# ---------------------------------------------------------------------------
# GAIN column: the measured `bench toolchain` ratio (3.4.0-F6)
# ---------------------------------------------------------------------------

def _bench(tmp_path, monkeypatch, *, installed="23.1.1-1", fp=None, ratio=1.2):
    from sysforge import bench_cmd, state_cmd
    from sysforge.primitives import bench_state, bench_workload

    tc = _tcfg(tmp_path)
    _file(tmp_path / "root" / "llvm-pgo" / "clang.profdata", 2048)
    _file(tmp_path / "root" / "pgo" / "htop" / "htop.profdata", 2048)
    monkeypatch.setattr(state_cmd, "_load_toolchain_cfg", lambda: tc)
    monkeypatch.setattr(state_cmd, "_bench_state_dir", lambda: tmp_path)
    monkeypatch.setattr(bench_cmd, "installed_version", lambda name: installed)
    monkeypatch.setattr(bench_cmd, "_load_tcfg", lambda: None)
    bench_state.save(tmp_path, "toolchain", bench_state.BenchResult(
        installed_version="23.1.1-1", stock_version="23.1.1-1", corpus="c",
        fingerprint=fp or bench_workload.default_fingerprint(), runs=5,
        stock_median_s=4.2, installed_median_s=3.5, ratio=ratio,
        measured_at="2026-10-11T00:00:00Z"))
    return state_cmd


def _gain(out, store):
    lines = out.splitlines()
    col = lines[0].index("GAIN")
    row = next(line for line in lines if line.startswith(store + " "))
    cell = row[col:].split()[0]
    return cell + (" (stale)" if "(stale)" in row else "")


def test_state_profiles_shows_gain_on_the_toolchain_row_only(tmp_path, monkeypatch, capsys):
    state_cmd = _bench(tmp_path, monkeypatch)
    assert state_cmd.cmd_state_profiles(_args()) == 0
    out = capsys.readouterr().out
    assert _gain(out, "instr-pgo") == "1.20x"
    assert _gain(out, "pgo/htop") == "-"


def test_state_profiles_gain_stale_on_version_bump(tmp_path, monkeypatch, capsys):
    state_cmd = _bench(tmp_path, monkeypatch, installed="23.1.2-1")
    state_cmd.cmd_state_profiles(_args())
    assert _gain(capsys.readouterr().out, "instr-pgo") == "1.20x (stale)"


def test_state_profiles_gain_stale_on_workload_change(tmp_path, monkeypatch, capsys):
    state_cmd = _bench(tmp_path, monkeypatch, fp="0" * 64)
    state_cmd.cmd_state_profiles(_args())
    assert _gain(capsys.readouterr().out, "instr-pgo") == "1.20x (stale)"


def test_state_profiles_gain_dash_without_ratio(tmp_path, monkeypatch, capsys):
    state_cmd = _bench(tmp_path, monkeypatch, ratio=None)
    state_cmd.cmd_state_profiles(_args())
    assert _gain(capsys.readouterr().out, "instr-pgo") == "-"
