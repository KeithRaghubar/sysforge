"""
conftest.py — shared pytest configuration for SysForge tests.

Sets SYSFORGE_CONFIG_DIR to point at the tracked fixture config dir
(tests/data/etc/sysforge) before any module imports, so load_config() and
other path-sensitive functions resolve test fixtures rather than
/etc/sysforge. SYSFORGE_CONFIG_DIR is the config dir *itself* (the dir that
directly holds the TOML files), not an FHS root prefix.
"""
import os
from pathlib import Path

import pytest

# Must be set before importing any sysforge module that reads CONFIG_DIR
# at import time.
TESTS_DIR = Path(__file__).parent
TEST_DATA = TESTS_DIR / "data"
FIXTURE_CONFIG_DIR = TEST_DATA / "etc/sysforge"

# Force (not setdefault) the fixture dir: a developer shell may export its own
# SYSFORGE_CONFIG_DIR pointing at a personal config dir (e.g. ~/sf-config), and
# the suite must always resolve the tracked tests/data fixtures regardless.
# Per-test config variation patches file *contents* (see update_scenario), not
# this env var, so pinning it is safe.
os.environ["SYSFORGE_CONFIG_DIR"] = str(FIXTURE_CONFIG_DIR)

# Scrub ambient colour overrides so log.use_color() resolves purely from the
# capture stream's TTY-ness. CI runners and editor-integrated shells commonly
# export FORCE_COLOR (or NO_COLOR), which would flip every plain-output log
# assertion; tests that exercise the override behaviour set these explicitly
# via monkeypatch.
os.environ.pop("FORCE_COLOR", None)
os.environ.pop("NO_COLOR", None)

# Force the subprocess fallback in primitives.pacman so existing tests that
# mock subprocess.run continue to drive the query. The pyalpm fast path is
# exercised explicitly in test_pacman_pyalpm.py.
os.environ.setdefault("SYSFORGE_PACMAN_NO_PYALPM", "1")

# Show all log messages in tests so assertions against log output work.
import sysforge.log as _sf_log
_sf_log.set_verbosity(2)


REPO_ROOT = TESTS_DIR.parent


