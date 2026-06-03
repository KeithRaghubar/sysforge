"""
test_update.py — unit tests for sysforge.update

All subprocess calls (pacman -Q, git pull, makepkg) and filesystem access to
the state dir are mocked so no real system state is required.

Iteration model under test: the live install set (`pacman -Qm` + repo
packages selected by overrides). packages.toml entries are overrides only;
override entries with no installed counterpart are inert and silently
skipped (no NOT_INSTALLED action).
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.update import (
    _is_vcs, cmd_update,
    _check_one_pkgbase, _sync_sources, _assemble_package_set,
)
from sysforge.primitives.pacman import get_installed_version, get_foreign_packages
from sysforge.primitives.build_state import BuildState


# ---------------------------------------------------------------------------
# _is_vcs
# ---------------------------------------------------------------------------

def test_is_vcs_git():
    assert _is_vcs("neovim-git")

def test_is_vcs_svn():
    assert _is_vcs("foo-svn")

def test_is_vcs_hg():
    assert _is_vcs("bar-hg")

def test_is_vcs_bzr():
    assert _is_vcs("baz-bzr")

def test_is_vcs_false():
    assert not _is_vcs("htop")
    assert not _is_vcs("llvm")
    assert not _is_vcs("python-requests")


# ---------------------------------------------------------------------------
# get_installed_version
# ---------------------------------------------------------------------------

def _mock_pacman(stdout, returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_get_installed_version_found():
    with patch("subprocess.run", return_value=_mock_pacman("htop 3.3.0-1\n")):
        assert get_installed_version("htop") == "3.3.0-1"


def test_get_installed_version_not_installed():
    with patch("subprocess.run", return_value=_mock_pacman("", returncode=1)):
        assert get_installed_version("htop") is None


# ---------------------------------------------------------------------------
# cmd_update — helpers
# ---------------------------------------------------------------------------

def _make_args(**kwargs):
    defaults = dict(
        state_dir=None,
        dry_run=False,
        devel=False,
        offline=True,  # skip network in most tests
        no_pkg_log=True,
        persist_log=False,
        log_dir=None,
        profile_conf=None,
        cache_report=False,
        packages=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


import pytest  # noqa: E402


@pytest.fixture
def update_scenario(fake_run, state_dir, tmp_path, monkeypatch):
    """Drive the real ``cmd_update`` behavior-first.

    Provides a real ``BuildState`` (seedable via ``record``), an on-disk
    minimal config so the real ``load_config`` / ``_assemble_package_set``
    resolve PKGBUILDs under a temp source root, ``fake_run`` pacman, and the
    build + VCS-eval externals faked at the subprocess/lazy-import seam — no
    ``sysforge.update.*`` patching. The conversion target for the cmd_update
    integration tests.
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
            self._build_cfg = {}
            self._overrides = []

        def add_pkg(self, pkgbase, body):
            d = src_root / pkgbase
            d.mkdir(parents=True, exist_ok=True)
            (d / "PKGBUILD").write_text(body)
            return d

        def record(self, pkgname, pkgver, pkgrel, *, epoch="0", pkgbase=None,
                   **kw):
            base = pkgbase or pkgname
            bs = BuildState(state_dir)
            bs.record(pkgname, pkgver, pkgrel, epoch, base,
                      src_root / base, build_mode="profiled", **kw)
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
            """Set ``[build] repo_mode`` (e.g. "profiled") in packages.toml."""
            self._build_cfg["repo_mode"] = mode
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
            foreign = foreign or {}
            # Steer the real config loaders at the harness's on-disk config
            # unless the test supplied its own paths.
            if not getattr(args, "profile_conf", None):
                args.profile_conf = str(profiles_path)
            if not getattr(args, "packages", None):
                args.packages = str(packages_path)
            setattr(args, "no_llvm_preflight", getattr(args, "no_llvm_preflight", True))
            fake_run.respond(["pacman", "-Qm"],
                             stdout="".join(f"{n} {v}\n" for n, v in foreign.items()))
            fake_run.respond(["pacman", "-Q"],
                             stdout="".join(f"{n} {v}\n" for n, v in installed.items()))
            cmd_update(args)
            return builds

    return _Scenario()


# ---------------------------------------------------------------------------
# cmd_update — empty state
# ---------------------------------------------------------------------------

def test_empty_install_set_exits_cleanly(capsys):
    """No foreign packages and no repo-source overrides → nothing in scope."""
    with patch("sysforge.update.BuildState") as MockBS, \
         patch("sysforge.update.load_config", return_value={}), \
         patch("sysforge.update._load_overrides", return_value=({}, {})), \
         patch("sysforge.update.get_all_installed_packages", return_value={}), \
         patch("sysforge.update.get_foreign_packages", return_value={}):
        MockBS.return_value.all_packages.return_value = {}
        cmd_update(_make_args())
    captured = capsys.readouterr()
    assert "No installed packages in scope" in captured.err


# ---------------------------------------------------------------------------
# cmd_update — version checks
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Version decision — _check_one_pkgbase called directly with a real PKGBUILD
# on disk (real parse_pkgbuild + real vercmp; no module-global patching).
# ---------------------------------------------------------------------------

def _decide(tmp_path, pkgbase, installed, pkgbuild_body, **kw):
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "PKGBUILD").write_text(pkgbuild_body)
    return _check_one_pkgbase(
        pkgbase=pkgbase,
        pkgnames=[pkgbase],
        entry={"pkgbuild_dir": str(pkg_dir), "source": "aur"},
        sync_failures={},
        all_installed={pkgbase: installed},
        unrecorded_names=set(),
        skip_sync_check=True,
        rpc_version_by_base={},
        **kw,
    )


