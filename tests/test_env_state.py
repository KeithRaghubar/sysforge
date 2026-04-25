"""
test_env_state.py — validate env and flag state delivered to the makepkg subprocess

Tests cover four areas:

  1. invoke_makepkg env assembly
     - CC/CXX stripped from inherited shell env
     - Inherited CC replaced by extra_env CC
     - MAKEPKG_CONF set to conf_path
     - Venv vars (VIRTUAL_ENV, PYTHONPATH, venv bin in PATH) stripped
     - Unrelated env vars preserved
     - extra_env applied on top

  2. invoke_makepkg flag assembly
     - Profile makepkg_flags in final cmd
     - extra_flags appended after profile flags
     - strip_flags removes matching flags from profile and extra_flags
     - --noconfirm stripped when interactive=True

  3. resolve_env_vars consumes gate
     - CC/CXX (toolchain keys) always injected regardless of active_consumes
     - RUSTC_WRAPPER injected when "env" in active_consumes
     - RUSTC_WRAPPER not injected when "env" absent
     - RUSTFLAGS (rust conf key) not injected via env
     - CFLAGS (makepkg conf key) not injected via env
     - CARGO_HOME, CARGO_NET_GIT_FETCH_WITH_CLI collected when env consumes active
     - Multiple keys collected in a single call

  4. cmd_update batch/strip flags
     - -C added to extra_flags by default
     - --no-cleanbuild removes -C from extra_flags
     - --no-cleanbuild adds --cleanbuild/-C to strip_flags (strips from profile)
     - Default run does NOT put --cleanbuild/-C in strip_flags
     - --syncdeps/-s always in strip_flags
     - --install/-i always in strip_flags
     - -m flags forwarded as extra_flags
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.makepkg_wrapper import (
    AlreadyBuilt,
    ToolchainMismatchError,
    invoke_makepkg,
    resolve_env_vars,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_pkgbuild(tmp_path: Path) -> Path:
    """Create a minimal fake PKGBUILD in tmp_path and return its path."""
    pb = tmp_path / "PKGBUILD"
    pb.write_text("# fake\n")
    return pb


def _capture_invoke(pkgbuild_path, conf_path, resolved_profile, **kwargs):
    """
    Call invoke_makepkg with a patched subprocess.Popen; return (cmd, env).
    Runs under a clean base environment so shell vars don't bleed into assertions.
    """
    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kw.get("env") or {})
        m = MagicMock()
        m.stdout = iter(())  # no output
        m.wait.return_value = 0
        m.returncode = 0
        return m

    clean_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/root",
        "USER": "testuser",
        "LANG": "C",
    }

    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.Popen",
                   side_effect=fake_popen):
            invoke_makepkg(pkgbuild_path, conf_path, resolved_profile, **kwargs)

    return captured.get("cmd", []), captured.get("env", {})


# ---------------------------------------------------------------------------
# Section 1: invoke_makepkg env assembly
# ---------------------------------------------------------------------------

def test_makepkg_conf_is_set_in_env(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    _, env = _capture_invoke(pb, conf, {})
    assert env.get("MAKEPKG_CONF") == str(conf)


def test_unrelated_env_vars_preserved(tmp_path):
    """PATH, HOME, USER, LANG survive the env assembly."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    _, env = _capture_invoke(pb, conf, {})
    assert "PATH" in env
    assert "HOME" in env
    assert "USER" in env


def _fake_popen_factory(captured):
    """Build a subprocess.Popen stand-in that captures env/cmd and exits 0."""
    def fake_popen(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kw.get("env") or {})
        m = MagicMock()
        m.stdout = iter(())
        m.wait.return_value = 0
        m.returncode = 0
        return m
    return fake_popen


def test_inherited_cc_stripped_when_no_override(tmp_path):
    """Shell CC is stripped (makepkg/toolchain key) even when no profile CC."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    captured = {}

    with patch.dict(os.environ, {"CC": "gcc", "PATH": "/usr/bin", "HOME": "/root"},
                    clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.Popen",
                   side_effect=_fake_popen_factory(captured)):
            invoke_makepkg(pb, conf, {})

    assert "CC" not in captured["env"]


def test_inherited_cc_replaced_by_extra_env(tmp_path):
    """Shell CC=gcc is stripped; extra_env CC=clang takes its place."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    captured = {}

    with patch.dict(os.environ, {"CC": "gcc", "PATH": "/usr/bin", "HOME": "/root"},
                    clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.Popen",
                   side_effect=_fake_popen_factory(captured)):
            invoke_makepkg(pb, conf, {}, extra_env={"CC": "clang"})

    assert captured["env"].get("CC") == "clang"


