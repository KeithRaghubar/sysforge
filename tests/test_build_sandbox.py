"""
test_build_sandbox.py — the opt-in build sandbox (3.1.0-F7)

Covers the four things that decide whether a sandboxed build is actually
isolated, plus the seam that swaps the argv:

  1. Policy resolution
     - defaults are off, with the documented chroot location
     - [security] keys read; per-profile override wins both ways
     - suppressed() forces host builds and restores the policy

  2. Preflight (refuse rather than silently downgrade)
     - missing makechrootpkg / missing chroot root / stale upstream stash
     - a non-canonical filename is admitted (the swap reconciles it)
     - clean pass when all of them hold

  3. Argv + conf derivation
     - makechrootpkg shape, no sudo prefix, no -p, flags after --
     - -c/-u honour the policy; -I seeds the run's own artifacts
     - container conf repoints the dest keys and re-exports the env-only keys
     - dest env is read from the emitted conf, unquoted

  4. The invocation seam
     - policy off  → plain `makepkg -p PKGBUILD`
     - policy on   → makechrootpkg, scratch conf written then removed
     - RLIMIT_AS cap dropped (it would cap the wrapper, not the build)
     - session registry: dedupe, .sig skip, vanished files filtered

  5. Canonical-PKGBUILD swap (3.2.0-B1)
     - the patched sidecar occupies ./PKGBUILD for the build, and only for it
     - the checkout is restored on success, on failure, and on exception
     - a stale stash from an interrupted run is refused, never clobbered
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives import build_sandbox as bs
from sysforge.primitives.makepkg_invoke import invoke_makepkg


@pytest.fixture(autouse=True)
def _clean_policy():
    """Every test starts from the default (host-build) policy and registry."""
    bs.reset_policy()
    bs.reset_session()
    yield
    bs.reset_policy()
    bs.reset_session()


def _chroot(tmp_path: Path) -> Path:
    """A chroot dir that passes preflight (only <dir>/root must exist)."""
    root = tmp_path / "chroot" / "root"
    root.mkdir(parents=True, exist_ok=True)
    return tmp_path / "chroot"


def _policy(tmp_path: Path, **kw) -> bs.SandboxPolicy:
    return bs.SandboxPolicy(
        enabled=True, chroot_dir=_chroot(tmp_path),
        clean=kw.pop("clean", True), update=kw.pop("update", True),
    )


# ---------------------------------------------------------------------------
# 1. Policy resolution
# ---------------------------------------------------------------------------

def test_default_policy_is_off():
    pol = bs.resolve_sandbox({})
    assert pol.enabled is False
    assert pol.chroot_dir == Path(bs.DEFAULT_CHROOT_DIR).expanduser()


def test_unset_policy_is_permissive():
    """A library caller that never calls set_policy still builds on the host."""
    assert bs.get_policy().enabled is False


def test_policy_read_from_security_section(tmp_path):
    pol = bs.resolve_sandbox({
        "sandbox_builds": True,
        "sandbox_chroot_dir": str(tmp_path / "cr"),
        "sandbox_clean": False,
        "sandbox_update": False,
    })
    assert pol.enabled is True
    assert pol.chroot_dir == tmp_path / "cr"
    assert pol.clean is False and pol.update is False


def test_chroot_dir_tilde_expanded():
    pol = bs.resolve_sandbox({"sandbox_chroot_dir": "~/somewhere"})
    assert not str(pol.chroot_dir).startswith("~")


def test_profile_can_opt_in_over_global_off():
    pol = bs.resolve_sandbox({})
    assert bs.for_profile(pol, {"sandbox_builds": True}).enabled is True


def test_profile_can_opt_out_over_global_on():
    pol = bs.resolve_sandbox({"sandbox_builds": True})
    assert bs.for_profile(pol, {"sandbox_builds": False}).enabled is False


def test_profile_without_the_key_leaves_policy_alone():
    pol = bs.resolve_sandbox({"sandbox_builds": True})
    assert bs.for_profile(pol, {"CC": "clang"}).enabled is True
    assert bs.for_profile(pol, None).enabled is True


def test_suppressed_forces_host_build_and_restores(tmp_path):
    bs.set_policy(_policy(tmp_path))
    with bs.suppressed(True):
        assert bs.get_policy().enabled is False
    assert bs.get_policy().enabled is True


def test_suppressed_inactive_is_a_noop(tmp_path):
    bs.set_policy(_policy(tmp_path))
    with bs.suppressed(False):
        assert bs.get_policy().enabled is True


def test_suppressed_restores_on_exception(tmp_path):
    bs.set_policy(_policy(tmp_path))
    with pytest.raises(RuntimeError), bs.suppressed(True):
        raise RuntimeError("build failed")
    assert bs.get_policy().enabled is True


# ---------------------------------------------------------------------------
# 2. Preflight
# ---------------------------------------------------------------------------

def test_preflight_noop_when_disabled(tmp_path):
    # No chroot, no makechrootpkg — and no complaint, because nothing is
    # being isolated.
    bs.preflight(bs.SandboxPolicy(enabled=False), tmp_path / "PKGBUILD")


def test_preflight_requires_makechrootpkg(tmp_path):
    with patch("sysforge.primitives.build_sandbox.shutil.which", return_value=None):
        with pytest.raises(bs.SandboxUnavailable) as e:
            bs.preflight(_policy(tmp_path), tmp_path / "PKGBUILD")
    assert "devtools" in str(e.value)


def test_preflight_requires_chroot_root(tmp_path):
    pol = bs.SandboxPolicy(enabled=True, chroot_dir=tmp_path / "absent")
    with patch("sysforge.primitives.build_sandbox.shutil.which",
               return_value="/usr/bin/makechrootpkg"):
        with pytest.raises(bs.SandboxUnavailable) as e:
            bs.preflight(pol, tmp_path / "PKGBUILD")
    assert "mkarchroot" in str(e.value)


def test_preflight_admits_a_non_canonical_filename(tmp_path):
    """makechrootpkg reads pkgbase from ./PKGBUILD regardless of -p, but that
    is reconciled by the scoped swap (3.2.0-B1) rather than by refusing the
    build — sysforge never hands the seam the canonical name."""
    with patch("sysforge.primitives.build_sandbox.shutil.which",
               return_value="/usr/bin/makechrootpkg"):
        bs.preflight(_policy(tmp_path), tmp_path / "PKGBUILD-git")


def test_preflight_refuses_a_stale_upstream_stash(tmp_path):
    """An interrupted run's stash means the canonical name holds a *patched*
    file; swapping again would lose the upstream one for good."""
    (tmp_path / bs.UPSTREAM_STASH_NAME).write_text("# real upstream\n")
    with patch("sysforge.primitives.build_sandbox.shutil.which",
               return_value="/usr/bin/makechrootpkg"):
        with pytest.raises(bs.SandboxUnavailable) as e:
            bs.preflight(_policy(tmp_path), tmp_path / "PKGBUILD.sysforge")
    assert "interrupted" in str(e.value)


def test_preflight_passes_when_everything_is_in_place(tmp_path):
    with patch("sysforge.primitives.build_sandbox.shutil.which",
               return_value="/usr/bin/makechrootpkg"):
        bs.preflight(_policy(tmp_path), tmp_path / "PKGBUILD")


# ---------------------------------------------------------------------------
# 3. Argv + conf derivation
# ---------------------------------------------------------------------------

def test_build_argv_shape(tmp_path):
    argv = bs.build_argv(_policy(tmp_path), ["--noconfirm", "-C"],
                         conf_dir_name=".sysforge-chroot-abc")
    assert argv[0] == "makechrootpkg"
    assert argv[1:3] == ["-r", str(_chroot(tmp_path))]
    assert "-c" in argv and "-u" in argv
    sep = argv.index("--")
    assert argv[sep + 1] == (
        "--config=/startdir/.sysforge-chroot-abc/.sysforge-chroot-makepkg.conf"
    )
    assert argv[sep + 2:] == ["--noconfirm", "-C"]


def test_build_argv_never_prefixes_sudo(tmp_path):
    """makechrootpkg escalates itself and would lose its preserved env
    (PKGDEST & co) if wrapped in a sudo of ours."""
    argv = bs.build_argv(_policy(tmp_path), [], conf_dir_name="d")
    assert "sudo" not in argv


def test_build_argv_never_passes_dash_p(tmp_path):
    argv = bs.build_argv(_policy(tmp_path), [], conf_dir_name="d")
    assert "-p" not in argv


def test_build_argv_honours_clean_and_update_off(tmp_path):
    argv = bs.build_argv(_policy(tmp_path, clean=False, update=False), [],
                         conf_dir_name="d")
    assert "-c" not in argv and "-u" not in argv


def test_build_argv_seeds_install_pkgs(tmp_path):
    a = tmp_path / "a-1-1-x86_64.pkg.tar.zst"
    b = tmp_path / "b-1-1-x86_64.pkg.tar.zst"
    argv = bs.build_argv(_policy(tmp_path), [], conf_dir_name="d",
                         install_pkgs=[a, b])
    assert argv.count("-I") == 2
    assert str(a) in argv and str(b) in argv
    # Seeds must precede the makepkg args separator.
    assert argv.index(str(b)) < argv.index("--")


def test_chroot_conf_repoints_dest_keys(tmp_path):
    conf = tmp_path / "makepkg.conf"
    conf.write_text('CFLAGS="-O2"\nPKGDEST="/home/u/packages"\nBUILDDIR="/home/u/builds"\n')
    text = bs.chroot_conf_text(conf)
    assert 'CFLAGS="-O2"' in text          # original body kept
    # Re-assigned after the body, so the container values win when sourced.
    for key, value in (("PKGDEST", "/pkgdest"), ("BUILDDIR", "/build"),
                       ("SRCDEST", "/srcdest"), ("LOGDEST", "/logdest"),
                       ("SRCPKGDEST", "/srcpkgdest")):
        assert text.index(f"{key}={value}") > text.index('CFLAGS="-O2"')


def test_chroot_conf_exports_env_only_keys(tmp_path):
    """CC/CXX are deliberately absent from the conf on the host path (they are
    delivered via the invocation env). Inside the container there is no
    invocation env, so the conf is the only channel left."""
    conf = tmp_path / "makepkg.conf"
    conf.write_text('CFLAGS="-O2"\n')
    text = bs.chroot_conf_text(conf, exports={"CC": "clang", "CXX": "clang++"})
    assert "export CC=clang" in text
    assert "export CXX=clang++" in text


def test_chroot_conf_quotes_export_values(tmp_path):
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    text = bs.chroot_conf_text(conf, exports={"RUSTFLAGS": "-C target-cpu=native"})
    assert "export RUSTFLAGS='-C target-cpu=native'" in text


def test_chroot_conf_drops_host_specific_exports(tmp_path):
    """The user's shell is not a build input and must not cross the boundary."""
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    text = bs.chroot_conf_text(conf, exports={
        "PATH": "/home/u/.local/bin", "HOME": "/home/u",
        "MAKEPKG_CONF": "/tmp/x.conf", "PKGDEST": "/home/u/packages",
        "CC": "gcc",
    })
    assert "export CC=gcc" in text
    for denied in ("PATH", "HOME", "MAKEPKG_CONF"):
        assert f"export {denied}=" not in text
    assert "export PKGDEST=" not in text
    assert "PKGDEST=/pkgdest" in text


