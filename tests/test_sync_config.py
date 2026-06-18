"""
test_sync_config.py — tests for tools/sync_config.py.

sync_config performs an add-only, comment-preserving merge of shipped config
defaults into a live config dir, plus a pacnew-style ``.sfnew`` companion for
comment/example drift the key-anchored merge can't carry.

tomlkit is a dev-only dependency (ephemeral uv overlay for `make sync-config`),
so these tests ``importorskip`` it — they run under
``uv run --with tomlkit pytest`` and skip under a plain `make test`.
"""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("tomlkit")

REPO = Path(__file__).resolve().parent.parent
_SCRIPT = REPO / "tools/sync_config.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sync_config", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sync_config = _load_module()
sync_file = sync_config.sync_file
_comment_signature = sync_config._comment_signature


def _sfnew(target: Path) -> Path:
    return target.with_name(target.name + ".sfnew")


# ---------------------------------------------------------------------------
# Key-anchored merge
# ---------------------------------------------------------------------------

def test_new_key_in_existing_table_syncs_with_leading_comment(tmp_path):
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text(
        "[ui]\n"
        "color = true\n"
        "# new option added upstream\n"
        "pager = true\n",
        encoding="utf-8",
    )
    target.write_text("[ui]\ncolor = true\n", encoding="utf-8")

    status, added, sfnew = sync_file(shipped, target, dry_run=False)

    assert status == "updated"
    assert "ui.pager" in added
    out = target.read_text(encoding="utf-8")
    assert "pager = true" in out
    assert "# new option added upstream" in out  # leading comment travelled
    # The injected key's comment is now present → no residual comment drift.
    assert sfnew is None
    assert not _sfnew(target).exists()


def test_existing_value_never_overwritten(tmp_path):
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text("[ui]\ncolor = true\n", encoding="utf-8")
    target.write_text("[ui]\ncolor = false\n", encoding="utf-8")

    status, added, _ = sync_file(shipped, target, dry_run=False)

    assert status == "up to date"
    assert added == []
    assert "color = false" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# .sfnew companion for comment / commented-example drift
# ---------------------------------------------------------------------------

def test_sfnew_written_for_commented_example_drift(tmp_path):
    """A commented-out example setting has no key to anchor to, so the merge
    reports 'up to date' but the .sfnew companion surfaces the drift."""
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text(
        "[kernel]\n"
        "enabled = true\n"
        "# interactive = true   # default — runs make nconfig\n",
        encoding="utf-8",
    )
    target.write_text("[kernel]\nenabled = true\n", encoding="utf-8")

    status, added, sfnew = sync_file(shipped, target, dry_run=False)

    assert status == "up to date"
    assert added == []
    assert sfnew == _sfnew(target)
    assert _sfnew(target).read_text(encoding="utf-8") == shipped.read_text(encoding="utf-8")
    # The live file itself is untouched.
    assert "interactive" not in target.read_text(encoding="utf-8")


def test_no_sfnew_when_comments_match(tmp_path):
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    body = "# header doc\n[ui]\ncolor = true\n"
    shipped.write_text(body, encoding="utf-8")
    target.write_text(body, encoding="utf-8")

    status, added, sfnew = sync_file(shipped, target, dry_run=False)

    assert status == "up to date"
    assert sfnew is None
    assert not _sfnew(target).exists()


def test_dry_run_writes_nothing(tmp_path):
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text(
        "[kernel]\nenabled = true\n# interactive = true\n", encoding="utf-8")
    original = "[kernel]\nenabled = true\n"
    target.write_text(original, encoding="utf-8")

    status, _added, sfnew = sync_file(shipped, target, dry_run=True)

    assert status == "up to date"
    assert sfnew == _sfnew(target)          # reported …
    assert not _sfnew(target).exists()       # … but not written
    assert target.read_text(encoding="utf-8") == original


def test_stale_sfnew_removed_when_drift_resolved(tmp_path):
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text(
        "[kernel]\nenabled = true\n# interactive = true\n", encoding="utf-8")
    target.write_text("[kernel]\nenabled = true\n", encoding="utf-8")

    # First run: drift → .sfnew written.
    _, _, sfnew = sync_file(shipped, target, dry_run=False)
    assert sfnew is not None and _sfnew(target).exists()

    # Operator adopts the comment; second run should clean up the companion.
    target.write_text(shipped.read_text(encoding="utf-8"), encoding="utf-8")
    _, _, sfnew2 = sync_file(shipped, target, dry_run=False)
    assert sfnew2 is None
    assert not _sfnew(target).exists()


def test_created_path_copies_wholesale_no_sfnew(tmp_path):
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "sub" / "live.toml"
    shipped.write_text("[ui]\ncolor = true\n# example\n", encoding="utf-8")

    status, added, sfnew = sync_file(shipped, target, dry_run=False)

    assert status == "created"
    assert added == []
    assert sfnew is None
    assert target.read_text(encoding="utf-8") == shipped.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _comment_signature
# ---------------------------------------------------------------------------

def test_comment_signature_normalizes_and_ignores_non_comments():
    sig = _comment_signature(
        "# alpha\n"
        "key = 1\n"
        "#   beta  \n"
        "value = 2  # inline comment, not a full-line comment\n"
    )
    assert sig == {"alpha", "beta"}


def test_default_target_uses_config_dir_directly(monkeypatch):
    monkeypatch.setenv("SYSFORGE_CONFIG_DIR", "/tmp/sf-cfg")
    assert sync_config._default_target() == Path("/tmp/sf-cfg")
    monkeypatch.delenv("SYSFORGE_CONFIG_DIR", raising=False)
    assert sync_config._default_target() == Path("/etc/sysforge")
