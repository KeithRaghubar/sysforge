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


def _review_args(tmp_path, **kw):
    ns = argparse.Namespace(
        state_dir=tmp_path / "state", all=False, include_unknown=False
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _mk_live(tmp_path, name, body="x"):
    p = tmp_path / name
    p.write_text(body)
    return p


def test_review_verb_is_read_only():
    from sysforge.verbs.artifact import ArtifactReviewVerb

    assert ArtifactReviewVerb.requires_sentinel is False


def test_review_adopts_on_a(tmp_path, monkeypatch):
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    live = _mk_live(tmp_path, "hello.sh")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=live, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU)
    ])
    monkeypatch.setattr(prompt, "is_interactive", lambda: True)
    monkeypatch.setattr(prompt, "prompt_choice", lambda *a, **k: "a")

    rc = ArtifactReviewVerb().execute(_review_args(tmp_path), None)

    assert rc.exit_code == 0
    reg = artifacts.ArtifactRegistry(state_dir=tmp_path / "state")
    assert "hello.sh" in reg.load()


def test_review_ignores_on_i(tmp_path, monkeypatch):
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    live = _mk_live(tmp_path, "skip.sh", "body")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=live, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU)
    ])
    monkeypatch.setattr(prompt, "is_interactive", lambda: True)
    monkeypatch.setattr(prompt, "prompt_choice", lambda *a, **k: "i")

    rc = ArtifactReviewVerb().execute(_review_args(tmp_path), None)

    assert rc.exit_code == 0
    ig = artifacts.IgnoreList(state_dir=tmp_path / "state")
    assert ig.load() == {live: artifacts.hash_file(live)}
    # Not adopted.
    assert "skip.sh" not in artifacts.ArtifactRegistry(
        state_dir=tmp_path / "state").load()


def test_review_ignore_skips_gone_file(tmp_path, monkeypatch):
    # TOCTOU: file vanishes between scan and the user's [i]gnore answer —
    # hash_file returns None; must not persist a junk "None" hash row.
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    live = _mk_live(tmp_path, "gone.sh")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=live, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU)
    ])
    monkeypatch.setattr(prompt, "is_interactive", lambda: True)
    monkeypatch.setattr(prompt, "prompt_choice", lambda *a, **k: "i")
    monkeypatch.setattr(artifacts, "hash_file", lambda path: None)

    rc = ArtifactReviewVerb().execute(_review_args(tmp_path), None)

    assert rc.exit_code == 0
    ig = artifacts.IgnoreList(state_dir=tmp_path / "state")
    assert ig.load() == {}
    assert "gone.sh" not in artifacts.ArtifactRegistry(
        state_dir=tmp_path / "state").load()


def test_review_non_tty_flags_ownership_unknown(tmp_path, monkeypatch, capsys):
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    live = _mk_live(tmp_path, "mystery.sh")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=live, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_UNKNOWN)
    ])
    monkeypatch.setattr(prompt, "is_interactive", lambda: False)

    rc = ArtifactReviewVerb().execute(_review_args(tmp_path), None)

    assert rc.exit_code == 0
    out = "".join(capsys.readouterr())
    assert "mystery.sh" in out
    assert "[ownership unknown]" in out


def test_review_quits_on_q_before_second_candidate(tmp_path, monkeypatch):
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    first = _mk_live(tmp_path, "a.sh")
    second = _mk_live(tmp_path, "b.sh")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=first, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU),
        artifacts.Candidate(path=second, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU),
    ])
    monkeypatch.setattr(prompt, "is_interactive", lambda: True)
    monkeypatch.setattr(prompt, "prompt_choice", lambda *a, **k: "q")

    rc = ArtifactReviewVerb().execute(_review_args(tmp_path), None)

    assert rc.exit_code == 0
    # Quit on the first prompt — neither adopted.
    assert artifacts.ArtifactRegistry(state_dir=tmp_path / "state").load() == {}


def test_review_non_tty_lists_and_exits_zero(tmp_path, monkeypatch, capsys):
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    live = _mk_live(tmp_path, "hello.sh")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=live, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU)
    ])
    monkeypatch.setattr(prompt, "is_interactive", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("must not prompt without a TTY")
    monkeypatch.setattr(prompt, "prompt_choice", _boom)

    rc = ArtifactReviewVerb().execute(_review_args(tmp_path), None)

    assert rc.exit_code == 0
    out = "".join(capsys.readouterr())
    assert "hello.sh" in out
    assert "artifact adopt" in out
    # Nothing adopted.
    assert artifacts.ArtifactRegistry(state_dir=tmp_path / "state").load() == {}


def test_cli_wires_artifact_review():
    from sysforge.cli import _build_parser
    from sysforge.verbs.artifact import ArtifactReviewVerb

    ns = _build_parser().parse_args(["artifact", "review"])
    assert ns.verb_cls is ArtifactReviewVerb


# ---------------------------------------------------------------------------
# 2.6.1-F28 — `artifact review --all` bulk adoption
# ---------------------------------------------------------------------------

def test_review_all_adopts_every_offerable_candidate(tmp_path, monkeypatch):
    """Bulk adopt across mixed classes: --all walks the same iter_offerable
    result the interactive loop does, so the exclusion rules cannot drift."""
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    a = _mk_live(tmp_path, "one.sh")
    b = _mk_live(tmp_path, "two.sh", "other")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=a, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU),
        artifacts.Candidate(path=b, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU),
    ])
    # --all must never prompt, even on a TTY.
    monkeypatch.setattr(prompt, "is_interactive", lambda: True)
    monkeypatch.setattr(prompt, "prompt_choice", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("--all must not prompt")))

    rc = ArtifactReviewVerb().execute(_review_args(tmp_path, all=True), None)

    assert rc.exit_code == 0
    names = artifacts.ArtifactRegistry(state_dir=tmp_path / "state").load()
    assert "one.sh" in names
    assert "two.sh" in names


