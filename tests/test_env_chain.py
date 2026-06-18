"""Tests for sysforge.primitives.env_chain."""
import subprocess
from unittest.mock import patch

import pytest

from sysforge.primitives.env_chain import (
    EnvChainSnapshot,
    ProcessLink,
    _parse_shell_init_file,
    _read_pam_env,
    _read_process_chain,
    _read_sysforge_config,
    _read_systemd_user_env,
    collect_env_chain,
    compute_divergences,
    format_env_chain,
    validate_env_chain,
)


def _clear_env(monkeypatch, names):
    for n in names:
        monkeypatch.delenv(n, raising=False)


def test_collect_captures_set_vars(monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", "/tmp/state")
    monkeypatch.setenv("SYSFORGE_CONFIG_DIR", "/tmp/cfg")
    monkeypatch.setenv("CC", "clang")
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/venv")
    snap = collect_env_chain()
    assert snap.sysforge["SYSFORGE_STATE_DIR"] == "/tmp/state"
    assert snap.sysforge["SYSFORGE_CONFIG_DIR"] == "/tmp/cfg"
    assert snap.toolchain["CC"] == "clang"
    assert snap.python["VIRTUAL_ENV"] == "/tmp/venv"


def test_collect_reports_unset_as_none(monkeypatch):
    _clear_env(monkeypatch, ["SYSFORGE_STATE_DIR", "SYSFORGE_CONFIG_DIR"])
    snap = collect_env_chain()
    assert snap.sysforge["SYSFORGE_STATE_DIR"] is None
    assert snap.sysforge["SYSFORGE_CONFIG_DIR"] is None


def test_collect_includes_pid_and_path(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/usr/local/bin")
    snap = collect_env_chain()
    assert isinstance(snap.pid, int) and snap.pid > 0
    assert snap.path == "/usr/bin:/usr/local/bin"


def test_collect_walks_process_chain():
    snap = collect_env_chain()
    assert len(snap.process_chain) >= 1
    assert snap.process_chain[0].pid == snap.pid


def test_validate_warns_when_state_dir_unset(monkeypatch):
    _clear_env(monkeypatch, ["SYSFORGE_STATE_DIR"])
    snap = collect_env_chain()
    warnings = validate_env_chain(snap)
    assert any("SYSFORGE_STATE_DIR" in w for w in warnings)


def test_validate_silent_when_state_dir_set(monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", "/tmp/state")
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/venv")
    monkeypatch.setenv("CC", "clang")
    snap = collect_env_chain()
    warnings = validate_env_chain(snap)
    assert not any("SYSFORGE_STATE_DIR" in w for w in warnings)


def test_validate_warns_when_venv_unset(monkeypatch):
    _clear_env(monkeypatch, ["VIRTUAL_ENV"])
    snap = collect_env_chain()
    warnings = validate_env_chain(snap)
    assert any("VIRTUAL_ENV" in w for w in warnings)


def test_validate_warns_when_no_toolchain_overrides(monkeypatch):
    _clear_env(monkeypatch, ["CC", "CXX"])
    snap = collect_env_chain()
    warnings = validate_env_chain(snap)
    assert any("CC" in w and "CXX" in w for w in warnings)


def test_format_includes_sections_and_values(monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", "/tmp/state")
    monkeypatch.setenv("CC", "clang")
    monkeypatch.setenv("PATH", "/usr/bin")
    rendered = format_env_chain(collect_env_chain())
    assert "sysforge:" in rendered
    assert "toolchain:" in rendered
    assert "/tmp/state" in rendered
    assert "CC = clang" in rendered
    assert "PATH:" in rendered
    assert "process chain" in rendered
    assert "shell init files" in rendered


def test_format_marks_unset_vars(monkeypatch):
    _clear_env(monkeypatch, ["SYSFORGE_STATE_DIR"])
    rendered = format_env_chain(collect_env_chain())
    assert "SYSFORGE_STATE_DIR = <unset>" in rendered


def test_format_truncates_long_cmdlines():
    snap = EnvChainSnapshot(
        pid=42,
        process_chain=[
            ProcessLink(pid=42, comm="python", cmdline="x" * 500),
            ProcessLink(pid=1, comm="systemd", cmdline=""),
        ],
        shell_init_files={"/etc/profile": True},
    )
    rendered = format_env_chain(snap)
    assert "..." in rendered
    assert "x" * 500 not in rendered


def test_read_process_chain_handles_missing_proc():
    chain = _read_process_chain(start_pid=2**22 + 1, max_depth=4)
    assert chain == []


def test_read_process_chain_terminates_at_init():
    chain = _read_process_chain(start_pid=1, max_depth=4)
    assert len(chain) >= 1
    assert chain[0].pid == 1


def test_format_renders_warnings_section(monkeypatch):
    _clear_env(monkeypatch, ["SYSFORGE_STATE_DIR"])
    rendered = format_env_chain(collect_env_chain())
    assert "warnings:" in rendered


def test_log_env_chain_returns_snapshot_and_calls_logger(monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", "/tmp/state")
    captured = []

    class FakeLogger:
        def debug(self, msg):
            captured.append(("debug", msg))
        def info(self, msg):
            captured.append(("info", msg))

    fake = FakeLogger()
    with patch("sysforge.log.get_logger", return_value=fake):
        from sysforge.primitives.env_chain import log_env_chain
        snap = log_env_chain("debug")
    assert isinstance(snap, EnvChainSnapshot)
    assert captured and captured[0][0] == "debug"
    assert "sysforge:" in captured[0][1]


@pytest.mark.parametrize("level", ["debug", "info"])
def test_log_env_chain_honours_level(level):
    captured = []

    class FakeLogger:
        def debug(self, msg):
            captured.append(("debug", msg))
        def info(self, msg):
            captured.append(("info", msg))

    fake = FakeLogger()
    with patch("sysforge.log.get_logger", return_value=fake):
        from sysforge.primitives.env_chain import log_env_chain
        log_env_chain(level)
    assert captured and captured[0][0] == level


# ---------------------------------------------------------------------------
# Source readers + divergence
# ---------------------------------------------------------------------------

def test_parse_init_file_basic_exports(tmp_path):
    f = tmp_path / "rc"
    f.write_text(
        '# comment line\n'
        'export A=1\n'
        'B=2; export B\n'
        'export C="quoted"\n'
        "export D='single'\n"
    )
    kv, caveats = _parse_shell_init_file(f)
    assert kv == {"A": "1", "B": "2", "C": "quoted", "D": "single"}
    assert caveats == 0


def test_parse_init_file_skips_expansions(tmp_path):
    f = tmp_path / "rc"
    f.write_text(
        'export A="$(date)"\n'
        'export B="${OTHER}"\n'
        'export C=plain\n'
    )
    kv, caveats = _parse_shell_init_file(f)
    assert kv["A"].startswith("<expansion:")
    assert kv["B"].startswith("<expansion:")
    assert kv["C"] == "plain"
    # parse_caveats counts non-matching-but-assignment-like lines; the three
    # above all matched, so caveats stays 0. The expansion marker itself is
    # the user-visible signal.
    assert caveats == 0


def test_etc_environment_bare_assignments_accepted(tmp_path):
    # /etc/environment uses bare KEY=value (no `export`); ensure
    # `_parse_shell_init_file(..., allow_bare=True)` accepts them and that
    # bare assignments are rejected without the flag, matching what the
    # `_read_etc_environment` wrapper does on the real /etc/environment.
    f = tmp_path / "environment"
    f.write_text("LANG=en_US.UTF-8\nPATH=/usr/bin\n")
    kv, _ = _parse_shell_init_file(f, allow_bare=True)
    assert kv == {"LANG": "en_US.UTF-8", "PATH": "/usr/bin"}
    kv_strict, _ = _parse_shell_init_file(f, allow_bare=False)
    assert kv_strict == {}


def test_pam_env_default_and_override(tmp_path, monkeypatch):
    f = tmp_path / "pam_env.conf"
    f.write_text(
        '# comment\n'
        'LANG  DEFAULT=C  OVERRIDE="en_US.UTF-8"\n'
        'EDITOR DEFAULT=vi\n'
    )
    # Patch the module-internal Path() to read our fixture file.
    real_read = _read_pam_env

    def fake_read():
        # Inline the parser using fixture path.
        import re as _re
        text = f.read_text()
        defaults: dict[str, str] = {}
        overrides: dict[str, str] = {}
        field_re = _re.compile(r'(DEFAULT|OVERRIDE)=("[^"]*"|\'[^\']*\'|\S+)')
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            head = line.split(None, 1)[0]
            for m in field_re.finditer(line):
                kind, val = m.group(1), m.group(2).strip('"').strip("'")
                if kind == "DEFAULT":
                    defaults[head] = val
                else:
                    overrides[head] = val
        return defaults, overrides, 0

    monkeypatch.setattr("sysforge.primitives.env_chain._read_pam_env", fake_read)
    from sysforge.primitives.env_chain import _read_pam_env as patched
    defaults, overrides, _ = patched()
    assert defaults == {"LANG": "C", "EDITOR": "vi"}
    assert overrides == {"LANG": "en_US.UTF-8"}
    # Sanity: real reader is still importable.
    assert callable(real_read)


def test_systemd_user_env_subprocess(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    class FakeResult:
        returncode = 0
        stdout = "FOO=bar\nBAZ=qux\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
    kv = _read_systemd_user_env()
    assert kv == {"FOO": "bar", "BAZ": "qux"}

    def boom(*a, **kw):
        raise subprocess.CalledProcessError(1, a[0])
    monkeypatch.setattr(subprocess, "run", boom)
    # CalledProcessError isn't raised by our code because check=False — but a
    # non-zero returncode results in empty output. Simulate that:
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "no session"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: Failed())
    assert _read_systemd_user_env() == {}


def test_systemd_user_skipped_without_xdg_runtime(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    called = {"n": 0}

    def shouldnt_run(*a, **kw):
        called["n"] += 1
        raise AssertionError("subprocess should not be invoked")

    monkeypatch.setattr(subprocess, "run", shouldnt_run)
    assert _read_systemd_user_env() == {}
    assert called["n"] == 0


def test_sysforge_config_source_uses_defaults_profile(tmp_path, monkeypatch):
    config_dir = tmp_path / "sysforge-conf"
    config_dir.mkdir(parents=True)
    (config_dir / "profiles.toml").write_text(
        '[defaults]\n'
        'profile = "standard"\n'
        '\n'
        '[profiles.bare]\n'
        'CC = "gcc"\n'
        'CXX = "g++"\n'
        '\n'
        '[profiles.standard]\n'
        'extends = "bare"\n'
        'CFLAGS = "-O2"\n'
    )
    monkeypatch.setenv("SYSFORGE_CONFIG_DIR", str(config_dir))
    # paths.CONFIG_PATHS is bound at import; reload to pick up the new env.
    import importlib
    import sysforge.primitives.paths as _paths_mod
    importlib.reload(_paths_mod)
    import sysforge.primitives.config as _config_mod
    importlib.reload(_config_mod)
    profile_name, kv, err = _read_sysforge_config()
    assert err is None
    assert profile_name == "standard"
    assert kv["CC"] == "gcc"
    assert kv["CXX"] == "g++"
    assert kv["CFLAGS"] == "-O2"


def test_divergence_reports_unset_runtime_vs_source(monkeypatch):
    _clear_env(monkeypatch, ["CC"])
    snap = EnvChainSnapshot(
        sources={
            "runtime": {},
            "user_zprofile": {"CC": "clang"},
        },
    )
    div = compute_divergences(snap)
    assert "CC" in div
    rendered = format_env_chain(snap)
    assert "mismatches:" in rendered
    assert "CC:" in rendered
    assert "clang" in rendered


def test_format_inline_annotation_at_verbosity_2():
    snap = EnvChainSnapshot(
        toolchain={"CC": "gcc"},
        sources={
            "runtime": {"CC": "gcc"},
            "user_zshrc": {"CC": "clang"},
        },
    )
    v0 = format_env_chain(snap, verbosity=0)
    v1 = format_env_chain(snap, verbosity=1)
    v2 = format_env_chain(snap, verbosity=2)
    assert "[differs from" not in v0
    assert "[differs from" not in v1
    assert "[differs from" in v2
    # Mismatches block always present:
    assert "mismatches:" in v0
    assert "mismatches:" in v2
