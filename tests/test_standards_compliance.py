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
from pathlib import Path

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
    with tempfile.NamedTemporaryFile(
        "w", suffix=".conf", encoding="utf-8", delete=False
    ) as sysconf:
        sysconf.write(
            'CARCH="x86_64"\nCHOST="x86_64-pc-linux-gnu"\n'
            f"OPTIONS={options}\n"
        )
    try:
        profile = {"CC": "clang", "CXX": "clang++",
                   "CFLAGS": "-O2", "LDFLAGS": "-fuse-ld=lld"}
        with emit_makepkg_conf(
            profile, active_consumes=None, system_conf_path=sysconf.name
        ) as path:
            emitted = next(
                ln for ln in Path(path).read_text(encoding="utf-8").splitlines()
                if ln.startswith("OPTIONS=")
            )
    finally:
        Path(sysconf.name).unlink()
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


# ---------------------------------------------------------------------------
# systemd.resource-control(5) — cgroup is the primary tier for build resource
# enforcement (standards row 19 / 2.3.0-F9). A configured build ceiling
# (cpu_quota or mem_limit) must reach the makepkg fork tree via a systemd-run
# --scope cgroup (CPUQuota=/MemoryMax=), not solely via an escapable RLIMIT_AS
# preexec, whenever systemd-run is available.


def test_mem_limit_enforced_via_cgroup_scope_when_systemd_available(monkeypatch):
    """A mem_limit alone earns a MemoryMax cgroup scope, not just the rlimit."""
    import sysforge.primitives.build_throttle as bt

    monkeypatch.setattr(bt.shutil, "which", lambda name: "/usr/bin/" + name)
    throttle = bt.BuildThrottle(mem_limit_bytes=24 * 1024 ** 3)
    argv = bt.wrapper_argv(throttle)
    assert argv[:4] == ["systemd-run", "--scope", "--user", "--quiet"]
    assert f"MemoryMax={24 * 1024 ** 3}" in argv
    # The cgroup owns the cap, so the rlimit preexec is suppressed (no double-apply).
    assert bt.resolve_child_mem_cap(throttle) is None


def _load_check_standards():
    """Load tools/check_standards.py as a module, registered in sys.modules.

    Registration is required so dataclasses (e.g. Finding) can resolve their
    deferred `from __future__ import annotations` string annotations against
    the module's own namespace.
    """
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "check_standards", "tools/check_standards.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_run_seam_flags_string_command(tmp_path):
    """A subprocess call with a string first-arg (shell-string form) is flagged."""
    repo = tmp_path
    (repo / "sysforge").mkdir()
    (repo / "sysforge" / "bad.py").write_text(
        "import subprocess\n"
        "subprocess.run('echo hi')\n",
        encoding="utf-8",
    )
    mod = _load_check_standards()
    findings = mod.check_run_seam(repo)
    assert any("bad.py" in f.location for f in findings), findings


def test_run_seam_flags_unjustified_shell_true(tmp_path):
    """shell=True without a `# noqa: S602` and outside the allowlist is flagged."""
    repo = tmp_path
    (repo / "sysforge").mkdir()
    (repo / "sysforge" / "bad2.py").write_text(
        "import subprocess\n"
        "subprocess.run(['sh', '-c', 'x'], shell=True)\n",
        encoding="utf-8",
    )
    mod = _load_check_standards()
    findings = mod.check_run_seam(repo)
    assert any("bad2.py" in f.location for f in findings), findings


def test_run_seam_allows_list_form(tmp_path):
    """A plain argv-list call is not flagged."""
    repo = tmp_path
    (repo / "sysforge").mkdir()
    (repo / "sysforge" / "good.py").write_text(
        "import subprocess\n"
        "subprocess.run(['pacman', '-Q'])\n",
        encoding="utf-8",
    )
    mod = _load_check_standards()
    findings = mod.check_run_seam(repo)
    assert not findings, findings


