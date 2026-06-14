# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_standards_compliance.py — behavioural guards for the committed standards.

This is the runtime half of the standards regime documented in
docs/design/21-standards.md. The *static* half (path/XDG-FHS discipline, SPDX
headers, Keep a Changelog headings, UTF-8 encoding) is enforced by
tools/check_standards.py / `make check-standards`. The checks here cover the
behavioural contracts that grep cannot see:

  * NO_COLOR / FORCE_COLOR honoured by the single colour authority.
  * `--version` / `--help` exit 0 and print to stdout; argparse errors → stderr,
    exit 2 (POSIX/GNU CLI + stdout/stderr discipline).
  * state timestamps are RFC 3339 UTC.

SemVer is deliberately *not* asserted here: the runtime `__version__` is
`0.0.0+unknown` in a run-from-repo checkout (no installed dist metadata), so the
declared `pyproject.toml` version is the real source of truth and is validated
statically by tools/check_shipped.py::check_versions (X.Y.Z fullmatch).
  * sysforge does not undermine the reproducibility of packages it builds
    (OPTIONS preserved verbatim; SOURCE_DATE_EPOCH not stripped from build env).

Each test names the standard it guards. When a standard's enforcement mechanism
is "behavioural test" in 21-standards.md, this is where it lives — don't scatter
a parallel standards check elsewhere.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

from sysforge import log

# RFC 3339 / ISO 8601, UTC "Z" form (the shape build_state/stage_sentinel emit).
_RFC3339_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class _FakeTTY:
    """Stand-in stream reporting isatty()=True (mirrors tests/test_log.py)."""

    def isatty(self):
        return True

    def write(self, _):
        pass

    def flush(self):
        pass


# ---------------------------------------------------------------------------
# Standard: NO_COLOR + FORCE_COLOR  (the single authority is log.use_color)
# ---------------------------------------------------------------------------

def test_no_color_disables_even_on_tty(monkeypatch):
    """NO_COLOR (any non-empty value) wins over an attached TTY."""
    monkeypatch.setattr(log, "_COLOR_MODE", "auto")
    monkeypatch.setattr(log, "_out", lambda: _FakeTTY())
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert log.use_color() is False


def test_force_color_enables_off_tty(monkeypatch):
    """FORCE_COLOR turns colour on even when the stream is not a TTY."""
    monkeypatch.setattr(log, "_COLOR_MODE", "auto")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    # capsys/pytest stderr is a non-TTY buffer; FORCE_COLOR overrides that.
    assert log.use_color() is True


def test_no_color_outranks_force_color(monkeypatch):
    """When both are set, NO_COLOR wins (it is checked first)."""
    monkeypatch.setattr(log, "_COLOR_MODE", "auto")
    monkeypatch.setattr(log, "_out", lambda: _FakeTTY())
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert log.use_color() is False


# ---------------------------------------------------------------------------
# Standard: POSIX/GNU CLI + stdout/stderr + exit-code contract
#
# Driven as a subprocess so we exercise the real console entry point exactly as
# a user does, without mutating this process's argv / colour mode / rlimits.
# HOME is sandboxed to a temp dir because main() runs migrate_legacy_user_dirs()
# before argparse — the sandbox keeps that side effect off the real home dir.
# ---------------------------------------------------------------------------

def _run_cli(*cli_args, tmp_home):
    prog = (
        "import sys; sys.argv = ['sysforge', *%r]; "
        "from sysforge.cli import main; main()" % list(cli_args)
    )
    env = {
        **os.environ,
        "HOME": str(tmp_home),
        "NO_COLOR": "1",  # deterministic, un-coloured output
    }
    # Resolve all XDG roots under the sandbox so nothing escapes tmp_home.
    for var, rel in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_CACHE_HOME", ".cache"),
        ("XDG_STATE_HOME", ".local/state"),
    ):
        env[var] = str(tmp_home / rel)
    return subprocess.run(
        [sys.executable, "-c", prog],
        capture_output=True, text=True, env=env,
    )