# --- host-only build accelerators (3.2.0-B2) -------------------------------
#
# The clean chroot is base-devel only. A BUILDENV naming ccache/distcc, or a
# RUSTC_WRAPPER naming sccache, points at host binaries that are not in there,
# and makepkg hard-errors before build() ever runs.


def test_chroot_conf_disables_host_only_buildenv(tmp_path):
    conf = tmp_path / "makepkg.conf"
    conf.write_text("BUILDENV=(!distcc color ccache !check !sign)\n")
    text = bs.chroot_conf_text(conf)
    tail = text[text.index("# --- sysforge build sandbox ---"):]
    assert "BUILDENV=(!distcc color !ccache !check !sign)" in tail


def test_chroot_conf_buildenv_preserves_unrelated_options(tmp_path):
    """Only the accelerators are touched: check/sign are policy, not tooling."""
    conf = tmp_path / "makepkg.conf"
    conf.write_text("BUILDENV=(distcc color ccache check sign)\n")
    text = bs.chroot_conf_text(conf)
    tail = text[text.index("# --- sysforge build sandbox ---"):]
    assert "BUILDENV=(!distcc color !ccache check sign)" in tail


def test_chroot_conf_buildenv_already_disabled_is_untouched(tmp_path):
    conf = tmp_path / "makepkg.conf"
    conf.write_text("BUILDENV=(!distcc color !ccache !check !sign)\n")
    text = bs.chroot_conf_text(conf)
    tail = text[text.index("# --- sysforge build sandbox ---"):]
    assert "BUILDENV=(!distcc color !ccache !check !sign)" in tail