def test_check_needs_rebuild(tmp_path):
    r = _decide(tmp_path, "htop", "3.3.0-1", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    assert r.action == "NEEDS_REBUILD"
    assert r.installed_ver == "3.3.0-1"
    assert r.pkgbuild_ver == "3.4.1-1"


def test_check_up_to_date(tmp_path):
    r = _decide(tmp_path, "htop", "3.4.1-1", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    assert r.action == "UP_TO_DATE"


def test_check_pkgrel_bump_needs_rebuild(tmp_path):
    r = _decide(tmp_path, "htop", "3.4.1-1", "pkgname=htop\npkgver=3.4.1\npkgrel=2\n")
    assert r.action == "NEEDS_REBUILD"


def test_check_epoch_dominates(tmp_path):
    r = _decide(tmp_path, "htop", "9.9-1",
                "pkgname=htop\nepoch=1\npkgver=1.0\npkgrel=1\n")
    assert r.action == "NEEDS_REBUILD"
    assert r.pkgbuild_ver == "1:1.0-1"


def test_check_downgrade_flagged(tmp_path):
    r = _decide(tmp_path, "htop", "3.4.1-1", "pkgname=htop\npkgver=3.3.0\npkgrel=1\n")
    assert r.action == "DOWNGRADE"


# ---------------------------------------------------------------------------
# _check_one_pkgbase — RPC version fallback for unresolvable bash expansions
# ---------------------------------------------------------------------------

def _write_pkgbuild(dir_: Path, body: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / "PKGBUILD"
    p.write_text(body)
    return p


def test_unresolved_pkgver_uses_cached_rpc_version(tmp_path):
    """PKGBUILD with bash parameter expansion falls back to cached RPC version."""
    pkgbase = "1password"
    pkg_dir = tmp_path / pkgbase
    _write_pkgbuild(
        pkg_dir,
        '_tarver=8.12.10-36\npkgname=1password\npkgver=${_tarver//-/_}\npkgrel=36\n',
    )
    entry = {"pkgbuild_dir": str(pkg_dir)}

    result = _check_one_pkgbase(
        pkgbase=pkgbase,
        pkgnames=[pkgbase],
        entry=entry,
        sync_failures={},
        all_installed={pkgbase: "8.12.10-36"},
        unrecorded_names=set(),
        skip_sync_check=False,
        rpc_version_by_base={pkgbase: "8.12.10-36"},
    )
    assert result is not None
    assert result.action == "UP_TO_DATE"
    assert result.pkgbuild_ver == "8.12.10-36"


def test_unresolved_pkgver_without_cache_is_skipped(tmp_path):
    """No cached RPC version → skip rather than compare gibberish."""
    pkgbase = "openssl-1.0"
    pkg_dir = tmp_path / pkgbase
    _write_pkgbuild(
        pkg_dir,
        '_ver=1.0.2u\npkgname=openssl-1.0\npkgver=${_ver/[a-z]/.${_ver//[0-9.]/}}\npkgrel=7\n',
    )
    entry = {"pkgbuild_dir": str(pkg_dir)}

    result = _check_one_pkgbase(
        pkgbase=pkgbase,
        pkgnames=[pkgbase],
        entry=entry,
        sync_failures={},
        all_installed={pkgbase: "1.0.2.u-7"},
        unrecorded_names=set(),
        skip_sync_check=False,
        rpc_version_by_base={},
    )
    assert result is None


# ---------------------------------------------------------------------------
# Live-install-set iteration: override entries for uninstalled packages are
# silently ignored (no NOT_INSTALLED action under the new model).
# ---------------------------------------------------------------------------

def test_uninstalled_override_is_silently_skipped(fake_run, state_dir):
    """An override entry for a package that isn't installed is inert — not
    pulled into scope (so no NOT_INSTALLED action, no source sync)."""
    # mesa (repo) is installed; mesa-git (override target) is not.
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout="mesa 1:25.3.1-1\n")
    overrides = {"mesa-git": {"name": "mesa-git", "source": "aur"}}
    packages, _ = _assemble_package_set(
        _make_args(), BuildState(state_dir), {}, {}, overrides,
    )
    assert packages == {}
    assert "mesa-git" not in packages


def test_installed_aur_without_override_uses_defaults(tmp_path):
    """AUR package installed but with no override entry → walked with defaults."""
    pkgbase = "yay"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(f"pkgname={pkgbase}\npkgver=12.3.3\npkgrel=1\n")

    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "12.3.3", "pkgrel": "1", "epoch": "0"}}

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),  # no overrides
        patch("sysforge.update.get_all_installed_packages",
              return_value={pkgbase: "12.3.3-1"}),
        patch("sysforge.update.get_foreign_packages",
              return_value={pkgbase: "12.3.3-1"}),
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = {}

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(_make_args())

    assert {r.pkgbase for r in results} == {pkgbase}


def test_foreign_split_package_resolves_pkgbase_from_local_db(tmp_path):
    """Foreign split-package subnames (e.g. linux-custom-headers) collapse to
    their parent pkgbase via pacman's local DB %BASE%, even when not in AUR.
    AUR RPC must NOT be called when the local DB already resolves the base."""
    pkgbase = "linux-custom"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(
        f"pkgbase={pkgbase}\n"
        f"pkgname=({pkgbase} {pkgbase}-headers)\n"
        f"pkgver=6.19.12.arch1\npkgrel=1\n"
    )

    parsed = {"globals": {
        "pkgbase": pkgbase,
        "pkgname": [pkgbase, f"{pkgbase}-headers"],
        "pkgver": "6.19.12.arch1", "pkgrel": "1", "epoch": "0",
    }}

    foreign = {pkgbase: "6.19.12.arch1-1", f"{pkgbase}-headers": "6.19.12.arch1-1"}

    def fake_get_pkgbase(name, root=None):
        if name in foreign:
            return pkgbase
        return None

    results = []
    with (
        # Isolate from the workstation's real kernel.toml (which names
        # linux-custom and would route it through the stage-owned skip).
        patch("sysforge.update.KERNEL_PATH", tmp_path / "no-kernel-toml"),
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
        patch("sysforge.update.get_pkgbase", side_effect=fake_get_pkgbase),
        patch("sysforge.update.aur_info") as mock_aur_info,
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = {}

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(_make_args())

    # Both subpackages collapse into the single linux-custom pkgbase group.
    assert {r.pkgbase for r in results} == {pkgbase}
    # AUR RPC must not be called — local DB already supplied %BASE%.
    mock_aur_info.assert_not_called()


def test_repo_package_without_override_is_not_iterated(fake_run, state_dir):
    """A repo (non-foreign) package with no override → out of scope."""
    # mesa is an installed repo package; no foreign packages.
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout="mesa 1:25.3.1-1\n")
    packages, _ = _assemble_package_set(
        _make_args(), BuildState(state_dir), {}, {}, {},
    )
    assert packages == {}


def test_repo_package_with_override_is_iterated(fake_run, state_dir):
    """A repo package WITH a behavior-changing override → in scope.

    The override only takes effect because it sets ``cache = False`` (a
    behavior-changing field). A bare ``source = "repo"`` entry by itself is
    inert metadata — see ``test_bare_source_only_override_is_inert``.
    """
    pkgbase = "llvm"
    # llvm is an installed repo package (not foreign); the cache=False override
    # pulls it into scope. include_stage_owned bypasses the toolchain
    # stage-owned skip without neutralising the workstation's toolchain.toml.
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout=f"{pkgbase} 20.1.0-1\n")
    overrides = {pkgbase: {"name": pkgbase, "source": "repo", "cache": False}}
    packages, _ = _assemble_package_set(
        _make_args(include_stage_owned=True), BuildState(state_dir), {}, {}, overrides,
    )
    assert set(packages) == {pkgbase}


def test_repo_mode_profiled_walks_installed_repo_packages(fake_run, state_dir):
    """With ``[build] repo_mode = "profiled"``, every installed repo package is
    iterated alongside foreign packages — no per-package override needed."""
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout="firefox 131.0-1\n")
    packages, _ = _assemble_package_set(
        _make_args(), BuildState(state_dir), {}, {"repo_mode": "profiled"}, {},
    )
    assert set(packages) == {"firefox"}


def test_repo_mode_pacman_skips_repo_packages(fake_run, state_dir):
    """Default (repo_mode "pacman"): a repo package without a behavior-changing
    override stays out of scope. Confirms the gate is load-bearing."""
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout="firefox 131.0-1\n")
    packages, _ = _assemble_package_set(
        _make_args(), BuildState(state_dir), {}, {"repo_mode": "pacman"}, {},
    )
    assert packages == {}


def test_bare_source_only_override_is_inert(fake_run, state_dir):
    """
    Regression: a `[[package]]` entry with only `name` + `source = "repo"`
    is inert metadata, not a trigger. The pipewire-style entry that
    surfaced this bug must not pull the package into update scope.
    """
    # Inert override on a repo package, no behavior-changing field set.
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout="pipewire 1:1.6.5-1\n")
    overrides = {"pipewire": {"name": "pipewire", "source": "repo"}}
    packages, _ = _assemble_package_set(
        _make_args(), BuildState(state_dir), {}, {}, overrides,
    )
    assert packages == {}