@pytest.fixture(scope="session", autouse=True)
def _no_mock_derived_paths():
    """
    Fail the session if a `MagicMock/` tree appears in the working tree.

    `unittest.mock` gives every `MagicMock` a working `__fspath__` whose
    default return value is the string ``"MagicMock/<mock-name>/<id>"``. So
    handing a mock where production code expects a path does *not* raise —
    `Path(mock)` yields that as a **relative** path, which then resolves
    against the CWD (the repo root under pytest) and gets happily `mkdir`'d.
    The affected test's writes never land where it asserts they do, and the
    suite quietly mutates the source tree (2.5.1-B12).

    A `.gitignore` entry would be the wrong fix — the directory should never
    exist. Route the mock through the `state_dir` fixture (or any real
    `tmp_path`) instead.
    """
    stray = REPO_ROOT / "MagicMock"
    yield
    if stray.exists():
        import shutil as _shutil
        leaked = sorted(p.name for p in stray.glob("*"))
        _shutil.rmtree(stray, ignore_errors=True)
        pytest.fail(
            f"a mock reached code that resolved it as a filesystem path: "
            f"{stray} was created (mock attributes: {leaked}). Pass a real "
            f"directory (the `state_dir` fixture) instead of a bare "
            f"MagicMock. The stray tree has been removed.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _isolate_filesystem_soname_cache(monkeypatch):
    """
    `dep_analysis.soname_available` consults the real /usr/lib (and friends)
    when the supplied ldconfig set misses. Tests assert against synthetic
    state, so default-patch the filesystem probe to an empty set. Tests
    that want to exercise the fallback explicitly override the patch.
    """
    from sysforge.primitives import dep_analysis as _da
    monkeypatch.setattr(_da, "_filesystem_soname_set",
                        lambda lib32=False: frozenset())


@pytest.fixture(autouse=True)
def _isolate_local_pacman_db(monkeypatch, tmp_path_factory):
    """
    Point the local-DB root at an empty tree instead of /var/lib/pacman/local.

    Reads that resolve a package to `<pkgname>-<pkgver>-<pkgrel>/` otherwise hit
    the host's real DB, which makes the suite depend on whatever is installed on
    the machine running it. The toolchain stage's soname gate did exactly that
    from a `dry_run=True` test, walking every installed package (2.6.1-B22).

    Mirrors `_isolate_filesystem_soname_cache`: tests that want real DB
    behaviour build a fixture tree and re-patch `_LOCAL_DB_ROOT` (or pass
    `root=`) themselves.
    """
    from sysforge.primitives import pacman as _pacman
    empty_db = tmp_path_factory.mktemp("empty-pacman-db")
    monkeypatch.setattr(_pacman, "_LOCAL_DB_ROOT", empty_db)


@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch, tmp_path_factory):
    """
    Point ``SYSFORGE_STATE_DIR`` at a throwaway tree for *every* test.

    The opt-in ``state_dir`` fixture below isolates the state dir only for tests
    that ask for one. Anything else inherits the developer's ambient
    ``SYSFORGE_STATE_DIR`` (this workstation exports ``~/sf-state`` from
    ``.zshrc``) and writes to the real ``build_state.toml``. A test does not have
    to be *about* build state to do it: patching ``_run_build`` still lets
    ``makepkg_wrapper.run`` fall through to ``_record_build_state``, which is how
    ``test_run_profile_override_kernel_derives_kernel_build`` stamped a
    ``linux-unruled`` entry — pkgbuild_dir pointing into ``/tmp/pytest-of-*`` and
    ``owner_stage = "kernel"`` — into a live state file, where it read as a real
    drifted kernel package and could not be demoted (stage-owned entries are
    exempt from external-install reconciliation).

    Isolation by default, not by opt-in: writing to a user's home is not
    something a test should have to remember to prevent. Mirrors
    ``_isolate_local_pacman_db``. The ``state_dir`` fixture still wins for tests
    that request it — it sets the same variable afterwards and returns the Path.
    """
    monkeypatch.setenv("SYSFORGE_STATE_DIR",
                       str(tmp_path_factory.mktemp("sf-state-isolated")))


# ---------------------------------------------------------------------------
# Centralized external-isolation fixtures (Phase 0 — behavior-first testing).
#
# OPT-IN (not autouse): request them by name. They give behavior-level tests a
# single seam per genuine external so tests assert on *observable* effects
# (which commands were emitted, what was asked) instead of patching internal
# call sites. Existing tests that roll their own patching are unaffected.
#
# The global seams are valid because the codebase uses `subprocess.run` and
# `shutil.which` everywhere (no `from subprocess import run` / `from shutil
# import which`), so patching the module attribute intercepts every caller.
# Proven in tests/test_fixtures_smoke.py.
# ---------------------------------------------------------------------------
import shutil
import subprocess


def _make_proc(returncode=0, stdout="", stderr="", args=None):
    """Build a subprocess.CompletedProcess the way the codebase expects."""
    return subprocess.CompletedProcess(
        args=args if args is not None else [],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.fixture
def make_proc():
    """Factory fixture for CompletedProcess results."""
    return _make_proc


class _RunRecorder:
    """
    Programmable stand-in for ``subprocess.run`` installed at the global seam.

    - Records every invocation's argv in ``.calls`` (``.commands`` as strings).
    - Returns the first matching programmed response, else ``default``.
    - ``respond(match, ...)``: ``match`` is a substring (vs the joined command),
      an argv-prefix list, or a callable predicate. ``check=True`` raises
      ``CalledProcessError`` on a non-zero response, like the real call.
    """

    def __init__(self, real_run=None):
        self.calls = []
        self.default = _make_proc()
        self._rules = []
        self._passthrough = []
        self._real_run = real_run

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(cmd)
        for predicate in self._passthrough:
            if predicate(cmd) and self._real_run is not None:
                return self._real_run(cmd, *args, **kwargs)
        proc = self.default
        for predicate, response in self._rules:
            if predicate(cmd):
                proc = response
                break
        if kwargs.get("check") and proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, proc.stdout, proc.stderr)
        return proc

    @staticmethod
    def _as_predicate(match):
        if callable(match):
            return match
        if isinstance(match, (list, tuple)):
            prefix = list(match)
            return lambda cmd: list(cmd)[: len(prefix)] == prefix
        return lambda cmd: match in (
            cmd if isinstance(cmd, str) else " ".join(map(str, cmd)))

    def respond(self, match, *, returncode=0, stdout="", stderr=""):
        """Register a response rule (first match wins, in registration order)."""
        self._rules.append(
            (self._as_predicate(match), _make_proc(returncode, stdout, stderr)))
        return self

    def set_default(self, *, returncode=0, stdout="", stderr=""):
        self.default = _make_proc(returncode, stdout, stderr)
        return self

    def passthrough(self, match):
        """Let commands matching ``match`` run for real instead of returning a
        programmed/default response. Use for pure, safe binaries (e.g.
        ``vercmp``) whose genuine output a behavior test depends on."""
        self._passthrough.append(self._as_predicate(match))
        return self

    @property
    def commands(self):
        """Each recorded call as one string, for ergonomic assertions."""
        return [c if isinstance(c, str) else " ".join(map(str, c))
                for c in self.calls]