def test_chroot_conf_omits_buildenv_when_the_conf_has_none(tmp_path):
    """No host BUILDENV to correct: makepkg's own default already disables
    both accelerators, so inventing a line here would be noise."""
    conf = tmp_path / "makepkg.conf"
    conf.write_text('CFLAGS="-O2"\n')
    text = bs.chroot_conf_text(conf)
    assert "BUILDENV=" not in text


def test_chroot_conf_drops_host_only_wrapper_exports(tmp_path):
    """RUSTC_WRAPPER=sccache would break every Rust build in the container
    exactly as BUILDENV's ccache breaks every C one."""
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    text = bs.chroot_conf_text(conf, exports={
        "RUSTC_WRAPPER": "sccache", "CCACHE_DIR": "/home/u/.ccache",
        "SCCACHE_DIR": "/home/u/.cache/sccache", "RUSTFLAGS": "-C opt-level=3",
    })
    assert "export RUSTFLAGS=" in text
    for denied in ("RUSTC_WRAPPER", "CCACHE_DIR", "SCCACHE_DIR"):
        assert f"export {denied}=" not in text


def test_dest_env_from_conf_unquotes(tmp_path):
    conf = tmp_path / "makepkg.conf"
    conf.write_text('PKGDEST="/home/u/packages"\nLOGDEST="/home/u/logs"\n')
    env = bs.dest_env_from_conf(conf)
    assert env["PKGDEST"] == "/home/u/packages"
    assert env["LOGDEST"] == "/home/u/logs"
    # Absent keys stay absent — makechrootpkg then falls back to $PWD, which
    # is where the host path leaves them too.
    assert "SRCPKGDEST" not in env


def test_chroot_env_drops_makepkg_conf(tmp_path):
    assert "MAKEPKG_CONF" not in bs.chroot_env({"MAKEPKG_CONF": "/tmp/x", "LANG": "C"})
    assert bs.chroot_env({"LANG": "C"})["LANG"] == "C"


def test_mem_cap_does_not_apply_under_sandbox(tmp_path):
    assert bs.mem_cap_applies(bs.SandboxPolicy(enabled=False)) is True
    assert bs.mem_cap_applies(_policy(tmp_path)) is False


# ---------------------------------------------------------------------------
# 4. Session artifact registry
# ---------------------------------------------------------------------------

def _artifact(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_text("pkg")
    return p


def test_registry_dedupes_and_preserves_order(tmp_path):
    a = _artifact(tmp_path, "a-1-1-x86_64.pkg.tar.zst")
    b = _artifact(tmp_path, "b-1-1-x86_64.pkg.tar.zst")
    bs.register_artifacts([a, b])
    bs.register_artifacts([a])
    assert bs.install_args() == [a, b]


def test_registry_skips_signatures(tmp_path):
    a = _artifact(tmp_path, "a-1-1-x86_64.pkg.tar.zst")
    sig = _artifact(tmp_path, "a-1-1-x86_64.pkg.tar.zst.sig")
    bs.register_artifacts([a, sig])
    assert bs.install_args() == [a]


def test_registry_filters_vanished_artifacts(tmp_path):
    a = _artifact(tmp_path, "a-1-1-x86_64.pkg.tar.zst")
    bs.register_artifacts([a])
    a.unlink()
    assert bs.install_args() == []


def test_reset_session_clears_registry(tmp_path):
    bs.register_artifacts([_artifact(tmp_path, "a-1-1-x86_64.pkg.tar.zst")])
    bs.reset_session()
    assert bs.install_args() == []


# ---------------------------------------------------------------------------
# 5. The invocation seam
# ---------------------------------------------------------------------------

def _invoke(pkgbuild, conf, profile, **kwargs):
    """Run invoke_makepkg with the subprocess seams stubbed; return capture."""
    captured = {}

    def fake_pty(cmd, *, cwd, env, line_callback, forward_bytes,
                 preexec_fn=None, **_kw):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(env)
        captured["preexec"] = preexec_fn
        # Snapshot what exists in the build dir *during* the run, so the
        # scratch-conf lifetime is observable.
        captured["during"] = sorted(p.name for p in Path(cwd).iterdir())
        # What ./PKGBUILD actually held while makepkg ran: under the sandbox
        # that must be the *patched* sidecar's text, not the upstream file.
        _canonical = Path(cwd) / "PKGBUILD"
        captured["during_pkgbuild"] = (
            _canonical.read_text() if _canonical.exists() else None)
        return 0

    def fake_popen(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kw.get("env") or {})
        m = MagicMock()
        m.wait.return_value = 0
        return m

    with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/root"}, clear=True), \
            patch("sysforge.primitives.makepkg_invoke.run_with_pty", side_effect=fake_pty), \
            patch("sysforge.primitives.makepkg_invoke.subprocess.Popen", side_effect=fake_popen), \
            patch("sysforge.primitives.build_sandbox.shutil.which",
                  return_value="/usr/bin/makechrootpkg"):
        invoke_makepkg(pkgbuild, conf, profile, **kwargs)
    return captured


