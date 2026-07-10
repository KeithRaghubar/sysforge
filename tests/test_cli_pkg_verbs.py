# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""CLI wiring for search / uninstall verbs."""
from sysforge.cli import _build_parser
from sysforge.search_cmd import SearchVerb
from sysforge.uninstall_cmd import UninstallVerb


def test_search_wires_verb_cls():
    ns = _build_parser().parse_args(["search", "nano"])
    assert ns.verb_cls is SearchVerb
    assert ns.term == "nano"


def test_uninstall_wires_verb_cls():
    ns = _build_parser().parse_args(["uninstall", "mesa"])
    assert ns.verb_cls is UninstallVerb
    assert ns.packages == ["mesa"]