def test_update_repo_profiled_alias_is_normalised(fake_run, state_dir):
    """The consumer side of the legacy ``update_repo_profiled`` alias: once
    ``_load_overrides`` has normalised it to ``repo_mode = "profiled"`` (the
    normalisation itself is asserted in
    ``test_load_overrides_normalises_deprecated_update_repo_profiled``), the
    package set walks repo packages exactly like the canonical key."""
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout="firefox 131.0-1\n")
    packages, _ = _assemble_package_set(
        _make_args(), BuildState(state_dir), {}, {"repo_mode": "profiled"}, {},
    )
    assert set(packages) == {"firefox"}


def test_load_overrides_warns_on_inert_entries(tmp_path, capsys):
    """
    `_load_overrides` emits a warn line for any inert `[[package]]` entry
    (no behavior-changing field). Hand-edited files accumulate these; the
    warning prompts cleanup.
    """
    from sysforge.update import _load_overrides
    p = tmp_path / "packages.toml"
    p.write_text(
        '[[package]]\nname = "pipewire"\nsource = "repo"\n'
        '[[package]]\nname = "llvm"\nsource = "repo"\ncache = false\n'
    )
    _, overrides = _load_overrides(p)
    assert set(overrides) == {"pipewire", "llvm"}
    err = capsys.readouterr().err
    assert "pipewire" in err and "inert" in err
    # llvm has a behavior-changing field (cache=false); no warning for it.
    assert "llvm" not in err or "inert" not in err.split("llvm", 1)[1]


def test_load_overrides_normalises_deprecated_update_repo_profiled(tmp_path, capsys):
    """
    `[build] update_repo_profiled = true` is normalised to
    `repo_mode = "profiled"` with a one-shot deprecation warning.
    """
    from sysforge.update import _load_overrides
    p = tmp_path / "packages.toml"
    p.write_text(
        '[build]\nupdate_repo_profiled = true\n'
    )
    build_cfg, _ = _load_overrides(p)
    assert build_cfg.get("repo_mode") == "profiled"
    assert "update_repo_profiled" not in build_cfg
    err = capsys.readouterr().err
    assert "update_repo_profiled" in err and "deprecated" in err


# ---------------------------------------------------------------------------
# DEVEL / dry-run / no-devel
# ---------------------------------------------------------------------------

def test_vcs_installed_is_devel(tmp_path):
    """Installed VCS package without --devel → DEVEL (rebuildable with --devel)."""
    r = _decide(tmp_path, "neovim-git", "r1234.gabcdef-1",
                "pkgname=neovim-git\npkgver=r1234.gabcdef\npkgrel=1\n")
    assert r.action == "DEVEL"


def test_dry_run_no_build(update_scenario):
    # htop installed at 3.3.0-1, PKGBUILD at 3.4.1 -> NEEDS_REBUILD, but
    # --dry-run must not invoke the build.
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    builds = update_scenario.run(
        _make_args(dry_run=True),
        installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
    )
    assert builds == []


def test_devel_flag_triggers_vcs_rebuild(update_scenario):
    """--devel + resolved pkgver newer than installed → build runs once."""
    update_scenario.add_pkg(
        "neovim-git", "pkgname=neovim-git\npkgver=r1234.gabcdef\npkgrel=1\n")
    update_scenario.fake_vcs_pkgver("neovim-git", "r5678.g9999999-1")
    builds = update_scenario.run(
        _make_args(devel=True),
        installed={"neovim-git": "r1234.gabcdef-1"},
        foreign={"neovim-git": "r1234.gabcdef-1"},
    )
    assert len(builds) == 1


def test_devel_skips_uptodate_vcs(update_scenario):
    """--devel + resolved pkgver equal to installed → build does NOT run."""
    update_scenario.add_pkg(
        "neovim-git", "pkgname=neovim-git\npkgver=r1234.gabcdef\npkgrel=1\n")
    update_scenario.fake_vcs_pkgver("neovim-git", "r1234.gabcdef-1")
    builds = update_scenario.run(
        _make_args(devel=True),
        installed={"neovim-git": "r1234.gabcdef-1"},
        foreign={"neovim-git": "r1234.gabcdef-1"},
    )
    assert builds == []


def test_devel_short_circuits_when_upstream_unmoved(update_scenario):
    """--devel + cached SHA matches the source's commit= pin → the expensive
    evaluate_vcs_pkgver (makepkg) pass is skipped and nothing rebuilds.

    The PKGBUILD pins ``#commit=<sha>``, which peek_upstream_commit resolves
    in-process (no ls-remote). When it matches the recorded
    built_upstream_commit, _check_one_pkgbase returns UP_TO_DATE directly."""
    sha = "f00dbabe" + "0" * 32  # 40 hex chars
    update_scenario.add_pkg(
        "neovim-git",
        "pkgname=neovim-git\npkgver=r1234.gabcdef\npkgrel=1\n"
        f'source=("neovim::git+https://example.invalid/n.git#commit={sha}")\n',
    )
    update_scenario.record("neovim-git", "r1234.gabcdef", "1",
                           built_upstream_commit=sha)
    builds = update_scenario.run(
        _make_args(devel=True),
        installed={"neovim-git": "r1234.gabcdef-1"},
        foreign={"neovim-git": "r1234.gabcdef-1"},
    )
    assert builds == []
    # The cache hit must skip the makepkg-based evaluate_vcs_pkgver pass.
    assert not any("--packagelist" in c for c in update_scenario.fake_run.commands)


def test_devel_full_resolve_on_lsremote_miss(update_scenario):
    """--devel + cached SHA differs from the source's commit= pin → the peek
    short-circuit misses, so the full evaluate_vcs_pkgver pass runs and a newer
    resolved pkgver triggers one build."""
    cached_sha = "a" * 40
    new_sha = "b" * 40
    update_scenario.add_pkg(
        "neovim-git",
        "pkgname=neovim-git\npkgver=r1234.gabcdef\npkgrel=1\n"
        f'source=("neovim::git+https://example.invalid/n.git#commit={new_sha}")\n',
    )
    update_scenario.record("neovim-git", "r1234.gabcdef", "1",
                           built_upstream_commit=cached_sha)
    update_scenario.fake_vcs_pkgver("neovim-git", "r5678.gfedcba0-1")  # newer
    builds = update_scenario.run(
        _make_args(devel=True),
        installed={"neovim-git": "r1234.gabcdef-1"},
        foreign={"neovim-git": "r1234.gabcdef-1"},
    )
    assert len(builds) == 1
    assert any("--packagelist" in c for c in update_scenario.fake_run.commands)


def test_devel_full_resolve_when_no_cached_commit(update_scenario):
    """--devel + no recorded built_upstream_commit → no peek, evaluate runs."""
    update_scenario.add_pkg(
        "neovim-git", "pkgname=neovim-git\npkgver=r1234.gabcdef\npkgrel=1\n")
    update_scenario.fake_vcs_pkgver("neovim-git", "r1234.gabcdef-1")  # == installed
    builds = update_scenario.run(
        _make_args(devel=True),
        installed={"neovim-git": "r1234.gabcdef-1"},
        foreign={"neovim-git": "r1234.gabcdef-1"},
    )
    assert builds == []  # resolved == installed
    # With no cached SHA, the full makepkg-based resolve must run.
    assert any("--packagelist" in c for c in update_scenario.fake_run.commands)


def test_devel_skips_when_pkgver_eval_fails(update_scenario, capsys):
    """--devel + pkgver() resolution returns None → skip with WARN, no build.

    With no fake_vcs_pkgver programmed, the real evaluate_vcs_pkgver runs but
    `makepkg --packagelist` yields nothing, so it returns None."""
    update_scenario.add_pkg(
        "neovim-git", "pkgname=neovim-git\npkgver=r1234.gabcdef\npkgrel=1\n")
    builds = update_scenario.run(
        _make_args(devel=True),
        installed={"neovim-git": "r1234.gabcdef-1"},
        foreign={"neovim-git": "r1234.gabcdef-1"},
    )
    assert builds == []
    combined = "".join(capsys.readouterr())
    assert "DEVEL_EVAL_FAILED" in combined or "pkgver() evaluation failed" in combined