def _pkg_and_conf(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    pb = src / "PKGBUILD"
    pb.write_text("# fake\n")
    conf = tmp_path / "makepkg.conf"
    conf.write_text('CFLAGS="-O2"\nPKGDEST="%s"\n' % (tmp_path / "packages"))
    return pb, conf


def test_seam_runs_plain_makepkg_when_policy_off(tmp_path):
    pb, conf = _pkg_and_conf(tmp_path)
    cap = _invoke(pb, conf, {})
    assert cap["cmd"][:3] == ["makepkg", "-p", "PKGBUILD"]
    assert cap["env"]["MAKEPKG_CONF"] == str(conf)


def test_seam_runs_makechrootpkg_when_policy_on(tmp_path):
    pb, conf = _pkg_and_conf(tmp_path)
    bs.set_policy(_policy(tmp_path))
    cap = _invoke(pb, conf, {"makepkg_flags": ["--noconfirm"]})
    assert cap["cmd"][0] == "makechrootpkg"
    assert "--noconfirm" in cap["cmd"]
    assert "sudo" not in cap["cmd"]


def test_seam_exports_dest_env_for_makechrootpkg(tmp_path):
    """makechrootpkg moves artifacts back to $PKGDEST — without this the
    sandbox path would drop them somewhere _find_built_packages never looks."""
    pb, conf = _pkg_and_conf(tmp_path)
    bs.set_policy(_policy(tmp_path))
    cap = _invoke(pb, conf, {})
    assert cap["env"]["PKGDEST"] == str(tmp_path / "packages")
    assert "MAKEPKG_CONF" not in cap["env"]


def test_seam_writes_then_removes_the_scratch_conf(tmp_path):
    pb, conf = _pkg_and_conf(tmp_path)
    bs.set_policy(_policy(tmp_path))
    cap = _invoke(pb, conf, {})
    scratch = [n for n in cap["during"] if n.startswith(bs.CHROOT_CONF_DIR_PREFIX)]
    assert len(scratch) == 1, "container conf must exist while makepkg runs"
    assert cap["cmd"][cap["cmd"].index("--") + 1] == (
        f"--config=/startdir/{scratch[0]}/{bs.CHROOT_CONF_NAME}"
    )
    # ...and is gone afterwards: it lives in the user's checkout.
    assert [p.name for p in pb.parent.iterdir()] == ["PKGBUILD"]


def test_seam_scratch_conf_removed_when_build_fails(tmp_path):
    pb, conf = _pkg_and_conf(tmp_path)
    bs.set_policy(_policy(tmp_path))

    def boom(*_a, **_kw):
        raise OSError("pty blew up")

    with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/root"}, clear=True), \
            patch("sysforge.primitives.makepkg_invoke.run_with_pty", side_effect=boom), \
            patch("sysforge.primitives.build_sandbox.shutil.which",
                  return_value="/usr/bin/makechrootpkg"):
        with pytest.raises(OSError):
            invoke_makepkg(pb, conf, {})

    assert [p.name for p in pb.parent.iterdir()] == ["PKGBUILD"]


def test_seam_seeds_artifacts_built_this_run(tmp_path):
    pb, conf = _pkg_and_conf(tmp_path)
    dep = _artifact(tmp_path, "dep-1-1-x86_64.pkg.tar.zst")
    bs.register_artifacts([dep])
    bs.set_policy(_policy(tmp_path))
    cap = _invoke(pb, conf, {})
    assert "-I" in cap["cmd"]
    assert str(dep) in cap["cmd"]


def test_seam_refuses_rather_than_downgrading(tmp_path):
    """No chroot → the build stops. Falling back to a host build would hand
    the user the exact exposure they opted out of."""
    pb, conf = _pkg_and_conf(tmp_path)
    bs.set_policy(bs.SandboxPolicy(enabled=True, chroot_dir=tmp_path / "absent"))
    with pytest.raises(bs.SandboxUnavailable):
        _invoke(pb, conf, {})


def test_seam_drops_rlimit_cap_under_sandbox(tmp_path):
    """RLIMIT_AS on the child would cap makechrootpkg, not the build."""
    pb, conf = _pkg_and_conf(tmp_path)
    profile = {"mem_limit": "8G"}
    bs.set_policy(_policy(tmp_path))
    with patch("sysforge.primitives.makepkg_invoke.resolve_child_mem_cap",
               return_value=8 * 1024**3) as cap_fn, \
            patch("sysforge.primitives.makepkg_invoke.make_child_preexec") as preexec:
        _invoke(pb, conf, profile)
    assert cap_fn.called
    preexec.assert_called_once_with(None)


