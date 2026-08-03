"""Tests for the post-build change summary primitive (2.6.1-F24)."""

from pathlib import Path

import pytest

from sysforge import log
from sysforge.primitives import change_report
from sysforge.primitives.change_report import ChangeRow, PkgFacts


def _facts(version, isize=None):
    return PkgFacts(version=version, isize=isize)


def test_diff_reports_changed_added_and_removed():
    before = {"a": _facts("1-1"), "gone": _facts("2-1")}
    after = {"a": _facts("1-2"), "new": _facts("3-1")}

    rows = change_report.diff(before, after)
    by_name = {r.name: r for r in rows}

    assert by_name["a"].old == _facts("1-1")
    assert by_name["a"].new == _facts("1-2")
    assert by_name["new"].old is None
    assert by_name["gone"].new is None


def test_diff_orders_changed_then_added_then_removed():
    before = {"z-changed": _facts("1-1"), "a-removed": _facts("1-1")}
    after = {"z-changed": _facts("1-2"), "m-added": _facts("1-1")}

    assert [r.name for r in change_report.diff(before, after)] == [
        "z-changed", "m-added", "a-removed",
    ]


def test_diff_ignores_untouched_packages():
    same = {"stable": _facts("1-1", isize=100)}
    assert change_report.diff(same, dict(same)) == []


def test_diff_reports_size_change_at_equal_version():
    """A pkgrel-neutral rebuild that changes installed size is still a change."""
    before = {"p": _facts("1-1", isize=1000)}
    after = {"p": _facts("1-1", isize=400)}

    rows = change_report.diff(before, after)
    assert len(rows) == 1
    assert rows[0].old.isize == 1000
    assert rows[0].new.isize == 400


def test_snapshot_maps_pacman_facts(monkeypatch):
    monkeypatch.setattr(
        change_report.pacman,
        "get_installed_facts",
        lambda root=None: {"a": ("1-1", 2048), "b": ("2-1", None)},
    )
    snap = change_report.snapshot()
    assert snap == {"a": _facts("1-1", 2048), "b": _facts("2-1", None)}


def test_snapshot_propagates_failure_as_snapshot_error(monkeypatch):
    def boom(root=None):
        raise OSError("pacman unavailable")

    monkeypatch.setattr(change_report.pacman, "get_installed_facts", boom)
    with pytest.raises(change_report.SnapshotError):
        change_report.snapshot()


def test_snapshot_wraps_non_none_root_not_implemented_as_snapshot_error():
    """snapshot() catches broad Exception, so pacman.get_installed_facts's
    NotImplementedError guard for a non-None root surfaces as SnapshotError
    like any other read failure -- callers only need to handle one error
    type regardless of why the read failed."""
    with pytest.raises(change_report.SnapshotError):
        change_report.snapshot(root=Path("/mnt/target"))


from sysforge.primitives.change_report import ChangeOutcome

_ROW = change_report.ChangeRow(name="p", old=PkgFacts("1-1"), new=PkgFacts("1-2"))


@pytest.mark.parametrize(
    "rows,stage_failed,unavailable,expected",
    [
        ([_ROW], False, None, ChangeOutcome.COMPLETE),
        ([], False, None, ChangeOutcome.NO_CHANGES),
        ([_ROW], True, None, ChangeOutcome.PARTIAL),
        ([], True, None, ChangeOutcome.NONE_APPLIED),
        ([], False, "pacman unavailable", ChangeOutcome.UNKNOWN),
        ([_ROW], True, "pacman unavailable", ChangeOutcome.UNKNOWN),
    ],
)
def test_classify_covers_every_outcome(rows, stage_failed, unavailable, expected):
    assert change_report.classify(
        rows, stage_failed=stage_failed, unavailable=unavailable
    ) is expected


def test_unavailable_never_collapses_to_no_changes():
    """Silence and 'nothing changed' must never be confusable."""
    assert change_report.classify(
        [], stage_failed=False, unavailable="target root not resolvable"
    ) is ChangeOutcome.UNKNOWN


