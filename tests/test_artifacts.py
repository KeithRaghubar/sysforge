# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
import shutil
from pathlib import Path

import pytest

from sysforge.primitives import artifacts


def _reg(tmp_path):
    return artifacts.ArtifactRegistry(
        state_dir=tmp_path / "state", data_dir=tmp_path / "data"
    )


def test_registry_roundtrip_preserves_all_fields(tmp_path):
    reg = _reg(tmp_path)
    entry = artifacts.Artifact(
        name="backup.sh",
        dest=Path("/home/u/scripts/backup.sh"),
        cls="script",
        auth_hash="a" * 64,
        deployed_hash="b" * 64,
        deployed_at="2026-07-19T10:00:00Z",
    )
    reg.save({"backup.sh": entry})
    loaded = reg.load()
    assert loaded == {"backup.sh": entry}


def test_registry_load_missing_file_returns_empty(tmp_path):
    assert _reg(tmp_path).load() == {}


def test_rehash_updates_auth_hash_making_entry_pending(tmp_path):
    reg = _reg(tmp_path)
    src = tmp_path / "s.sh"
    src.write_text("original")
    artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)

    reg.content_path("s.sh").write_text("edited")
    art = artifacts.rehash(reg, "s.sh")

    assert art.auth_hash == artifacts.hash_bytes(b"edited")
    assert art.deployed_hash == artifacts.hash_bytes(b"original")
    assert artifacts.status_of(reg, art) == artifacts.STATUS_PENDING


def test_rehash_unknown_name_raises(tmp_path):
    with pytest.raises(artifacts.ArtifactError, match="not managed"):
        artifacts.rehash(_reg(tmp_path), "nope")


def test_rehash_noop_leaves_status_ok(tmp_path):
    reg = _reg(tmp_path)
    src = tmp_path / "s.sh"
    src.write_text("original")
    artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)

    art = artifacts.rehash(reg, "s.sh")

    assert artifacts.status_of(reg, art) == artifacts.STATUS_OK


def test_registry_roundtrip_never_deployed(tmp_path):
    reg = _reg(tmp_path)
    entry = artifacts.Artifact(
        name="x.hook",
        dest=Path("/etc/pacman.d/hooks/x.hook"),
        cls="pacman-hook",
        auth_hash="c" * 64,
        deployed_hash=None,
        deployed_at=None,
    )
    reg.save({"x.hook": entry})
    assert reg.load()["x.hook"].deployed_hash is None


def test_registry_escapes_quotes_and_backslashes_in_paths(tmp_path):
    reg = _reg(tmp_path)
    weird = Path('/home/u/scripts/we"ird\\name.sh')
    entry = artifacts.Artifact(
        name="weird", dest=weird, cls="script",
        auth_hash="d" * 64, deployed_hash=None, deployed_at=None,
    )
    reg.save({"weird": entry})
    assert reg.load()["weird"].dest == weird


def test_hash_file_returns_none_when_absent(tmp_path):
    assert artifacts.hash_file(tmp_path / "nope") is None


def test_hash_file_matches_hash_bytes(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"hello")
    assert artifacts.hash_file(p) == artifacts.hash_bytes(b"hello")


def test_registry_roundtrip_survives_carriage_return_in_name_and_dest(tmp_path):
    reg = _reg(tmp_path)
    entry = artifacts.Artifact(
        name="weird\rname",
        dest=Path("/home/u/scripts/back\rup.sh"),
        cls="script",
        auth_hash="e" * 64,
        deployed_hash=None,
        deployed_at=None,
    )
    reg.save({"weird\rname": entry})
    loaded = reg.load()
    assert loaded == {"weird\rname": entry}


def test_registry_roundtrip_survives_other_c0_control_char(tmp_path):
    reg = _reg(tmp_path)
    entry = artifacts.Artifact(
        name="weird\x01name",
        dest=Path("/home/u/scripts/back\x01up.sh"),
        cls="script",
        auth_hash="f" * 64,
        deployed_hash=None,
        deployed_at=None,
    )
    reg.save({"weird\x01name": entry})
    loaded = reg.load()
    assert loaded == {"weird\x01name": entry}


def test_registry_load_corrupt_toml_raises(tmp_path):
    reg = _reg(tmp_path)
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text("not [valid toml")
    try:
        reg.load()
    except artifacts.ArtifactRegistryError:
        pass
    else:
        raise AssertionError("expected ArtifactRegistryError")


def test_registry_load_missing_file_still_returns_empty(tmp_path):
    reg = _reg(tmp_path)
    assert not reg.path.exists()
    assert reg.load() == {}