def test_seam_keeps_rlimit_cap_on_the_host_path(tmp_path):
    pb, conf = _pkg_and_conf(tmp_path)
    with patch("sysforge.primitives.makepkg_invoke.resolve_child_mem_cap",
               return_value=8 * 1024**3), \
            patch("sysforge.primitives.makepkg_invoke.make_child_preexec") as preexec:
        _invoke(pb, conf, {"mem_limit": "8G"})
    preexec.assert_called_once_with(8 * 1024**3)


def test_seam_honours_the_profile_override(tmp_path):
    """A profile opting out builds on the host even with the global on."""
    pb, conf = _pkg_and_conf(tmp_path)
    bs.set_policy(_policy(tmp_path))
    cap = _invoke(pb, conf, {"sandbox_builds": False})
    assert cap["cmd"][0] == "makepkg"


def test_seam_profile_opt_in_over_global_off(tmp_path):
    pb, conf = _pkg_and_conf(tmp_path)
    bs.set_policy(bs.SandboxPolicy(enabled=False, chroot_dir=_chroot(tmp_path)))
    cap = _invoke(pb, conf, {"sandbox_builds": True})
    assert cap["cmd"][0] == "makechrootpkg"


def test_seam_throttle_prefix_stays_outside_the_container(tmp_path):
    """nice/ionice must wrap makechrootpkg — inside, they would only bind the
    wrapper's own short-lived process."""
    pb, conf = _pkg_and_conf(tmp_path)
    bs.set_policy(_policy(tmp_path))
    with patch("sysforge.primitives.makepkg_invoke.wrapper_argv",
               return_value=["nice", "-n", "19"]):
        cap = _invoke(pb, conf, {})
    assert cap["cmd"][:4] == ["nice", "-n", "19", "makechrootpkg"]


# ---------------------------------------------------------------------------
# 5. Canonical-PKGBUILD swap (3.2.0-B1)
# ---------------------------------------------------------------------------
#
# sysforge never builds the upstream PKGBUILD: every route through
# makepkg_wrapper._run_build reassigns pkgbuild_path to the patched sidecar
# PKGBUILD.sysforge. makechrootpkg can only build a file named PKGBUILD, so
# without a swap the sandbox refused every package it was ever pointed at.


def _patched_pair(tmp_path: Path):
    """The realistic seam input: an upstream PKGBUILD *and* its sidecar."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "PKGBUILD").write_text("# upstream\n")
    sidecar = src / "PKGBUILD.sysforge"
    sidecar.write_text("# patched by sysforge\n")
    conf = tmp_path / "makepkg.conf"
    conf.write_text('CFLAGS="-O2"\nPKGDEST="%s"\n' % (tmp_path / "packages"))
    return sidecar, conf


def test_canonical_swap_puts_the_sidecar_at_pkgbuild(tmp_path):
    sidecar, _ = _patched_pair(tmp_path)
    with bs.as_canonical_pkgbuild(sidecar) as canonical:
        assert canonical.name == "PKGBUILD"
        assert canonical.read_text() == "# patched by sysforge\n"
        assert not sidecar.exists()


def test_canonical_swap_restores_both_names(tmp_path):
    sidecar, _ = _patched_pair(tmp_path)
    before = sorted(p.name for p in sidecar.parent.iterdir())
    with bs.as_canonical_pkgbuild(sidecar):
        pass
    assert sorted(p.name for p in sidecar.parent.iterdir()) == before
    assert (sidecar.parent / "PKGBUILD").read_text() == "# upstream\n"
    assert sidecar.read_text() == "# patched by sysforge\n"


def test_canonical_swap_restores_on_exception(tmp_path):
    sidecar, _ = _patched_pair(tmp_path)
    with pytest.raises(RuntimeError), bs.as_canonical_pkgbuild(sidecar):
        raise RuntimeError("build blew up")
    assert (sidecar.parent / "PKGBUILD").read_text() == "# upstream\n"
    assert sidecar.read_text() == "# patched by sysforge\n"


def test_canonical_swap_is_a_noop_for_an_already_canonical_name(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    pb = src / "PKGBUILD"
    pb.write_text("# upstream\n")
    with bs.as_canonical_pkgbuild(pb) as canonical:
        assert canonical == pb
        assert [p.name for p in src.iterdir()] == ["PKGBUILD"]


def test_canonical_swap_without_an_upstream_sibling(tmp_path):
    """A sidecar whose upstream was already cleaned away still builds."""
    src = tmp_path / "src"
    src.mkdir()
    sidecar = src / "PKGBUILD.sysforge"
    sidecar.write_text("# patched\n")
    with bs.as_canonical_pkgbuild(sidecar) as canonical:
        assert canonical.read_text() == "# patched\n"
    assert [p.name for p in src.iterdir()] == ["PKGBUILD.sysforge"]


def test_canonical_swap_refuses_a_stale_stash(tmp_path):
    """An interrupted earlier run left the upstream stashed; clobbering it
    would destroy the user's checkout, so refuse instead."""
    sidecar, _ = _patched_pair(tmp_path)
    (sidecar.parent / bs.UPSTREAM_STASH_NAME).write_text("# real upstream\n")
    with pytest.raises(bs.SandboxUnavailable) as e:
        with bs.as_canonical_pkgbuild(sidecar):
            pass
    assert bs.UPSTREAM_STASH_NAME in str(e.value)


def test_preflight_accepts_the_patched_sidecar(tmp_path):
    """The regression: this is the only filename the real pipeline ever
    hands the seam, and preflight used to refuse it outright."""
    sidecar, _ = _patched_pair(tmp_path)
    with patch("sysforge.primitives.build_sandbox.shutil.which",
               return_value="/usr/bin/makechrootpkg"):
        bs.preflight(_policy(tmp_path), sidecar)


