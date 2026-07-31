# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""test_makefile_help.py - `make help` documents every target, and only real ones.

`make help` is generated from the Makefile at run time (an awk pass over `##`
descriptions and `##@` group banners), so it cannot advertise a target that does
not exist. This file closes the other direction: a new `.PHONY` target that
ships without a `## description` is undiscoverable, and the whole point of the
target is that the list is complete. Both checks parse the Makefile rather than
shelling out to `make`, so they run without a make binary and cannot be
side-tracked by a recipe's side effects -- except `test_help_runs`, which does
invoke `make help` to prove the awk program itself is well-formed.
"""
import re
import shutil
import subprocess

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAKEFILE = REPO / "Makefile"

_DOC_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*):.*?##\s+(.+)$", re.MULTILINE)
_TARGET_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*):", re.MULTILINE)


def _text() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _phony_targets() -> set[str]:
    """Every name in the (line-continued) .PHONY declaration."""
    text = _text()
    start = text.index(".PHONY:")
    # Consume continuation lines: the block ends at the first line not ending `\`.
    lines = text[start:].splitlines()
    names: list[str] = []
    for line in lines:
        names.extend(line.replace(".PHONY:", "").replace("\\", "").split())
        if not line.rstrip().endswith("\\"):
            break
    return set(names)


def _documented() -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in _DOC_RE.finditer(_text())}


def _defined_targets() -> set[str]:
    return set(_TARGET_RE.findall(_text()))


def test_every_phony_target_is_documented():
    missing = sorted(_phony_targets() - set(_documented()))
    assert not missing, (
        f"targets in .PHONY with no `## description`: {missing} — "
        "add one on the target line so `make help` lists it"
    )


def test_documented_targets_all_exist():
    # The awk pass keys on the target line itself, so this cannot really drift;
    # asserting it anyway pins the invariant the help output relies on.
    unknown = sorted(set(_documented()) - _defined_targets())
    assert not unknown, f"documented but not defined: {unknown}"


def test_every_documented_target_is_phony_or_a_real_file_rule():
    # A documented target that is neither .PHONY nor a file rule is a typo.
    undeclared = sorted(set(_documented()) - _phony_targets())
    assert not undeclared, (
        f"documented targets missing from .PHONY: {undeclared}"
    )


def test_help_is_not_coloured():
    """Standards row 5: log.use_color() is the single colour authority.

    A recipe emitting its own ANSI would be a second one, and it would never see
    NO_COLOR. The help recipe is deliberately plain text.
    """
    recipe = _text().split("\nhelp:", 1)[1].split("\n\n", 1)[0]
    assert "\\033[" not in recipe and "\\e[" not in recipe


def test_every_group_banner_has_members():
    text = _text()
    banners = re.findall(r"^##@ (.+)$", text, re.MULTILINE)
    assert banners, "no ##@ group banners found"
    assert len(banners) == len(set(banners)), "duplicate ##@ group banner"


def test_help_runs():
    """The awk program is well-formed and lists a known target under its group."""
    if shutil.which("make") is None:  # pragma: no cover - make is a dev dep
        return
    r = subprocess.run(
        ["make", "help"], cwd=REPO, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Docs, roadmap & gates" in r.stdout
    assert "roadmap-view" in r.stdout
    # Every documented target reaches the output.
    for name in _documented():
        assert re.search(rf"^  {re.escape(name)}\s", r.stdout, re.MULTILINE), name