def test_venv_stripped_from_env(tmp_path):
    """VIRTUAL_ENV removed, venv bin dir stripped from PATH."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    venv = "/home/user/.venv"

    captured = {}

    with patch.dict(os.environ, {
        "VIRTUAL_ENV": venv,
        "PATH": f"{venv}/bin:/usr/bin:/bin",
        "HOME": "/root",
    }, clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.Popen",
                   side_effect=_fake_popen_factory(captured)):
            invoke_makepkg(pb, conf, {})

    env = captured["env"]
    assert "VIRTUAL_ENV" not in env
    assert f"{venv}/bin" not in env.get("PATH", "")
    assert "/usr/bin" in env.get("PATH", "")


def test_extra_env_applied(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    _, env = _capture_invoke(pb, conf, {},
                             extra_env={"RUSTC_WRAPPER": "sccache",
                                        "SCCACHE_DIR": "/var/cache/sccache"})
    assert env.get("RUSTC_WRAPPER") == "sccache"
    assert env.get("SCCACHE_DIR") == "/var/cache/sccache"


# ---------------------------------------------------------------------------
# Section 2: invoke_makepkg flag assembly
# ---------------------------------------------------------------------------

def test_profile_makepkg_flags_in_cmd(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    profile = {"makepkg_flags": ["--noconfirm", "--syncdeps"]}
    cmd, _ = _capture_invoke(pb, conf, profile)
    assert "--noconfirm" in cmd
    assert "--syncdeps" in cmd


def test_extra_flags_appended_after_profile_flags(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    profile = {"makepkg_flags": ["--noconfirm"]}
    cmd, _ = _capture_invoke(pb, conf, profile, extra_flags=["-C", "--log"])
    assert "-C" in cmd
    assert "--log" in cmd
    # extra_flags must come after profile flags
    assert cmd.index("--noconfirm") < cmd.index("-C")


def test_strip_flags_removes_from_profile_flags(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    profile = {"makepkg_flags": ["--noconfirm", "--syncdeps", "--install"]}
    cmd, _ = _capture_invoke(pb, conf, profile,
                             strip_flags={"--syncdeps", "--install"})
    assert "--noconfirm" in cmd
    assert "--syncdeps" not in cmd
    assert "--install" not in cmd


def test_strip_flags_removes_from_extra_flags(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    profile = {"makepkg_flags": ["--noconfirm"]}
    cmd, _ = _capture_invoke(pb, conf, profile,
                             extra_flags=["--syncdeps", "-C"],
                             strip_flags={"--syncdeps", "-s"})
    assert "--syncdeps" not in cmd
    assert "-C" in cmd  # not in strip_flags, must survive


def test_interactive_strips_noconfirm(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    profile = {"makepkg_flags": ["--noconfirm", "--syncdeps"]}
    cmd, _ = _capture_invoke(pb, conf, profile, interactive=True)
    assert "--noconfirm" not in cmd
    assert "--syncdeps" in cmd


def test_empty_profile_produces_base_cmd(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    cmd, _ = _capture_invoke(pb, conf, {})
    assert cmd[0] == "makepkg"
    assert cmd[1] == "-p"


# ---------------------------------------------------------------------------
# Toolchain mismatch detection (GCC rejects clang-only flag)
# ---------------------------------------------------------------------------

def _popen_with_stdout(lines, returncode):
    """Build a Popen stand-in that replays `lines` on stdout and exits with returncode."""
    def fake_popen(_cmd, **_kw):
        m = MagicMock()
        m.stdout = iter(lines)
        m.wait.return_value = returncode
        m.returncode = returncode
        return m
    return fake_popen


def test_toolchain_mismatch_raises_on_flto_thin_rejection(tmp_path):
    """GCC rejecting -flto=thin triggers ToolchainMismatchError, not CalledProcessError."""
    import subprocess as _sp

    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    output = [
        "==> Starting build()...\n",
        "g++ -flto=thin -o foo foo.cpp\n",
        "cc1plus: error: unrecognized argument to '-flto=' option: 'thin'\n",
        "make: *** [Makefile:45: foo.o] Error 1\n",
        "==> ERROR: A failure occurred in build().\n",
    ]

    clean_env = {"PATH": "/usr/bin:/bin", "HOME": "/root"}
    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.Popen",
                   side_effect=_popen_with_stdout(output, 2)):
            try:
                invoke_makepkg(pb, conf, {})
            except ToolchainMismatchError:
                raised = "mismatch"
            except _sp.CalledProcessError:
                raised = "generic"
            else:
                raised = "none"

    assert raised == "mismatch"


def test_toolchain_mismatch_raises_on_unrecognized_thin_flag(tmp_path):
    """Alternative error wording ('unrecognized command-line option') is also caught."""
    import subprocess as _sp

    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    output = [
        "g++: error: unrecognized command-line option '-flto=thin'\n",
        "==> ERROR: A failure occurred in build().\n",
    ]

    clean_env = {"PATH": "/usr/bin:/bin", "HOME": "/root"}
    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.Popen",
                   side_effect=_popen_with_stdout(output, 2)):
            try:
                invoke_makepkg(pb, conf, {})
            except ToolchainMismatchError:
                raised = "mismatch"
            except _sp.CalledProcessError:
                raised = "generic"
            else:
                raised = "none"

    assert raised == "mismatch"


def test_toolchain_mismatch_raises_on_curly_quoted_error(tmp_path):
    """GCC emits Unicode smart quotes in localized errors — must still match."""
    import subprocess as _sp

    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    # Actual output from gpu-burn build: U+2018 LEFT / U+2019 RIGHT single quote.
    output = [
        "cc1plus: error: unrecognized argument to \u2018-flto=\u2019 option: \u2018thin\u2019\n",
        "==> ERROR: A failure occurred in build().\n",
    ]

    clean_env = {"PATH": "/usr/bin:/bin", "HOME": "/root"}
    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.Popen",
                   side_effect=_popen_with_stdout(output, 2)):
            try:
                invoke_makepkg(pb, conf, {})
            except ToolchainMismatchError:
                raised = "mismatch"
            except _sp.CalledProcessError:
                raised = "generic"
            else:
                raised = "none"

    assert raised == "mismatch"


def test_toolchain_mismatch_not_raised_on_unrelated_failure(tmp_path):
    """Non-matching errors raise plain CalledProcessError, not ToolchainMismatchError."""
    import subprocess as _sp

    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    output = [
        "==> ERROR: patching failed\n",
        "==> ERROR: A failure occurred in prepare().\n",
    ]

    clean_env = {"PATH": "/usr/bin:/bin", "HOME": "/root"}
    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.Popen",
                   side_effect=_popen_with_stdout(output, 1)):
            try:
                invoke_makepkg(pb, conf, {})
            except ToolchainMismatchError:
                raised = "mismatch"
            except _sp.CalledProcessError:
                raised = "generic"
            else:
                raised = "none"

    assert raised == "generic"


def test_toolchain_mismatch_not_raised_on_success(tmp_path):
    """Even if the output contains the pattern, a successful build does not raise."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    output = [
        "==> Building...\n",
        "==> Finished making\n",
    ]

    clean_env = {"PATH": "/usr/bin:/bin", "HOME": "/root"}
    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.Popen",
                   side_effect=_popen_with_stdout(output, 0)):
            invoke_makepkg(pb, conf, {})  # must not raise