def test_run_seam_flags_string_command_via_module_alias(tmp_path):
    """An aliased module import (`import subprocess as _sp`) doesn't evade the check."""
    repo = tmp_path
    (repo / "sysforge").mkdir()
    (repo / "sysforge" / "aliased.py").write_text(
        "import subprocess as _sp\n"
        "_sp.run('echo hi')\n",
        encoding="utf-8",
    )
    mod = _load_check_standards()
    findings = mod.check_run_seam(repo)
    assert any("aliased.py" in f.location for f in findings), findings


def test_run_seam_flags_string_command_via_direct_import(tmp_path):
    """A direct func import (`from subprocess import run`) doesn't evade the check."""
    repo = tmp_path
    (repo / "sysforge").mkdir()
    (repo / "sysforge" / "direct.py").write_text(
        "from subprocess import run\n"
        "run('echo hi')\n",
        encoding="utf-8",
    )
    mod = _load_check_standards()
    findings = mod.check_run_seam(repo)
    assert any("direct.py" in f.location for f in findings), findings


def _privilege_seam_flags(src: str) -> list:
    import ast
    mod = _load_check_standards()
    tree = ast.parse(src)
    return mod._privilege_seam_findings_for_tree(tree, "sysforge/x.py")


def test_privilege_seam_flags_raw_sudo_list():
    assert _privilege_seam_flags('subprocess.run(["sudo", "pacman", "-Syu"])')


def test_privilege_seam_allows_auth_probe_v():
    assert not _privilege_seam_flags('subprocess.run(["sudo", "-v"])')


def test_privilege_seam_allows_auth_probe_n_true():
    assert not _privilege_seam_flags('subprocess.run(["sudo", "-n", "true"])')


def test_privilege_seam_allows_drop_privilege_any_user():
    assert not _privilege_seam_flags(
        'subprocess.run(["sudo", "-u", cfg.username, "git", "clone"])'
    )


def test_privilege_seam_ignores_non_first_sudo():
    # a "sudo" string that is not the argv[0] literal is not an escalation prefix
    assert not _privilege_seam_flags('x = ["--flag", "sudo", "thing"]')


def test_privilege_seam_behaviour():
    """Row 18: privileged_argv is the escalation authority."""
    from unittest.mock import patch

    from sysforge.primitives import privilege
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=1000):
        assert privilege.privileged_argv(["pacman", "-Syu"])[0] == "sudo"
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=0):
        assert privilege.privileged_argv(["pacman", "-Syu"]) == ["pacman", "-Syu"]


def test_journald_mirror_emits_only_for_sentinel_verbs(tmp_path, monkeypatch):
    """STD row 20: run_verb mirrors sentinel-gated verbs to journald, and only those.

    Structural invariant — emission is keyed off ``requires_sentinel``, not a
    hard-coded verb list — so it cannot rot as verbs are added.
    """
    from contextlib import nullcontext
    from types import SimpleNamespace

    from sysforge.verbs import runner
    from sysforge.verbs.base import ExecResult, PreCheckResult, Verb

    calls: list[tuple[str, str | None, int]] = []
    monkeypatch.setattr(
        runner.journal, "record_verb",
        lambda verb, target, exit_code: calls.append((verb, target, exit_code)),
    )
    # Isolate from the real sentinel file lifecycle.
    monkeypatch.setattr(runner, "sentinel_scope", lambda *a, **k: nullcontext())

    class _Mut(Verb):
        name = "mut"
        requires_sentinel = True

        def pre_check(self, args):
            return PreCheckResult()

        def execute(self, args, pre):
            return ExecResult(exit_code=0)

        def journal_target(self, args):
            return "widget"

    class _Ro(Verb):
        name = "ro"
        requires_sentinel = False

        def pre_check(self, args):
            return PreCheckResult()

        def execute(self, args, pre):
            return ExecResult(exit_code=0)

    args = SimpleNamespace(state_dir=str(tmp_path), dry_run=True)

    assert runner.run_verb(_Mut(), args) == 0
    assert runner.run_verb(_Ro(), args) == 0

    assert calls == [("mut", "widget", 0)]  # ro emitted nothing


