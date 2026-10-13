# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""bench_state: bench.toml round-trip and staleness (3.4.0-F6)."""
from sysforge.primitives import bench_state as bs


def _r(**kw):
    base = dict(installed_version="23.1.1-1", stock_version="23.1.1-1",
                corpus='built-in "synthetic" TUs', fingerprint="f" * 64, runs=5,
                stock_median_s=4.21, installed_median_s=3.52, ratio=1.196,
                measured_at="2026-10-11T10:30:00Z", built_at="2026-10-10T08:00:00Z")
    base.update(kw)
    return bs.BenchResult(**base)


def test_round_trip(tmp_path):
    bs.save(tmp_path, "toolchain", _r())
    assert bs.load(tmp_path, "toolchain") == _r()


def test_ratio_less_result_round_trips(tmp_path):
    bs.save(tmp_path, "toolchain", _r(ratio=None))
    assert bs.load(tmp_path, "toolchain").ratio is None


def test_save_keeps_other_targets(tmp_path):
    bs.save(tmp_path, "mesa", _r(corpus="m"))
    bs.save(tmp_path, "toolchain", _r())
    assert bs.load(tmp_path, "mesa").corpus == "m"


def test_missing_and_malformed_load_none(tmp_path):
    assert bs.load(tmp_path, "toolchain") is None
    (tmp_path / bs.FILENAME).write_text("not = [toml")
    assert bs.load(tmp_path, "toolchain") is None
    (tmp_path / bs.FILENAME).write_text("[toolchain]\nruns = 5\n")
    assert bs.load(tmp_path, "toolchain") is None


_AT = "2026-10-10T08:00:00Z"


def test_is_stale():
    r = _r()
    assert not bs.is_stale(r, "23.1.1-1", "f" * 64, _AT)
    assert bs.is_stale(r, "23.1.2-1", "f" * 64, _AT)
    assert bs.is_stale(r, "23.1.1-1", "e" * 64, _AT)
    assert bs.is_stale(r, None, "f" * 64, _AT)


def test_same_version_rebuild_is_stale():
    """3.4.0-B18: a toolchain rebuild with a fresh profile keeps pkgver-pkgrel,
    so the build's own timestamp is what tells the old ratio apart."""
    assert bs.is_stale(_r(), "23.1.1-1", "f" * 64, "2026-10-12T09:00:00Z")
    assert bs.is_stale(_r(), "23.1.1-1", "f" * 64, None)


def test_result_without_built_at_loads_as_stale(tmp_path):
    """A bench.toml written before the field existed loads, and reads as stale."""
    bs.save(tmp_path, "toolchain", _r())
    path = tmp_path / bs.FILENAME
    path.write_text("".join(
        line for line in path.read_text().splitlines(keepends=True)
        if not line.startswith("built_at")))
    old = bs.load(tmp_path, "toolchain")
    assert old is not None and old.ratio == 1.196
    assert bs.is_stale(old, "23.1.1-1", "f" * 64, _AT)


def test_save_never_escalates_on_an_unwritable_dir(tmp_path, monkeypatch):
    """bench is read-only: a save it cannot make warns, it never asks for sudo."""
    import os

    import pytest

    from sysforge.primitives import atomic_write

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    calls = []
    monkeypatch.setattr(atomic_write, "run_privileged",
                        lambda *a, **k: calls.append(a))
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(OSError):
            bs.save(locked, "toolchain", _r())
    finally:
        locked.chmod(0o700)
    assert calls == []
    assert not (locked / bs.FILENAME).exists()


def test_load_rejects_wrongly_typed_fields(tmp_path):
    (tmp_path / bs.FILENAME).write_text(
        '[toolchain]\ninstalled_version = "1"\nstock_version = "1"\ncorpus = "c"\n'
        'fingerprint = "f"\nruns = 5\nstock_median_s = 1.0\ninstalled_median_s = 1.0\n'
        'ratio = "x"\nmeasured_at = "t"\n')
    assert bs.load(tmp_path, "toolchain") is None
    (tmp_path / bs.FILENAME).write_text(
        '[toolchain]\ninstalled_version = "1"\nstock_version = "1"\ncorpus = "c"\n'
        'fingerprint = "f"\nruns = "five"\nstock_median_s = 1.0\ninstalled_median_s = 1\n'
        'measured_at = "t"\n')
    assert bs.load(tmp_path, "toolchain") is None
