# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
import argparse
from pathlib import Path

import pytest

from sysforge.primitives import artifacts, pacman_hooks


def test_unified_rows_include_sysforge_owned_rows(tmp_path, monkeypatch):
    reg = artifacts.ArtifactRegistry(
        state_dir=tmp_path / "state", data_dir=tmp_path / "data"
    )
    art = pacman_hooks.HookArtifact(
        dest=Path("/usr/share/libalpm/hooks/sysforge-kernel.hook"),
        content=b"x",
        mode=0o644,
    )
    monkeypatch.setattr(
        pacman_hooks, "diff_status", lambda: [(art, pacman_hooks.STATE_STALE)]
    )
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [])

    rows = artifacts.unified_rows(reg)
    sysforge_rows = [r for r in rows if r["owner"] == artifacts.OWNER_SYSFORGE]
    assert len(sysforge_rows) == 1
    assert sysforge_rows[0]["status"] == "drifted"
    assert sysforge_rows[0]["name"] == "sysforge-kernel.hook"


def test_unified_rows_maps_all_pacman_hook_states(tmp_path, monkeypatch):
    reg = artifacts.ArtifactRegistry(
        state_dir=tmp_path / "state", data_dir=tmp_path / "data"
    )
    def mk(n):
        return pacman_hooks.HookArtifact(
            dest=Path("/usr/share/libalpm/hooks") / n, content=b"x", mode=0o644
        )
    monkeypatch.setattr(pacman_hooks, "diff_status", lambda: [
        (mk("a.hook"), pacman_hooks.STATE_OK),
        (mk("b.hook"), pacman_hooks.STATE_STALE),
        (mk("c.hook"), pacman_hooks.STATE_MISSING),
    ])
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [])

    got = {r["name"]: r["status"] for r in artifacts.unified_rows(reg)}
    assert got == {"a.hook": "ok", "b.hook": "drifted", "c.hook": "missing"}


def test_artifact_list_verb_is_read_only():
    from sysforge.verbs.artifact import ArtifactListVerb

    assert ArtifactListVerb.requires_sentinel is False


def test_list_warns_once_when_script_root_off_path(tmp_path, monkeypatch, capsys):
    from sysforge.verbs.artifact import ArtifactListVerb

    monkeypatch.setattr(artifacts, "unified_rows", lambda reg, roots=None: [])
    monkeypatch.setattr(artifacts, "script_root_on_path", lambda root=None: False)
    monkeypatch.setattr(
        artifacts, "default_script_root", lambda: tmp_path / "scripts"
    )

    args = argparse.Namespace(unmanaged=False, state_dir=tmp_path)
    ArtifactListVerb().execute(args, None)

    out = capsys.readouterr()
    assert "not on PATH" in (out.out + out.err)


@pytest.mark.parametrize("verdict", [True, None])
def test_list_stays_quiet_when_on_path_or_unknown(
    tmp_path, monkeypatch, capsys, verdict
):
    from sysforge.verbs.artifact import ArtifactListVerb

    monkeypatch.setattr(artifacts, "unified_rows", lambda reg, roots=None: [])
    monkeypatch.setattr(artifacts, "script_root_on_path", lambda root=None: verdict)

    args = argparse.Namespace(unmanaged=False, state_dir=tmp_path)
    ArtifactListVerb().execute(args, None)

    out = capsys.readouterr()
    assert "not on PATH" not in (out.out + out.err)


def test_deploy_verb_requires_sentinel_and_names_target():
    from sysforge.verbs.artifact import ArtifactDeployVerb

    assert ArtifactDeployVerb.requires_sentinel is True
    args = argparse.Namespace(name="s.sh")
    assert ArtifactDeployVerb().journal_target(args) == "s.sh"


