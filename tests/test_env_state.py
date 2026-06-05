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
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.makepkg_env import resolve_env_vars
from sysforge.primitives.makepkg_invoke import (
    AlreadyBuilt,
    ToolchainMismatchError,
    invoke_makepkg,
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
    Call invoke_makepkg with both subprocess.Popen and run_with_pty patched.
    Returns (cmd, env) — captured from whichever code path runs (interactive
    uses Popen directly; non-interactive goes through run_with_pty).
    Runs under a clean base environment so shell vars don't bleed in.
    """
    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kw.get("env") or {})
        m = MagicMock()
        m.stdout = iter(())
        m.wait.return_value = 0
        m.returncode = 0
        return m

    def fake_run_with_pty(cmd, *, cwd, env, line_callback, forward_bytes, preexec_fn=None, **_kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(env)
        return 0

    clean_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/root",
        "USER": "testuser",
        "LANG": "C",
    }

    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_invoke.subprocess.Popen",
                   side_effect=fake_popen):
            with patch("sysforge.primitives.makepkg_invoke.run_with_pty",
                       side_effect=fake_run_with_pty):
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


def _fake_pty_factory(captured, lines=(), returncode=0):
    """Build a run_with_pty stand-in that captures env/cmd, replays `lines`
    via line_callback, and returns `returncode`."""
    def fake_pty(cmd, *, cwd, env, line_callback, forward_bytes, preexec_fn=None, **_kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(env)
        for line in lines:
            line_callback(line.rstrip("\n"))
        return returncode
    return fake_pty


@contextmanager
def _patch_makepkg(captured, *, lines=(), returncode=0):
    """Patch both subprocess.Popen (interactive branch) and run_with_pty
    (non-interactive branch). Whichever code path the invoke takes,
    `captured` ends up with cmd and env."""
    with patch("sysforge.primitives.makepkg_invoke.subprocess.Popen",
               side_effect=_fake_popen_factory(captured)):
        with patch("sysforge.primitives.makepkg_invoke.run_with_pty",
                   side_effect=_fake_pty_factory(captured, lines=lines, returncode=returncode)):
            yield


def test_inherited_cc_stripped_when_no_override(tmp_path):
    """Shell CC is stripped (makepkg/toolchain key) even when no profile CC."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    captured = {}

    with patch.dict(os.environ, {"CC": "gcc", "PATH": "/usr/bin", "HOME": "/root"},
                    clear=True):
        with _patch_makepkg(captured):
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
        with _patch_makepkg(captured):
            invoke_makepkg(pb, conf, {}, extra_env={"CC": "clang"})

    assert captured["env"].get("CC") == "clang"


def test_venv_env_vars_popped(tmp_path):
    """invoke_makepkg pops VIRTUAL_ENV/PYTHONPATH defensively. PATH-level
    stripping is owned by cli._strip_venv_from_path at CLI startup (covered
    by tests/test_cli.py), so this test exercises only the wrapper's local
    pop responsibility — not the PATH scrub."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    venv = "/home/user/.venv"

    captured = {}

    with patch.dict(os.environ, {
        "VIRTUAL_ENV": venv,
        "PYTHONPATH": f"{venv}/lib/python3/site-packages",
        "PATH": "/usr/bin:/bin",
        "HOME": "/root",
    }, clear=True):
        with _patch_makepkg(captured):
            invoke_makepkg(pb, conf, {})

    env = captured["env"]
    assert "VIRTUAL_ENV" not in env
    assert "PYTHONPATH" not in env
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


def test_pager_env_overridden_for_non_interactive_build(tmp_path):
    """Regression for the libinput-git pager-hang bug. A user with
    `export PAGER=less` in .zshrc would otherwise have the value
    inherited into the makepkg subprocess; `meson configure build` then
    pipes its summary through less(1) and stalls the PTY. The scrub
    must override (not setdefault) so PAGER=cat regardless of what the
    shell exported."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    captured = {}

    with patch.dict(os.environ, {
        "PAGER": "less",
        "GIT_PAGER": "less",
        "SYSTEMD_PAGER": "less",
        "LESS": "-R",
        "PATH": "/usr/bin",
        "HOME": "/root",
    }, clear=True):
        with _patch_makepkg(captured):
            invoke_makepkg(pb, conf, {})

    env = captured["env"]
    assert env.get("PAGER") == "cat"
    assert env.get("GIT_PAGER") == "cat"
    assert env.get("SYSTEMD_PAGER") == "cat"
    assert env.get("LESS") == "-RFX"


def test_pager_env_preserved_for_interactive_build(tmp_path):
    """`--interactive` is the single documented opt-in for paging — when
    the user has explicitly asked to pause and interact with the build,
    leave their exported PAGER alone so they can scroll through e.g. a
    `meson configure` summary."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    captured = {}

    with patch.dict(os.environ, {
        "PAGER": "less",
        "PATH": "/usr/bin",
        "HOME": "/root",
    }, clear=True):
        with _patch_makepkg(captured):
            invoke_makepkg(pb, conf, {}, interactive=True)

    assert captured["env"].get("PAGER") == "less"


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


def _capture_popen_kwargs(pkgbuild_path, conf_path, resolved_profile, **kwargs):
    """Same as _capture_invoke but returns the full kwargs dict passed to Popen."""
    captured = {}

    def fake_popen(cmd, **kw):
        captured["kwargs"] = dict(kw)
        m = MagicMock()
        m.stdout = iter(())
        m.wait.return_value = 0
        m.returncode = 0
        return m

    clean_env = {"PATH": "/usr/bin:/bin", "HOME": "/root", "USER": "testuser", "LANG": "C"}
    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_invoke.subprocess.Popen",
                   side_effect=fake_popen):
            invoke_makepkg(pkgbuild_path, conf_path, resolved_profile, **kwargs)
    return captured.get("kwargs", {})


def test_interactive_inherits_stdio(tmp_path):
    """interactive=True: child inherits parent stdout/stderr so unbuffered
    prompts (pacman conflict, sudo) reach the terminal immediately."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    kwargs = _capture_popen_kwargs(pb, conf, {}, interactive=True)
    assert "stdout" not in kwargs, (
        f"interactive=True must NOT pipe stdout (got stdout={kwargs.get('stdout')!r})")
    assert "stderr" not in kwargs, (
        f"interactive=True must NOT pipe stderr (got stderr={kwargs.get('stderr')!r})")


def test_noninteractive_uses_pty(tmp_path):
    """interactive=False: stdout/stderr are routed through a pty so child
    tools that gate live UI on isatty() (cargo, configure spinners) emit
    their progress animation. invoke_makepkg still classifies failure stages
    and detects toolchain mismatches via the line_callback."""
    pb = _fake_pkgbuild(tmp_path)
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")

    pty_calls = []

    def fake_pty(cmd, *, cwd, env, line_callback, forward_bytes, preexec_fn=None, **_kwargs):
        pty_calls.append(list(cmd))
        return 0

    clean_env = {"PATH": "/usr/bin:/bin", "HOME": "/root", "USER": "testuser", "LANG": "C"}
    with patch.dict(os.environ, clean_env, clear=True):
        with patch("sysforge.primitives.makepkg_invoke.run_with_pty",
                   side_effect=fake_pty):
            with patch("sysforge.primitives.makepkg_invoke.subprocess.Popen") as popen:
                invoke_makepkg(pb, conf, {}, interactive=False)
                assert not popen.called, "non-interactive must NOT call subprocess.Popen directly"

    assert len(pty_calls) == 1
    assert pty_calls[0][0] == "makepkg"


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

def _pty_with_lines(lines, returncode):
    """Build a run_with_pty stand-in that replays `lines` via line_callback
    and returns `returncode`."""
    def fake_pty(cmd, *, cwd, env, line_callback, forward_bytes, preexec_fn=None, **_kwargs):
        for line in lines:
            line_callback(line.rstrip("\n"))
        return returncode
    return fake_pty


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
        with patch("sysforge.primitives.makepkg_invoke.run_with_pty",
                   side_effect=_pty_with_lines(output, 2)):
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
        with patch("sysforge.primitives.makepkg_invoke.run_with_pty",
                   side_effect=_pty_with_lines(output, 2)):
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
        with patch("sysforge.primitives.makepkg_invoke.run_with_pty",
                   side_effect=_pty_with_lines(output, 2)):
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
        with patch("sysforge.primitives.makepkg_invoke.run_with_pty",
                   side_effect=_pty_with_lines(output, 1)):
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
        with patch("sysforge.primitives.makepkg_invoke.run_with_pty",
                   side_effect=_pty_with_lines(output, 0)):
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
        with patch("sysforge.primitives.makepkg_invoke.run_with_pty",
                   side_effect=_pty_with_lines(output, 13)):
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
        with patch("sysforge.primitives.makepkg_invoke.run_with_pty",
                   side_effect=_pty_with_lines(output, 1)):
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


def _run_update_capture_build_calls(update_scenario, args):
    """
    Run the real cmd_update through the update_scenario harness with a single
    foreign package that needs rebuilding (installed 0.9 < PKGBUILD 1.0), and
    return the list of BuildOptions (as ``vars()`` dicts) passed to each faked
    build — so the -C/--cleanbuild and strip-flag handling can be asserted on
    ``extra_flags`` / ``strip_flags`` without any sysforge.update.* patching.
    """
    update_scenario.add_pkg("testpkg", "pkgname=testpkg\npkgver=1.0\npkgrel=1\n")
    update_scenario.run(
        args,
        installed={"testpkg": "0.9-1"},
        foreign={"testpkg": "0.9-1"},
    )
    calls = []
    for a, k in update_scenario.builds:
        options = a[1] if len(a) > 1 else k.get("options")
        calls.append(vars(options) if options is not None else {})
    return calls


def test_update_default_adds_cleanbuild_flag(update_scenario):
    args = _make_update_args()
    calls = _run_update_capture_build_calls(update_scenario, args)
    assert calls, "build_run should have been called once"
    extra_flags = calls[0].get("extra_flags") or []
    assert "-C" in extra_flags


def test_update_no_cleanbuild_removes_c_from_extra_flags(update_scenario):
    args = _make_update_args(no_cleanbuild=True)
    calls = _run_update_capture_build_calls(update_scenario, args)
    assert calls
    extra_flags = calls[0].get("extra_flags") or []
    assert "-C" not in extra_flags
    assert "--cleanbuild" not in extra_flags


def test_update_syncdeps_always_in_strip_flags(update_scenario):
    args = _make_update_args()
    calls = _run_update_capture_build_calls(update_scenario, args)
    assert calls
    strip = calls[0].get("strip_flags") or set()
    assert "--syncdeps" in strip
    assert "-s" in strip


def test_update_install_always_in_strip_flags(update_scenario):
    args = _make_update_args()
    calls = _run_update_capture_build_calls(update_scenario, args)
    assert calls
    strip = calls[0].get("strip_flags") or set()
    assert "--install" in strip
    assert "-i" in strip


def test_update_no_cleanbuild_adds_cleanbuild_to_strip_flags(update_scenario):
    """--no-cleanbuild must also strip --cleanbuild/-C from profile makepkg_flags."""
    args = _make_update_args(no_cleanbuild=True)
    calls = _run_update_capture_build_calls(update_scenario, args)
    assert calls
    strip = calls[0].get("strip_flags") or set()
    assert "--cleanbuild" in strip
    assert "-C" in strip


def test_update_default_does_not_strip_cleanbuild_from_profile(update_scenario):
    """Default run: strip_flags must NOT contain --cleanbuild/-C.
    The -C is added via extra_flags, not stripped from profile."""
    args = _make_update_args(no_cleanbuild=False)
    calls = _run_update_capture_build_calls(update_scenario, args)
    assert calls
    strip = calls[0].get("strip_flags") or set()
    assert "--cleanbuild" not in strip
    assert "-C" not in strip


def test_update_makepkg_m_flags_forwarded(update_scenario):
    args = _make_update_args(makepkg="--log --force")
    calls = _run_update_capture_build_calls(update_scenario, args)
    assert calls
    extra_flags = calls[0].get("extra_flags") or []
    assert "--log" in extra_flags
    assert "--force" in extra_flags


def test_update_makepkg_m_combined_short_flags_expanded(update_scenario):
    """Combined short flags like -lf are expanded to [-l, -f]."""
    args = _make_update_args(makepkg="-lf")
    calls = _run_update_capture_build_calls(update_scenario, args)
    assert calls
    extra_flags = calls[0].get("extra_flags") or []
    assert "-l" in extra_flags
    assert "-f" in extra_flags
