# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for aur.aur_search (RPC v5 name-desc search)."""
import io
import json
import urllib.error

from sysforge.primitives import aur


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_aur_search_parses_results(monkeypatch):
    payload = {"results": [
        {"Name": "cosmic-ext-foo", "Version": "1.0-1", "Description": "a thing"},
    ]}
    monkeypatch.setattr(aur.urllib.request, "urlopen",
                        lambda url, timeout=None: _Resp(json.dumps(payload).encode()))
    out = aur.aur_search("cosmic-ext-foo")
    assert out[0]["Name"] == "cosmic-ext-foo"


def test_aur_search_empty_term_returns_empty(monkeypatch):
    monkeypatch.setattr(aur.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("hit network")))
    assert aur.aur_search("") == []


def test_aur_search_network_error_is_nonfatal(monkeypatch):
    def _boom(url, timeout=None):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(aur.urllib.request, "urlopen", _boom)
    assert aur.aur_search("anything") == []