def test_qkk_mtree_contract_backup_and_missing_classification(monkeypatch):
    """STD row 22: the `pacman -Qkk` / libalpm mtree contract is consumed with
    pacman's own backup-vs-altered classification (backup edits are expected;
    a missing package-owned file is an integrity error)."""
    from types import SimpleNamespace

    from sysforge.primitives import diagnostics as diag
    from sysforge.primitives import pkgfiles_probe

    monkeypatch.setattr(pkgfiles_probe, "_run", lambda packages: SimpleNamespace(
        stdout="backup file: pacman: /etc/pacman.conf (SHA256 checksum mismatch)\n",
        stderr="warning: coreutils: /usr/bin/ls (No such file or directory)\n",
        returncode=1,
    ))
    findings = {f.check_id: f for f in pkgfiles_probe.collect_integrity_findings()}
    assert findings["integrity_backup_edited"].severity == diag.SEV_INFO
    assert findings["integrity_missing"].severity == diag.SEV_ERROR


# ---------------------------------------------------------------------------
# distro_portability group — Arch-derivative portability (STD row 23)
#
# Three sub-invariants: (a) no hardcoded sync-repo names, (b) the system
# makepkg.conf is the merge baseline, (c) distro identity from os-release(5)
# through one primitive.
# ---------------------------------------------------------------------------

def _repo_with_os_release_home(tmp_path):
    """A synthetic repo whose os-release home exists, so only the module under
    test can produce a finding."""
    home = tmp_path / "sysforge" / "primitives" / "os_release.py"
    home.parent.mkdir(parents=True)
    home.write_text('P = "/etc/os-release"\nQ = "/usr/lib/os-release"\n',
                    encoding="utf-8")
    return tmp_path


def test_distro_portability_c_allows_the_one_home(tmp_path):
    repo = _repo_with_os_release_home(tmp_path)
    mod = _load_check_standards()
    assert mod._check_identity_home(repo) == []


def test_distro_portability_c_flags_os_release_read_elsewhere(tmp_path):
    repo = _repo_with_os_release_home(tmp_path)
    (repo / "sysforge" / "sneaky.py").write_text(
        'from pathlib import Path\n'
        'ID = Path("/etc/os-release").read_text()\n',
        encoding="utf-8",
    )
    mod = _load_check_standards()
    findings = mod._check_identity_home(repo)
    assert any("sneaky.py" in f.location for f in findings), findings


def test_distro_portability_c_flags_arch_release_marker(tmp_path):
    """/etc/arch-release identifies nothing — derivatives ship it too."""
    repo = _repo_with_os_release_home(tmp_path)
    (repo / "sysforge" / "sniff.py").write_text(
        'from pathlib import Path\n'
        'IS_ARCH = Path("/etc/arch-release").exists()\n',
        encoding="utf-8",
    )
    mod = _load_check_standards()
    findings = mod._check_identity_home(repo)
    assert any("sniff.py" in f.location for f in findings), findings


def test_distro_portability_c_flags_missing_home(tmp_path):
    (tmp_path / "sysforge").mkdir()
    mod = _load_check_standards()
    findings = mod._check_identity_home(tmp_path)
    assert any("os_release.py" in f.location for f in findings), findings


# --- sub-invariant (a): no hardcoded sync-repo names ------------------------

def _repo_with_pacman_home(tmp_path):
    """Synthetic repo carrying the allowlisted repo-name fallback."""
    home = tmp_path / "sysforge" / "primitives" / "pacman.py"
    home.parent.mkdir(parents=True)
    home.write_text('def _read_sync_repo_names():\n    return ["core", "extra"]\n',
                    encoding="utf-8")
    return tmp_path


