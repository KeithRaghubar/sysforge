# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""The artifact registry finds its state directory the way the rest of sysforge does (3.5.0-B142).

It resolved the directory itself, so it never provisioned it: in a user's first
session, before the `sysforge` group applies, an `artifact` write as the first
command ended in a PermissionError traceback. Reads resolve without
provisioning (that can ask for sudo); the first write provisions, as `build`'s
does.
"""
import pytest

from sysforge.primitives import artifacts, build_sandbox, paths


@pytest.fixture
def resolver(tmp_path, monkeypatch):
    calls = []
    provisioned = tmp_path / "provisioned"

    def fake(cli_override=None, *, provision=True):
        calls.append((cli_override, provision))
        if provision:
            provisioned.mkdir(exist_ok=True)
            return provisioned, "default"
        return tmp_path / "unprovisioned", "default"

    monkeypatch.setattr(paths, "resolve_state_dir", fake)
    return calls, provisioned


@pytest.mark.parametrize("cls", [artifacts.ArtifactRegistry, artifacts.IgnoreList])
def test_reading_does_not_provision(resolver, cls):
    calls, _ = resolver
    cls()
    assert calls == [(None, False)]


def test_the_first_write_provisions_and_lands_there(resolver):
    calls, provisioned = resolver
    reg = artifacts.ArtifactRegistry()
    reg.save({})
    assert calls[-1] == (None, True)
    assert reg.path == provisioned / "artifacts.toml"
    assert reg.path.is_file()


def test_ignore_list_write_provisions(resolver):
    calls, provisioned = resolver
    ign = artifacts.IgnoreList()
    ign.save({})
    assert calls[-1] == (None, True)
    assert ign.path.parent == provisioned


def test_an_explicit_dir_is_used_as_given(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "env"))
    reg = artifacts.ArtifactRegistry(state_dir=tmp_path / "mine")
    reg.save({})
    assert reg.path == tmp_path / "mine" / "artifacts.toml"


def test_source_built_packages_reads_without_provisioning(resolver):
    calls, _ = resolver
    assert build_sandbox.source_built_packages() == set()
    assert calls == [(None, False)]