@pytest.fixture
def fake_run(monkeypatch):
    """Install a programmable ``subprocess.run`` recorder at the global seam."""
    recorder = _RunRecorder(subprocess.run)
    monkeypatch.setattr(subprocess, "run", recorder)
    return recorder


class _WhichMap:
    """Programmable ``shutil.which``: only registered binaries are 'found'."""

    def __init__(self):
        self._paths = {}

    def add(self, *names, prefix="/usr/bin"):
        for name in names:
            self._paths[name] = f"{prefix}/{name}"
        return self

    def set(self, name, path):
        self._paths[name] = path
        return self

    def __call__(self, name, *args, **kwargs):
        return self._paths.get(Path(name).name)


@pytest.fixture
def fake_which(monkeypatch):
    """Install a programmable ``shutil.which`` (empty by default)."""
    which = _WhichMap()
    monkeypatch.setattr(shutil, "which", which)
    return which


class _InputQueue:
    """Programmable ``builtins.input``: pops queued answers, records prompts."""

    def __init__(self):
        self.answers = []
        self.prompts = []

    def push(self, *answers):
        self.answers.extend(answers)
        return self

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError("no queued input")
        return self.answers.pop(0)


@pytest.fixture
def fake_input(monkeypatch):
    """Install a programmable ``builtins.input`` (EOFError when drained)."""
    queue = _InputQueue()
    monkeypatch.setattr("builtins.input", queue)
    return queue


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """A throwaway state dir wired via ``SYSFORGE_STATE_DIR``; returns the Path."""
    d = tmp_path / "sf-state"
    d.mkdir()
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(d))
    return d


@pytest.fixture
def no_network(monkeypatch):
    """Hard-fail any accidental real network call from a behavior test."""
    import urllib.request

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "network access in a behavior test (fake the RPC/subprocess seam)")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    return _blocked


@pytest.fixture
def frozen_policy():
    """Install a source freeze for the duration of a test, then reset.

    Yields a callable so a test can re-install a narrower policy (e.g. with
    thawed packages) without repeating the teardown.
    """
    from sysforge.primitives.net_policy import NetPolicy, reset_policy, set_policy

    def _install(*, thawed=()):
        set_policy(NetPolicy(frozen=True, thawed=frozenset(thawed)))

    _install()
    try:
        yield _install
    finally:
        reset_policy()


# ---------------------------------------------------------------------------
# Verb behavior harness (Phase 0 — behavior-first testing).
#
# Drives a verb through the *real* parser + dispatcher and captures the
# observable surface — exit code, stdout/stderr, and parsed log lines
# (level/tag/message) — so behavior tests assert on what a verb DOES rather
# than patching its internals. Replaces the `patch("sysforge.update.X")` style
# with `run_cli(["update", ...])` + assertions on the captured result.
#
#   cli_capture(fn)  — capture core for any callable (e.g. a synthetic verb
#                      via run_verb); returns CliResult.
#   run_cli(argv)    — parse real argv, dispatch the real verb_cls, capture.
# ---------------------------------------------------------------------------
import io
import re
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