@pytest.mark.parametrize(
    "name,expect_excluded",
    [
        ("real-unit.service", False),
        ("sshd.service.pacnew", True),
        ("thing.conf.pacsave", True),
        ("thing.conf.pacorig", True),
        ("backup.sh~", True),
        (".#locked.service", True),
    ],
)
def test_is_excluded_by_filename_rules(tmp_path, name, expect_excluded):
    p = tmp_path / name
    p.write_text("x")
    assert (artifacts.is_excluded(p) is not None) is expect_excluded


def test_scan_skips_wants_and_requires_dirs(tmp_path, monkeypatch):
    """systemctl-enable symlinks are systemd-owned enablement state, not
    user-authored artifacts."""
    root = tmp_path / "system"
    (root / "multi-user.target.wants").mkdir(parents=True)
    (root / "multi-user.target.wants" / "foo.service").write_text("link")
    (root / "sockets.target.requires").mkdir(parents=True)
    (root / "sockets.target.requires" / "bar.socket").write_text("link")
    (root / "mine.service").write_text("[Unit]\n")

    monkeypatch.setattr(
        artifacts.pacman, "owners_of",
        lambda c: {Path(p): None for p in c},
    )
    found = artifacts.scan([(str(root), artifacts.CLASS_UNIT)])
    assert [c.path.name for c in found] == ["mine.service"]


def test_scan_excludes_package_owned_files(tmp_path, monkeypatch):
    root = tmp_path / "hooks"
    root.mkdir()
    (root / "distro.hook").write_text("x")
    (root / "mine.hook").write_text("y")

    monkeypatch.setattr(
        artifacts.pacman, "owners_of",
        lambda c: {
            Path(p): ("somepkg" if Path(p).name == "distro.hook" else None)
            for p in c
        },
    )
    found = artifacts.scan([(str(root), artifacts.CLASS_HOOK)])
    assert [c.path.name for c in found] == ["mine.hook"]


def test_scan_labels_sysforge_hooks_rather_than_hiding_them(tmp_path, monkeypatch):
    from sysforge.primitives import pacman_hooks

    root = tmp_path / "hooks"
    root.mkdir()
    (root / pacman_hooks.HOOK_NAMES[0]).write_text("x")
    (root / "mine.hook").write_text("y")

    monkeypatch.setattr(
        artifacts.pacman, "owners_of",
        lambda c: {Path(p): None for p in c},
    )
    found = {c.path.name: c.owner for c in artifacts.scan(
        [(str(root), artifacts.CLASS_HOOK)]
    )}
    assert found[pacman_hooks.HOOK_NAMES[0]] == "sysforge"
    assert found["mine.hook"] == "you"


def test_scan_marks_unknown_when_ownership_lookup_fails(tmp_path, monkeypatch):
    """A failed lookup must never be presented as 'this is yours'."""
    root = tmp_path / "hooks"
    root.mkdir()
    (root / "mine.hook").write_text("y")

    monkeypatch.setattr(artifacts.pacman, "owners_of", lambda c: {})
    found = artifacts.scan([(str(root), artifacts.CLASS_HOOK)])
    assert [c.owner for c in found] == ["unknown"]


def test_scan_missing_root_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts.pacman, "owners_of", lambda c: {})
    assert artifacts.scan([(str(tmp_path / "absent"), artifacts.CLASS_SCRIPT)]) == []


def test_scan_does_not_label_sysforge_owned_basename_outside_hook_class(tmp_path, monkeypatch):
    """A user script sharing a basename with a sysforge hook, sitting in a
    script-class root, must never be labelled owner=sysforge — the basename
    guard exists only to bridge the hook scan-root/install-dir mismatch."""
    from sysforge.primitives import pacman_hooks

    root = tmp_path / "scripts"
    root.mkdir()
    (root / pacman_hooks.HOOK_NAMES[0]).write_text("mine, not sysforge's")

    monkeypatch.setattr(
        artifacts.pacman, "owners_of",
        lambda c: {Path(p): None for p in c},
    )
    found = {c.path.name: c.owner for c in artifacts.scan(
        [(str(root), artifacts.CLASS_SCRIPT)]
    )}
    assert found[pacman_hooks.HOOK_NAMES[0]] == "you"


def test_roots_from_config_uses_configured_table():
    cfg = {
        "artifacts": {
            "roots": [
                {"path": "/tmp/foo", "class": artifacts.CLASS_SCRIPT},
                {"path": "/tmp/bar", "class": artifacts.CLASS_HOOK},
            ]
        }
    }
    assert artifacts.roots_from_config(cfg) == (
        ("/tmp/foo", artifacts.CLASS_SCRIPT),
        ("/tmp/bar", artifacts.CLASS_HOOK),
    )