def test_no_devel_skips_vcs_build(update_scenario):
    """Without --devel an installed VCS package is DEVEL-classified only — it
    is never rebuilt."""
    update_scenario.add_pkg(
        "neovim-git", "pkgname=neovim-git\npkgver=r1234.gabcdef\npkgrel=1\n")
    builds = update_scenario.run(
        _make_args(devel=False),
        installed={"neovim-git": "r1234.gabcdef-1"},
        foreign={"neovim-git": "r1234.gabcdef-1"},
    )
    assert builds == []


def test_check_one_pkgbase_vcs_no_devel_skips_parse(tmp_path):
    """Without --devel, _check_one_pkgbase returns DEVEL via the VCS fast-path,
    before the pkgbuild_dir probe or PKGBUILD parse. The dir intentionally does
    not exist — reaching the probe would return None, not DEVEL, so a DEVEL
    result proves neither the probe nor the parse ran.
    """
    result = _check_one_pkgbase(
        pkgbase="neovim-git",
        pkgnames=["neovim-git"],
        entry={"pkgbuild_dir": str(tmp_path / "does-not-exist" / "neovim-git")},
        sync_failures={},
        all_installed={"neovim-git": "r1234.gabcdef-1"},
        unrecorded_names=set(),
        skip_sync_check=False,
        rpc_version_by_base={},
        force_devel=False,
    )
    assert result.action == "DEVEL"
    assert result.installed_ver == "r1234.gabcdef-1"
    assert result.pkgbuild_ver is None
    assert result.pkgbuild_path is None


def test_sync_sources_skips_vcs_without_devel(tmp_path, monkeypatch):
    """``_sync_sources`` omits ``-git`` pkgbases when ``--devel`` is off, even
    under ``--cleansrc`` — purge_src/aur_clone must never see those dirs.
    """
    from sysforge.primitives import source_sync
    from sysforge.primitives.source_sync import STATUS_UP_TO_DATE, SyncResult

    for name in ("htop", "mesa-git"):
        d = tmp_path / name
        d.mkdir()
        (d / "PKGBUILD").write_text(f"pkgname={name}\n")

    pkgbase_map = {"htop": ["htop"], "mesa-git": ["mesa-git"]}
    pkgbase_entry = {
        "htop": {"pkgbuild_dir": str(tmp_path / "htop"), "source": "aur"},
        "mesa-git": {"pkgbuild_dir": str(tmp_path / "mesa-git"), "source": "aur"},
    }

    seen: list[str] = []

    class _FakeScheduler:
        offline = cleansrc = cleansrc_force = force_devel = False
        cache = MagicMock()
        def _ensure_rpc(self, bases):  # noqa: ARG002
            pass
        def request(self, req):
            seen.append(req.pkgbase)
            return SyncResult(pkgbase=req.pkgbase, status=STATUS_UP_TO_DATE)
        def close(self):
            pass

    # Inject at the source_sync singleton so update's bound get_scheduler()
    # returns the fake without patching sysforge.update.*.
    monkeypatch.setattr(source_sync, "_scheduler", _FakeScheduler())
    args = _make_args(offline=False, cleansrc=True, cleansrc_force=False,
                      devel=False, state_dir=str(tmp_path))
    failures = _sync_sources(pkgbase_map, pkgbase_entry, args)

    assert seen == ["htop"]
    assert failures == {}


def test_sync_sources_includes_vcs_under_devel(tmp_path, monkeypatch):
    """With ``--devel`` the VCS filter is bypassed — both pkgbases are synced."""
    from sysforge.primitives import source_sync
    from sysforge.primitives.source_sync import STATUS_UP_TO_DATE, SyncResult

    for name in ("htop", "mesa-git"):
        d = tmp_path / name
        d.mkdir()
        (d / "PKGBUILD").write_text(f"pkgname={name}\n")

    pkgbase_map = {"htop": ["htop"], "mesa-git": ["mesa-git"]}
    pkgbase_entry = {
        "htop": {"pkgbuild_dir": str(tmp_path / "htop"), "source": "aur"},
        "mesa-git": {"pkgbuild_dir": str(tmp_path / "mesa-git"), "source": "aur"},
    }

    seen: list[str] = []

    class _FakeScheduler:
        offline = cleansrc = cleansrc_force = force_devel = False
        cache = MagicMock()
        def _ensure_rpc(self, bases):  # noqa: ARG002
            pass
        def request(self, req):
            seen.append(req.pkgbase)
            return SyncResult(pkgbase=req.pkgbase, status=STATUS_UP_TO_DATE)
        def close(self):
            pass

    monkeypatch.setattr(source_sync, "_scheduler", _FakeScheduler())
    args = _make_args(offline=False, cleansrc=False, cleansrc_force=False,
                      devel=True, state_dir=str(tmp_path))
    _sync_sources(pkgbase_map, pkgbase_entry, args)

    assert sorted(seen) == ["htop", "mesa-git"]


def test_pull_failure_continues_to_next_package(update_scenario, capsys):
    """A source-sync failure for one pkgbase doesn't block the rest: htop's
    sync failure surfaces (PULL_FAILED), while neovim is still version-checked
    and found up to date."""
    from sysforge.primitives.source_sync import STATUS_FAILED
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=0.9.0\npkgrel=1\n")
    update_scenario.add_pkg("neovim", "pkgname=neovim\npkgver=0.9.0\npkgrel=1\n")
    update_scenario.fake_sync({"htop": (STATUS_FAILED, "git fetch failed")})
    update_scenario.run(
        _make_args(offline=False),
        installed={"htop": "0.9.0-1", "neovim": "0.9.0-1"},
        foreign={"htop": "0.9.0-1", "neovim": "0.9.0-1"},
    )
    combined = "".join(capsys.readouterr())
    # htop's sync failure surfaces (PULL_FAILED) ...
    assert "git fetch failed" in combined
    assert "1 pull failed" in combined
    # ... and the run is NOT aborted by it: neovim is still version-checked
    # (counted up to date) and the run reaches a clean finish. Both packages
    # are at the installed version, so there is nothing to rebuild.
    assert "1 up to date" in combined
    assert "Nothing to rebuild" in combined


# ---------------------------------------------------------------------------
# get_foreign_packages
# ---------------------------------------------------------------------------