_LOG_LINE_RE = re.compile(r"\[SYSFORGE\]\[([A-Z]+)\]\[([A-Z0-9_]+)\]\s?(.*)")


@dataclass
class CliResult:
    """Observable outcome of a verb run captured by the behavior harness."""

    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self):
        """stdout + stderr concatenated (log lines may land on either)."""
        return self.stdout + self.stderr

    @property
    def log_lines(self):
        """Parsed ``(level, tag, message)`` tuples from the captured output."""
        rows = []
        for line in self.output.splitlines():
            m = _LOG_LINE_RE.match(line)
            if m:
                rows.append((m.group(1), m.group(2), m.group(3)))
        return rows

    @property
    def tags(self):
        """Set of log tags emitted during the run."""
        return {tag for _lvl, tag, _msg in self.log_lines}

    def messages(self, *, tag=None, level=None):
        """Captured messages, optionally filtered by tag and/or level."""
        return [
            msg for lvl, t, msg in self.log_lines
            if (tag is None or t == tag) and (level is None or lvl == level)
        ]

    def logged(self, substring, *, tag=None, level=None):
        """True iff any captured message contains ``substring``."""
        return any(substring in msg for msg in self.messages(tag=tag, level=level))


def _capture(fn, *, verbosity=3):
    """Run ``fn()`` with stdout/stderr captured and verbosity pinned.

    Restores log verbosity / dry-run globals afterward. ``SystemExit`` is
    caught and its code mapped onto ``CliResult.exit_code``.
    """
    from sysforge import log

    out_buf, err_buf = io.StringIO(), io.StringIO()
    saved_verbosity = log.get_verbosity()
    saved_dry = log._DRY_RUN
    code = 0
    try:
        log.set_verbosity(verbosity)
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            try:
                code = fn()
                code = 0 if code is None else code
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        log.set_verbosity(saved_verbosity)
        log._DRY_RUN = saved_dry
    return CliResult(int(code), out_buf.getvalue(), err_buf.getvalue())


@pytest.fixture
def cli_capture():
    """Capture core: ``cli_capture(callable, *, verbosity=3) -> CliResult``."""
    return _capture


@pytest.fixture
def quiet_at_default(capsys):
    """Assert a stage emits no level-tagged narration at the shipped default.

    Standards row 25 (log-level rubric, docs/design/12-logging.md): `ui()` is
    the answer the user ran the command to see; `info()`/`warn()` are narration
    about producing it and are gated behind -vv/-v. The one failure mode that
    matters is narration leaking into always-visible output, so this runs the
    callable twice — once at the shipped default (verbosity 0) and once at -vv —
    and fails if the default run carried an [INFO] or [WARN] line.

    Returns (err_v0, err_v2) so the caller can additionally assert that the
    narration it expects *does* appear at -vv, and that its ui() output appears
    in both. Restores the ambient verbosity unconditionally: it is global
    process state and a leak breaks unrelated tests non-deterministically.

    A new pipeline stage acquires the guard by requesting this fixture. Do not
    re-hand-roll the set_verbosity dance in a stage test.
    """
    def _run(fn):
        from sysforge import log
        saved = log.get_verbosity()
        try:
            log.set_verbosity(0)
            fn()
            err_v0 = capsys.readouterr().err
            log.set_verbosity(2)
            fn()
            err_v2 = capsys.readouterr().err
        finally:
            log.set_verbosity(saved)

        assert "[INFO]" not in err_v0, (
            "narration leaked into default-verbosity output (standards row 25):\n"
            f"{err_v0}"
        )
        assert "[WARN]" not in err_v0, (
            "narration leaked into default-verbosity output (standards row 25):\n"
            f"{err_v0}"
        )
        return err_v0, err_v2

    return _run