# ---------------------------------------------------------------------------
# Already-built detection (PKGDEST holds matching .pkg.tar)
# ---------------------------------------------------------------------------

def test_already_built_raises_on_exit_13(tmp_path):
    """Exit code 13 (E_ALREADY_BUILT) raises AlreadyBuilt, not CalledProcessError."""
    import subprocess as _sp

    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    output = ["==> Making package: htop 3.4.1-1\n"]

    clean_env = {"PATH": "/usr/bin:/bin", "HOME": "/root"}
    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.Popen",
                   side_effect=_popen_with_stdout(output, 13)):
            try:
                invoke_makepkg(pb, conf, {})
            except AlreadyBuilt as e:
                raised = ("already_built", e.pkgbuild_path)
            except _sp.CalledProcessError:
                raised = ("generic", None)
            else:
                raised = ("none", None)

    assert raised[0] == "already_built"
    assert raised[1] == pb


def test_already_built_raises_on_message_match(tmp_path):
    """Diagnostic line matches even when the wrapper rewrites the exit code."""
    import subprocess as _sp

    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    output = [
        "==> Making package: htop 3.4.1-1\n",
        "==> ERROR: A package has already been built. (use -f to overwrite)\n",
    ]

    clean_env = {"PATH": "/usr/bin:/bin", "HOME": "/root"}
    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.Popen",
                   side_effect=_popen_with_stdout(output, 1)):
            try:
                invoke_makepkg(pb, conf, {})
            except AlreadyBuilt:
                raised = "already_built"
            except _sp.CalledProcessError:
                raised = "generic"
            else:
                raised = "none"

    assert raised == "already_built"