def _mock_pacman_qm(stdout, returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_get_foreign_packages_returns_dict():
    output = "yay 12.3.3-1\nneovim-git r1234.gabcdef-1\n"
    with patch("subprocess.run", return_value=_mock_pacman_qm(output)):
        result = get_foreign_packages()
    assert result == {"yay": "12.3.3-1", "neovim-git": "r1234.gabcdef-1"}


def test_get_foreign_packages_empty_on_failure():
    with patch("subprocess.run", return_value=_mock_pacman_qm("", returncode=1)):
        result = get_foreign_packages()
    assert result == {}


def test_get_foreign_packages_empty_output():
    with patch("subprocess.run", return_value=_mock_pacman_qm("")):
        result = get_foreign_packages()
    assert result == {}


# ---------------------------------------------------------------------------
# Split-pkgbase install filter: only install pkgnames already on the system.
# Regression — pipewire-full-git (16 split pkgnames) was rebuilding all
# sub-packages when only 2 were installed, silently adding 14 new packages.
# ---------------------------------------------------------------------------

def test_already_built_installs_existing_artifact(update_scenario):
    """makepkg AlreadyBuilt → locate existing .pkg.tar and install, not fail.

    Regression: makepkg's "A package has already been built" was treated as a
    build failure even though PKGDEST already held the right artifact.
    """
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.use_pkgdest()
    update_scenario.add_artifact("htop-3.4.1-1-x86_64.pkg.tar.zst", "htop")
    update_scenario.build_raises_already_built("htop")

    update_scenario.run(
        _make_args(),
        installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
    )

    assert update_scenario.installed_pkg_files() == [
        ["htop-3.4.1-1-x86_64.pkg.tar.zst"]]


def test_split_pkgbase_only_installs_installed_subpkgnames(update_scenario):
    """A split-pkgbase build emits a .pkg.tar per sub-package, but only the
    sub-packages the user already has installed get queued for install."""
    update_scenario.add_pkg(
        "pipewire-full-git",
        "pkgname=pipewire-full-git\npkgver=1.0\npkgrel=1\n",
    )
    # --devel resolves a newer VCS version, so the pkgbase rebuilds.
    update_scenario.fake_vcs_pkgver("pipewire-full-git", "1.0.r1.gffffff-1")
    update_scenario.use_pkgdest()
    # Both installed sub-packages are recorded under the shared pkgbase.
    update_scenario.record("pipewire-full-ffmpeg-git", "1.0", "1",
                           pkgbase="pipewire-full-git")
    update_scenario.record("pipewire-full-vulkan-git", "1.0", "1",
                           pkgbase="pipewire-full-git")
    # The build emits all four split artifacts into PKGDEST.
    update_scenario.build_produces("pipewire-full-git", {
        "pipewire-full-git-1.0-1-x86_64.pkg.tar.zst": "pipewire-full-git",
        "pipewire-full-ffmpeg-git-1.0-1-x86_64.pkg.tar.zst": "pipewire-full-ffmpeg-git",
        "pipewire-full-vulkan-git-1.0-1-x86_64.pkg.tar.zst": "pipewire-full-vulkan-git",
        "libpipewire-full-git-1.0-1-x86_64.pkg.tar.zst": "libpipewire-full-git",
    })

    installed = {
        "pipewire-full-ffmpeg-git": "1.0-1",
        "pipewire-full-vulkan-git": "1.0-1",
    }
    update_scenario.run(
        _make_args(devel=True), installed=installed, foreign=installed,
    )

    calls = update_scenario.installed_pkg_files()
    assert len(calls) == 1
    installed_names = calls[0]
    assert set(installed_names) == {
        "pipewire-full-ffmpeg-git-1.0-1-x86_64.pkg.tar.zst",
        "pipewire-full-vulkan-git-1.0-1-x86_64.pkg.tar.zst",
    }
    # Crucially, the un-installed split sub-packages must NOT be installed.
    assert "libpipewire-full-git-1.0-1-x86_64.pkg.tar.zst" not in installed_names
    assert "pipewire-full-git-1.0-1-x86_64.pkg.tar.zst" not in installed_names


# ---------------------------------------------------------------------------
# --install-only: install pre-built artifacts without re-running makepkg.
# ---------------------------------------------------------------------------

def test_install_only_installs_existing_artifact_without_building(update_scenario):
    """--install-only: locate the artifact in PKGDEST and install it; never build."""
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.use_pkgdest()
    update_scenario.add_artifact("htop-3.4.1-1-x86_64.pkg.tar.zst", "htop")

    update_scenario.run(
        _make_args(install_only=True),
        installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
    )

    # --install-only must never invoke the build seam.
    assert update_scenario.builds == []
    assert update_scenario.installed_pkg_files() == [
        ["htop-3.4.1-1-x86_64.pkg.tar.zst"]]


def test_install_only_skips_when_artifact_missing(update_scenario):
    """--install-only: PKGBUILD newer than installed but no matching artifact in
    PKGDEST → skip, no install."""
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.use_pkgdest()
    # Only an older artifact exists; nothing matches the 3.4.1 build.
    update_scenario.add_artifact("htop-3.3.0-1-x86_64.pkg.tar.zst", "htop")

    update_scenario.run(
        _make_args(install_only=True),
        installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
    )

    assert update_scenario.builds == []
    # Nothing eligible → no install transaction at all.
    assert update_scenario.installed_pkg_files() == []


def test_install_only_rejects_incompatible_flags():
    """--install-only with build-tuning flags must short-circuit pre_check with a blocker."""
    from sysforge.update import UpdateVerb

    args = _make_args(install_only=True, no_cleanbuild=True)
    pre = UpdateVerb().pre_check(args)
    assert pre.blocker is not None
    assert "--install-only is incompatible with" in pre.blocker
    assert "--no-cleanbuild" in pre.blocker
    assert pre.exit_code == 1


# ---------------------------------------------------------------------------
# VCS fallback: pkgver() bumps the version dynamically, so the static
# pkgbuild_ver never matches the actual artifact filename. Both the
# AlreadyBuilt path and --install-only must fall back to a pkgname-only
# lookup and pick the newest by vercmp.
# ---------------------------------------------------------------------------

def test_already_built_vcs_falls_back_to_newest_pkgname_match(update_scenario):
    """VCS package: AlreadyBuilt → static pkgbuild_ver doesn't match the
    bumped filename (0.1.0-1 vs 0.1.0.r45.g1234567-1); helper must fall
    back to a pkgname-only glob and queue the newest artifact for install.
    """
    update_scenario.add_pkg(
        "neovim-git", "pkgname=neovim-git\npkgver=0.1.0\npkgrel=1\n")
    update_scenario.fake_vcs_pkgver("neovim-git", "0.1.0.r45.g1234567-1")
    update_scenario.use_pkgdest()
    update_scenario.add_artifact(
        "neovim-git-0.1.0.r45.g1234567-1-x86_64.pkg.tar.zst", "neovim-git")
    update_scenario.build_raises_already_built("neovim-git")

    installed = {"neovim-git": "0.1.0.r10.gaaaaaaa-1"}
    update_scenario.run(
        _make_args(devel=True), installed=installed, foreign=installed,
    )

    assert update_scenario.installed_pkg_files() == [
        ["neovim-git-0.1.0.r45.g1234567-1-x86_64.pkg.tar.zst"]]


def test_install_only_vcs_picks_newest_artifact_in_pkgdest(update_scenario):
    """--install-only on a VCS package: static pkgbuild_ver mismatches the
    artifact filename; the helper must fall back to a pkgname-only glob
    and select the newest by vercmp, while excluding artifacts not strictly
    newer than installed.
    """
    update_scenario.add_pkg(
        "neovim-git", "pkgname=neovim-git\npkgver=0.1.0\npkgrel=1\n")
    update_scenario.fake_vcs_pkgver("neovim-git", "0.1.0.r45.g1234567-1")
    update_scenario.use_pkgdest()
    # Two artifacts: an older one (== installed, skip) and a newer target.
    update_scenario.add_artifact(
        "neovim-git-0.1.0.r10.gaaaaaaa-1-x86_64.pkg.tar.zst", "neovim-git")
    update_scenario.add_artifact(
        "neovim-git-0.1.0.r45.g1234567-1-x86_64.pkg.tar.zst", "neovim-git")

    installed = {"neovim-git": "0.1.0.r10.gaaaaaaa-1"}
    update_scenario.run(
        _make_args(install_only=True, devel=True),
        installed=installed, foreign=installed,
    )

    assert update_scenario.builds == []
    assert update_scenario.installed_pkg_files() == [
        ["neovim-git-0.1.0.r45.g1234567-1-x86_64.pkg.tar.zst"]]


def test_install_only_vcs_skips_when_only_older_artifacts_present(update_scenario):
    """--install-only on a VCS package: only an artifact == installed exists,
    so the helper's installed_ver guard rejects it and nothing installs."""
    update_scenario.add_pkg(
        "neovim-git", "pkgname=neovim-git\npkgver=0.1.0\npkgrel=1\n")
    update_scenario.fake_vcs_pkgver("neovim-git", "0.1.0.r45.g1234567-1")
    update_scenario.use_pkgdest()
    # Only an artifact at the same version as installed → not strictly newer.
    update_scenario.add_artifact(
        "neovim-git-0.1.0.r10.gaaaaaaa-1-x86_64.pkg.tar.zst", "neovim-git")

    installed = {"neovim-git": "0.1.0.r10.gaaaaaaa-1"}
    update_scenario.run(
        _make_args(install_only=True, devel=True),
        installed=installed, foreign=installed,
    )

    assert update_scenario.builds == []
    assert update_scenario.installed_pkg_files() == []


# ---------------------------------------------------------------------------
# Sync-status → action dispatch (verbose skip messaging)
# ---------------------------------------------------------------------------

def _check_pkgbase_with_sync_status(tmp_path, status):
    """Run _check_one_pkgbase with a sync_failures entry carrying `status`."""
    pkgbase = "htop"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=htop\npkgver=1\npkgrel=1\n")

    return _check_one_pkgbase(
        pkgbase=pkgbase,
        pkgnames=[pkgbase],
        entry={"pkgbuild_dir": str(pkg_dir)},
        sync_failures={pkgbase: (status, "synthetic error")},
        all_installed={pkgbase: "1-1"},
        unrecorded_names=set(),
        skip_sync_check=False,
        rpc_version_by_base={},
    )


def test_sync_status_failed_maps_to_pull_failed(tmp_path):
    result = _check_pkgbase_with_sync_status(tmp_path, "failed")
    assert result is not None
    assert result.action == "PULL_FAILED"


def test_sync_status_rate_limited_maps_to_rate_limited(tmp_path):
    result = _check_pkgbase_with_sync_status(tmp_path, "rate_limited")
    assert result is not None
    assert result.action == "RATE_LIMITED"


def test_sync_status_purge_refused_maps_to_purge_refused(tmp_path):
    result = _check_pkgbase_with_sync_status(tmp_path, "purge_refused")
    assert result is not None
    assert result.action == "PURGE_REFUSED"


def test_sync_status_unknown_falls_back_to_pull_failed(tmp_path):
    """Defensive: an unmapped sync status still produces a recognizable action."""
    result = _check_pkgbase_with_sync_status(tmp_path, "some_future_status")
    assert result is not None
    assert result.action == "PULL_FAILED"


# ---------------------------------------------------------------------------
# _print_summary verbose-vs-default behavior
# ---------------------------------------------------------------------------

def _make_summary_results():
    from sysforge.update import _UpdateResult
    from pathlib import Path
    return [
        _UpdateResult("htop", ["htop"], "NEEDS_REBUILD", "3.3.0-1", "3.4.1-1",
                      Path("/tmp/htop/PKGBUILD")),
        _UpdateResult("neovim", ["neovim"], "UP_TO_DATE", "0.9.5-1", "0.9.5-1",
                      Path("/tmp/neovim/PKGBUILD")),
        _UpdateResult("foo-git", ["foo-git"], "DEVEL", "r10.abc-1", None,
                      Path("/tmp/foo-git/PKGBUILD")),
        _UpdateResult("bar", ["bar"], "RATE_LIMITED", None, None,
                      Path("/tmp/bar/PKGBUILD")),
    ]


def test_print_summary_default_hides_skip_lines(capsys):
    from sysforge.update import _print_summary
    args = SimpleNamespace(verbose=0)
    _print_summary(_make_summary_results(), args)
    captured = capsys.readouterr().out
    # NEEDS_REBUILD always shown (actionable)
    assert "[NEEDS_REBUILD]" in captured
    assert "htop" in captured
    # UP_TO_DATE / DEVEL / RATE_LIMITED hidden at default verbosity
    assert "[UP_TO_DATE]" not in captured
    assert "[DEVEL]" not in captured
    assert "[RATE_LIMITED]" not in captured
    # Header counts still mention every category
    assert "1 up to date" in captured
    assert "1 devel" in captured
    assert "1 rate-limited" in captured
    # Hint to the user about -v
    assert "-v" in captured


def test_print_summary_verbose_shows_all_lines(capsys):
    from sysforge.update import _print_summary
    args = SimpleNamespace(verbose=1)
    _print_summary(_make_summary_results(), args)
    captured = capsys.readouterr().out
    assert "[NEEDS_REBUILD]" in captured
    assert "[UP_TO_DATE]" in captured
    assert "[DEVEL]" in captured
    assert "[RATE_LIMITED]" in captured
    # No -v hint when already verbose
    assert "run with -v" not in captured


# ---------------------------------------------------------------------------
# repo_mode = "profiled" → pacman-class fast path
# ---------------------------------------------------------------------------

def _syu_fired(scenario):
    """True iff the bulk ``sudo pacman -Syu`` repo-upgrade transaction ran."""
    return any("pacman -Syu" in c for c in scenario.fake_run.commands)


def _checkupdates_called(scenario):
    """True iff the ``checkupdates`` repo-upgrade probe was invoked."""
    return any("checkupdates" in c for c in scenario.fake_run.commands)


def test_repo_pacman_class_flags_needs_pacman_upgrade(update_scenario, capsys):
    """repo_mode=profiled + no override + checkupdates newer → the package is
    flagged for a pacman upgrade and the bulk pacman -Syu fires (no source build)."""
    update_scenario.set_repo_mode("profiled")
    update_scenario.fake_sync()  # neutralize the real source-sync scheduler
    update_scenario.fake_checkupdates({"firefox": "131.0-1"})
    update_scenario.run(
        _make_args(offline=False),
        installed={"firefox": "130.0-1"}, foreign={},
    )
    combined = "".join(capsys.readouterr())
    assert "1 need pacman upgrade" in combined
    # Pacman fast path: deferred to one bulk -Syu, no source build.
    assert update_scenario.builds == []
    assert _syu_fired(update_scenario)


def test_repo_pacman_class_up_to_date_when_not_in_checkupdates(update_scenario, capsys):
    """repo_mode=profiled + nothing pending in checkupdates → UP_TO_DATE, no -Syu."""
    update_scenario.set_repo_mode("profiled")
    update_scenario.fake_sync()
    update_scenario.fake_checkupdates({})  # ran, nothing pending
    update_scenario.run(
        _make_args(offline=False),
        installed={"firefox": "131.0-1"}, foreign={},
    )
    combined = "".join(capsys.readouterr())
    assert "1 up to date" in combined
    assert not _syu_fired(update_scenario)


def test_repo_pacman_class_skipped_when_checkupdates_missing(update_scenario, capsys):
    """repo_mode=profiled + checkupdates errors (binary unavailable) →
    SKIPPED_NO_CHECKUPDATES surfaces and nothing is upgraded."""
    update_scenario.set_repo_mode("profiled")
    update_scenario.fake_sync()
    update_scenario.fake_checkupdates(None)  # fast path unavailable
    update_scenario.run(
        _make_args(offline=False, verbose=1),
        installed={"firefox": "131.0-1"}, foreign={},
    )
    combined = "".join(capsys.readouterr())
    assert "skipped (no checkupdates)" in combined
    assert not _syu_fired(update_scenario)


def test_repo_source_class_still_goes_through_pkgbuild_parse(update_scenario, capsys):
    """repo_mode=profiled + a behavior-changing override (pkgbuild_patch) →
    source path (real PKGBUILD parse + vercmp), NOT the pacman fast path:
    checkupdates is never consulted for a source-class package."""
    update_scenario.set_repo_mode("profiled")
    update_scenario.add_override("firefox", source="repo", pkgbuild_patch=True)
    update_scenario.add_pkg("firefox", "pkgname=firefox\npkgver=131.0\npkgrel=1\n")
    update_scenario.fake_sync()
    # Programmed but must be ignored — the override forces the source path.
    update_scenario.fake_checkupdates({"firefox": "132.0-1"})
    update_scenario.run(
        _make_args(offline=False, dry_run=True),
        installed={"firefox": "131.0-1"}, foreign={},
    )
    combined = "".join(capsys.readouterr())
    # Real parse + vercmp say equal → up to date, and no pacman fast path.
    assert "1 up to date" in combined
    assert not _checkupdates_called(update_scenario)
    assert not _syu_fired(update_scenario)


def test_offline_skips_checkupdates_call(update_scenario, capsys):
    """--offline → checkupdates is never invoked even in profiled repo mode."""
    update_scenario.set_repo_mode("profiled")
    update_scenario.fake_sync()
    update_scenario.fake_checkupdates({"firefox": "131.0-1"})
    update_scenario.run(
        _make_args(offline=True),
        installed={"firefox": "130.0-1"}, foreign={},
    )
    assert not _checkupdates_called(update_scenario)
    assert not _syu_fired(update_scenario)


def test_default_mode_does_not_call_checkupdates(update_scenario):
    """repo_mode unset (default pacman) → no pacman-class entries in scope, so
    checkupdates is never invoked."""
    update_scenario.fake_sync()
    update_scenario.fake_checkupdates({"firefox": "131.0-1"})
    update_scenario.run(
        _make_args(offline=False),
        installed={"firefox": "131.0-1"}, foreign={},
    )
    assert not _checkupdates_called(update_scenario)


# ---------------------------------------------------------------------------
# Stage-owned packages — kernel ownership filter
# ---------------------------------------------------------------------------

def _stage_owned_setup(tmp_path, args_extra=None, *, owner_in_state=False,
                      kernel_toml_present=True):
    """Build the patches+args needed to exercise the stage-owned filter for
    `linux-custom`.

    Two ownership signals are switchable:
      - ``owner_in_state``: kernel stage has stamped ``owner_stage = "kernel"``.
      - ``kernel_toml_present``: the bootstrap fallback (read kernel.toml's
        pkgname) finds the package.
    """
    pkgbase = "linux-custom"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(
        f"pkgname={pkgbase}\npkgver=6.13\npkgrel=1\n"
    )

    foreign = {pkgbase: "6.13-1"}
    state_data: dict = {}
    if owner_in_state:
        state_data[pkgbase] = {
            "pkgver": "6.13", "pkgrel": "1", "epoch": "0",
            "pkgbase": pkgbase, "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
            "source": "local", "owner_stage": "kernel",
        }

    args = _make_args(**(args_extra or {}))

    kernel_path = tmp_path / "kernel.toml"
    if kernel_toml_present:
        kernel_path.write_text(
            'enabled = true\n'
            f'pkgname = "{pkgbase}"\n'
            f'pkgbuild_src_dir = "{tmp_path}"\n'
        )

    results: list = []

    def capture(res_list, a):
        results.extend(res_list)

    return pkgbase, pkg_dir, foreign, state_data, args, kernel_path, results, capture


def test_kernel_owned_package_skipped_by_default(tmp_path, capsys):
    """linux-custom matched via kernel.toml bootstrap is skipped + info-logged."""
    (pkgbase, _, foreign, state_data, args, kernel_path, results,
     capture) = _stage_owned_setup(tmp_path)

    with (
        patch("sysforge.update.KERNEL_PATH", kernel_path),
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
        patch("sysforge.update.get_pkgbase", return_value=pkgbase),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    # linux-custom must NOT appear in the result set
    assert pkgbase not in {r.pkgbase for r in results}
    # And the skip notice fired on stderr (sysforge's custom logger writes there).
    captured = capsys.readouterr()
    assert "kernel-stage package" in captured.err
    assert "linux-custom" in captured.err


def test_kernel_owned_via_build_state_marker_skipped(tmp_path):
    """The owner_stage marker in build_state is honored even without kernel.toml."""
    (pkgbase, _, foreign, state_data, args, _kernel_path, results,
     capture) = _stage_owned_setup(
        tmp_path, owner_in_state=True, kernel_toml_present=False,
    )

    with (
        # KERNEL_PATH points at a nonexistent file so the bootstrap fallback
        # is inactive — only the build_state marker should drive the skip.
        patch("sysforge.update.KERNEL_PATH", tmp_path / "nope.toml"),
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    assert pkgbase not in {r.pkgbase for r in results}


def test_include_stage_owned_flag_includes_kernel_package(tmp_path):
    """--include-stage-owned overrides the skip."""
    (pkgbase, _, foreign, state_data, args, kernel_path, results,
     capture) = _stage_owned_setup(
        tmp_path, args_extra={"include_stage_owned": True},
        owner_in_state=True,
    )

    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "6.13",
                          "pkgrel": "1", "epoch": "0"}}

    with (
        patch("sysforge.update.KERNEL_PATH", kernel_path),
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    assert pkgbase in {r.pkgbase for r in results}


def test_explicit_pkgname_overrides_stage_owned_skip(tmp_path):
    """Naming a stage-owned package on the CLI opts it back in for that run."""
    (pkgbase, _, foreign, state_data, args, kernel_path, results,
     capture) = _stage_owned_setup(
        tmp_path, args_extra={"pkgnames": ["linux-custom"]},
        owner_in_state=True,
    )

    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "6.13",
                          "pkgrel": "1", "epoch": "0"}}

    with (
        patch("sysforge.update.KERNEL_PATH", kernel_path),
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    assert pkgbase in {r.pkgbase for r in results}


# ---------------------------------------------------------------------------
# Stage-owned packages — toolchain ownership filter (LLVM suite)
# ---------------------------------------------------------------------------

def _toolchain_owned_setup(tmp_path, args_extra=None, *, owner_in_state=False,
                           compiler="llvm", enabled=True, toolchain_toml_present=True):
    """Build the patches+args needed to exercise the toolchain stage-owned
    filter for an LLVM-suite package (``llvm``).

    Mirrors ``_stage_owned_setup`` but for the toolchain stage:
      - ``owner_in_state``: toolchain stage has stamped ``owner_stage="toolchain"``.
      - ``toolchain_toml_present`` + ``enabled`` + ``compiler``: drive the
        ``_toolchain_owns_llvm()`` bootstrap fallback (active only for
        enabled + compiler="llvm").
    """
    pkgbase = "llvm"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(
        f"pkgname={pkgbase}\npkgver=22.1.6\npkgrel=1\n"
    )

    foreign = {pkgbase: "22.1.6-1"}
    state_data: dict = {
        pkgbase: {
            "pkgver": "22.1.6", "pkgrel": "1", "epoch": "0",
            "pkgbase": pkgbase, "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
            "build_mode": "pgo_llvm_toolchain",
        }
    }
    if owner_in_state:
        state_data[pkgbase]["owner_stage"] = "toolchain"

    args = _make_args(**(args_extra or {}))

    toolchain_path = tmp_path / "toolchain.toml"
    if toolchain_toml_present:
        body = f"enabled = {str(enabled).lower()}\n"
        if compiler is not None:
            body += f'compiler = "{compiler}"\n'
        toolchain_path.write_text(body)

    results: list = []

    def capture(res_list, a):
        results.extend(res_list)

    return (pkgbase, pkg_dir, foreign, state_data, args, toolchain_path,
            results, capture)


def test_toolchain_owned_llvm_skipped_by_default(tmp_path, capsys):
    """An LLVM-suite package matched via the toolchain.toml (enabled + llvm)
    bootstrap fallback is skipped + info-logged, even with no owner_stage stamp."""
    (pkgbase, _, foreign, state_data, args, toolchain_path, results,
     capture) = _toolchain_owned_setup(tmp_path)

    with (
        patch("sysforge.update.KERNEL_PATH", tmp_path / "nope-kernel.toml"),
        patch("sysforge.update.TOOLCHAIN_PATH", toolchain_path),
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
        patch("sysforge.update.get_pkgbase", return_value=pkgbase),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    assert pkgbase not in {r.pkgbase for r in results}
    captured = capsys.readouterr()
    assert "toolchain-stage package" in captured.err
    assert "run `sysforge run toolchain`" in captured.err


def test_toolchain_owned_via_build_state_marker_skipped(tmp_path):
    """The owner_stage="toolchain" marker is honored even without toolchain.toml."""
    (pkgbase, _, foreign, state_data, args, _toolchain_path, results,
     capture) = _toolchain_owned_setup(
        tmp_path, owner_in_state=True, toolchain_toml_present=False,
    )

    with (
        patch("sysforge.update.KERNEL_PATH", tmp_path / "nope-kernel.toml"),
        # Nonexistent toolchain.toml so the bootstrap fallback is inactive —
        # only the build_state marker should drive the skip.
        patch("sysforge.update.TOOLCHAIN_PATH", tmp_path / "nope-toolchain.toml"),
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    assert pkgbase not in {r.pkgbase for r in results}


def test_toolchain_gcc_compiler_does_not_skip_llvm(tmp_path):
    """Dual-toolchain parity: with toolchain.toml compiler="gcc" the fallback is
    inactive (register-only path owns no LLVM), so the LLVM package is NOT
    skipped — it flows through to the build set."""
    (pkgbase, _, foreign, state_data, args, toolchain_path, results,
     capture) = _toolchain_owned_setup(tmp_path, compiler="gcc")

    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "22.1.6",
                          "pkgrel": "1", "epoch": "0"}}

    with (
        patch("sysforge.update.KERNEL_PATH", tmp_path / "nope-kernel.toml"),
        patch("sysforge.update.TOOLCHAIN_PATH", toolchain_path),
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    assert pkgbase in {r.pkgbase for r in results}


def test_include_stage_owned_includes_toolchain_llvm(tmp_path):
    """--include-stage-owned overrides the toolchain skip (compiler=llvm)."""
    (pkgbase, _, foreign, state_data, args, toolchain_path, results,
     capture) = _toolchain_owned_setup(
        tmp_path, args_extra={"include_stage_owned": True},
    )

    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "22.1.6",
                          "pkgrel": "1", "epoch": "0"}}

    with (
        patch("sysforge.update.KERNEL_PATH", tmp_path / "nope-kernel.toml"),
        patch("sysforge.update.TOOLCHAIN_PATH", toolchain_path),
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    assert pkgbase in {r.pkgbase for r in results}


def test_explicit_pkgname_overrides_toolchain_skip(tmp_path):
    """Naming the LLVM package on the CLI opts it back in for that run."""
    (pkgbase, _, foreign, state_data, args, toolchain_path, results,
     capture) = _toolchain_owned_setup(
        tmp_path, args_extra={"pkgnames": ["llvm"]},
    )

    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "22.1.6",
                          "pkgrel": "1", "epoch": "0"}}

    with (
        patch("sysforge.update.KERNEL_PATH", tmp_path / "nope-kernel.toml"),
        patch("sysforge.update.TOOLCHAIN_PATH", toolchain_path),
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    assert pkgbase in {r.pkgbase for r in results}


def test_toolchain_owned_spirv_skipped_via_configured_list(tmp_path, capsys):
    """spirv-llvm-translator is NOT matched by is_llvm_pkgbase (prefix set), so
    only the toolchain.toml [packages] configured-set union skips it. This pins
    the ownership broadening: a configured-but-unmatched member is skipped."""
    from sysforge.primitives.pkgbuild_patcher import is_llvm_pkgbase

    pkgbase = "spirv-llvm-translator"
    assert not is_llvm_pkgbase(pkgbase)  # the gap the broadening closes

    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(f"pkgname={pkgbase}\npkgver=19.1.5\npkgrel=1\n")
    foreign = {pkgbase: "19.1.5-1"}
    state_data = {
        pkgbase: {
            "pkgver": "19.1.5", "pkgrel": "1", "epoch": "0",
            "pkgbase": pkgbase, "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    # toolchain.toml owns LLVM (enabled + llvm) and lists spirv in non_pgo.
    toolchain_path = tmp_path / "toolchain.toml"
    toolchain_path.write_text(
        'enabled = true\ncompiler = "llvm"\n'
        '[packages]\npgo = ["llvm"]\n'
        'non_pgo = ["clang", "spirv-llvm-translator"]\nlib32 = []\n'
    )
    args = _make_args()
    results: list = []

    with (
        patch("sysforge.update.KERNEL_PATH", tmp_path / "nope-kernel.toml"),
        patch("sysforge.update.TOOLCHAIN_PATH", toolchain_path),
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
        patch("sysforge.update.get_pkgbase", return_value=pkgbase),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        with patch("sysforge.update._print_summary",
                   side_effect=lambda res, a: results.extend(res)):
            cmd_update(args)

    assert pkgbase not in {r.pkgbase for r in results}
    assert "toolchain-stage package" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Build-failure recording (_record_build_failure)
# ---------------------------------------------------------------------------

def test_record_build_failure_persists_diagnosis(tmp_path):
    from types import SimpleNamespace

    from sysforge.primitives.build_diag import FixSuggestion
    from sysforge.build_core import _record_build_failure

    result = SimpleNamespace(pkgbase="gpu-burn-git", pkgbuild_ver="r93.a113ce7")
    exc = RuntimeError("[build_failed] makepkg exit 4")
    exc.diagnosis = [FixSuggestion(
        signature="cuda:host-gcc-too-new",
        message="nvcc rejected the system host compiler",
        fix_cmd="NVCC_APPEND_FLAGS='-ccbin /usr/bin/g++-15'",
    )]

    _record_build_failure(tmp_path, result, exc)

    rec = BuildState(tmp_path).all_failures()["gpu-burn-git"]
    assert rec["signature"] == "cuda:host-gcc-too-new"
    assert rec["fix_cmd"] == "NVCC_APPEND_FLAGS='-ccbin /usr/bin/g++-15'"
    assert rec["pkgver"] == "r93.a113ce7"
    assert "[build_failed]" in rec["error"]


def test_record_build_failure_without_diagnosis(tmp_path):
    from types import SimpleNamespace

    from sysforge.build_core import _record_build_failure

    result = SimpleNamespace(pkgbase="foo-git", pkgbuild_ver=None)
    _record_build_failure(tmp_path, result, RuntimeError("[build_failed] boom"))

    rec = BuildState(tmp_path).all_failures()["foo-git"]
    assert "signature" not in rec
    assert "fix_cmd" not in rec
    assert "boom" in rec["error"]
