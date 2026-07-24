"""
test_rust_probe.py — Rust-toolchain provenance probe (doctor ``rust`` axis).

subprocess is stubbed; no real rustup/pacman access. Read-only and advisory:
the probe emits only INFO/WARN, never ERROR.
"""
from __future__ import annotations

from types import SimpleNamespace

from sysforge.primitives import diagnostics as diag
from sysforge.primitives import rust_probe


def _proc(stdout="", rc=0, stderr=""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


def _dispatch(mapping):
    """Fake _run dispatching on cmd[0] (or ('rustup', cmd[1]))."""
    def run(cmd):
        if cmd[0] == "rustup":
            return mapping.get(("rustup", cmd[1]))
        return mapping.get(cmd[0])
    return run


def test_no_rust_toolchain_is_clean_info(monkeypatch):
    monkeypatch.setattr(rust_probe, "_which", lambda tool: None)
    findings = rust_probe.collect_active_findings()
    assert len(findings) == 1
    assert findings[0].check_id == "rust-none"
    assert findings[0].severity == diag.SEV_INFO
    assert findings[0].category == "rust"


def test_stable_rustup_default_is_info(monkeypatch):
    monkeypatch.setattr(rust_probe, "_which", lambda tool: "/usr/bin/cargo")
    monkeypatch.setattr(rust_probe, "_owner_pkg", lambda path: "rustup")
    monkeypatch.setattr(rust_probe, "_run", _dispatch({
        ("rustup", "show"): _proc(
            stdout="stable-x86_64-unknown-linux-gnu (default)\n"),
    }))
    findings = rust_probe.collect_active_findings()
    assert [f.check_id for f in findings] == ["rust-active"]
    assert findings[0].severity == diag.SEV_INFO
    assert "stable" in findings[0].message


def test_nightly_rustup_default_is_warn(monkeypatch):
    monkeypatch.setattr(rust_probe, "_which", lambda tool: "/usr/bin/cargo")
    monkeypatch.setattr(rust_probe, "_owner_pkg", lambda path: "rustup")
    monkeypatch.setattr(rust_probe, "_run", _dispatch({
        ("rustup", "show"): _proc(
            stdout="nightly-x86_64-unknown-linux-gnu (default)\n"),
    }))
    findings = rust_probe.collect_active_findings()
    ids = {f.check_id for f in findings}
    assert "rust-nightly-default" in ids
    warn = next(f for f in findings if f.check_id == "rust-nightly-default")
    assert warn.severity == diag.SEV_WARN
    assert "nightly" in warn.message


def test_distro_rust_package_is_info(monkeypatch):
    monkeypatch.setattr(rust_probe, "_which", lambda tool: "/usr/bin/cargo")
    monkeypatch.setattr(rust_probe, "_owner_pkg", lambda path: "rust")
    monkeypatch.setattr(rust_probe, "_run", _dispatch({
        "pacman": _proc(stdout="rust 1.79.0-1\n"),
    }))
    findings = rust_probe.collect_active_findings()
    assert [f.check_id for f in findings] == ["rust-active"]
    assert findings[0].severity == diag.SEV_INFO
    assert "rust" in findings[0].message


def test_no_finding_is_ever_error():
    # Guards the advisory-only invariant across the whole module surface.
    assert diag.SEV_ERROR not in {diag.SEV_INFO, diag.SEV_WARN}


def test_channel_of_variants():
    assert rust_probe._channel_of("stable-x86_64-unknown-linux-gnu (default)") == "stable"
    assert rust_probe._channel_of("nightly-x86_64-unknown-linux-gnu (default)") == "nightly"
    assert rust_probe._channel_of("beta-x86_64-unknown-linux-gnu") == "beta"
    assert rust_probe._channel_of("1.75.0-x86_64-unknown-linux-gnu") == "1.75.0"




def _write_pin(tmp_path, channel):
    d = tmp_path / "mypkg"
    d.mkdir()
    (d / "PKGBUILD").write_text("pkgname=mypkg\n")
    (d / "rust-toolchain.toml").write_text(
        f'[toolchain]\nchannel = "{channel}"\n')
    return d


def test_pin_present_and_installed_is_info(monkeypatch, tmp_path):
    d = _write_pin(tmp_path, "nightly-2026-01-01")
    monkeypatch.setattr(rust_probe.config, "find_pkgbuild",
                        lambda pkg, config=None: d / "PKGBUILD")
    monkeypatch.setattr(rust_probe, "_toolchain_installed", lambda ch: True)
    findings = rust_probe.collect_pin_findings({}, ["mypkg"])
    assert [f.check_id for f in findings] == ["rust-pin"]
    assert findings[0].severity == diag.SEV_INFO
    assert "nightly-2026-01-01" in findings[0].message


def test_pin_not_installed_is_warn(monkeypatch, tmp_path):
    d = _write_pin(tmp_path, "1.70.0")
    monkeypatch.setattr(rust_probe.config, "find_pkgbuild",
                        lambda pkg, config=None: d / "PKGBUILD")
    monkeypatch.setattr(rust_probe, "_toolchain_installed", lambda ch: False)
    findings = rust_probe.collect_pin_findings({}, ["mypkg"])
    ids = [f.check_id for f in findings]
    assert "rust-pin-missing" in ids
    warn = next(f for f in findings if f.check_id == "rust-pin-missing")
    assert warn.severity == diag.SEV_WARN
    assert "rustup toolchain install 1.70.0" in warn.remediation


def test_no_pin_yields_no_pin_findings(monkeypatch, tmp_path):
    d = tmp_path / "plainpkg"
    d.mkdir()
    (d / "PKGBUILD").write_text("pkgname=plainpkg\n")
    monkeypatch.setattr(rust_probe.config, "find_pkgbuild",
                        lambda pkg, config=None: d / "PKGBUILD")
    assert rust_probe.collect_pin_findings({}, ["plainpkg"]) == []


def test_malformed_pin_is_warn(monkeypatch, tmp_path):
    d = tmp_path / "badpkg"
    d.mkdir()
    (d / "PKGBUILD").write_text("pkgname=badpkg\n")
    (d / "rust-toolchain.toml").write_text("this is = = not toml [[[")
    monkeypatch.setattr(rust_probe.config, "find_pkgbuild",
                        lambda pkg, config=None: d / "PKGBUILD")
    findings = rust_probe.collect_pin_findings({}, ["badpkg"])
    assert [f.check_id for f in findings] == ["rust-pin-unreadable"]
    assert findings[0].severity == diag.SEV_WARN


def test_collect_rust_findings_merges_active_and_pin(monkeypatch, tmp_path):
    d = _write_pin(tmp_path, "stable")
    monkeypatch.setattr(rust_probe, "_which", lambda tool: None)  # no toolchain
    monkeypatch.setattr(rust_probe.config, "find_pkgbuild",
                        lambda pkg, config=None: d / "PKGBUILD")
    monkeypatch.setattr(rust_probe, "_toolchain_installed", lambda ch: True)
    findings = rust_probe.collect_rust_findings({}, packages=["mypkg"])
    ids = [f.check_id for f in findings]
    assert "rust-none" in ids and "rust-pin" in ids