def test_already_built_propagates_through_run_build(tmp_path):
    """Regression: _run_build's catch-all `except Exception` previously
    laundered AlreadyBuilt into RuntimeError("[tempfile_write_failed] ..."),
    so update.py's `except AlreadyBuilt` branch never fired and packages
    with an existing .pkg.tar were misreported as build failures
    (`sysforge update --all --devel` surfaced the raw makepkg error).
    """
    from contextlib import contextmanager
    from sysforge.primitives.makepkg_wrapper import _run_build

    pb = tmp_path / "PKGBUILD"
    pb.write_text("pkgname=htop\npkgver=3.4.1\npkgrel=1\n")

    @contextmanager
    def fake_emit(*a, **kw):
        yield "/tmp/fake_makepkg.conf"

    def raise_already_built(*a, **kw):
        raise AlreadyBuilt(pb)

    with (
        patch("sysforge.primitives.makepkg_wrapper.patch_pkgbuild_groups",
              return_value=pb),
        patch("sysforge.primitives.makepkg_wrapper.emit_makepkg_conf",
              side_effect=fake_emit),
        patch("sysforge.primitives.makepkg_wrapper.resolve_env_vars",
              return_value={}),
        patch("sysforge.primitives.makepkg_wrapper._invoke_with_retry",
              side_effect=raise_already_built),
    ):
        try:
            _run_build(pb, {"batch": True}, {}, [],
                       extracted_profile=None,
                       pkgmeta={"globals": {"pkgname": "htop"}})
        except AlreadyBuilt as e:
            outcome = ("already_built", e.pkgbuild_path)
        except RuntimeError as e:
            outcome = ("runtime_error", str(e))
        else:
            outcome = ("none", None)

    assert outcome[0] == "already_built", (
        f"_run_build laundered AlreadyBuilt into {outcome[0]}: {outcome[1]!r}"
    )
    assert outcome[1] == pb


# ---------------------------------------------------------------------------
# Section 3: resolve_env_vars consumes gate
# ---------------------------------------------------------------------------

def test_cc_always_injected_with_empty_consumes():
    """CC is a toolchain key — always delivered regardless of active_consumes."""
    result = resolve_env_vars({"CC": "clang"}, [])
    assert result.get("CC") == "clang"


def test_cxx_always_injected_with_no_matching_consumes():
    result = resolve_env_vars({"CXX": "clang++"}, ["makepkg"])
    assert result.get("CXX") == "clang++"


def test_rustc_wrapper_injected_when_env_in_consumes():
    result = resolve_env_vars({"RUSTC_WRAPPER": "sccache"}, ["makepkg", "rust", "env"])
    assert result.get("RUSTC_WRAPPER") == "sccache"


def test_rustc_wrapper_not_injected_when_env_absent():
    result = resolve_env_vars({"RUSTC_WRAPPER": "sccache"}, ["makepkg", "rust"])
    assert "RUSTC_WRAPPER" not in result


def test_rustc_wrapper_injected_in_fallback_mode():
    """active_consumes=None means no inference: all env-type keys collected."""
    result = resolve_env_vars({"RUSTC_WRAPPER": "sccache"}, None)
    assert result.get("RUSTC_WRAPPER") == "sccache"