def test_seam_sandboxes_the_patched_sidecar(tmp_path):
    """End-to-end at the seam: the sandbox path must build the *patched*
    file, under the canonical name, and leave the checkout as it found it."""
    sidecar, conf = _patched_pair(tmp_path)
    bs.set_policy(_policy(tmp_path))
    cap = _invoke(sidecar, conf, {})
    assert cap["cmd"][0] == "makechrootpkg"
    assert "-p" not in cap["cmd"]
    # While makepkg ran, ./PKGBUILD was the patched text.
    assert cap["during_pkgbuild"] == "# patched by sysforge\n"
    assert sorted(p.name for p in sidecar.parent.iterdir()) == [
        "PKGBUILD", "PKGBUILD.sysforge"]
    assert (sidecar.parent / "PKGBUILD").read_text() == "# upstream\n"


def test_seam_restores_the_checkout_when_the_build_fails(tmp_path):
    sidecar, conf = _patched_pair(tmp_path)
    bs.set_policy(_policy(tmp_path))

    def boom(*_a, **_kw):
        raise OSError("pty blew up")

    with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/root"}, clear=True), \
            patch("sysforge.primitives.makepkg_invoke.run_with_pty", side_effect=boom), \
            patch("sysforge.primitives.build_sandbox.shutil.which",
                  return_value="/usr/bin/makechrootpkg"):
        with pytest.raises(OSError):
            invoke_makepkg(sidecar, conf, {})

    assert sorted(p.name for p in sidecar.parent.iterdir()) == [
        "PKGBUILD", "PKGBUILD.sysforge"]
    assert (sidecar.parent / "PKGBUILD").read_text() == "# upstream\n"


# ---------------------------------------------------------------------------
# Chroot toolchain provisioning (3.2.0-B4)
# ---------------------------------------------------------------------------


def _chroot_with(tmp_path: Path, *binaries: str) -> Path:
    """A chroot root whose /usr/bin holds exactly *binaries*."""
    bindir = tmp_path / "chroot" / "root" / "usr" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name in binaries:
        (bindir / name).write_text("#!/bin/sh\n")
    return tmp_path / "chroot"


def test_missing_toolchain_flags_a_compiler_the_chroot_lacks(tmp_path):
    """The exact 3.2.0-B4 failure: the profile exports CC=clang and the
    base-devel chroot ships only gcc, so every C build dies in configure."""
    pol = bs.SandboxPolicy(enabled=True, chroot_dir=_chroot_with(tmp_path, "gcc", "g++"))
    missing = bs.missing_toolchain(pol, {"CC": "clang", "CXX": "clang++"})
    assert missing == {"clang": "clang", "clang++": "clang"}


def test_missing_toolchain_is_empty_when_the_chroot_has_them(tmp_path):
    pol = bs.SandboxPolicy(enabled=True, chroot_dir=_chroot_with(tmp_path, "gcc", "g++"))
    assert bs.missing_toolchain(pol, {"CC": "gcc", "CXX": "g++"}) == {}


def test_missing_toolchain_maps_the_llvm_binutils(tmp_path):
    """LD/AR/NM/RANLIB name binaries too, and they come from two more
    packages than the compiler does."""
    pol = bs.SandboxPolicy(enabled=True, chroot_dir=_chroot_with(tmp_path, "gcc"))
    missing = bs.missing_toolchain(pol, {
        "LD": "ld.lld", "AR": "llvm-ar", "NM": "llvm-nm", "RANLIB": "llvm-ranlib",
    })
    assert missing == {
        "ld.lld": "lld", "llvm-ar": "llvm", "llvm-nm": "llvm", "llvm-ranlib": "llvm",
    }


def test_missing_toolchain_ignores_non_toolchain_exports(tmp_path):
    """CFLAGS names flags, not a binary — only the binary-valued keys are
    checkable against the chroot's filesystem."""
    pol = bs.SandboxPolicy(enabled=True, chroot_dir=_chroot_with(tmp_path, "gcc"))
    assert bs.missing_toolchain(pol, {"CFLAGS": "-O3 -march=native"}) == {}


def test_missing_toolchain_takes_the_binary_out_of_a_wrapped_value(tmp_path):
    """CC='clang -m32' is a legal makepkg value; the binary is the first word."""
    pol = bs.SandboxPolicy(enabled=True, chroot_dir=_chroot_with(tmp_path, "gcc"))
    assert bs.missing_toolchain(pol, {"CC": "clang -m32"}) == {"clang": "clang"}


def test_missing_toolchain_resolves_an_absolute_value_inside_the_chroot(tmp_path):
    """An absolute CC is a path *in the container*, so it must be probed
    relative to the chroot root, never against the host filesystem."""
    pol = bs.SandboxPolicy(enabled=True, chroot_dir=_chroot_with(tmp_path, "clang"))
    assert bs.missing_toolchain(pol, {"CC": "/usr/bin/clang"}) == {}


def test_provision_toolchain_installs_the_missing_packages_once(tmp_path):
    pol = bs.SandboxPolicy(enabled=True, chroot_dir=_chroot_with(tmp_path, "gcc"))
    with patch("sysforge.primitives.build_sandbox.run_privileged") as run:
        bs.provision_toolchain(pol, {"CC": "clang", "CXX": "clang++", "LD": "ld.lld"})
    argv = run.call_args[0][0]
    assert argv[0] == "arch-nspawn"
    assert str(pol.chroot_dir / "root") in argv
    # Deduplicated and deterministic: clang++ and clang are one package.
    assert argv[-2:] == ["clang", "lld"]
    assert "--noconfirm" in argv