@pytest.mark.parametrize("cfg", [{}, {"artifacts": {}}, {"artifacts": {"roots": []}}])
def test_roots_from_config_falls_back_to_defaults_when_absent_or_empty(cfg):
    assert artifacts.roots_from_config(cfg) == artifacts.DEFAULT_ROOTS


def test_roots_from_config_skips_row_with_invalid_class(monkeypatch):
    warnings = []
    monkeypatch.setattr(artifacts.log, "warn", lambda tag, msg: warnings.append(msg))
    cfg = {
        "artifacts": {
            "roots": [
                {"path": "/tmp/foo", "class": "not-a-real-class"},
                {"path": "/tmp/bar", "class": artifacts.CLASS_SCRIPT},
            ]
        }
    }
    assert artifacts.roots_from_config(cfg) == (("/tmp/bar", artifacts.CLASS_SCRIPT),)
    assert warnings


def test_roots_from_config_skips_malformed_row_missing_key(monkeypatch):
    warnings = []
    monkeypatch.setattr(artifacts.log, "warn", lambda tag, msg: warnings.append(msg))
    cfg = {
        "artifacts": {
            "roots": [
                {"path": "/tmp/foo"},  # missing class
                {"path": "/tmp/bar", "class": artifacts.CLASS_SCRIPT},
            ]
        }
    }
    assert artifacts.roots_from_config(cfg) == (("/tmp/bar", artifacts.CLASS_SCRIPT),)
    assert warnings


AUTH = "a" * 64
DEPL = "b" * 64
OTHER = "c" * 64


def _art(tmp_path, auth, deployed):
    return artifacts.Artifact(
        name="f.sh", dest=tmp_path / "live" / "f.sh", cls=artifacts.CLASS_SCRIPT,
        auth_hash=auth, deployed_hash=deployed, deployed_at="2026-01-01T00:00:00Z",
    )


def _write_live(tmp_path, content: bytes | None):
    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    p = live / "f.sh"
    if content is None:
        p.unlink(missing_ok=True)
    else:
        p.write_bytes(content)
    return p


@pytest.mark.parametrize(
    "auth,deployed,live_bytes,expected",
    [
        # all three agree
        (None, None, b"same", "ok"),
        # managed copy edited, live still matches last deploy
        (OTHER, None, b"same", "pending"),
        # live changed outside sysforge
        (None, None, b"changed", "drifted"),
        # both sides moved
        (OTHER, None, b"changed", "conflict"),
        # live file deleted
        (None, None, None, "missing"),
    ],
)
def test_status_of_three_way_matrix(tmp_path, auth, deployed, live_bytes, expected):
    """auth/deployed default to the hash of b'same' so each row varies one axis."""
    base = artifacts.hash_bytes(b"same")
    art = _art(tmp_path, auth or base, deployed or base)
    _write_live(tmp_path, live_bytes)
    reg = _reg(tmp_path)
    assert artifacts.status_of(reg, art) == expected


def test_status_never_deployed_with_live_file_is_conflict(tmp_path):
    """Adopted but never deployed, yet something exists at dest that differs:
    we have no last-deployed anchor, so this needs a human."""
    art = _art(tmp_path, AUTH, None)
    art = artifacts.Artifact(**{**art.__dict__, "deployed_hash": None})
    _write_live(tmp_path, b"unexpected")
    assert artifacts.status_of(_reg(tmp_path), art) == "conflict"


def test_script_root_on_path_true_when_present(tmp_path, monkeypatch):
    root = tmp_path / "scripts"
    root.mkdir()
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("PATH", f"/usr/bin:{root}")
    assert artifacts.script_root_on_path(root) is True


def test_script_root_on_path_false_when_absent(tmp_path, monkeypatch):
    root = tmp_path / "scripts"
    root.mkdir()
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/usr/sbin")
    assert artifacts.script_root_on_path(root) is False


def test_script_root_on_path_ignores_trailing_slash(tmp_path, monkeypatch):
    root = tmp_path / "scripts"
    root.mkdir()
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("PATH", f"/usr/bin:{root}/")
    assert artifacts.script_root_on_path(root) is True


def test_script_root_on_path_matches_through_symlink(tmp_path, monkeypatch):
    real = tmp_path / "real-scripts"
    real.mkdir()
    link = tmp_path / "scripts"
    link.symlink_to(real)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("PATH", f"/usr/bin:{real}")
    assert artifacts.script_root_on_path(link) is True


def test_script_root_on_path_abstains_under_sudo(tmp_path, monkeypatch):
    """sudo's secure_path replaces PATH — the process PATH is not the user's,
    so the check must not claim the root is missing."""
    root = tmp_path / "scripts"
    root.mkdir()
    monkeypatch.setenv("SUDO_USER", "keith")
    monkeypatch.setenv("PATH", "/usr/bin:/usr/sbin")
    assert artifacts.script_root_on_path(root) is None


