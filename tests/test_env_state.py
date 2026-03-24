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

from sysforge.primitives.makepkg_wrapper import invoke_makepkg, resolve_env_vars


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
    Call invoke_makepkg with a patched subprocess.run; return (cmd, env).
    Runs under a clean base environment so shell vars don't bleed into assertions.
    """
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kw.get("env") or {})
        m = MagicMock()
        m.returncode = 0
        return m

    clean_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/root",
        "USER": "testuser",
        "LANG": "C",
    }

    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.run",
                   side_effect=fake_run):
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


def test_inherited_cc_stripped_when_no_override(tmp_path):
    """Shell CC is stripped (makepkg/toolchain key) even when no profile CC."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    captured = {}

    def fake_run(_cmd, **kw):
        captured["env"] = dict(kw.get("env") or {})
        m = MagicMock()
        m.returncode = 0
        return m

    with patch.dict(os.environ, {"CC": "gcc", "PATH": "/usr/bin", "HOME": "/root"},
                    clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.run",
                   side_effect=fake_run):
            invoke_makepkg(pb, conf, {})

    assert "CC" not in captured["env"]


def test_inherited_cc_replaced_by_extra_env(tmp_path):
    """Shell CC=gcc is stripped; extra_env CC=clang takes its place."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    captured = {}

    def fake_run(_cmd, **kw):
        captured["env"] = dict(kw.get("env") or {})
        m = MagicMock()
        m.returncode = 0
        return m

    with patch.dict(os.environ, {"CC": "gcc", "PATH": "/usr/bin", "HOME": "/root"},
                    clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.run",
                   side_effect=fake_run):
            invoke_makepkg(pb, conf, {}, extra_env={"CC": "clang"})

    assert captured["env"].get("CC") == "clang"


def test_venv_stripped_from_env(tmp_path):
    """VIRTUAL_ENV removed, venv bin dir stripped from PATH."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    venv = "/home/user/.venv"

    captured = {}

    def fake_run(_cmd, **kw):
        captured["env"] = dict(kw.get("env") or {})
        m = MagicMock()
        m.returncode = 0
        return m

    with patch.dict(os.environ, {
        "VIRTUAL_ENV": venv,
        "PATH": f"{venv}/bin:/usr/bin:/bin",
        "HOME": "/root",
    }, clear=True):
        with patch("sysforge.primitives.makepkg_wrapper.subprocess.run",
                   side_effect=fake_run):
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
        no_update=True,    # skip git pull; parallel pull block is no-op
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
        no_unified_log=True,
        purge_log=False,
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

    with patch("sysforge.update.BuildState") as MockBS, \
         patch("sysforge.update.fetch_aur_name_cache"), \
         patch("sysforge.update.resolve_state_dir",
               return_value=(Path("/tmp/sf-state-test"), False)), \
         patch("sysforge.update.parse_pkgbuild",
               side_effect=fake_parse_pkgbuild), \
         patch("sysforge.update.get_installed_version",
               return_value="0.9-1"), \
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