def test_rustflags_not_injected_via_env():
    """RUSTFLAGS is a rust conf key — written to the conf file, not env."""
    result = resolve_env_vars({"RUSTFLAGS": "-C opt-level=3"}, ["makepkg", "rust", "env"])
    assert "RUSTFLAGS" not in result


def test_cflags_not_injected_via_env():
    """CFLAGS is a makepkg conf key — written to the conf file, not env."""
    result = resolve_env_vars({"CFLAGS": "-march=native -O3"}, ["makepkg", "rust", "env"])
    assert "CFLAGS" not in result


def test_cargo_home_injected_when_env_consumes():
    result = resolve_env_vars({"CARGO_HOME": "/var/cache/cargo"}, ["env"])
    assert result.get("CARGO_HOME") == "/var/cache/cargo"


def test_cargo_net_git_fetch_injected_when_env_consumes():
    result = resolve_env_vars({"CARGO_NET_GIT_FETCH_WITH_CLI": "true"}, ["env"])
    assert result.get("CARGO_NET_GIT_FETCH_WITH_CLI") == "true"


def test_cargo_home_not_injected_without_env_consumes():
    result = resolve_env_vars({"CARGO_HOME": "/var/cache/cargo"}, ["makepkg"])
    assert "CARGO_HOME" not in result


def test_multiple_keys_collected_correctly():
    """Toolchain key always collected; env key gated; conf key excluded."""
    profile = {
        "CC": "clang",
        "RUSTC_WRAPPER": "sccache",
        "CARGO_HOME": "/var/cache/cargo",
        "CFLAGS": "-O3",
        "RUSTFLAGS": "-C opt-level=3",
    }
    result = resolve_env_vars(profile, ["makepkg", "rust", "env"])
    assert result.get("CC") == "clang"
    assert result.get("RUSTC_WRAPPER") == "sccache"
    assert result.get("CARGO_HOME") == "/var/cache/cargo"
    assert "CFLAGS" not in result
    assert "RUSTFLAGS" not in result


def test_sysforge_keys_never_injected():
    """Internal keys (batch, build_mode, etc.) must not appear in env."""
    profile = {
        "batch": True,
        "build_mode": "patched_pkgbuild",
        "clean_builddir": True,
        "CC": "clang",
    }
    result = resolve_env_vars(profile, ["makepkg", "env"])
    assert "batch" not in result
    assert "build_mode" not in result
    assert "clean_builddir" not in result
    assert result.get("CC") == "clang"


# ---------------------------------------------------------------------------
# Section 4: cmd_update batch/strip flags
# ---------------------------------------------------------------------------