def test_script_root_on_path_false_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    assert artifacts.script_root_on_path(tmp_path / "absent") is False


def test_adopt_copies_content_and_records_entry(tmp_path):
    reg = _reg(tmp_path)
    src = tmp_path / "scripts" / "backup.sh"
    src.parent.mkdir(parents=True)
    src.write_text("#!/bin/sh\necho hi\n")

    art = artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)

    assert reg.content_path("backup.sh").read_text() == "#!/bin/sh\necho hi\n"
    assert reg.load()["backup.sh"].dest == src
    assert art.auth_hash == art.deployed_hash
    assert art.deployed_at is not None


def test_adopt_leaves_source_in_place(tmp_path):
    reg = _reg(tmp_path)
    src = tmp_path / "scripts" / "s.sh"
    src.parent.mkdir(parents=True)
    src.write_text("x")
    artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)
    assert src.exists(), "adopt must copy, never move"


def test_freshly_adopted_artifact_reads_ok(tmp_path):
    reg = _reg(tmp_path)
    src = tmp_path / "scripts" / "s.sh"
    src.parent.mkdir(parents=True)
    src.write_text("x")
    art = artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)
    assert artifacts.status_of(reg, art) == artifacts.STATUS_OK


def test_adopt_rejects_unknown_class(tmp_path):
    reg = _reg(tmp_path)
    src = tmp_path / "s.sh"
    src.write_text("x")
    with pytest.raises(artifacts.ArtifactError, match="unknown class"):
        artifacts.adopt(reg, src, cls="nonsense")


def test_adopt_rejects_missing_source(tmp_path):
    reg = _reg(tmp_path)
    with pytest.raises(artifacts.ArtifactError, match="not found"):
        artifacts.adopt(reg, tmp_path / "absent.sh", cls=artifacts.CLASS_SCRIPT)


def test_adopt_rejects_duplicate_name(tmp_path):
    reg = _reg(tmp_path)
    src = tmp_path / "s.sh"
    src.write_text("x")
    artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)
    with pytest.raises(artifacts.ArtifactError, match="already managed"):
        artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)


def test_adopt_refuses_sysforge_owned_artifact(tmp_path):
    from sysforge.primitives import pacman_hooks
    reg = _reg(tmp_path)
    src = tmp_path / "hooks" / pacman_hooks.HOOK_NAMES[0]
    src.parent.mkdir(parents=True)
    src.write_text("x")
    with pytest.raises(artifacts.ArtifactError, match="sysforge"):
        artifacts.adopt(reg, src, cls=artifacts.CLASS_HOOK)


def test_adopt_does_not_refuse_non_hook_class_sharing_sysforge_basename(tmp_path):
    """The sysforge-owned basename guard is scoped to CLASS_HOOK — a script
    that happens to share a basename with a sysforge hook must stay
    adoptable (mirrors the scan()-side scoping)."""
    from sysforge.primitives import pacman_hooks
    reg = _reg(tmp_path)
    src = tmp_path / "scripts" / pacman_hooks.HOOK_NAMES[0]
    src.parent.mkdir(parents=True)
    src.write_text("x")
    art = artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)
    assert art.name == pacman_hooks.HOOK_NAMES[0]


def test_class_for_path_infers_from_root(tmp_path):
    roots = ((str(tmp_path / "hooks"), artifacts.CLASS_HOOK),)
    assert artifacts.class_for_path(tmp_path / "hooks" / "a.hook", roots) == \
        artifacts.CLASS_HOOK
    assert artifacts.class_for_path(tmp_path / "elsewhere" / "a", roots) is None


# ---------------------------------------------------------------------------
# Per-class deploy/remove contracts (Task 11)
# ---------------------------------------------------------------------------


class _Done:
    returncode = 0
    stdout = ""
    stderr = ""


def _capture_priv(monkeypatch):
    calls = []
    monkeypatch.setattr(
        artifacts, "run_privileged",
        lambda argv, **kw: calls.append(argv) or _Done(),
    )
    return calls


def test_post_deploy_reloads_for_units_only(monkeypatch, tmp_path):
    calls = _capture_priv(monkeypatch)
    unit = artifacts.Artifact("a.service", tmp_path / "a.service",
                              artifacts.CLASS_UNIT, AUTH, AUTH, None)
    script = artifacts.Artifact("a.sh", tmp_path / "a.sh",
                              artifacts.CLASS_SCRIPT, AUTH, AUTH, None)
    artifacts.post_deploy(script)
    assert calls == [], "scripts need no post-action"
    artifacts.post_deploy(unit)
    assert any("daemon-reload" in " ".join(c) for c in calls)