def test_provision_toolchain_is_a_noop_when_nothing_is_missing(tmp_path):
    pol = bs.SandboxPolicy(enabled=True, chroot_dir=_chroot_with(tmp_path, "gcc"))
    with patch("sysforge.primitives.build_sandbox.run_privileged") as run:
        bs.provision_toolchain(pol, {"CC": "gcc"})
    run.assert_not_called()


def test_provision_toolchain_noop_when_the_sandbox_is_off(tmp_path):
    pol = bs.SandboxPolicy(enabled=False, chroot_dir=_chroot_with(tmp_path, "gcc"))
    with patch("sysforge.primitives.build_sandbox.run_privileged") as run:
        bs.provision_toolchain(pol, {"CC": "clang"})
    run.assert_not_called()


def test_provision_toolchain_refuses_an_unmappable_binary(tmp_path):
    """No package known for it, so it cannot be installed — stop with an
    actionable message rather than spending the build to reach a cryptic
    'compiler cannot compile programs' several minutes in."""
    pol = bs.SandboxPolicy(enabled=True, chroot_dir=_chroot_with(tmp_path, "gcc"))
    with patch("sysforge.primitives.build_sandbox.run_privileged") as run:
        with pytest.raises(bs.SandboxUnavailable) as e:
            bs.provision_toolchain(pol, {"CC": "zapcc"})
    run.assert_not_called()
    assert "zapcc" in str(e.value)
    assert "arch-nspawn" in str(e.value)


def test_provision_toolchain_reports_a_failed_install(tmp_path):
    pol = bs.SandboxPolicy(enabled=True, chroot_dir=_chroot_with(tmp_path, "gcc"))
    with patch("sysforge.primitives.build_sandbox.run_privileged",
               side_effect=RuntimeError("boom")):
        with pytest.raises(bs.SandboxUnavailable) as e:
            bs.provision_toolchain(pol, {"CC": "clang"})
    assert "clang" in str(e.value)


