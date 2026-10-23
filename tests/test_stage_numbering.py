# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Stage numbers quoted in prose match the registry (3.4.0-B16).

``pipeline/stages/__init__.py`` ``STAGES`` is the one ordering. The pipeline
once had eight stages (partition and base install were separate), and the
old numbers survived in shipped config headers, the README and fresh-install guide, ``--help`` and
module docstrings after the two merged into ``install``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from sysforge.pipeline.stages import STAGE_NAMES

REPO = Path(__file__).resolve().parent.parent
NUMBER = {name: i for i, name in enumerate(STAGE_NAMES, 1)}
LAST = len(STAGE_NAMES)


def _module_path(name: str) -> Path:
    base = REPO / "sysforge/pipeline/stages"
    pkg = base / name / "__init__.py"
    return pkg if pkg.exists() else base / f"{name}.py"


@pytest.mark.parametrize("name", STAGE_NAMES)
def test_stage_module_docstring_names_its_registry_number(name):
    text = _module_path(name).read_text(encoding="utf-8")
    m = re.search(r"stage (\d+)", text)
    assert m, f"{name}: module docstring names no stage number"
    assert int(m.group(1)) == NUMBER[name], name


@pytest.mark.parametrize("cfg,name", [("kernel.toml", "kernel"),
                                      ("toolchain.toml", "toolchain")])
@pytest.mark.parametrize("root", ["etc/sysforge", "tests/data/etc/sysforge"])
def test_shipped_config_header_names_its_stage_number(root, cfg, name):
    head = (REPO / root / cfg).read_text(encoding="utf-8").splitlines()[1]
    m = re.search(r"stage (\d+) / sysforge run " + name, head)
    assert m and int(m.group(1)) == NUMBER[name], head


def test_bootstrap_toml_sections_name_the_stage_that_reads_them():
    text = (REPO / "etc/sysforge/bootstrap.toml").read_text(encoding="utf-8")
    want = {"Partition configuration": "install", "System identity": "install",
            "Mirrorlist": "configure", "Desktop environment": "configure",
            "makepkg defaults": "configure"}
    for section, stage in want.items():
        m = re.search(re.escape(section) + r" \(stage (\d+)", text)
        assert m and int(m.group(1)) == NUMBER[stage], section


@pytest.mark.parametrize("rel", ["README.md", "docs/guides/fresh-install.md",
                                 "sysforge/run_cmd.py",
                                 "completions/_sysforge",
                                 "sysforge/pipeline/stages/reconfigure.py"])
def test_no_stage_range_runs_past_the_last_stage(rel):
    text = (REPO / rel).read_text(encoding="utf-8")
    for lo, hi in re.findall(r"stages? (\d+)[–-](\d+)", text):
        assert 1 <= int(lo) <= int(hi) <= LAST, f"{rel}: stages {lo}-{hi}"


def test_fresh_install_resume_range_starts_at_reconfigure():
    text = (REPO / "docs/guides/fresh-install.md").read_text(encoding="utf-8")
    assert f"This runs stages {NUMBER['reconfigure']}–{LAST}:" in text


def test_reboot_message_counts_only_the_bootstrap_stages():
    """The live-ISO stop fires once configure (the last bootstrap stage) is done."""
    text = (REPO / "sysforge/pipeline/stages/reconfigure.py").read_text(encoding="utf-8")
    assert f"Bootstrap complete (stages 1–{NUMBER['configure']} done)" in text