def test_pre_remove_disables_only_when_unit_enabled(monkeypatch, tmp_path):
    calls = _capture_priv(monkeypatch)
    monkeypatch.setattr(artifacts, "unit_is_enabled", lambda n: False)
    unit = artifacts.Artifact("a.service", tmp_path / "a.service",
                              artifacts.CLASS_UNIT, AUTH, AUTH, None)
    artifacts.pre_remove(unit)
    assert not any("disable" in " ".join(c) for c in calls)

    calls.clear()
    monkeypatch.setattr(artifacts, "unit_is_enabled", lambda n: True)
    artifacts.pre_remove(unit)
    assert any("disable" in " ".join(c) and "--now" in " ".join(c) for c in calls)


def test_write_live_script_is_unprivileged(monkeypatch, tmp_path):
    calls = _capture_priv(monkeypatch)
    dest = tmp_path / "scripts" / "s.sh"
    art = artifacts.Artifact("s.sh", dest, artifacts.CLASS_SCRIPT, AUTH, AUTH, None)
    artifacts.write_live(art, b"#!/bin/sh\n")
    assert dest.read_bytes() == b"#!/bin/sh\n"
    assert dest.stat().st_mode & 0o777 == 0o755
    assert calls == [], "user-owned scripts must not escalate"


def test_write_live_unit_is_privileged(monkeypatch, tmp_path):
    calls = _capture_priv(monkeypatch)
    dest = tmp_path / "etc" / "systemd" / "system" / "a.service"
    art = artifacts.Artifact("a.service", dest, artifacts.CLASS_UNIT, AUTH, AUTH, None)
    artifacts.write_live(art, b"[Unit]\n")
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "install"
    assert "0644" in " ".join(argv) or "644" in " ".join(argv)
    assert str(dest) in argv


def test_remove_live_script_unprivileged(tmp_path):
    calls_holder = []
    dest = tmp_path / "s.sh"
    dest.write_text("x")
    art = artifacts.Artifact("s.sh", dest, artifacts.CLASS_SCRIPT, AUTH, AUTH, None)
    artifacts.remove_live(art)
    assert not dest.exists()
    assert calls_holder == []


def test_remove_live_hook_privileged(monkeypatch, tmp_path):
    calls = _capture_priv(monkeypatch)
    dest = tmp_path / "hooks" / "a.hook"
    art = artifacts.Artifact("a.hook", dest, artifacts.CLASS_HOOK, AUTH, AUTH, None)
    artifacts.remove_live(art)
    assert any("rm" in c[0] and str(dest) in c for c in calls)


def test_unit_is_enabled_false_on_error(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("no systemctl")
    monkeypatch.setattr(artifacts.subprocess, "run", _boom)
    assert artifacts.unit_is_enabled("a.service") is False


def _adopted(tmp_path, content=b"v1"):
    reg = _reg(tmp_path)
    src = tmp_path / "live" / "s.sh"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(content)
    artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)
    return reg, src


def test_deploy_pending_writes_and_clears_status(tmp_path):
    reg, src = _adopted(tmp_path)
    reg.content_path("s.sh").write_bytes(b"v2")
    artifacts.rehash(reg, "s.sh")

    assert artifacts.deploy(reg, "s.sh") == artifacts.STATUS_OK
    assert src.read_bytes() == b"v2"
    assert artifacts.status_of(reg, reg.load()["s.sh"]) == artifacts.STATUS_OK


def test_deploy_refuses_on_drift(tmp_path):
    reg, src = _adopted(tmp_path)
    src.write_bytes(b"hand-edited")
    with pytest.raises(artifacts.ArtifactError, match="drifted"):
        artifacts.deploy(reg, "s.sh")
    assert src.read_bytes() == b"hand-edited", "refusal must not write"


def test_deploy_force_overwrites_drift(tmp_path):
    reg, src = _adopted(tmp_path)
    src.write_bytes(b"hand-edited")
    assert artifacts.deploy(reg, "s.sh", force=True) == artifacts.STATUS_OK
    assert src.read_bytes() == b"v1"


def test_deploy_adopt_live_pulls_live_into_managed(tmp_path):
    reg, src = _adopted(tmp_path)
    src.write_bytes(b"hand-edited")
    assert artifacts.deploy(reg, "s.sh", adopt_live=True) == artifacts.STATUS_OK
    assert reg.content_path("s.sh").read_bytes() == b"hand-edited"
    assert src.read_bytes() == b"hand-edited"


