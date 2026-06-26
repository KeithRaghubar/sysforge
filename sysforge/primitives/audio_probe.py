# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
audio_probe.py — audio / sound-stack diagnostics (doctor ``audio`` axis).

Read-only checks of the live PipeWire/WirePlumber sound stack:

  - failed audio *user* services — ``systemctl --user --failed`` filtered to the
    PipeWire/WirePlumber unit family (the "pipewire services were failing with no
    surfaced cause" class of bug).
  - a vanished output sink — ``pactl list short sinks`` showing only the dummy
    ``auto_null`` device (the "audio device disappeared" symptom).

Best-effort and false-positive-averse. The sound stack is *user*-scoped, so when
``doctor`` runs under ``sudo`` (no reachable session bus) ``systemctl --user`` /
``pactl`` exit nonzero — in that case we degrade to **no findings** rather than
reporting a phantom "no audio device". A missed finding is recoverable; a false
alarm is not. Input sources are deliberately *not* flagged: a machine with no
microphone is normal and would false-positive.

Returns :class:`diagnostics.Finding` objects directly (category ``"audio"``).
Mutates nothing — never restarts a unit.
"""
from __future__ import annotations

import re
import subprocess

from sysforge.primitives import diagnostics as diag

# Match the PipeWire/WirePlumber user-unit family: pipewire.service,
# pipewire-pulse.service, wireplumber.service (and any future pipewire-* unit).
_AUDIO_UNIT_RE = re.compile(r"^(?:pipewire|wireplumber)\b", re.IGNORECASE)

# Sink names that are not real audio outputs: the dummy fallback and any
# per-sink monitor source (the latter only appears under `sources`, but guard
# both paths uniformly).
_DUMMY_SINK_NAMES = frozenset({"auto_null"})


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except (FileNotFoundError, OSError):
        return None


def _check_failed_audio_units() -> list[diag.Finding]:
    """`systemctl --user --failed` filtered to the audio unit family.

    A nonzero exit means the user session bus is unreachable (e.g. running under
    sudo) — degrade to no findings rather than guess.
    """
    proc = _run(["systemctl", "--user", "--failed", "--no-legend", "--plain"])
    if proc is None or proc.returncode != 0:
        return []
    findings: list[diag.Finding] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        unit = line.split()[0]
        if not unit.endswith((".service", ".socket")):
            continue
        if not _AUDIO_UNIT_RE.match(unit):
            continue
        findings.append(diag.Finding(
            "audio", diag.SEV_ERROR, f"audio_unit_failed:{unit}",
            f"audio user service '{unit}' is in failed state",
            remediation=(f"inspect: systemctl --user status {unit}; "
                         f"journalctl --user -u {unit} — then "
                         f"systemctl --user restart {unit}"),
        ))
    return findings


def _check_audio_sinks() -> list[diag.Finding]:
    """`pactl list short sinks` — warn when only the dummy device remains.

    pactl absent or the server unreachable (nonzero exit) → no findings. Empty
    output with a clean exit is treated the same as the dummy-only case.
    """
    proc = _run(["pactl", "list", "short", "sinks"])
    if proc is None or proc.returncode != 0:
        return []
    real: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Columns are tab-separated: id, name, driver, sample-spec, state.
        cols = line.split("\t") if "\t" in line else line.split()
        if len(cols) < 2:
            continue
        name = cols[1]
        if name in _DUMMY_SINK_NAMES or name.endswith(".monitor"):
            continue
        real.append(name)
    if real:
        return []
    return [diag.Finding(
        "audio", diag.SEV_WARN, "no_audio_sink",
        "no usable audio output sink — only the dummy (auto_null) device is "
        "present; the sound device may have disappeared",
        remediation=("check `pactl list short sinks`; restart the audio stack: "
                     "systemctl --user restart wireplumber pipewire pipewire-pulse"),
    )]


def collect_audio_findings() -> list[diag.Finding]:
    """Run all audio checks; return findings (read-only, never mutates)."""
    findings: list[diag.Finding] = []
    findings += _check_failed_audio_units()
    findings += _check_audio_sinks()
    return findings