def _make_update_args(**overrides):
    defaults = dict(
        offline=True,      # skip network; parallel pull block is no-op
        dry_run=False,
        devel=False,
        all=False,
        no_cleanbuild=False,
        makepkg=None,
        interactive=False,
        profile_conf=None,
        state_dir=None,
        no_pkg_log=True,
        persist_log=False,
        log_dir=None,
        cache_report=False,
        packages=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _run_update_capture_build_calls(args, pkgbuild_path):
    """
    Run cmd_update with a single fake package that needs rebuilding.
    Returns the list of kwargs dicts from each build_run call.
    """
    from sysforge.update import cmd_update

    pkgbase = "testpkg"
    build_calls = []

    fake_entry = {
        "pkgbase": pkgbase,
        "pkgbuild_dir": str(pkgbuild_path.parent),
        "pkgver": "0.9",
        "pkgrel": "1",
        "epoch": None,
        "build_mode": "profiled",
        "flags_string": "",
        "built_at": "2026-01-01T00:00:00",
    }

    def fake_build_run(_path, options=None):
        build_calls.append(vars(options) if options is not None else {})

    def fake_parse_pkgbuild(_path):
        return {"globals": {"pkgver": "1.0", "pkgrel": "1"}}

    fake_manifest = ({}, [{"name": pkgbase, "source": "aur"}])

    with patch("sysforge.update.BuildState") as MockBS, \
         patch("sysforge.update.fetch_aur_name_cache"), \
         patch("sysforge.update.resolve_state_dir",
               return_value=(Path("/tmp/sf-state-test"), False)), \
         patch("sysforge.update.load_config", return_value={}), \
         patch("sysforge.update._load_full_packages_toml",
               return_value=fake_manifest), \
         patch("sysforge.update.parse_pkgbuild",
               side_effect=fake_parse_pkgbuild), \
         patch("sysforge.update.get_all_installed_packages",
               return_value={pkgbase: "0.9-1"}), \
         patch("sysforge.update.vercmp", return_value=1), \
         patch("sysforge.update.collect_makedeps", return_value=[]), \
         patch("sysforge.update.filter_missing_deps", return_value=[]), \
         patch("sysforge.update.get_pkgdest", return_value=None), \
         patch("sysforge.update.snapshot_pkg_dir", return_value=[]), \
         patch("sysforge.update.batch_install_pkgs", return_value=True), \
         patch("sysforge.primitives.makepkg_wrapper.run",
               side_effect=fake_build_run), \
         patch("sysforge.primitives.cache_probe.reset_session"), \
         patch("sysforge.primitives.cache_probe.emit_session_report"):

        bs_instance = MagicMock()
        bs_instance.all_packages.return_value = {pkgbase: fake_entry}
        MockBS.return_value = bs_instance

        cmd_update(args)

    return build_calls


def test_update_default_adds_cleanbuild_flag(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    args = _make_update_args()
    calls = _run_update_capture_build_calls(args, pb)
    assert calls, "build_run should have been called once"
    extra_flags = calls[0].get("extra_flags") or []
    assert "-C" in extra_flags


def test_update_no_cleanbuild_removes_c_from_extra_flags(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    args = _make_update_args(no_cleanbuild=True)
    calls = _run_update_capture_build_calls(args, pb)
    assert calls
    extra_flags = calls[0].get("extra_flags") or []
    assert "-C" not in extra_flags
    assert "--cleanbuild" not in extra_flags


def test_update_syncdeps_always_in_strip_flags(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    args = _make_update_args()
    calls = _run_update_capture_build_calls(args, pb)
    assert calls
    strip = calls[0].get("strip_flags") or set()
    assert "--syncdeps" in strip
    assert "-s" in strip


def test_update_install_always_in_strip_flags(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    args = _make_update_args()
    calls = _run_update_capture_build_calls(args, pb)
    assert calls
    strip = calls[0].get("strip_flags") or set()
    assert "--install" in strip
    assert "-i" in strip


def test_update_no_cleanbuild_adds_cleanbuild_to_strip_flags(tmp_path):
    """--no-cleanbuild must also strip --cleanbuild/-C from profile makepkg_flags."""
    pb = _fake_pkgbuild(tmp_path)
    args = _make_update_args(no_cleanbuild=True)
    calls = _run_update_capture_build_calls(args, pb)
    assert calls
    strip = calls[0].get("strip_flags") or set()
    assert "--cleanbuild" in strip
    assert "-C" in strip


def test_update_default_does_not_strip_cleanbuild_from_profile(tmp_path):
    """Default run: strip_flags must NOT contain --cleanbuild/-C.
    The -C is added via extra_flags, not stripped from profile."""
    pb = _fake_pkgbuild(tmp_path)
    args = _make_update_args(no_cleanbuild=False)
    calls = _run_update_capture_build_calls(args, pb)
    assert calls
    strip = calls[0].get("strip_flags") or set()
    assert "--cleanbuild" not in strip
    assert "-C" not in strip


def test_update_makepkg_m_flags_forwarded(tmp_path):
    pb = _fake_pkgbuild(tmp_path)
    args = _make_update_args(makepkg="--log --force")
    calls = _run_update_capture_build_calls(args, pb)
    assert calls
    extra_flags = calls[0].get("extra_flags") or []
    assert "--log" in extra_flags
    assert "--force" in extra_flags


def test_update_makepkg_m_combined_short_flags_expanded(tmp_path):
    """Combined short flags like -lf are expanded to [-l, -f]."""
    pb = _fake_pkgbuild(tmp_path)
    args = _make_update_args(makepkg="-lf")
    calls = _run_update_capture_build_calls(args, pb)
    assert calls
    extra_flags = calls[0].get("extra_flags") or []
    assert "-l" in extra_flags
    assert "-f" in extra_flags