def test_deploy_refuses_on_conflict(tmp_path):
    reg, src = _adopted(tmp_path)
    reg.content_path("s.sh").write_bytes(b"managed-edit")
    artifacts.rehash(reg, "s.sh")
    src.write_bytes(b"live-edit")
    with pytest.raises(artifacts.ArtifactError, match="conflict"):
        artifacts.deploy(reg, "s.sh")
    assert src.read_bytes() == b"live-edit", "refusal must not write the live file"


def test_deploy_missing_rewrites_the_file(tmp_path):
    reg, src = _adopted(tmp_path)
    src.unlink()
    assert artifacts.deploy(reg, "s.sh") == artifacts.STATUS_OK
    assert src.read_bytes() == b"v1"


def test_deploy_mutually_exclusive_force_and_adopt_live(tmp_path):
    reg, src = _adopted(tmp_path)
    with pytest.raises(artifacts.ArtifactError, match="mutually exclusive"):
        artifacts.deploy(reg, "s.sh", force=True, adopt_live=True)


def test_remove_unlinks_live_but_keeps_managed_copy(tmp_path):
    reg, src = _adopted(tmp_path)
    artifacts.remove(reg, "s.sh")
    assert not src.exists()
    assert reg.content_path("s.sh").exists(), "without --purge the copy survives"
    assert "s.sh" in reg.load(), "entry remains so it can be redeployed"


def test_remove_purge_drops_copy_and_registry_row(tmp_path):
    reg, src = _adopted(tmp_path)
    artifacts.remove(reg, "s.sh", purge=True)
    assert not src.exists()
    assert not reg.content_path("s.sh").exists()
    assert "s.sh" not in reg.load()


def test_remove_runs_pre_remove_before_unlink(tmp_path, monkeypatch):
    reg, src = _adopted(tmp_path)
    order = []
    monkeypatch.setattr(artifacts, "pre_remove", lambda a: order.append("pre"))
    real = artifacts.remove_live
    monkeypatch.setattr(
        artifacts, "remove_live",
        lambda a: (order.append("unlink"), real(a))[1],
    )
    artifacts.remove(reg, "s.sh")
    assert order == ["pre", "unlink"]


def test_remove_unknown_name_raises(tmp_path):
    with pytest.raises(artifacts.ArtifactError, match="not managed"):
        artifacts.remove(_reg(tmp_path), "nope")


# --- Whole-branch review fixes ---------------------------------------------


def test_deploy_adopt_live_refuses_on_missing_without_crashing(tmp_path):
    # A missing live file has nothing to adopt: --adopt-live must raise a
    # clean ArtifactError, not an unguarded FileNotFoundError traceback.
    reg, src = _adopted(tmp_path)
    src.unlink()
    with pytest.raises(artifacts.ArtifactError, match="absent"):
        artifacts.deploy(reg, "s.sh", adopt_live=True)


def test_deploy_adopt_live_refuses_on_pending_preserving_managed(tmp_path):
    # An undeployed managed edit (pending) must not be silently overwritten by
    # --adopt-live pulling back the older live content.
    reg, src = _adopted(tmp_path)
    reg.content_path("s.sh").write_bytes(b"managed-edit")
    artifacts.rehash(reg, "s.sh")
    assert artifacts.status_of(reg, reg.load()["s.sh"]) == artifacts.STATUS_PENDING

    with pytest.raises(artifacts.ArtifactError, match="pending"):
        artifacts.deploy(reg, "s.sh", adopt_live=True)
    assert reg.content_path("s.sh").read_bytes() == b"managed-edit", (
        "the pending managed edit must survive the refusal"
    )
    assert src.read_bytes() == b"v1", "refusal must not write the live file"


def test_remove_refuses_on_drift_without_force(tmp_path):
    reg, src = _adopted(tmp_path)
    src.write_bytes(b"hand-edited")
    with pytest.raises(artifacts.ArtifactError, match="drifted"):
        artifacts.remove(reg, "s.sh")
    assert src.read_bytes() == b"hand-edited", "refusal must not unlink the live file"
    assert "s.sh" in reg.load(), "refusal must not touch the registry"


def test_remove_force_removes_drifted(tmp_path):
    reg, src = _adopted(tmp_path)
    src.write_bytes(b"hand-edited")
    artifacts.remove(reg, "s.sh", force=True)
    assert not src.exists()


def test_remove_pending_removes_without_force(tmp_path):
    # Pending = managed edit not yet deployed; the live file still matches what
    # was deployed, so removing it loses nothing unrecoverable — no force needed.
    reg, src = _adopted(tmp_path)
    reg.content_path("s.sh").write_bytes(b"managed-edit")
    artifacts.rehash(reg, "s.sh")
    assert artifacts.status_of(reg, reg.load()["s.sh"]) == artifacts.STATUS_PENDING
    artifacts.remove(reg, "s.sh")
    assert not src.exists()
    assert "s.sh" in reg.load(), "non-purge keeps the redeployable entry"


