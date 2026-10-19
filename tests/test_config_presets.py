"""The preset table names real shipped keys with their real defaults (3.5.0-F9)."""
import re
from pathlib import Path

import pytest

from sysforge.primitives import config_presets as cp
from sysforge.primitives.config import toml_literal

_ETC = Path(__file__).resolve().parent.parent / "etc" / "sysforge"
_ROWS = [(p.name, r) for p in cp.PRESETS.values() for r in p.rows]


def _shipped_literal(target, section, key):
    """The value a shipped file gives ``key`` (active or commented example);
    ``section=None`` = above the first table header."""
    cur = None
    pat = re.compile(rf"^#?\s*{re.escape(key)}\s*=\s*(\S+)")
    for line in (_ETC / cp.TARGET_FILES[target]).read_text().splitlines():
        h = re.match(r"^\[([^\]]+)\]", line)
        if h:
            cur = h.group(1)
            continue
        m = pat.match(line)
        if m and cur == section:
            return m.group(1)
    return None


@pytest.mark.parametrize("preset,row", _ROWS, ids=[f"{p}:{r.key}" for p, r in _ROWS])
def test_row_matches_a_shipped_key_and_default(preset, row):
    lit = _shipped_literal(row.target, row.section, row.key)
    assert lit is not None, f"{row.key} not in shipped {row.target}"
    assert lit == toml_literal(row.default)


def test_names_and_order():
    assert cp.PRESET_NAMES == ("defaults", "performance", "low-friction", "secure")
    assert cp.PRESETS["defaults"].rows == ()


def test_menu_letters_are_unique():
    assert len({n[0] for n in cp.PRESET_NAMES}) == len(cp.PRESET_NAMES)


def test_never_touches_stage_enable_or_checksum():
    keys = {(r.target, r.section, r.key) for _, r in _ROWS}
    assert ("toolchain", None, "enabled") not in keys
    assert ("kernel", None, "enabled") not in keys
    assert not any(k == "checksum_mismatch" for _, _, k in keys)


def test_low_friction_turns_review_off():
    rows = {r.key: r.value for r in cp.PRESETS["low-friction"].rows}
    assert rows["review"] is False


def test_no_em_dashes_in_user_text():
    for p in cp.PRESETS.values():
        for text in (p.summary, *p.notes):
            assert "—" not in text
    assert "—" not in cp.LOW_FRICTION_REVIEW_WARNING


def test_completions_list_exactly_the_preset_names():
    root = Path(__file__).resolve().parent.parent / "completions"
    want = " ".join(cp.PRESET_NAMES)
    assert f"preset:({want})" in (root / "_sysforge").read_text()
    assert f'compgen -W "{want}"' in (root / "sysforge.bash").read_text()


def test_low_friction_does_not_touch_the_sandbox():
    """The sandbox adds no prompts, so "fewest prompts" leaves it alone (3.5.0-B103)."""
    keys = {r.key for r in cp.PRESETS["low-friction"].rows}
    assert "sandbox_builds" not in keys
