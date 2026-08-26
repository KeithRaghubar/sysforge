"""
test_sync_config.py — tests for tools/sync_config.py.

sync_config performs an add-only, comment-preserving merge of shipped config
defaults into a live config dir, plus a pacnew-style ``.sfnew`` companion for
comment/example drift the key-anchored merge can't carry.

tomlkit is a dev-only dependency (ephemeral uv overlay for `make sync-config`),
so these tests ``importorskip`` it. `make coverage` layers tomlkit into its
overlay and so always runs them; a plain `make test` uses the system pytest and
runs them only if python-tomlkit happens to be installed there (2.6.1-B23).
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
_active_signature = sync_config._active_signature


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


def test_sfnew_written_when_live_comments_out_a_shipped_live_header(tmp_path):
    """A section header active in shipped but commented out in live is drift.

    Regression for a live packages.toml carrying a pre-3.0.0-STD1 vintage where
    ``[build]`` was still commented. The add-only, key-anchored merge cannot flip
    an existing line from commented to active, and the comment-signature
    subtraction is one-directional (it only sees comments shipped has that live
    lacks), so the stale ``# [build]`` was invisible — every key beneath it was
    silently reassigned to the previous table or to the top level, disabling
    ``repo_mode`` with no warning.
    """
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text("[build]\nrepo_mode = \"pacman\"\n", encoding="utf-8")
    target.write_text("# [build]\nrepo_mode = \"build_from_source\"\n", encoding="utf-8")

    _status, _added, sfnew = sync_file(shipped, target, dry_run=False)

    assert sfnew == _sfnew(target), "commented-out shipped-live header must surface as drift"
    # add-only guarantee: the live file is never rewritten to uncomment it.
    assert target.read_text(encoding="utf-8").startswith("# [build]")


def test_commented_header_never_gets_a_duplicate_shipped_default_table(tmp_path):
    """The merge must not append a second copy of a table whose live header is
    commented out. tomlkit reads the orphaned keys as top-level, so the table
    looks absent and the shipped default would be injected below the operator's
    own value — not an overwrite textually, but it supersedes it on reparse.
    """
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text("[build]\nrepo_mode = \"pacman\"\n", encoding="utf-8")
    target.write_text("# [build]\nrepo_mode = \"build_from_source\"\n", encoding="utf-8")

    status, added, _sfnew_path = sync_file(shipped, target, dry_run=False)

    out = target.read_text(encoding="utf-8")
    assert out.count("[build]") == 1, "shipped default table was injected as a duplicate"
    assert "pacman" not in out, "operator's repo_mode was superseded by the shipped default"
    assert "build_from_source" in out
    assert added == [], "nothing may be reported as merged when the write is skipped"
    assert status == "needs merge"


def test_commented_header_drift_detected_for_array_of_tables(tmp_path):
    """The check covers ``[[aot]]`` headers, not just ``[table]``."""
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text("[[rules]]\nname = \"a\"\n", encoding="utf-8")
    target.write_text("# [[rules]]\nname = \"a\"\n", encoding="utf-8")

    _status, _added, sfnew = sync_file(shipped, target, dry_run=False)

    assert sfnew == _sfnew(target)


def test_no_drift_when_shipped_header_is_itself_a_commented_example(tmp_path):
    """A header commented out in *shipped* is an example block, not a live
    section — live keeping it commented is correct and must not spill a .sfnew."""
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text(
        "[build]\nrepo_mode = \"pacman\"\n\n#[group.cosmic]\n#packages = []\n",
        encoding="utf-8",
    )
    target.write_text(
        "[build]\nrepo_mode = \"pacman\"\n\n#[group.cosmic]\n#packages = []\n",
        encoding="utf-8",
    )

    _status, _added, sfnew = sync_file(shipped, target, dry_run=False)

    assert sfnew is None


def test_no_sfnew_when_commented_example_adopted_uncommented(tmp_path):
    """A commented example the live file has already adopted by uncommenting it
    into an identical active key is not drift — no .sfnew should be written."""
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text(
        "[kernel]\n"
        "enabled = true\n"
        "# interactive = true\n",
        encoding="utf-8",
    )
    # Live file already carries `interactive` uncommented as an active key.
    target.write_text(
        "[kernel]\nenabled = true\ninteractive = true\n", encoding="utf-8")

    status, added, sfnew = sync_file(shipped, target, dry_run=False)

    assert status == "up to date"
    assert added == []
    assert sfnew is None
    assert not _sfnew(target).exists()
    # A *differing* value is still genuine drift → .sfnew reappears.
    target.write_text(
        "[kernel]\nenabled = true\ninteractive = false\n", encoding="utf-8")
    _, _, sfnew2 = sync_file(shipped, target, dry_run=False)
    assert sfnew2 == _sfnew(target)


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


def test_active_signature_collects_uncommented_lines():
    sig = _active_signature(
        "# doc comment\n"
        "[ui]\n"
        "  color = true  \n"
        "\n"
        "# interactive = true\n"
    )
    # Active (non-comment, non-blank) lines, strip-normalized; comments excluded.
    assert sig == {"[ui]", "color = true"}


def test_default_target_uses_config_dir_directly(monkeypatch):
    monkeypatch.setenv("SYSFORGE_CONFIG_DIR", "/tmp/sf-cfg")
    assert sync_config._default_target() == Path("/tmp/sf-cfg")
    monkeypatch.delenv("SYSFORGE_CONFIG_DIR", raising=False)
    assert sync_config._default_target() == Path("/etc/sysforge")


# ---------------------------------------------------------------------------
# Shipped section order (3.1.0-B9)
# ---------------------------------------------------------------------------

def _headers(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("[")]


def test_injected_table_lands_in_shipped_position_not_at_eof(tmp_path):
    """A new table is spliced after its shipped-order predecessor.

    Appending at EOF leaves the live file in a different section order than
    shipped. TOML ignores order, so the config still resolves — but every later
    ``.sfnew`` diff then renders the relocated section as a delete-plus-add far
    apart, which reads as "this section is missing" and invites the operator to
    hand-merge it away for real.
    """
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text(
        "[alpha]\na = 1\n\n[beta]\nb = 2\n\n[gamma]\ng = 3\n", encoding="utf-8")
    target.write_text("[alpha]\na = 1\n\n[gamma]\ng = 3\n", encoding="utf-8")

    status, added, _ = sync_file(shipped, target, dry_run=False)

    assert status == "updated"
    assert added == ["beta"]
    assert _headers(target.read_text(encoding="utf-8")) == ["[alpha]", "[beta]", "[gamma]"]


def test_injected_trailing_table_still_appends(tmp_path):
    """A table with no shipped successor in live keeps the EOF placement."""
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text("[alpha]\na = 1\n\n[omega]\no = 9\n", encoding="utf-8")
    target.write_text("[alpha]\na = 1\n", encoding="utf-8")

    sync_file(shipped, target, dry_run=False)

    assert _headers(target.read_text(encoding="utf-8")) == ["[alpha]", "[omega]"]


def test_injected_table_order_survives_live_reordering(tmp_path):
    """Live may already be reordered; the new table anchors to a real neighbour.

    The predecessor search must fall back through the shipped order until it
    finds a section live actually has, rather than assuming shipped positions
    map onto live ones.
    """
    shipped = tmp_path / "ship.toml"
    target = tmp_path / "live.toml"
    shipped.write_text(
        "[alpha]\na = 1\n\n[beta]\nb = 2\n\n[gamma]\ng = 3\n\n[delta]\nd = 4\n",
        encoding="utf-8")
    # live keeps gamma before alpha — an older append-at-EOF sync's legacy.
    target.write_text("[gamma]\ng = 3\n\n[alpha]\na = 1\n", encoding="utf-8")

    sync_file(shipped, target, dry_run=False)

    out = _headers(target.read_text(encoding="utf-8"))
    # beta anchors after alpha (its nearest shipped predecessor present in live)
    assert out.index("[beta]") == out.index("[alpha]") + 1
    assert "[delta]" in out
