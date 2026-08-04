# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_kconfig_history.py — the resolved-.config archive behind 2.6.1-F25.

Covers the write/prune/read cycle plus the best-effort contract: nothing in
this module may raise into a kernel build.
"""
import gzip

from sysforge.primitives import kconfig_history


def _write_config(path, **symbols):
    lines = []
    for key, val in symbols.items():
        lines.append(f"CONFIG_{key}=n" if val == "n" else f"CONFIG_{key}={val}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_archive_writes_a_gzipped_copy(tmp_path):
    src = _write_config(tmp_path / ".config", SMP="y")
    dest = kconfig_history.archive(tmp_path / "state", "linux-custom", "6.15.0", src)

    assert dest is not None
    assert dest.name == "linux-custom-6.15.0.config.gz"
    with gzip.open(dest, "rt", encoding="utf-8") as fh:
        assert "CONFIG_SMP=y" in fh.read()


def test_archive_prunes_to_the_newest_keep(tmp_path):
    """Only the newest KEEP archives per pkgname survive a write."""
    src = _write_config(tmp_path / ".config", SMP="y")
    state = tmp_path / "state"
    for n in range(8):
        kconfig_history.archive(state, "linux-custom", f"6.15.{n}", src, keep=3)

    remaining = sorted(p.name for p in kconfig_history.history_dir(state).iterdir())
    assert len(remaining) == 3


def test_prune_is_scoped_per_pkgname(tmp_path):
    """A second kernel's history does not evict the first kernel's."""
    src = _write_config(tmp_path / ".config", SMP="y")
    state = tmp_path / "state"
    kconfig_history.archive(state, "linux-a", "1.0", src, keep=1)
    for n in range(4):
        kconfig_history.archive(state, "linux-b", f"2.{n}", src, keep=1)

    names = {p.name for p in kconfig_history.history_dir(state).iterdir()}
    assert "linux-a-1.0.config.gz" in names
    assert len(names) == 2


def test_previous_returns_the_newest_prior_archive(tmp_path):
    state = tmp_path / "state"
    kconfig_history.archive(
        state, "linux-custom", "6.14.0",
        _write_config(tmp_path / "old", SMP="y", NUMA="n"),
    )
    kconfig_history.archive(
        state, "linux-custom", "6.15.0",
        _write_config(tmp_path / "new", SMP="y", NUMA="y"),
    )

    result = kconfig_history.previous(state, "linux-custom", exclude_release="6.15.0")
    assert result is not None
    release, parsed = result
    assert release == "6.14.0"
    assert parsed["CONFIG_NUMA"] == "n"


def test_previous_is_none_on_a_first_build(tmp_path):
    """Nothing archived yet means nothing to compare — not an empty diff."""
    assert kconfig_history.previous(tmp_path / "state", "linux-custom") is None


def test_previous_skips_a_corrupt_archive(tmp_path):
    """A truncated gz is stepped over, not raised through."""
    state = tmp_path / "state"
    kconfig_history.archive(
        state, "linux-custom", "6.14.0", _write_config(tmp_path / "ok", SMP="y")
    )
    bad = kconfig_history.archive_path(state, "linux-custom", "6.15.0")
    bad.write_bytes(b"not gzip at all")

    result = kconfig_history.previous(state, "linux-custom")
    assert result is not None
    assert result[0] == "6.14.0"


def test_archive_returns_none_when_the_source_is_missing(tmp_path):
    """A missing build tree degrades to None rather than raising."""
    assert kconfig_history.archive(
        tmp_path / "state", "linux-custom", "6.15.0", tmp_path / "nope"
    ) is None


def test_archive_returns_none_when_the_destination_is_unwritable(tmp_path):
    """A state dir that is a file, not a directory, must not raise."""
    src = _write_config(tmp_path / ".config", SMP="y")
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file")
    assert kconfig_history.archive(blocked, "linux-custom", "6.15.0", src) is None


def test_release_and_pkgname_are_sanitized_into_the_filename(tmp_path):
    """Path separators in a release string never escape the history dir."""
    src = _write_config(tmp_path / ".config", SMP="y")
    state = tmp_path / "state"
    dest = kconfig_history.archive(state, "linux/custom", "../../6.15", src)

    assert dest is not None
    assert dest.parent == kconfig_history.history_dir(state)
    assert "/" not in dest.name.removesuffix(".config.gz")


def test_rebuilding_the_same_release_overwrites_rather_than_accumulates(tmp_path):
    state = tmp_path / "state"
    kconfig_history.archive(
        state, "linux-custom", "6.15.0", _write_config(tmp_path / "a", SMP="y")
    )
    kconfig_history.archive(
        state, "linux-custom", "6.15.0", _write_config(tmp_path / "b", SMP="n")
    )

    entries = list(kconfig_history.history_dir(state).iterdir())
    assert len(entries) == 1
    with gzip.open(entries[0], "rt", encoding="utf-8") as fh:
        assert "CONFIG_SMP=n" in fh.read()
