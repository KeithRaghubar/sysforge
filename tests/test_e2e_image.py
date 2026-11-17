"""The demo image's sync db cannot outlive the mirrors (4.0.0-DEV4).

`podman build` caches the provision layer for as long as provision.sh and the
base image are unchanged, so its `pacman -Syu` snapshot froze while the mirrors
moved on: a scenario's `pacman -S` then asked for files the mirrors had dropped
(404). Each builder passes a daily stamp that the provision RUN reads, which
busts that layer once a day.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONTAINERFILE = ROOT / "tools" / "demo" / "Containerfile"
BUILDERS = [ROOT / "tools" / "e2e" / "run.sh", ROOT / "tools" / "record-demo.sh"]


def test_provision_run_reads_the_sync_stamp() -> None:
    text = CONTAINERFILE.read_text()
    arg = text.index("ARG SYNC_STAMP")
    run = re.search(r"^RUN .*\$SYNC_STAMP.*sysforge-provision", text, re.M)
    assert run, "the provision RUN does not read $SYNC_STAMP, so its layer stays cached"
    assert arg < run.start()


@pytest.mark.parametrize("builder", BUILDERS, ids=lambda p: p.name)
def test_builder_passes_a_daily_stamp(builder: Path) -> None:
    build = next(
        line for line in builder.read_text().splitlines() if "podman build" in line
    )
    assert "--build-arg SYNC_STAMP=" in build
    assert "--pull=newer" in build