def test_version_flag_exits_zero_to_stdout(tmp_path):
    """`--version` prints to stdout and exits 0 (GNU CLI convention)."""
    proc = _run_cli("--version", tmp_home=tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.startswith("sysforge ")
    assert proc.stderr == ""


def test_short_version_flag_matches_long(tmp_path):
    """`-V` is an alias for `--version`."""
    proc = _run_cli("-V", tmp_home=tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.startswith("sysforge ")


def test_help_flag_exits_zero_to_stdout(tmp_path):
    """`--help` prints usage to stdout and exits 0."""
    proc = _run_cli("--help", tmp_home=tmp_path)
    assert proc.returncode == 0
    assert "usage:" in proc.stdout.lower()
    assert proc.stderr == ""


def test_unknown_flag_errors_to_stderr_exit_2(tmp_path):
    """Argparse usage errors go to stderr with exit code 2, never stdout."""
    proc = _run_cli("--definitely-not-a-real-flag", tmp_home=tmp_path)
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert proc.stderr != ""


# ---------------------------------------------------------------------------
# Standard: RFC 3339 / ISO 8601 timestamps
# ---------------------------------------------------------------------------

def test_state_timestamp_is_rfc3339_utc():
    """build_state._now_iso emits an RFC 3339 UTC instant that round-trips."""
    from datetime import datetime

    from sysforge.primitives.build_state import _now_iso

    stamp = _now_iso()
    assert _RFC3339_Z.match(stamp), f"{stamp!r} is not RFC 3339 UTC (…Z)"
    # Must parse back as a real instant.
    datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Standard: Reproducible builds
#
# sysforge must not undermine the reproducibility of packages it builds. Two
# concrete guards: (1) it does not mutate the makepkg OPTIONS array for a normal
# (non-GCC-on-lld) build, so reproducibility-relevant options survive; (2) it
# does not strip SOURCE_DATE_EPOCH from the build subprocess environment.
# ---------------------------------------------------------------------------

def test_options_preserved_verbatim_for_clang_build():
    """A clang profile leaves the system OPTIONS array untouched (no lto flip)."""
    from sysforge.primitives.makepkg_conf import emit_makepkg_conf

    options = "(strip docs !libtool staticlibs emptydirs zipman purge debug lto)"
    sysconf = tempfile.NamedTemporaryFile(
        "w", suffix=".conf", encoding="utf-8", delete=False
    )
    try:
        sysconf.write(
            'CARCH="x86_64"\nCHOST="x86_64-pc-linux-gnu"\n'
            f"OPTIONS={options}\n"
        )
        sysconf.close()
        profile = {"CC": "clang", "CXX": "clang++",
                   "CFLAGS": "-O2", "LDFLAGS": "-fuse-ld=lld"}
        with emit_makepkg_conf(
            profile, active_consumes=None, system_conf_path=sysconf.name
        ) as path:
            emitted = next(
                ln for ln in open(path, encoding="utf-8").read().splitlines()
                if ln.startswith("OPTIONS=")
            )
    finally:
        os.unlink(sysconf.name)
    # Verbatim: lto stays lto (not !lto) and no token is dropped.
    assert emitted == f"OPTIONS={options}"


def test_source_date_epoch_not_stripped_from_build_env():
    """SOURCE_DATE_EPOCH is not among the keys invoke_makepkg scrubs.

    invoke_makepkg starts from os.environ.copy() and removes only
    makepkg-/toolchain-managed keys. SOURCE_DATE_EPOCH (the reproducible-build
    clock makepkg honours) must survive so packages built through sysforge stay
    reproducible.
    """
    from sysforge.primitives.profile import CONF_KEY_MAP

    stripped = CONF_KEY_MAP.get("makepkg", set()) | CONF_KEY_MAP.get("toolchain", set())
    assert "SOURCE_DATE_EPOCH" not in stripped
