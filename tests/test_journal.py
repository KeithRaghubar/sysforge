# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""Wire-format + no-op guards for the journald seam (2.3.0-F6, STD row 20)."""
from __future__ import annotations

import socket
import struct
from types import SimpleNamespace

from sysforge.primitives import journal


def _fake_socket(tmp_path, monkeypatch):
    """Bind a real AF_UNIX datagram socket and point journal_send at it.

    Returns the receiving socket; the test reads the datagram off it.
    """
    sock_path = tmp_path / "journal.sock"
    rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    rx.bind(str(sock_path))
    monkeypatch.setattr(journal, "_JOURNAL_SOCKET", str(sock_path))
    return rx


def test_journal_send_single_line_fields(tmp_path, monkeypatch):
    rx = _fake_socket(tmp_path, monkeypatch)
    journal.journal_send(SYSFORGE_VERB="build", SYSFORGE_EXIT=0)
    data = rx.recv(4096)
    rx.close()
    assert b"SYSFORGE_VERB=build\n" in data
    assert b"SYSFORGE_EXIT=0\n" in data  # int coerced to str


def test_journal_send_multiline_uses_binary_frame(tmp_path, monkeypatch):
    rx = _fake_socket(tmp_path, monkeypatch)
    journal.journal_send(MESSAGE="line1\nline2")
    data = rx.recv(4096)
    rx.close()
    # binary form: NAME\n <uint64 LE length> value \n
    value = b"line1\nline2"
    expected = b"MESSAGE\n" + struct.pack("<Q", len(value)) + value + b"\n"
    assert data == expected


def test_journal_send_no_socket_is_silent_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "_JOURNAL_SOCKET", str(tmp_path / "absent.sock"))
    # Must not raise even though nothing is listening.
    journal.journal_send(MESSAGE="x")


def test_record_verb_maps_fields_and_priority(tmp_path, monkeypatch):
    rx = _fake_socket(tmp_path, monkeypatch)
    journal.record_verb("build", "mesa", 0)
    data = rx.recv(4096)
    rx.close()
    assert b"PRIORITY=6\n" in data          # exit 0 -> info
    assert b"SYSLOG_IDENTIFIER=sysforge\n" in data
    assert b"SYSFORGE_VERB=build\n" in data
    assert b"SYSFORGE_TARGET=mesa\n" in data
    assert b"SYSFORGE_EXIT=0\n" in data
    assert b"MESSAGE=" in data


def test_record_verb_failure_priority_and_no_target(tmp_path, monkeypatch):
    rx = _fake_socket(tmp_path, monkeypatch)
    journal.record_verb("update", None, 1)
    data = rx.recv(4096)
    rx.close()
    assert b"PRIORITY=3\n" in data          # nonzero -> err
    assert b"SYSFORGE_TARGET=" not in data  # omitted when target is None
    assert b"SYSFORGE_EXIT=1\n" in data


def test_pkg_target_prefixes_and_joins():
    assert journal.pkg_target(["mesa"]) == "pkg:mesa"
    assert journal.pkg_target(["mesa", "llvm"]) == "pkg:mesa llvm"


def test_pkg_target_empty_is_none():
    assert journal.pkg_target([]) is None


def test_mode_target_prefixes():
    assert journal.mode_target("repair") == "mode:repair"
    assert journal.mode_target("orphans") == "mode:orphans"


def test_build_verb_journal_target_joins_packages():
    from sysforge.build_cmd import BuildVerb

    verb = BuildVerb()
    assert verb.journal_target(SimpleNamespace(packages=["mesa", "llvm"])) == "pkg:mesa llvm"
    assert verb.journal_target(SimpleNamespace(packages=[])) is None


def test_uninstall_verb_journal_target():
    from sysforge.uninstall_cmd import UninstallVerb

    verb = UninstallVerb()
    assert verb.journal_target(SimpleNamespace(packages=["mesa"])) == "pkg:mesa"
    assert verb.journal_target(SimpleNamespace(packages=[])) is None


def test_revert_verb_journal_target():
    from sysforge.revert_cmd import RevertToStockVerb

    verb = RevertToStockVerb()
    assert verb.journal_target(SimpleNamespace(packages=["mesa", "llvm"])) == "pkg:mesa llvm"
    assert verb.journal_target(SimpleNamespace(packages=[])) is None


def test_state_forget_verb_journal_target():
    from sysforge.state_cmd import StateForgetVerb

    verb = StateForgetVerb()
    assert verb.journal_target(SimpleNamespace(pkgnames=["mesa"])) == "pkg:mesa"
    assert verb.journal_target(SimpleNamespace(pkgnames=[])) is None


def test_state_failed_verb_journal_target():
    from sysforge.state_cmd import StateFailedVerb

    verb = StateFailedVerb()
    assert verb.journal_target(SimpleNamespace(clear="mesa")) == "pkg:mesa"
    # no --clear: not sentinel-gated, no subject
    assert verb.journal_target(SimpleNamespace(clear=None)) is None


def test_state_repair_verb_journal_target():
    from sysforge.state_cmd import StateRepairVerb

    assert StateRepairVerb().journal_target(SimpleNamespace()) == "mode:repair"


def test_state_orphans_verb_journal_target():
    from sysforge.state_cmd import StateOrphansVerb

    assert StateOrphansVerb().journal_target(SimpleNamespace(prune=True)) == "mode:orphans"