@pytest.fixture
def run_cli():
    """Parse real argv, dispatch the real verb, return a captured CliResult.

    ``run_cli(argv, *, verbosity=3)``. Mirrors ``cli.main`` for the parts that
    affect a verb's behavior (verbosity + dry-run) while skipping entry
    plumbing (resource guard, legacy migration, stale-sentinel gate, progress
    init) that is not part of the verb under test.
    """
    from sysforge import log
    from sysforge.cli import _build_parser
    from sysforge.verbs import run_verb

    def _run(argv, *, verbosity=3):
        def _dispatch():
            args = _build_parser().parse_args(argv)
            if getattr(args, "dry_run", False):
                log.set_dry_run_mode()
            verb_cls = getattr(args, "verb_cls", None)
            if verb_cls is None:
                return 2
            return run_verb(verb_cls(), args)

        return _capture(_dispatch, verbosity=verbosity)

    return _run


@pytest.fixture
def update_scenario(fake_run, state_dir, tmp_path, monkeypatch):
    """Drive the real ``cmd_update`` behavior-first.

    Provides a real ``BuildState`` (seedable via ``record``), an on-disk
    minimal config so the real ``load_config`` / ``_assemble_package_set``
    resolve PKGBUILDs under a temp source root, ``fake_run`` pacman, and the
    build + VCS-eval externals faked at the subprocess/lazy-import seam — no
    ``sysforge.update.*`` patching. The conversion target for the cmd_update
    integration tests (in test_update.py and the build-flag tests in
    test_env_state.py).
    """
    import sysforge.primitives.makepkg_wrapper as _mw

    src_root = tmp_path / "src"
    src_root.mkdir()
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    # Real config files steered through the genuine CLI seams (--profile-conf /
    # --packages) so the production load_config / _load_overrides run against
    # them. The frozen CONFIG_PATHS/PACKAGES_PATH constants (captured at import)
    # can't be redirected by env, so injecting explicit paths is the only
    # deterministic seam — and it's the real one a user drives. The profile is a
    # copy of the test default with pkgbuild_src_dir repointed at our temp src
    # root; packages.toml is empty (no overrides).
    import re as _re
    _test_data = Path(__file__).parent / "data"
    _profiles_src = (_test_data / "etc/sysforge/profiles.toml").read_text()
    _profiles_src = _re.sub(
        r'(?m)^\s*pkgbuild_src_dir\s*=.*$',
        f'pkgbuild_src_dir = "{src_root}"',
        _profiles_src,
    )
    profiles_path = cfg_dir / "profiles.toml"
    profiles_path.write_text(_profiles_src)
    packages_path = cfg_dir / "packages.toml"
    packages_path.write_text("")

    # The build is the true external these tests observe. build_core lazily
    # re-imports makepkg_wrapper.run at call time, so patching the module
    # attribute intercepts it; returning None means "no artifact" so no install.
    #
    # ``build_behaviors`` lets a test attach a per-pkgbase side effect to the
    # faked build (raise AlreadyBuilt, or "produce" artifacts on disk) so the
    # real install path in build_core runs against observable state instead of
    # patched-out internals. The pkgbase is recovered from the PKGBUILD's parent
    # dir name (the harness lays every package out as ``src_root/<pkgbase>``).
    builds: list = []
    build_behaviors: dict = {}

    def _fake_build(*a, **k):
        builds.append((a, k))
        pkgbuild_path = a[0] if a else k.get("pkgbuild_path")
        pkgbase = Path(pkgbuild_path).parent.name if pkgbuild_path else None
        behavior = build_behaviors.get(pkgbase)
        if behavior is not None:
            behavior()

    monkeypatch.setattr(_mw, "run", _fake_build)
    # vercmp is a pure, safe binary; let the real version comparison run so the
    # rebuild decision is genuinely exercised.
    fake_run.passthrough("vercmp")

    # Neutralize the host's real /etc/makepkg.conf so get_pkgdest() is None by
    # default (the build then searches each PKGBUILD's own dir). use_pkgdest()
    # overrides this to point PKGDEST at a temp dir. Without this, tests would
    # non-deterministically pick up the developer machine's actual PKGDEST.
    monkeypatch.setattr(
        "sysforge.primitives.config.parse_system_makepkg_conf", lambda: {})

    class _Scenario:
        def __init__(self):
            self.src_root = src_root
            self.state_dir = state_dir
            self.builds = builds
            self.fake_run = fake_run  # .commands exposes every emitted argv
            self.pkgdest = None
            self.exit_code: int | None = None  # set by run() (3.0.0-B4)
            self._build_cfg = {}
            self._overrides = []

        def add_pkg(self, pkgbase, body):
            d = src_root / pkgbase
            d.mkdir(parents=True, exist_ok=True)
            (d / "PKGBUILD").write_text(body)
            return d

        def record(self, pkgname, pkgver, pkgrel, *, epoch="0", pkgbase=None,
                   **kw):
            from sysforge.primitives.build_state import BuildState
            base = pkgbase or pkgname
            bs = BuildState(state_dir)
            bs.record(pkgname, pkgver, pkgrel, epoch, base,
                      src_root / base, build_mode="source_built", **kw)
            bs.save()

        def use_pkgdest(self):
            """Point the real get_pkgdest() at a temp PKGDEST dir.

            Fakes the genuine external (the system makepkg.conf) at
            ``parse_system_makepkg_conf`` rather than patching update's
            ``get_pkgdest`` binding, so the real PKGDEST resolution runs.
            """
            pd = tmp_path / "pkgdest"
            pd.mkdir(exist_ok=True)
            self.pkgdest = pd
            monkeypatch.setattr(
                "sysforge.primitives.config.parse_system_makepkg_conf",
                lambda: {"PKGDEST": str(pd)},
            )
            return pd

        def add_artifact(self, filename, pkgname, *, in_dir=None):
            """Place a pre-built ``.pkg.tar`` artifact and teach the install
            path to read its pkgname.

            ``read_pkgname_from_file`` shells ``bsdtar -xOqf <path> .PKGINFO``;
            registering a fake_run response keyed on the file path returns the
            embedded pkgname so ``filter_pkgs_to_installed`` resolves it without
            a real archive. Defaults to the active PKGDEST.
            """
            target = Path(in_dir) if in_dir else self.pkgdest
            assert target is not None, "call use_pkgdest() or pass in_dir="
            path = target / filename
            path.touch()
            fake_run.respond(["bsdtar", "-xOqf", str(path)],
                             stdout=f"pkgname = {pkgname}\n")
            return path

        def build_raises_already_built(self, pkgbase):
            """Make the faked build for ``pkgbase`` raise AlreadyBuilt, exercising
            build_core's existing-artifact recovery path."""
            from sysforge.primitives.makepkg_wrapper import AlreadyBuilt
            pkgbuild = src_root / pkgbase / "PKGBUILD"

            def _raise():
                raise AlreadyBuilt(pkgbuild)

            build_behaviors[pkgbase] = _raise

        def build_produces(self, pkgbase, artifacts, *, in_dir=None):
            """Make the faked build emit ``artifacts`` ({filename: pkgname}) on
            disk with a fresh mtime so snapshot_pkg_dir picks them up, and
            register their pkgname reads for the install filter."""
            target = Path(in_dir) if in_dir else (self.pkgdest or src_root / pkgbase)

            def _produce():
                import time
                time.sleep(0.01)  # ensure mtime >= build_start
                for fn in artifacts:
                    (target / fn).touch()

            for fn, pn in artifacts.items():
                fake_run.respond(["bsdtar", "-xOqf", str(target / fn)],
                                 stdout=f"pkgname = {pn}\n")
            build_behaviors[pkgbase] = _produce

        def _write_packages(self):
            """Serialize the accumulated [build] cfg + [[package]] overrides
            into the harness packages.toml (read by the real _load_overrides
            via the --packages CLI seam)."""
            lines = []
            if self._build_cfg:
                lines.append("[build]")
                for k, v in self._build_cfg.items():
                    if isinstance(v, bool):
                        lines.append(f"{k} = {str(v).lower()}")
                    else:
                        lines.append(f'{k} = "{v}"')
                lines.append("")
            for ov in self._overrides:
                lines.append("[[package]]")
                for k, v in ov.items():
                    if isinstance(v, bool):
                        lines.append(f"{k} = {str(v).lower()}")
                    else:
                        lines.append(f'{k} = "{v}"')
                lines.append("")
            packages_path.write_text("\n".join(lines))

        def set_repo_mode(self, mode):
            """Set ``[build] repo_mode`` (e.g. "build_from_source") in packages.toml."""
            self._build_cfg["repo_mode"] = mode
            self._write_packages()

        def set_build_key(self, key, value):
            """Set an arbitrary ``[build]`` key (e.g. ``system_upgrade``)."""
            self._build_cfg[key] = value
            self._write_packages()

        def add_override(self, name, **fields):
            """Add a ``[[package]]`` override entry to packages.toml."""
            self._overrides.append({"name": name, **fields})
            self._write_packages()

        def fake_checkupdates(self, updates):
            """Program the ``checkupdates`` repo-upgrade probe.

            ``updates`` is ``{pkgname: newver}`` (checkupdates ran and listed
            them) or ``None`` (binary errors → fast path unavailable). Drives
            the real checkupdates_map via fake_run.
            """
            if updates is None:
                fake_run.respond(["checkupdates"], returncode=127)
            else:
                out = "".join(f"{n} 0-0 -> {v}\n" for n, v in updates.items())
                fake_run.respond(["checkupdates"], stdout=out)

        def installed_pkg_files(self):
            """Filenames passed to the final ``pacman -U`` install transaction(s)."""
            calls = []
            for cmd in fake_run.commands:
                if "pacman -U" in cmd:
                    calls.append([
                        Path(tok).name for tok in cmd.split()
                        if ".pkg.tar" in tok
                    ])
            return calls

        def fake_vcs_pkgver(self, pkgname, version, arch="x86_64"):
            # evaluate_vcs_pkgver runs `makepkg -od ...` then
            # `makepkg --packagelist`, parsing the resolved version from the
            # printed package filename. Drive the real function via fake_run.
            fake_run.respond(["makepkg", "-od"], returncode=0)
            fake_run.respond(["makepkg", "--packagelist"],
                             stdout=f"{pkgname}-{version}-{arch}.pkg.tar.zst\n")

        def fake_sync(self, statuses=None):
            """Inject a fake source-sync scheduler (for offline=False runs).

            ``statuses`` maps pkgbase -> status string, or a (status, error)
            tuple; unlisted pkgbases resolve UP_TO_DATE. Injected at the
            source_sync singleton so update's get_scheduler() returns it.
            """
            from sysforge.primitives import source_sync
            from sysforge.primitives.source_sync import (
                STATUS_UP_TO_DATE, SyncResult,
            )
            table = statuses or {}

            class _FakeCache:
                def all(self):
                    return {}

            class _FakeScheduler:
                offline = cleansrc = cleansrc_force = force_devel = False
                cache = _FakeCache()

                def _ensure_rpc(self, bases):
                    pass

                def request(self, req):
                    spec = table.get(req.pkgbase, STATUS_UP_TO_DATE)
                    status, error = spec if isinstance(spec, tuple) else (spec, None)
                    return SyncResult(pkgbase=req.pkgbase, status=status, error=error)

                def close(self):
                    pass

            monkeypatch.setattr(source_sync, "_scheduler", _FakeScheduler())

        def run(self, args, *, installed, foreign=None):
            from sysforge.update import cmd_update
            foreign = foreign or {}
            # Steer the real config loaders at the harness's on-disk config
            # unless the test supplied its own paths.
            if not getattr(args, "profile_conf", None):
                args.profile_conf = str(profiles_path)
            if not getattr(args, "packages", None):
                args.packages = str(packages_path)
            args.no_llvm_preflight = getattr(args, "no_llvm_preflight", True)
            fake_run.respond(["pacman", "-Qm"],
                             stdout="".join(f"{n} {v}\n" for n, v in foreign.items()))
            fake_run.respond(["pacman", "-Q"],
                             stdout="".join(f"{n} {v}\n" for n, v in installed.items()))
            # 3.0.0-B4: the run's exit code is now part of its observable
            # behavior. `run()` keeps returning the recorded builds (every
            # existing caller reads those); the code lands on the scenario so
            # exit-code tests can assert it without a second harness.
            self.exit_code = cmd_update(args)
            return builds

    return _Scenario()