def test_distro_portability_a_allows_the_pacman_fallback(tmp_path):
    repo = _repo_with_pacman_home(tmp_path)
    mod = _load_check_standards()
    assert mod._check_repo_name_literals(repo) == []


def test_distro_portability_a_flags_repo_name_list_elsewhere(tmp_path):
    repo = _repo_with_pacman_home(tmp_path)
    (repo / "sysforge" / "deps.py").write_text(
        'SYNC_REPOS = ["core", "extra", "multilib"]\n', encoding="utf-8")
    mod = _load_check_standards()
    findings = mod._check_repo_name_literals(repo)
    assert len(findings) == 3, findings
    assert all("deps.py" in f.location for f in findings)


def test_distro_portability_a_flags_repo_name_comparison(tmp_path):
    """The failure class: deciding repo-vs-AUR by comparing a repo name."""
    repo = _repo_with_pacman_home(tmp_path)
    (repo / "sysforge" / "split.py").write_text(
        'def is_repo(db):\n    return db == "extra"\n', encoding="utf-8")
    mod = _load_check_standards()
    findings = mod._check_repo_name_literals(repo)
    assert any("split.py" in f.location for f in findings), findings


def test_distro_portability_a_ignores_non_decision_strings(tmp_path):
    """A bare string that isn't a repo-name decision is not a violation —
    'extra' and 'core' are ordinary English words."""
    repo = _repo_with_pacman_home(tmp_path)
    (repo / "sysforge" / "prose.py").write_text(
        'def f(**kw):\n'
        '    msg = "extra"\n'
        '    return kw.get("core")\n',
        encoding="utf-8",
    )
    mod = _load_check_standards()
    assert mod._check_repo_name_literals(repo) == []


# --- sub-invariant (b): system makepkg.conf is the merge baseline -----------

_EMITTER_OK = (
    'from sysforge.primitives.config import parse_system_makepkg_conf\n'
    'def emit():\n'
    '    system_assignments = parse_system_makepkg_conf(None)\n'
    '    lines = []\n'
    '    for key, raw_val in system_assignments.items():\n'
    '        lines.append(f"{key}={raw_val}")\n'
    '    return lines\n'
)


def _repo_with_emitter(tmp_path, emitter_src=_EMITTER_OK, conf_home=True):
    prim = tmp_path / "sysforge" / "primitives"
    prim.mkdir(parents=True)
    (prim / "makepkg_conf.py").write_text(emitter_src, encoding="utf-8")
    if conf_home:
        (prim / "config.py").write_text(
            'from pathlib import Path\n'
            'SYSTEM_MAKEPKG_CONF = Path("/etc/makepkg.conf")\n',
            encoding="utf-8")
    return tmp_path


def test_distro_portability_b_allows_baseline_merge(tmp_path):
    repo = _repo_with_emitter(tmp_path)
    mod = _load_check_standards()
    assert mod._check_makepkg_baseline(repo) == []


def test_distro_portability_b_flags_conf_path_outside_config(tmp_path):
    repo = _repo_with_emitter(tmp_path)
    (repo / "sysforge" / "peek.py").write_text(
        'from pathlib import Path\n'
        'CONF = Path("/etc/makepkg.conf").read_text()\n',
        encoding="utf-8",
    )
    mod = _load_check_standards()
    findings = mod._check_makepkg_baseline(repo)
    assert any("peek.py" in f.location for f in findings), findings


def test_distro_portability_b_flags_dropped_baseline_load(tmp_path):
    """An emitter that stops loading the system conf replaces the derivative's
    -march/LTO defaults instead of overriding them."""
    repo = _repo_with_emitter(tmp_path, emitter_src=(
        'def emit():\n'
        '    system_assignments = {}\n'
        '    for key, raw_val in system_assignments.items():\n'
        '        pass\n'
    ))
    mod = _load_check_standards()
    findings = mod._check_makepkg_baseline(repo)
    assert any("parse_system_makepkg_conf" in f.message for f in findings), findings


