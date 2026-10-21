# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
Concept pages explain the technique, not sysforge (3.5.0-DEV16).

The rule in CLAUDE.md: no sysforge vocabulary outside the closing "In sysforge"
section, and upstream sources cited. It was review-only; this pins the parts a
test can see. Upstream tools' own flags (makepkg's --nocheck, the linker's
--emit-relocs) are the technique and stay allowed. The glossary defines both
kinds of term on purpose and is exempt.
"""
import re
from pathlib import Path

import pytest

CONCEPTS = Path(__file__).resolve().parent.parent / "docs/concepts"
PAGES = sorted(p for p in CONCEPTS.glob("*.md") if p.name != "glossary.md")

# sysforge's own vocabulary: its name, its config files, its state file.
_SYSFORGE_TERMS = re.compile(
    r"\bsysforge\b|\b(?:profiles|toolchain|kernel|packages|bootstrap)\.toml\b|\bbuild_state\b",
    re.IGNORECASE)


def _sections(text):
    return re.findall(r"^## (.+)$", text, re.MULTILINE)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_page_closes_with_further_reading_then_in_sysforge(page):
    assert _sections(page.read_text(encoding="utf-8"))[-2:] == ["Further reading", "In sysforge"]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_further_reading_cites_upstream_sources(page):
    text = page.read_text(encoding="utf-8")
    further = text.split("\n## Further reading", 1)[1].split("\n## In sysforge", 1)[0]
    assert re.search(r"\]\(https?://", further), "cite at least one upstream source"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_sysforge_vocabulary_before_in_sysforge(page):
    text = page.read_text(encoding="utf-8")
    body = text.split("\n## In sysforge", 1)[0]
    # The page title may name the guide it belongs beside; judge the body.
    body = body.split("\n", 1)[1] if body.startswith("# ") else body
    bad = [f"{page.name}:{n}: {line.strip()[:80]}"
           for n, line in enumerate(body.splitlines(), 2) if _SYSFORGE_TERMS.search(line)]
    assert not bad, "sysforge terms belong in the 'In sysforge' section:\n  " + "\n  ".join(bad)


def test_the_check_sees_a_leak():
    assert _SYSFORGE_TERMS.search("set it in profiles.toml")
    assert _SYSFORGE_TERMS.search("sysforge rebuilds it")
    assert not _SYSFORGE_TERMS.search("makepkg --nocheck skips check()")