def test_adopt_records_absolute_dest_from_relative_path(tmp_path, monkeypatch):
    reg = _reg(tmp_path)
    live = tmp_path / "live"
    live.mkdir()
    (live / "rel.sh").write_text("x")
    monkeypatch.chdir(live)

    art = artifacts.adopt(reg, Path("rel.sh"), cls=artifacts.CLASS_SCRIPT)
    assert art.dest.is_absolute(), "a relative adopt argument must be absolutized"
    assert art.dest == (live / "rel.sh").resolve()


def test_class_for_path_uses_configured_roots(tmp_path, monkeypatch):
    configured = ((str(tmp_path / "myhooks"), artifacts.CLASS_HOOK),)
    monkeypatch.setattr(artifacts, "roots_from_config", lambda: configured)
    (tmp_path / "myhooks").mkdir()
    assert artifacts.class_for_path(tmp_path / "myhooks" / "a.hook") == \
        artifacts.CLASS_HOOK


def test_default_script_root_reflects_configured_roots(tmp_path, monkeypatch):
    configured = ((str(tmp_path / "bin"), artifacts.CLASS_SCRIPT),)
    monkeypatch.setattr(artifacts, "roots_from_config", lambda: configured)
    assert artifacts.default_script_root() == tmp_path / "bin"


def test_scan_excludes_symlinks(tmp_path):
    root = tmp_path / "hooks"
    root.mkdir()
    (root / "real.hook").write_text("y")
    target = tmp_path / "target.hook"
    target.write_text("z")
    (root / "alias.hook").symlink_to(target)

    found = [c.path.name for c in artifacts.scan(
        [(str(root), artifacts.CLASS_HOOK)]
    )]
    assert found == ["real.hook"], "symlinked entries are enablement/alias state"


# ---------------------------------------------------------------------------
# Real-pacman integration tests (Task 14)
# ---------------------------------------------------------------------------

pacman_required = pytest.mark.skipif(
    shutil.which("pacman") is None, reason="requires a live pacman"
)


@pacman_required
def test_owners_of_against_real_pacman_classifies_both_directions(tmp_path):
    """Guards the real `pacman -Qo` output format: owned paths resolve to a
    package name, definitively-unowned paths map to None (not absent)."""
    from sysforge.primitives import pacman

    owned = Path("/etc/pacman.conf")
    if not owned.exists():
        pytest.skip("/etc/pacman.conf absent")
    unowned = tmp_path / "definitely-not-packaged.txt"
    unowned.write_text("x")

    result = pacman.owners_of([owned, unowned])

    assert result.get(owned), "an owned path must resolve to a package name"
    assert unowned in result, "an unowned path must be PRESENT, not absent"
    assert result[unowned] is None, "an unowned path must map to None"


@pacman_required
def test_scan_against_real_pacman_excludes_package_owned(tmp_path):
    """End-to-end discovery with no mocking of the ownership lookup."""
    root = tmp_path / "hooks"
    root.mkdir()
    (root / "mine.hook").write_text("y")

    found = [c.path.name for c in artifacts.scan(
        [(str(root), artifacts.CLASS_HOOK)]
    )]
    assert found == ["mine.hook"]
    assert all(c.owner == artifacts.OWNER_YOU for c in artifacts.scan(
        [(str(root), artifacts.CLASS_HOOK)]
    ))


# ---------------------------------------------------------------------------
# IgnoreList tests (Task 1)
# ---------------------------------------------------------------------------


def _ignore(tmp_path):
    return artifacts.IgnoreList(state_dir=tmp_path / "state")


def test_ignorelist_roundtrip(tmp_path):
    ig = _ignore(tmp_path)
    live = tmp_path / "u.sh"
    live.write_text("x")
    ig.save({live: "d" * 64})
    assert ig.load() == {live: "d" * 64}


def test_ignorelist_load_prunes_missing_file(tmp_path):
    ig = _ignore(tmp_path)
    present = tmp_path / "here.sh"
    present.write_text("x")
    gone = tmp_path / "gone.sh"
    ig.save({present: "a" * 64, gone: "b" * 64})
    # gone.sh never created — a deleted-then-recreated file must be re-offerable.
    assert ig.load() == {present: "a" * 64}


def test_ignorelist_corrupt_raises(tmp_path):
    ig = _ignore(tmp_path)
    ig.path.parent.mkdir(parents=True, exist_ok=True)
    ig.path.write_text("this is not [ valid toml")
    with pytest.raises(artifacts.ArtifactRegistryError, match="ignore-list"):
        ig.load()