def test_distro_portability_b_flags_unemitted_baseline(tmp_path):
    """Loading the baseline but not writing it out drops every key the
    derivative set — a read alone is not a merge."""
    repo = _repo_with_emitter(tmp_path, emitter_src=(
        'from sysforge.primitives.config import parse_system_makepkg_conf\n'
        'def emit():\n'
        '    system_assignments = parse_system_makepkg_conf(None)\n'
        '    return ["CFLAGS=-O2"]\n'
    ))
    mod = _load_check_standards()
    findings = mod._check_makepkg_baseline(repo)
    assert any("iterates system_assignments" in f.message for f in findings), findings


def test_distro_portability_b_flags_missing_emitter(tmp_path):
    (tmp_path / "sysforge").mkdir()
    mod = _load_check_standards()
    findings = mod._check_makepkg_baseline(tmp_path)
    assert any("makepkg_conf.py" in f.location for f in findings), findings


# --- the whole group on the shipped tree ------------------------------------

def test_distro_portability_clean_on_the_real_tree():
    """The shipped tree conforms to all three sub-invariants."""
    from pathlib import Path
    mod = _load_check_standards()
    assert mod.check_distro_portability(Path()) == []


def test_deprecations_group_passes_on_this_repo():
    """Row 24 — the committed registry is internally consistent."""
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "tools"))
    try:
        import check_standards as cs
    finally:
        sys.path.pop(0)
    findings = cs.check_deprecations(repo)
    errors = [f for f in findings if f.severity == "error"]
    assert not errors, "\n".join(f"{f.location}: {f.message}" for f in errors)


def test_deprecations_registry_parse_finds_every_record():
    """A check that cannot fail is worse than no check: the AST parse must find
    the same number of records the module exposes at runtime."""
    import sys
    from pathlib import Path
    from sysforge.primitives import deprecations as dep
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "tools"))
    try:
        import check_standards as cs
    finally:
        sys.path.pop(0)
    assert len(cs._parse_registry(repo)) == len(dep.all_deprecations())


def test_deprecations_group_flags_unregistered_warn_used(tmp_path):
    """An unregistered warn_used literal is an R1 error."""
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "tools"))
    try:
        import check_standards as cs
    finally:
        sys.path.pop(0)
    # Synthetic tree: an empty registry plus one warn_used call.
    (tmp_path / "sysforge" / "primitives").mkdir(parents=True)
    (tmp_path / "sysforge" / "primitives" / "deprecations.py").write_text(
        "_REGISTRY = ()\n", encoding="utf-8")
    (tmp_path / "sysforge" / "caller.py").write_text(
        'deprecations.warn_used("ghost.surface")\n', encoding="utf-8")
    msgs = [f.message for f in cs.check_deprecations(tmp_path)]
    assert any("ghost.surface" in m for m in msgs)


def test_deprecations_flags_overdue_removal_at_target_version():
    """R3 — at 3.0.0 the five compat surfaces are overdue and must error."""
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "tools"))
    try:
        import check_standards as cs
    finally:
        sys.path.pop(0)
    findings = cs.check_deprecations(repo, target_version="3.0.0")
    overdue = [f for f in findings if "still" in f.message and "present" in f.message]
    assert len(overdue) == 5, \
        f"expected the 5 compat surfaces overdue at 3.0.0, got {len(overdue)}"
    # The 3.1.0 shim is NOT overdue at 3.0.0.
    assert not any("doctor.flat_flags" in f.message for f in overdue)


def test_target_version_rejects_malformed_value():
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    r = subprocess.run(
        [sys.executable, "tools/check_standards.py",
         "--check=deprecations", "--target-version=not-a-version"],
        cwd=repo, capture_output=True, text=True)
    assert r.returncode == 2, r.stdout + r.stderr