def test_review_all_skips_unknown_owner_by_default(tmp_path, monkeypatch, capsys):
    """owner == unknown means pacman returned no verdict, so the file may in
    fact be package-owned — a blind bulk adopt is where that mislabel does
    damage. The interactive path can ask a human; the bulk path cannot."""
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    mine = _mk_live(tmp_path, "mine.sh")
    mystery = _mk_live(tmp_path, "mystery.sh", "who")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=mine, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU),
        artifacts.Candidate(path=mystery, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_UNKNOWN),
    ])
    monkeypatch.setattr(prompt, "is_interactive", lambda: False)

    rc = ArtifactReviewVerb().execute(_review_args(tmp_path, all=True), None)

    assert rc.exit_code == 0
    names = artifacts.ArtifactRegistry(state_dir=tmp_path / "state").load()
    assert "mine.sh" in names
    assert "mystery.sh" not in names
    assert "--include-unknown" in "".join(capsys.readouterr())


def test_review_all_include_unknown_adopts_them(tmp_path, monkeypatch):
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    mystery = _mk_live(tmp_path, "mystery.sh")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=mystery, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_UNKNOWN),
    ])
    monkeypatch.setattr(prompt, "is_interactive", lambda: False)

    rc = ArtifactReviewVerb().execute(
        _review_args(tmp_path, all=True, include_unknown=True), None
    )

    assert rc.exit_code == 0
    assert "mystery.sh" in artifacts.ArtifactRegistry(
        state_dir=tmp_path / "state").load()


def test_review_all_respects_the_ignore_list(tmp_path, monkeypatch):
    """A recorded decline is a decision. --all is a convenience over the offer
    set, not an override of it."""
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    declined = _mk_live(tmp_path, "declined.sh", "body")
    keep = _mk_live(tmp_path, "keep.sh", "other")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=declined, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU),
        artifacts.Candidate(path=keep, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU),
    ])
    monkeypatch.setattr(prompt, "is_interactive", lambda: False)
    ig = artifacts.IgnoreList(state_dir=tmp_path / "state")
    ig.save({declined: artifacts.hash_file(declined)})

    rc = ArtifactReviewVerb().execute(_review_args(tmp_path, all=True), None)

    assert rc.exit_code == 0
    names = artifacts.ArtifactRegistry(state_dir=tmp_path / "state").load()
    assert "declined.sh" not in names
    assert "keep.sh" in names


def test_review_all_runs_off_tty(tmp_path, monkeypatch):
    """--all needs no prompt, so it replaces the list-and-hint branch rather
    than falling into it."""
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    live = _mk_live(tmp_path, "batch.sh")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=live, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU),
    ])
    monkeypatch.setattr(prompt, "is_interactive", lambda: False)

    rc = ArtifactReviewVerb().execute(_review_args(tmp_path, all=True), None)

    assert rc.exit_code == 0
    assert "batch.sh" in artifacts.ArtifactRegistry(
        state_dir=tmp_path / "state").load()


def test_review_all_continues_past_a_failed_adopt(tmp_path, monkeypatch, capsys):
    """A mid-run ArtifactError logs and continues, mirroring the interactive
    loop — one unreadable file must not abandon the rest of the batch."""
    from sysforge.primitives import prompt
    from sysforge.verbs.artifact import ArtifactReviewVerb

    bad = _mk_live(tmp_path, "bad.sh")
    good = _mk_live(tmp_path, "good.sh", "ok")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        artifacts.Candidate(path=bad, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU),
        artifacts.Candidate(path=good, cls=artifacts.CLASS_SCRIPT,
                            owner=artifacts.OWNER_YOU),
    ])
    monkeypatch.setattr(prompt, "is_interactive", lambda: False)

    real_adopt = artifacts.adopt

    def flaky(registry, path, cls=None):
        if path == bad:
            raise artifacts.ArtifactError("boom")
        return real_adopt(registry, path, cls=cls)
    monkeypatch.setattr(artifacts, "adopt", flaky)

    rc = ArtifactReviewVerb().execute(_review_args(tmp_path, all=True), None)

    assert rc.exit_code == 0
    names = artifacts.ArtifactRegistry(state_dir=tmp_path / "state").load()
    assert "good.sh" in names
    assert "bad.sh" not in names
    assert "boom" in "".join(capsys.readouterr())