# ---------------------------------------------------------------------------
# iter_offerable tests (Task 2)
# ---------------------------------------------------------------------------


def _cand(path, cls=artifacts.CLASS_SCRIPT, owner=artifacts.OWNER_YOU):
    return artifacts.Candidate(path=path, cls=cls, owner=owner)


def test_iter_offerable_excludes_managed_and_sysforge(tmp_path, monkeypatch):
    reg = _reg(tmp_path)
    managed = tmp_path / "managed.sh"
    managed.write_text("m")
    artifacts.adopt(reg, managed, cls=artifacts.CLASS_SCRIPT)

    free = tmp_path / "free.sh"
    free.write_text("f")
    owned = tmp_path / "sf.hook"
    owned.write_text("s")
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [
        _cand(managed),
        _cand(free),
        _cand(owned, cls=artifacts.CLASS_HOOK, owner=artifacts.OWNER_SYSFORGE),
    ])

    got = [c.path for c in artifacts.iter_offerable(reg)]
    assert got == [free]


def test_iter_offerable_drops_ignored_matching_hash(tmp_path, monkeypatch):
    reg = _reg(tmp_path)
    ig = _ignore(tmp_path)
    declined = tmp_path / "no.sh"
    declined.write_text("body")
    ig.save({declined: artifacts.hash_file(declined)})
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [_cand(declined)])

    assert artifacts.iter_offerable(reg, ig) == []


def test_iter_offerable_reoffers_when_content_changed(tmp_path, monkeypatch):
    reg = _reg(tmp_path)
    ig = _ignore(tmp_path)
    declined = tmp_path / "no.sh"
    declined.write_text("body")
    ig.save({declined: artifacts.hash_file(declined)})
    declined.write_text("body v2")  # content moved on
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [_cand(declined)])

    assert [c.path for c in artifacts.iter_offerable(reg, ig)] == [declined]


def test_iter_offerable_without_ignore_shows_declined(tmp_path, monkeypatch):
    # Parity with `list --unmanaged`: ignore=None means the ignore step is skipped.
    reg = _reg(tmp_path)
    ig = _ignore(tmp_path)
    declined = tmp_path / "no.sh"
    declined.write_text("body")
    ig.save({declined: artifacts.hash_file(declined)})
    monkeypatch.setattr(artifacts, "scan", lambda roots=None: [_cand(declined)])

    assert [c.path for c in artifacts.iter_offerable(reg)] == [declined]


def test_iter_drifted_includes_drifted_conflict_missing(tmp_path):
    reg = _reg(tmp_path)
    # Adopt + deploy so status starts ok, then mutate the live file → drifted.
    src = tmp_path / "d.sh"
    src.write_text("orig")
    art = artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)
    artifacts.deploy(reg, "d.sh")           # live == deployed == auth → ok
    assert artifacts.status_of(reg, art) not in (
        artifacts.STATUS_DRIFTED, artifacts.STATUS_CONFLICT, artifacts.STATUS_MISSING
    )
    art.dest.write_text("changed-by-pacman")  # live moves → drifted
    drifted = artifacts.iter_drifted(reg)
    assert [a.name for a in drifted] == ["d.sh"]


def test_iter_drifted_excludes_ok_and_pending(tmp_path):
    reg = _reg(tmp_path)
    src = tmp_path / "p.sh"
    src.write_text("orig")
    artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)
    artifacts.deploy(reg, "p.sh")            # ok
    # Edit managed copy only → pending, not drift.
    reg.content_path("p.sh").write_text("edited")
    artifacts.rehash(reg, "p.sh")
    assert artifacts.iter_drifted(reg) == []


def test_iter_drifted_missing_live_file_is_reported(tmp_path):
    reg = _reg(tmp_path)
    src = tmp_path / "m.sh"
    src.write_text("orig")
    art = artifacts.adopt(reg, src, cls=artifacts.CLASS_SCRIPT)
    artifacts.deploy(reg, "m.sh")
    art.dest.unlink()                        # pacman removed it → missing
    assert [a.name for a in artifacts.iter_drifted(reg)] == ["m.sh"]


def test_shipped_artifacts_hook_is_posttransaction_no_needstargets():
    hook = (
        Path(__file__).resolve().parents[1]
        / "etc/pacman.d/hooks/sysforge-artifacts.hook"
    )
    text = hook.read_text()
    assert "When = PostTransaction" in text
    assert "NeedsTargets" not in text          # re-scans; targets are moot
    assert "pacman-hook-helper.sh artifacts" in text
