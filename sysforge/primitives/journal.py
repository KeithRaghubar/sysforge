# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""
primitives/journal.py — the journald sink seam (2.3.0-F6 / STD row 20).

The ONE home for emitting SysForge's system-mutating operations to the systemd
journal, additively alongside the unified run-log (`log.py`). Writes native
journal-protocol datagrams to the journal socket so fields are queryable
(``journalctl SYSFORGE_VERB=build``), not just greppable.

Additive and never load-bearing: when the journal socket is absent (non-systemd
host, sandboxed CI) or the send fails, every function is a silent no-op and
never raises into a caller.
"""
from __future__ import annotations

import socket
import struct

#: The systemd journal datagram socket. Patched in tests.
_JOURNAL_SOCKET = "/run/systemd/journal/socket"


def _encode_field(name: str, value: str | int) -> bytes:
    """Frame one field per the sd_journal_send socket protocol.

    Single-line values use ``NAME=value\\n``; values containing a newline use
    the binary form ``NAME\\n`` + little-endian uint64 byte-length + value +
    ``\\n`` (so newlines can't be confused with the field separator).
    """
    text = str(value)
    raw = text.encode("utf-8")
    if b"\n" in raw:
        return name.encode("utf-8") + b"\n" + struct.pack("<Q", len(raw)) + raw + b"\n"
    return f"{name}={text}\n".encode("utf-8")


def journal_send(**fields: str | int) -> None:
    """Send *fields* to the journal socket; silent no-op on any failure."""
    try:
        payload = b"".join(_encode_field(k, v) for k, v in fields.items())
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(_JOURNAL_SOCKET)
            sock.send(payload)
    except OSError:
        # Any failure is a deliberate silent no-op: absent socket (non-systemd
        # host / sandboxed CI), a refused connect, or EMSGSIZE on an oversized
        # datagram. This sink is additive and never load-bearing, so it never
        # raises into a verb. (Records are verb-name + package-list — tens of
        # bytes — so the datagram-size ceiling is not reached in practice; we
        # do not implement sd_journal_send's memfd/SCM_RIGHTS large-payload
        # fallback.)
        return


def record_verb(verb: str, target: str | None, exit_code: int) -> None:
    """Emit one structured record for a completed system-mutating verb."""
    message = f"sysforge: {verb}" + (f" {target}" if target else "") + f" exited {exit_code}"
    fields: dict[str, str | int] = {
        "MESSAGE": message,
        "PRIORITY": 6 if exit_code == 0 else 3,
        "SYSLOG_IDENTIFIER": "sysforge",
        "SYSFORGE_VERB": verb,
        "SYSFORGE_EXIT": exit_code,
    }
    if target:
        fields["SYSFORGE_TARGET"] = target
    journal_send(**fields)
