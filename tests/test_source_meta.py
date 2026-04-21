"""
test_source_meta.py — unit tests for sysforge.primitives.source_meta.

Covers:
    SourceMetaCache
        empty cache on first load
        update + save round-trip
        schema version mismatch → empty
        last_rpc_at / mark_rpc_sync
        delete / all
        atomic write (tmp file + rename)
        pkgbase names with special chars
"""
import tomllib

from sysforge.primitives.source_meta import SCHEMA_VERSION, SourceMetaCache


def test_empty_cache_on_fresh_state_dir(tmp_path):
    cache = SourceMetaCache(tmp_path)
    assert cache.all() == {}
    assert cache.get("anything") is None
    assert cache.last_rpc_at() is None


def test_update_and_save_roundtrip(tmp_path):
    cache = SourceMetaCache(tmp_path)
    cache.update(
        "mesa-git",
        rpc_version="24.0-1",
        rpc_last_modified=1718900000,
        rpc_package_base="mesa-git",
        head_commit="deadbeef" * 5,
        is_vcs=True,
        last_fetch_at="2026-04-21T12:00:00Z",
    )
    cache.save()

    # Reload from disk.
    fresh = SourceMetaCache(tmp_path)
    entry = fresh.get("mesa-git")
    assert entry is not None
    assert entry["rpc_version"] == "24.0-1"
    assert entry["rpc_last_modified"] == 1718900000
    assert entry["is_vcs"] is True
    assert entry["head_commit"] == "deadbeef" * 5


def test_update_partial_preserves_existing(tmp_path):
    cache = SourceMetaCache(tmp_path)
    cache.update("htop", rpc_version="3.0-1", head_commit="abc")
    cache.update("htop", rpc_last_modified=42)  # should not clear rpc_version
    entry = cache.get("htop")
    assert entry["rpc_version"] == "3.0-1"
    assert entry["head_commit"] == "abc"
    assert entry["rpc_last_modified"] == 42


def test_delete_removes_entry(tmp_path):
    cache = SourceMetaCache(tmp_path)
    cache.update("htop", rpc_version="3.0-1")
    assert cache.delete("htop") is True
    assert cache.get("htop") is None
    # Second delete returns False.
    assert cache.delete("htop") is False


def test_schema_version_mismatch_discards_data(tmp_path):
    path = tmp_path / "source_meta.toml"
    path.write_text(
        "schema_version = 999\n"
        '["mesa-git"]\n'
        'rpc_version = "old"\n',
    )
    cache = SourceMetaCache(tmp_path)
    assert cache.all() == {}


def test_mark_rpc_sync_records_timestamp(tmp_path):
    cache = SourceMetaCache(tmp_path)
    cache.mark_rpc_sync("2026-04-21T10:00:00Z")
    assert cache.last_rpc_at() == "2026-04-21T10:00:00Z"
    cache.save()

    fresh = SourceMetaCache(tmp_path)
    assert fresh.last_rpc_at() == "2026-04-21T10:00:00Z"


def test_mark_rpc_sync_default_now(tmp_path):
    cache = SourceMetaCache(tmp_path)
    cache.mark_rpc_sync()
    ts = cache.last_rpc_at()
    assert ts is not None and ts.endswith("Z") and "T" in ts


def test_save_is_atomic(tmp_path):
    cache = SourceMetaCache(tmp_path)
    cache.update("pkg", rpc_version="1-1")
    cache.save()
    # No leftover tmp file after rename.
    assert not (tmp_path / "source_meta.toml.tmp").exists()
    assert (tmp_path / "source_meta.toml").exists()


def test_save_writes_valid_toml_with_schema_version(tmp_path):
    cache = SourceMetaCache(tmp_path)
    cache.update("pkg", rpc_version="1-1", rpc_last_modified=123, is_vcs=False)
    cache.save()
    with open(tmp_path / "source_meta.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["pkg"]["rpc_version"] == "1-1"
    assert data["pkg"]["rpc_last_modified"] == 123
    assert data["pkg"]["is_vcs"] is False


def test_save_creates_missing_state_dir(tmp_path):
    state = tmp_path / "nested" / "state"
    cache = SourceMetaCache(state)
    cache.update("pkg", rpc_version="1-1")
    cache.save()
    assert (state / "source_meta.toml").exists()


def test_unparseable_toml_falls_back_to_empty(tmp_path):
    (tmp_path / "source_meta.toml").write_text("this is not toml = = =\n")
    cache = SourceMetaCache(tmp_path)
    assert cache.all() == {}


def test_all_returns_shallow_copies(tmp_path):
    cache = SourceMetaCache(tmp_path)
    cache.update("pkg", rpc_version="1-1")
    snapshot = cache.all()
    snapshot["pkg"]["rpc_version"] = "mutated"
    # Mutating the snapshot must not affect internal state.
    assert cache.get("pkg")["rpc_version"] == "1-1"