def _render(rows, outcome, **kw):
    lines = []
    change_report.render(rows, stage="kernel", outcome=outcome, emit=lines.append, **kw)
    return lines


def test_render_groups_updated_added_removed():
    rows = [
        ChangeRow("linux-custom", PkgFacts("6.15.4-1"), PkgFacts("6.15.5-1")),
        ChangeRow("linux-headers", None, PkgFacts("6.15.5-1")),
        ChangeRow("old-pkg", PkgFacts("1-1"), None),
    ]
    out = "\n".join(_render(rows, ChangeOutcome.COMPLETE))

    assert "kernel stage changes: 1 updated, 1 added, 1 removed." in out.lower()
    assert "Updated:" in out and "Added:" in out and "Removed:" in out
    assert "6.15.4-1" in out and "6.15.5-1" in out
    assert "linux-headers" in out


def test_render_includes_size_column_and_delta():
    rows = [ChangeRow("p", PkgFacts("1-1", 1024 * 1024), PkgFacts("1-2", 3 * 1024 * 1024))]
    out = "\n".join(_render(rows, ChangeOutcome.COMPLETE))
    assert "1.0 MiB" in out and "3.0 MiB" in out
    assert "+2.0 MiB" in out


def test_render_omits_size_column_when_no_row_has_size():
    rows = [ChangeRow("p", PkgFacts("1-1"), PkgFacts("1-2"))]
    out = "\n".join(_render(rows, ChangeOutcome.COMPLETE))
    assert "MiB" not in out and "KiB" not in out
    assert " B" not in out


def test_render_states_no_changes_rather_than_staying_silent():
    out = "\n".join(_render([], ChangeOutcome.NO_CHANGES))
    assert out.strip()
    assert "no package changes" in out.lower()


def test_render_partial_names_the_mixed_state():
    rows = [ChangeRow("p", PkgFacts("1-1"), PkgFacts("1-2"))]
    out = "\n".join(_render(rows, ChangeOutcome.PARTIAL))
    assert "FAILED" in out
    assert "after applying changes" in out.lower()


def test_render_none_applied_says_system_unchanged():
    out = "\n".join(_render([], ChangeOutcome.NONE_APPLIED))
    assert "system unchanged" in out.lower()


def test_render_unknown_states_its_reason():
    out = "\n".join(_render([], ChangeOutcome.UNKNOWN, reason="pacman unavailable"))
    assert "unavailable" in out.lower()
    assert "pacman unavailable" in out


def test_render_appends_extra_blocks_below_version_rows():
    rows = [ChangeRow("p", PkgFacts("1-1"), PkgFacts("1-2"))]
    extras = [change_report.ExtraBlock(label="Kconfig changes:", lines=["CONFIG_X  n -> y"])]
    lines = _render(rows, ChangeOutcome.COMPLETE, extras=extras)
    body = "\n".join(lines)
    assert "Kconfig changes:" in body
    assert "CONFIG_X" in body
    assert lines.index("  Kconfig changes:") > lines.index("  Updated:")


def test_render_extra_blocks_render_even_with_no_version_rows():
    """A stage can have nothing to install and still have something to report."""
    extras = [change_report.ExtraBlock(label="Kconfig changes:", lines=["CONFIG_X  n -> y"])]
    out = "\n".join(_render([], ChangeOutcome.NO_CHANGES, extras=extras))
    assert "CONFIG_X" in out


def test_render_none_applied_degrades_the_em_dash_under_the_ascii_gate():
    """The NONE_APPLIED header must route its em-dash through the glyph
    downgrade like every other renderer, not hardcode the Unicode form."""
    log.set_unicode_mode("never")
    try:
        out = "\n".join(_render([], ChangeOutcome.NONE_APPLIED))
    finally:
        log.set_unicode_mode("auto")

    assert "—" not in out
    assert "system unchanged" in out.lower()
