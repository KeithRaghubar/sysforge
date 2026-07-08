"""
test_storage_probe.py — storage / filesystem probe (doctor ``storage`` axis).

Filesystem reads use ``tmp_path`` fixtures; no real /etc/fstab or disk access
beyond a monkeypatched ``probe_free_space``.
"""
from __future__ import annotations

from sysforge.primitives import storage_probe


# --- probe_free_space -------------------------------------------------------

def test_probe_free_space_real_dir(tmp_path):
    res = storage_probe.probe_free_space(tmp_path)
    assert res is not None
    free_gb, total_gb = res
    assert free_gb >= 0 and total_gb > 0


def test_probe_free_space_walks_to_existing_ancestor(tmp_path):
    missing = tmp_path / "does" / "not" / "exist" / "yet"
    res = storage_probe.probe_free_space(missing)
    assert res is not None  # resolved via the existing ancestor


# --- disk space (F17) -------------------------------------------------------

def test_disk_space_warns_under_threshold(monkeypatch):
    monkeypatch.setattr(storage_probe, "probe_free_space", lambda p: (3.0, 100.0))
    monkeypatch.setattr(storage_probe, "_resolve_build_dir", lambda c: "/home/x/src")
    out = storage_probe._check_disk_space({}, {"disk_low_gb": 10.0})
    assert len(out) == 1 and out[0].check_id == "disk_low"


def test_disk_space_clean_over_threshold(monkeypatch):
    monkeypatch.setattr(storage_probe, "probe_free_space", lambda p: (50.0, 100.0))
    monkeypatch.setattr(storage_probe, "_resolve_build_dir", lambda c: "/home/x/src")
    assert storage_probe._check_disk_space({}, {"disk_low_gb": 10.0}) == []


def test_disk_space_no_build_dir(monkeypatch):
    monkeypatch.setattr(storage_probe, "_resolve_build_dir", lambda c: None)
    assert storage_probe._check_disk_space({}, {}) == []


# --- fstab integrity (F18) --------------------------------------------------

def _write_fstab(tmp_path, body: str):
    p = tmp_path / "fstab"
    p.write_text(body, encoding="utf-8")
    return p


def test_fstab_dangling_uuid_flagged(tmp_path, monkeypatch):
    fstab = _write_fstab(
        tmp_path,
        "# comment\n"
        "UUID=dead-beef  /  ext4  defaults  0 1\n",
    )
    monkeypatch.setattr(storage_probe, "_fs_spec_resolves", lambda s: False)
    out = storage_probe._check_fstab(fstab)
    assert len(out) == 1 and out[0].check_id == "fstab_dangling"


def test_fstab_resolving_entry_clean(tmp_path, monkeypatch):
    fstab = _write_fstab(tmp_path, "UUID=abc  /  ext4  defaults  0 1\n")
    monkeypatch.setattr(storage_probe, "_fs_spec_resolves", lambda s: True)
    assert storage_probe._check_fstab(fstab) == []


def test_fstab_skips_pseudo_network_and_nofail(tmp_path, monkeypatch):
    fstab = _write_fstab(
        tmp_path,
        "tmpfs            /tmp        tmpfs  defaults        0 0\n"
        "server:/export   /mnt/nfs    nfs    defaults        0 0\n"
        "UUID=optional    /mnt/opt    ext4   defaults,nofail 0 2\n",
    )
    # Everything resolves-false, but all three lines are skip-listed → clean.
    monkeypatch.setattr(storage_probe, "_fs_spec_resolves", lambda s: False)
    assert storage_probe._check_fstab(fstab) == []


def test_fstab_unreadable_yields_nothing():
    assert storage_probe._check_fstab("/nonexistent/fstab/path") == []