def test_deploy_warns_once_for_scripts_off_path(tmp_path, monkeypatch, capsys):
    from sysforge.verbs.artifact import ArtifactDeployVerb

    reg = artifacts.ArtifactRegistry(state_dir=tmp_path / "s", data_dir=tmp_path / "d")
    src = tmp_path / "live" / "a.sh"
    src.parent.mkdir(parents=True)
    src.write_text("x")
    artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)
    src2 = tmp_path / "live" / "b.sh"
    src2.write_text("y")
    artifacts.adopt(reg, src2, cls=artifacts.CLASS_SCRIPT)

    monkeypatch.setattr(artifacts, "deploy", lambda *a, **k: artifacts.STATUS_OK)
    monkeypatch.setattr(artifacts, "script_root_on_path", lambda root=None: False)
    monkeypatch.setattr(
        artifacts, "default_script_root", lambda: tmp_path / "scripts"
    )

    args = argparse.Namespace(
        name=None, all=True, force=False, adopt_live=False, state_dir=tmp_path / "s"
    )
    ArtifactDeployVerb().execute(args, None)

    combined = "".join(capsys.readouterr())
    assert combined.count("not on PATH") == 1, "warn once per run, not per artifact"


def test_deploy_does_not_warn_for_non_script_classes(tmp_path, monkeypatch, capsys):
    from sysforge.verbs.artifact import ArtifactDeployVerb

    reg = artifacts.ArtifactRegistry(state_dir=tmp_path / "s", data_dir=tmp_path / "d")
    src = tmp_path / "live" / "a.hook"
    src.parent.mkdir(parents=True)
    src.write_text("x")
    artifacts.adopt(reg, src, cls=artifacts.CLASS_HOOK)

    monkeypatch.setattr(artifacts, "deploy", lambda *a, **k: artifacts.STATUS_OK)
    monkeypatch.setattr(artifacts, "script_root_on_path", lambda root=None: False)

    args = argparse.Namespace(
        name="a.hook", all=False, force=False, adopt_live=False,
        state_dir=tmp_path / "s",
    )
    ArtifactDeployVerb().execute(args, None)

    assert "not on PATH" not in "".join(capsys.readouterr())


def test_deploy_verb_exit_code_1_on_failure(tmp_path):
    from sysforge.verbs.artifact import ArtifactDeployVerb

    args = argparse.Namespace(
        name="missing.sh", all=False, force=False, adopt_live=False,
        state_dir=tmp_path / "s",
    )
    result = ArtifactDeployVerb().execute(args, None)
    assert result.exit_code == 1


def test_list_verb_corrupt_registry_exits_1_not_traceback(tmp_path, capsys):
    # A corrupt artifacts.toml must surface as a clean exit-1 with the repair
    # guidance (ArtifactRegistryError subclasses ArtifactError, which the verb
    # catches), never as an uncaught traceback.
    from sysforge.verbs.artifact import ArtifactListVerb

    reg = artifacts.ArtifactRegistry(state_dir=tmp_path / "s", data_dir=tmp_path / "d")
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text("not [valid toml")

    result = ArtifactListVerb().execute(
        argparse.Namespace(unmanaged=False, state_dir=tmp_path / "s"), None
    )
    assert result.exit_code == 1
    assert "corrupt" in "".join(capsys.readouterr())


def test_remove_verb_passes_force_through(tmp_path, monkeypatch):
    from sysforge.verbs.artifact import ArtifactRemoveVerb

    reg = artifacts.ArtifactRegistry(state_dir=tmp_path / "s", data_dir=tmp_path / "d")
    src = tmp_path / "live" / "a.sh"
    src.parent.mkdir(parents=True)
    src.write_text("x")
    artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)
    src.write_text("hand-edited")  # drift

    captured = {}
    real_remove = artifacts.remove
    monkeypatch.setattr(
        artifacts, "remove",
        lambda r, n, **kw: captured.update(kw) or real_remove(r, n, **kw),
    )
    args = argparse.Namespace(
        name="a.sh", purge=False, force=True, state_dir=tmp_path / "s"
    )
    result = ArtifactRemoveVerb().execute(args, None)
    assert captured.get("force") is True
    assert result.exit_code == 0
    assert not src.exists()