def test_seam_installs_the_missing_toolchain_into_the_chroot(tmp_path):
    """End-to-end 3.2.0-B4: a clang profile against a gcc-only chroot
    provisions clang before makechrootpkg runs, instead of reaching a
    'compiler cannot compile programs' failure minutes into the build."""
    pb, conf = _pkg_and_conf(tmp_path)
    pol = _policy(tmp_path)
    (pol.chroot_dir / "root" / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    (pol.chroot_dir / "root" / "usr" / "bin" / "gcc").write_text("#!/bin/sh\n")
    bs.set_policy(pol)

    with patch("sysforge.primitives.build_sandbox.run_privileged") as run:
        cap = _invoke(pb, conf, {}, extra_env={"CC": "clang", "CXX": "clang++"})

    argv = run.call_args[0][0]
    assert argv[0] == "arch-nspawn"
    assert argv[-1] == "clang"
    # ...and the build still ran, with the same toolchain in its container conf.
    assert cap["cmd"][0] == "makechrootpkg"


def test_seam_refuses_before_building_when_the_toolchain_cannot_be_installed(tmp_path):
    """The refusal must land *before* makechrootpkg is spawned — its whole
    value over the status quo is not paying for the build first."""
    pb, conf = _pkg_and_conf(tmp_path)
    pol = _policy(tmp_path)
    (pol.chroot_dir / "root" / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    bs.set_policy(pol)

    with patch("sysforge.primitives.build_sandbox.run_privileged",
               side_effect=RuntimeError("no network")):
        with pytest.raises(bs.SandboxUnavailable):
            _invoke(pb, conf, {}, extra_env={"CC": "clang"})

    # The checkout is left exactly as it was found.
    assert [p.name for p in pb.parent.iterdir()] == ["PKGBUILD"]


# ---------------------------------------------------------------------------
# Dependency injection from build state (3.1.0-F9)
# ---------------------------------------------------------------------------
#
# The container resolves deps from the stock repos only, so a source-built dep
# that exists solely as an installed *host* package is `target not found` in
# there. These cover the resolver that seeds `-I` from build_state.toml.


def _dep_fixture(tmp_path: Path, *, pkgbuild_deps, installed, source_built, artifacts):
    """Build the four inputs the resolver reads, and return its kwargs.

    pkgbuild_deps  — the target's own depends, as collect_builddeps yields them
    installed      — {pkgname: (version, [deps])} for the host's local DB
    source_built   — pkgnames build_state records as not-from-pacman
    artifacts      — filenames to create in the PKGDEST archive
    """
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    pb = src / "PKGBUILD"
    pb.write_text("# fake\n")

    dest = tmp_path / "pkgdest"
    dest.mkdir(exist_ok=True)
    for name in artifacts:
        (dest / name).write_text("")

    graph = {name: deps for name, (_v, deps) in installed.items()}
    versions = {name: v for name, (v, _d) in installed.items()}
    state = {name: {"build_mode": ("source_built" if name in source_built
                                   else "pacman")}
             for name in installed}

    return pb, dest, graph, versions, state


def _resolve(pb, dest, graph, versions, state, pkgbuild_deps):
    with patch("sysforge.primitives.pacman.collect_builddeps",
               return_value=pkgbuild_deps), \
         patch("sysforge.primitives.pacman.get_all_package_depends",
               return_value=graph), \
         patch("sysforge.primitives.pacman.get_installed_version",
               side_effect=lambda n: versions.get(n)), \
         patch("sysforge.primitives.build_sandbox._source_built_packages",
               return_value=state):
        return bs.resolve_dep_artifacts(pb, search_dir=dest, state_dir=None)


def test_resolver_injects_a_source_built_direct_dep(tmp_path):
    pb, dest, graph, versions, state = _dep_fixture(
        tmp_path,
        pkgbuild_deps=["libfoo"],
        installed={"libfoo": ("1.0.0-1", [])},
        source_built={"libfoo"},
        artifacts=["libfoo-1.0.0-1-x86_64.pkg.tar.zst"],
    )
    got = _resolve(pb, dest, graph, versions,
                   {"libfoo"}, ["libfoo"])
    assert [p.name for p in got] == ["libfoo-1.0.0-1-x86_64.pkg.tar.zst"]


def test_resolver_skips_repo_packages(tmp_path):
    """A dep pacman installed is resolvable inside the container already;
    injecting it would copy the whole repo set into every build."""
    pb, dest, *_ = _dep_fixture(
        tmp_path,
        pkgbuild_deps=["zlib"],
        installed={"zlib": ("1.3-1", [])},
        source_built=set(),
        artifacts=["zlib-1.3-1-x86_64.pkg.tar.zst"],
    )
    got = _resolve(pb, dest, {"zlib": []}, {"zlib": "1.3-1"}, set(), ["zlib"])
    assert got == []


def test_resolver_walks_transitively(tmp_path):
    """build_state records no deps, so the host's local DB is the authority
    on what a source-built package itself needs."""
    pb, dest, *_ = _dep_fixture(
        tmp_path,
        pkgbuild_deps=["libfoo"],
        installed={},
        source_built=set(),
        artifacts=["libfoo-1.0.0-1-x86_64.pkg.tar.zst",
                   "libbar-2.0.0-1-x86_64.pkg.tar.zst"],
    )
    graph = {"libfoo": ["libbar>=2.0"], "libbar": []}
    versions = {"libfoo": "1.0.0-1", "libbar": "2.0.0-1"}
    got = _resolve(pb, dest, graph, versions, {"libfoo", "libbar"}, ["libfoo"])
    assert sorted(p.name for p in got) == [
        "libbar-2.0.0-1-x86_64.pkg.tar.zst",
        "libfoo-1.0.0-1-x86_64.pkg.tar.zst",
    ]


def test_resolver_survives_a_dependency_cycle(tmp_path):
    pb, dest, *_ = _dep_fixture(
        tmp_path, pkgbuild_deps=["a"], installed={}, source_built=set(),
        artifacts=["a-1-1-x86_64.pkg.tar.zst", "b-1-1-x86_64.pkg.tar.zst"],
    )
    graph = {"a": ["b"], "b": ["a"]}
    got = _resolve(pb, dest, graph, {"a": "1-1", "b": "1-1"}, {"a", "b"}, ["a"])
    assert sorted(p.name for p in got) == [
        "a-1-1-x86_64.pkg.tar.zst", "b-1-1-x86_64.pkg.tar.zst"]


def test_resolver_picks_the_installed_version_not_the_newest(tmp_path):
    """PKGDEST is a historical archive; the newest file there is very often
    a version the host does not run."""
    pb, dest, *_ = _dep_fixture(
        tmp_path, pkgbuild_deps=["libfoo"], installed={}, source_built=set(),
        artifacts=["libfoo-1.0.0-1-x86_64.pkg.tar.zst",
                   "libfoo-9.9.9-1-x86_64.pkg.tar.zst"],
    )
    got = _resolve(pb, dest, {"libfoo": []}, {"libfoo": "1.0.0-1"},
                   {"libfoo"}, ["libfoo"])
    assert [p.name for p in got] == ["libfoo-1.0.0-1-x86_64.pkg.tar.zst"]


def test_resolver_warns_and_continues_when_the_artifact_was_pruned(tmp_path, capsys):
    """A build-fidelity mismatch, not a security one — the container falls
    back to the repo version and the user is told which one and why."""
    pb, dest, *_ = _dep_fixture(
        tmp_path, pkgbuild_deps=["libfoo", "libbar"], installed={},
        source_built=set(), artifacts=["libbar-2.0.0-1-x86_64.pkg.tar.zst"],
    )
    got = _resolve(pb, dest, {"libfoo": [], "libbar": []},
                   {"libfoo": "1.0.0-1", "libbar": "2.0.0-1"},
                   {"libfoo", "libbar"}, ["libfoo", "libbar"])
    # The one that still exists is still injected.
    assert [p.name for p in got] == ["libbar-2.0.0-1-x86_64.pkg.tar.zst"]
    warned = capsys.readouterr().err
    assert "libfoo" in warned
    assert "1.0.0-1" in warned


def test_resolver_skips_a_dep_that_is_not_installed(tmp_path):
    pb, dest, *_ = _dep_fixture(
        tmp_path, pkgbuild_deps=["ghost"], installed={}, source_built=set(),
        artifacts=[],
    )
    assert _resolve(pb, dest, {}, {}, set(), ["ghost"]) == []


def test_install_args_unions_the_session_registry_over_the_resolver(tmp_path):
    """The run's own freshly-built artifact is newer than anything the store
    points at, so the registry wins and lands last."""
    bs.reset_session()
    fresh = tmp_path / "libfoo-2.0.0-1-x86_64.pkg.tar.zst"
    fresh.write_text("")
    bs.register_artifacts([fresh])
    stale = tmp_path / "libbar-1.0.0-1-x86_64.pkg.tar.zst"
    stale.write_text("")

    with patch("sysforge.primitives.build_sandbox.resolve_dep_artifacts",
               return_value=[stale, fresh]):
        got = bs.install_args(tmp_path / "PKGBUILD", search_dir=tmp_path)

    assert got == [stale, fresh]
    assert got.count(fresh) == 1


def test_install_args_without_a_pkgbuild_is_the_session_registry_only(tmp_path):
    """Back-compatible: the resolver is opt-in per call site."""
    bs.reset_session()
    fresh = tmp_path / "libfoo-2.0.0-1-x86_64.pkg.tar.zst"
    fresh.write_text("")
    bs.register_artifacts([fresh])
    assert bs.install_args() == [fresh]
