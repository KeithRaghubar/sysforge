"""
test_update.py — unit tests for sysforge.update

All subprocess calls (pacman -Q, git pull, makepkg) and filesystem access to
the state dir are mocked so no real system state is required.

Iteration model under test: the live install set (`pacman -Qm` + repo
packages selected by overrides). packages.toml entries are overrides only;
override entries with no installed counterpart are inert and silently
skipped (no NOT_INSTALLED action).
"""
import re
import itertools
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent / ".."))

import sysforge.update
from sysforge.update import (
    _check_one_pkgbase, _sync_sources, _assemble_package_set,
    _resolve_drift_axes,
)
from sysforge.update_common import _is_vcs
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




# ---------------------------------------------------------------------------
# cmd_update — empty state
# ---------------------------------------------------------------------------

def test_empty_install_set_exits_cleanly(update_scenario, capsys):
    """No foreign packages and no repo-source overrides → nothing in scope."""
    update_scenario.run(_make_args(), installed={}, foreign={})
    captured = capsys.readouterr()
    assert "No installed packages in scope" in (captured.out + captured.err)


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
# Split pkgbase whose installed members disagree on version. The pkgnames list
# reaches _check_one_pkgbase in set-iteration (hash) order, so picking "the"
# installed version must not depend on which member comes first, and members
# the current build no longer produces (orphans left installed from an older
# build with a different subpackage toggle) must not drive the verdict.
# ---------------------------------------------------------------------------

_SPLIT_PKGBUILD = (
    "pkgbase=linux-sysforge\n"
    'pkgname=("$pkgbase" "$pkgbase-headers" "$pkgbase-docs")\n'
    "pkgver=7.1.5.arch1\npkgrel=2\n"
)

# What the last build actually produced: docs dropped by the subpackage toggle.
_SPLIT_PATCHED = (
    "pkgbase=linux-sysforge\n"
    'pkgname=("$pkgbase" "$pkgbase-headers")\n'
    "pkgver=7.1.5.arch1\npkgrel=2\n"
)

_SPLIT_MEMBERS = [
    "linux-sysforge",
    "linux-sysforge-headers",
    "linux-sysforge-docs",
]


def _decide_split(tmp_path, *, pkgnames, all_installed, patched=None):
    pkg_dir = tmp_path / "linux-sysforge"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "PKGBUILD").write_text(_SPLIT_PKGBUILD)
    if patched is not None:
        (pkg_dir / "PKGBUILD.sysforge").write_text(patched)
    return _check_one_pkgbase(
        pkgbase="linux-sysforge",
        pkgnames=list(pkgnames),
        entry={"pkgbuild_dir": str(pkg_dir), "source": "repo"},
        sync_failures={},
        all_installed=dict(all_installed),
        unrecorded_names=set(),
        skip_sync_check=True,
        rpc_version_by_base={},
    )


@pytest.mark.parametrize("order", list(itertools.permutations(_SPLIT_MEMBERS)))
def test_split_pkgbase_verdict_is_pkgname_order_independent(tmp_path, order):
    """Every pkgnames ordering yields the same verdict.

    Regression: installed_ver was "first pkgname of the pkgbase that happens to
    be installed", and pkgnames arrives in set-iteration order, so the same
    system produced NEEDS_REBUILD or UP_TO_DATE at random across runs.
    """
    r = _decide_split(
        tmp_path,
        pkgnames=order,
        all_installed={
            "linux-sysforge": "7.1.5.arch1-2",
            "linux-sysforge-headers": "7.1.5.arch1-2",
            "linux-sysforge-docs": "7.1.4.arch1-1",
        },
        patched=_SPLIT_PATCHED,
    )
    assert r.installed_ver == "7.1.5.arch1-2"
    assert r.action == "UP_TO_DATE"


def test_split_pkgbase_ignores_member_the_build_no_longer_produces(tmp_path):
    """A stale member absent from PKGBUILD.sysforge does not force a rebuild.

    linux-sysforge-docs is left installed from a build made when the docs
    subpackage was enabled. The current build drops it, so nothing will ever
    refresh it — counting it would pin a permanent phantom advisory.
    """
    r = _decide_split(
        tmp_path,
        pkgnames=_SPLIT_MEMBERS,
        all_installed={
            "linux-sysforge": "7.1.5.arch1-2",
            "linux-sysforge-headers": "7.1.5.arch1-2",
            "linux-sysforge-docs": "7.1.4.arch1-1",
        },
        patched=_SPLIT_PATCHED,
    )
    assert r.action == "UP_TO_DATE"
    assert r.installed_ver == "7.1.5.arch1-2"


def test_split_pkgbase_counts_stale_member_the_build_still_produces(tmp_path):
    """A produced member that is genuinely behind still drives NEEDS_REBUILD.

    Same drift as above, but the patched PKGBUILD still lists -docs, so the
    rebuild will actually refresh it. The oldest produced member wins.
    """
    r = _decide_split(
        tmp_path,
        pkgnames=_SPLIT_MEMBERS,
        all_installed={
            "linux-sysforge": "7.1.5.arch1-2",
            "linux-sysforge-headers": "7.1.5.arch1-2",
            "linux-sysforge-docs": "7.1.4.arch1-1",
        },
        patched=_SPLIT_PKGBUILD,
    )
    assert r.action == "NEEDS_REBUILD"
    assert r.installed_ver == "7.1.4.arch1-1"


def test_split_pkgbase_without_patched_pkgbuild_uses_oldest_member(tmp_path):
    """No PKGBUILD.sysforge on disk (never built by sysforge) — stay
    conservative and treat every installed member as produced."""
    r = _decide_split(
        tmp_path,
        pkgnames=_SPLIT_MEMBERS,
        all_installed={
            "linux-sysforge": "7.1.5.arch1-2",
            "linux-sysforge-headers": "7.1.5.arch1-2",
            "linux-sysforge-docs": "7.1.4.arch1-1",
        },
    )
    assert r.action == "NEEDS_REBUILD"
    assert r.installed_ver == "7.1.4.arch1-1"


def test_split_pkgbase_all_members_orphaned_falls_back_to_all(tmp_path):
    """Patched PKGBUILD lists no installed member — don't produce an empty
    set; fall back to every installed member so the assert can't trip."""
    r = _decide_split(
        tmp_path,
        pkgnames=_SPLIT_MEMBERS,
        all_installed={"linux-sysforge-docs": "7.1.4.arch1-1"},
        patched=(
            "pkgbase=linux-sysforge\npkgname=(unrelated-name)\n"
            "pkgver=7.1.5.arch1\npkgrel=2\n"
        ),
    )
    assert r.installed_ver == "7.1.4.arch1-1"
    assert r.action == "NEEDS_REBUILD"


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
        # Command substitution is genuinely unevaluatable by the static parser.
        # (The ${var//-/_} replace form used to live here, but the parser now
        # resolves it — see test_parser.py::test_scalar_parameter_expansion_*.)
        '_tarver=8.12.10-36\npkgname=1password\n'
        'pkgver=$(printf %s "$_tarver" | tr - _)\npkgrel=36\n',
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


def test_replace_form_pkgver_resolves_without_rpc(tmp_path):
    """``pkgver=${_tarver//-/_}`` is resolved statically — no RPC cache needed.

    Regression for 3.0.0-B6: the literal used to survive into the comparison and
    the package presented as a same-version reinstall on every run. The empty
    ``rpc_version_by_base`` proves the verdict comes from the PKGBUILD itself.
    """
    pkgbase = "1password"
    pkg_dir = tmp_path / pkgbase
    _write_pkgbuild(
        pkg_dir,
        '_tarver=8.12.32\npkgname=1password\npkgver=${_tarver//-/_}\npkgrel=33\n',
    )

    result = _check_one_pkgbase(
        pkgbase=pkgbase,
        pkgnames=[pkgbase],
        entry={"pkgbuild_dir": str(pkg_dir)},
        sync_failures={},
        all_installed={pkgbase: "8.12.32-33"},
        unrecorded_names=set(),
        skip_sync_check=False,
        rpc_version_by_base={},
    )
    assert result is not None
    assert result.pkgbuild_ver == "8.12.32-33"
    assert result.action == "UP_TO_DATE"


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


def test_unresolved_pkgver_renamed_uses_origin_pkgbase_rpc(tmp_path):
    """A coexist-renamed package (installed name carries the -sysforge suffix)
    with unresolvable bash expansion falls back to the RPC version cached under
    its *origin* pkgbase — the cache is keyed by the stock upstream base, not
    the renamed one."""
    renamed = "1password-sysforge"
    origin = "1password"
    pkg_dir = tmp_path / origin
    _write_pkgbuild(
        pkg_dir,
        # Command substitution is genuinely unevaluatable by the static parser.
        # (The ${var//-/_} replace form used to live here, but the parser now
        # resolves it — see test_parser.py::test_scalar_parameter_expansion_*.)
        '_tarver=8.12.10-36\npkgname=1password\n'
        'pkgver=$(printf %s "$_tarver" | tr - _)\npkgrel=36\n',
    )
    entry = {"pkgbuild_dir": str(pkg_dir), "origin_pkgbase": origin}

    result = _check_one_pkgbase(
        pkgbase=renamed,
        pkgnames=[renamed],
        entry=entry,
        sync_failures={},
        all_installed={renamed: "8.12.10-36"},
        unrecorded_names=set(),
        skip_sync_check=False,
        # RPC cache is keyed by the stock upstream base only — the renamed
        # base is absent, so the rescue must consult origin_pkgbase.
        rpc_version_by_base={origin: "8.12.10-36"},
    )
    assert result is not None
    assert result.action == "UP_TO_DATE"
    assert result.pkgbuild_ver == "8.12.10-36"


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
    packages, _, _stage_owned = _assemble_package_set(
        _make_args(), BuildState(state_dir), {}, {}, overrides,
    )
    assert packages == {}
    assert "mesa-git" not in packages


def test_installed_aur_without_override_uses_defaults(update_scenario, capsys):
    """AUR package installed but with no override entry → walked with defaults
    (in scope, version-checked, reported as up to date)."""
    pkgbase = "example-aur-pkg"
    update_scenario.add_pkg(
        pkgbase, f"pkgname={pkgbase}\npkgver=12.3.3\npkgrel=1\n")
    update_scenario.run(
        _make_args(verbose=1),
        installed={pkgbase: "12.3.3-1"}, foreign={pkgbase: "12.3.3-1"},
    )
    combined = "".join(capsys.readouterr())
    # The package is iterated (1 package checked) and found up to date.
    assert "1 packages" in combined
    assert "1 up to date" in combined
    assert pkgbase in combined


def test_foreign_split_package_resolves_pkgbase_from_local_db(
        update_scenario, no_network, monkeypatch):
    """Foreign split-package subnames (e.g. linux-custom-headers) collapse to
    their parent pkgbase via pacman's local DB %BASE%, even when not in AUR.
    AUR RPC must NOT be called when the local DB already resolves the base."""
    pkgbase = "linux-custom"
    update_scenario.add_pkg(
        pkgbase,
        f"pkgbase={pkgbase}\n"
        f"pkgname=({pkgbase} {pkgbase}-headers)\n"
        f"pkgver=6.19.12.arch1\npkgrel=1\n",
    )

    # Fake pacman local DB recording %BASE%=linux-custom for both subpackages,
    # so the real get_pkgbase resolves the split parent without any AUR access.
    local_db = update_scenario.src_root.parent / "pacman-local"
    for sub in (pkgbase, f"{pkgbase}-headers"):
        d = local_db / f"{sub}-6.19.11-1"
        d.mkdir(parents=True)
        (d / "desc").write_text(f"%NAME%\n{sub}\n%BASE%\n{pkgbase}\n")
    monkeypatch.setattr("sysforge.primitives.pacman._LOCAL_DB_ROOT", local_db)

    # Would-rebuild (installed 6.19.11 < PKGBUILD 6.19.12) so a single build proves
    # both subpackages collapsed into one pkgbase group; no_network proves the
    # local-DB resolution avoided any AUR RPC.
    with patch("sysforge.primitives.stage_ownership.KERNEL_PATH",
               update_scenario.src_root.parent / "no-kernel-toml"):
        builds = update_scenario.run(
            _make_args(),
            installed={pkgbase: "6.19.11-1", f"{pkgbase}-headers": "6.19.11-1"},
            foreign={pkgbase: "6.19.11-1", f"{pkgbase}-headers": "6.19.11-1"},
        )

    assert len(builds) == 1


def test_repo_package_without_override_is_not_iterated(fake_run, state_dir):
    """A repo (non-foreign) package with no override → out of scope."""
    # mesa is an installed repo package; no foreign packages.
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout="mesa 1:25.3.1-1\n")
    packages, _, _stage_owned = _assemble_package_set(
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
    packages, _, _stage_owned = _assemble_package_set(
        _make_args(include_stage_owned=True), BuildState(state_dir), {}, {}, overrides,
    )
    assert set(packages) == {pkgbase}


def test_repo_mode_profiled_walks_installed_repo_packages(fake_run, state_dir):
    """With ``[build] repo_mode = "build_from_source"``, every installed repo package is
    iterated alongside foreign packages — no per-package override needed."""
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout="firefox 131.0-1\n")
    packages, _, _stage_owned = _assemble_package_set(
        _make_args(), BuildState(state_dir), {}, {"repo_mode": "build_from_source"}, {},
    )
    assert set(packages) == {"firefox"}


def test_repo_mode_pacman_skips_repo_packages(fake_run, state_dir):
    """Default (repo_mode "pacman"): a repo package without a behavior-changing
    override stays out of scope. Confirms the gate is load-bearing."""
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout="firefox 131.0-1\n")
    packages, _, _stage_owned = _assemble_package_set(
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
    packages, _, _stage_owned = _assemble_package_set(
        _make_args(), BuildState(state_dir), {}, {}, overrides,
    )
    assert packages == {}


def test_source_built_repo_package_is_tracked(fake_run, state_dir):
    """build_state is the tracking authority: a repo package sysforge
    source-built (build_mode != "pacman") is in update scope even with no
    override and default repo_mode — this is what makes `sysforge build mesa`
    durable. It must classify as repo_class="source" (rebuild from source),
    not "pacman" (which a deferred pacman -Syu would no-op behind IgnoreGroup).
    """
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout="mesa 1:25.3.1-1\n")
    bs = BuildState(state_dir)
    bs.record(pkgname="mesa", pkgver="25.3.1", pkgrel="1", epoch="1",
              pkgbase="mesa", pkgbuild_dir=state_dir,
              build_mode="source_built", source="repo")
    bs.save()
    packages, _, _stage_owned = _assemble_package_set(
        _make_args(), bs, {}, {}, {},
    )
    assert set(packages) == {"mesa"}
    assert packages["mesa"]["repo_class"] == "source"


def test_pacman_mode_record_does_not_track_repo_package(fake_run, state_dir):
    """Negative: a bare build_mode="pacman" marker (written by
    sync_with_installed for everything installed) must NOT pull a repo package
    into scope — only genuinely source-built records track."""
    fake_run.respond(["pacman", "-Qm"], stdout="")
    fake_run.respond(["pacman", "-Q"], stdout="mesa 1:25.3.1-1\n")
    bs = BuildState(state_dir)
    bs.record(pkgname="mesa", pkgver="25.3.1", pkgrel="1", epoch="1",
              pkgbase="mesa", pkgbuild_dir=state_dir,
              build_mode="pacman", source="repo")
    bs.save()
    packages, _, _stage_owned = _assemble_package_set(
        _make_args(), bs, {}, {}, {},
    )
    assert packages == {}


def test_assemble_returns_stage_owned_partition(fake_run, state_dir):
    """Stage-owned packages are partitioned out of `packages` into a third
    return value, stamped with `owner_stage`, instead of being subtracted
    out of scope entirely — the advisory check (Task 4) needs them."""
    fake_run.respond(["pacman", "-Qm"], stdout="linux-custom 6.19.11-1\n")
    fake_run.respond(["pacman", "-Q"], stdout="linux-custom 6.19.11-1\nmesa 1:25.3.1-1\n")
    bs = BuildState(state_dir)
    bs.record(pkgname="linux-custom", pkgver="6.19.11", pkgrel="1", epoch=None,
              pkgbase="linux-custom", pkgbuild_dir=state_dir,
              build_mode="source_built", source="aur", owner_stage="kernel")
    bs.record(pkgname="mesa", pkgver="25.3.1", pkgrel="1", epoch="1",
              pkgbase="mesa", pkgbuild_dir=state_dir,
              build_mode="source_built", source="repo")
    bs.save()
    with patch("sysforge.primitives.stage_ownership.KERNEL_PATH",
               state_dir.parent / "no-kernel-toml"):
        packages, unrecorded, stage_owned = _assemble_package_set(
            _make_args(), bs, {}, {}, {},
        )
    assert "linux-custom" not in packages
    assert "linux-custom" in stage_owned
    assert stage_owned["linux-custom"]["owner_stage"] == "kernel"
    assert "mesa" in packages


def test_detect_stage_owned_updates_offline_returns_empty():
    from sysforge.update import _detect_stage_owned_updates
    stage_owned = {"linux-custom": {"owner_stage": "kernel", "source": "aur"}}
    out = _detect_stage_owned_updates(
        stage_owned, all_installed={"linux-custom": "6.9"},
        sync_failures={}, rpc_version_by_base={}, pacman_updates_map=None,
        skip_sync_check=True, offline=True,
    )
    assert out == []


def test_detect_stage_owned_updates_reports_behind(monkeypatch):
    import sysforge.update as up
    from sysforge.update_result import _UpdateResult

    def fake_check(pkgbase, pkgnames, entry, *a, **k):
        return _UpdateResult(pkgbase=pkgbase, pkgnames=pkgnames,
                             action="NEEDS_REBUILD", installed_ver="6.9",
                             pkgbuild_ver="6.10", pkgbuild_path=None)
    monkeypatch.setattr(up, "_check_one_pkgbase", fake_check)

    stage_owned = {"linux-custom": {"owner_stage": "kernel", "source": "aur"}}
    out = up._detect_stage_owned_updates(
        stage_owned, all_installed={"linux-custom": "6.9"},
        sync_failures={}, rpc_version_by_base={}, pacman_updates_map=None,
        skip_sync_check=False, offline=False,
    )
    assert out == [("linux-custom", "6.9", "6.10", "kernel")]


# ---------------------------------------------------------------------------
# 3.0.0-B1 — stage-owned advisory for `source = "repo"` pinned checkouts
# ---------------------------------------------------------------------------

def test_detect_stage_owned_repo_reads_sync_db_not_local_pkgbuild(monkeypatch):
    """3.0.0-B1: a stage-owned `source = "repo"` entry sits on a detached-HEAD
    pin, so its local pkgbuild_ver always equals the installed version and
    _check_one_pkgbase can never yield NEEDS_REBUILD. The advisory must come
    from pacman's sync DB instead — the authority the pin targets."""
    import sysforge.update as up
    import sysforge.primitives.pacman as pac

    def boom(*a, **k):  # the repo path must not consult the local PKGBUILD
        raise AssertionError("_check_one_pkgbase called for a repo-source entry")
    monkeypatch.setattr(up, "_check_one_pkgbase", boom)
    monkeypatch.setattr(pac, "get_pacman_sync_version", lambda n: "22.1.5-1")

    stage_owned = {
        "spirv-llvm-translator": {"owner_stage": "toolchain", "source": "repo"},
    }
    out = up._detect_stage_owned_updates(
        stage_owned, all_installed={"spirv-llvm-translator": "22.1.2-1"},
        sync_failures={}, rpc_version_by_base={}, pacman_updates_map=None,
        skip_sync_check=False, offline=False,
    )
    assert out == [
        ("spirv-llvm-translator", "22.1.2-1", "22.1.5-1", "toolchain")
    ]


def test_detect_stage_owned_repo_no_advisory_when_sync_db_not_newer(monkeypatch):
    """Sync DB at or behind the installed version yields no advisory."""
    import sysforge.update as up
    import sysforge.primitives.pacman as pac
    monkeypatch.setattr(pac, "get_pacman_sync_version", lambda n: "22.1.2-1")

    stage_owned = {
        "spirv-llvm-translator": {"owner_stage": "toolchain", "source": "repo"},
    }
    out = up._detect_stage_owned_updates(
        stage_owned, all_installed={"spirv-llvm-translator": "22.1.2-1"},
        sync_failures={}, rpc_version_by_base={}, pacman_updates_map=None,
        skip_sync_check=False, offline=False,
    )
    assert out == []


def test_detect_stage_owned_repo_unknown_to_sync_db_is_silent(monkeypatch):
    """Best-effort: a package the sync DB does not carry omits the advisory
    rather than raising — this path must never fail an update run."""
    import sysforge.update as up
    import sysforge.primitives.pacman as pac
    monkeypatch.setattr(pac, "get_pacman_sync_version", lambda n: None)

    stage_owned = {"some-local-pkg": {"owner_stage": "toolchain", "source": "repo"}}
    out = up._detect_stage_owned_updates(
        stage_owned, all_installed={"some-local-pkg": "1.0-1"},
        sync_failures={}, rpc_version_by_base={}, pacman_updates_map=None,
        skip_sync_check=False, offline=False,
    )
    assert out == []


def test_detect_stage_owned_aur_still_uses_rpc_path(monkeypatch):
    """3.0.0-B1 fixes the repo origin only: AUR-sourced stage-owned packages
    get their upstream version from RPC without a sync, so they keep running
    through _check_one_pkgbase unchanged."""
    import sysforge.update as up
    import sysforge.primitives.pacman as pac
    from sysforge.update_result import _UpdateResult

    def unexpected(name):
        raise AssertionError("sync DB consulted for an AUR-source entry")
    monkeypatch.setattr(pac, "get_pacman_sync_version", unexpected)

    seen = []

    def fake_check(pkgbase, pkgnames, entry, *a, **k):
        seen.append(pkgbase)
        return _UpdateResult(pkgbase=pkgbase, pkgnames=pkgnames,
                             action="NEEDS_REBUILD", installed_ver="6.9",
                             pkgbuild_ver="6.10", pkgbuild_path=None)
    monkeypatch.setattr(up, "_check_one_pkgbase", fake_check)

    stage_owned = {"linux-custom": {"owner_stage": "kernel", "source": "aur"}}
    out = up._detect_stage_owned_updates(
        stage_owned, all_installed={"linux-custom": "6.9"},
        sync_failures={}, rpc_version_by_base={}, pacman_updates_map=None,
        skip_sync_check=False, offline=False,
    )
    assert seen == ["linux-custom"]
    assert out == [("linux-custom", "6.9", "6.10", "kernel")]


def test_source_built_record_survives_sync_with_installed(state_dir):
    """Durability: sync_with_installed only adds pacman markers for packages
    with no record and prunes uninstalled ones — it must never downgrade an
    existing source-built record to build_mode="pacman"."""
    bs = BuildState(state_dir)
    bs.record(pkgname="mesa", pkgver="25.3.1", pkgrel="1", epoch="1",
              pkgbase="mesa", pkgbuild_dir=state_dir,
              build_mode="source_built", source="repo")
    bs.save()
    bs.sync_with_installed({"mesa": "1:25.3.1-1", "bash": "5.2-1"})
    assert bs.get("mesa")["build_mode"] == "source_built"   # unchanged
    assert bs.get("bash")["build_mode"] == "pacman"     # new inert marker


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


def test_timings_flag_emits_phase_report(update_scenario, capsys):
    """--timings promotes the phase wall-clock report to UI output."""
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.run(
        _make_args(dry_run=True, timings=True),
        installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
    )
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "Phase timings" in out
    assert "version check" in out


def test_no_timings_flag_keeps_report_at_info_level(update_scenario, capsys):
    """Without --timings the report stays at info level, not promoted to UI."""
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.run(
        _make_args(dry_run=True),
        installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
    )
    captured = capsys.readouterr()
    report_lines = [
        line for line in (captured.out + captured.err).splitlines()
        if "Phase timings" in line
    ]
    assert report_lines and all("[INFO]" in line for line in report_lines)


# ---------------------------------------------------------------------------
# Phase 4.3 — flag drift (canonical surface; absorbed the removed `converge`)
# ---------------------------------------------------------------------------

def _seed_flag_drift(scenario, *, stored_flags="CFLAGS=-this-is-stale"):
    """htop is version-current but recorded with stale flags -> flag-drifted.

    Returns the (installed, foreign) maps for the run.
    """
    scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    scenario.record("htop", "3.4.1", "1", flags_string=stored_flags)
    return {"htop": "3.4.1-1"}, {"htop": "3.4.1-1"}


def test_flag_drift_reported_but_not_rebuilt_by_default(update_scenario, capsys):
    """Version-current but flag-drifted -> reported, never rebuilt without a flag."""
    installed, foreign = _seed_flag_drift(update_scenario)
    builds = update_scenario.run(
        _make_args(), installed=installed, foreign=foreign,
    )
    assert builds == []  # detection only — no opt-in flag passed
    captured = capsys.readouterr()
    assert "flag drift" in (captured.out + captured.err).lower()


def test_offline_dry_run_flag_drift_is_network_free(update_scenario, capsys):
    """`update --offline --dry-run` is the read-only flag-drift report (no network)."""
    installed, foreign = _seed_flag_drift(update_scenario)
    builds = update_scenario.run(
        _make_args(offline=True, dry_run=True), installed=installed, foreign=foreign,
    )
    assert builds == []
    captured = capsys.readouterr()
    assert "flag drift" in (captured.out + captured.err).lower()
    # offline must not have triggered any git fetch/clone against sources.
    assert not any(
        "git" in c and ("fetch" in c or "clone" in c)
        for c in update_scenario.fake_run.commands
    )


# ---------------------------------------------------------------------------
# Phase 4.25 — toolchain-fingerprint drift (Q9)
# ---------------------------------------------------------------------------

def _seed_toolchain_fp(scenario, monkeypatch, *, active_fp, rec_fp,
                       variant="pgo_llvm"):
    """htop is version-current, recorded under ``variant``/``rec_fp``; the
    active toolchain reports ``variant``/``active_fp``. Patches the two canonical
    accessors in update's namespace so no real toolchain state is needed."""
    scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    scenario.record("htop", "3.4.1", "1", toolchain_variant=variant,
                    toolchain_fingerprint=rec_fp)
    monkeypatch.setattr("sysforge.update.get_toolchain_variant",
                        lambda s: variant)
    monkeypatch.setattr("sysforge.update.get_toolchain_fingerprint",
                        lambda s: active_fp)
    return {"htop": "3.4.1-1"}, {"htop": "3.4.1-1"}


def test_same_variant_different_fingerprint_is_drift(update_scenario, capsys,
                                                     monkeypatch):
    installed, foreign = _seed_toolchain_fp(
        update_scenario, monkeypatch, active_fp="fp-new", rec_fp="fp-old")
    builds = update_scenario.run(
        _make_args(), installed=installed, foreign=foreign)
    assert builds == []  # advisory only, no opt-in flag
    captured = capsys.readouterr()
    assert "toolchain drift" in (captured.out + captured.err).lower()


def test_same_variant_identical_fingerprint_no_drift(update_scenario, capsys,
                                                     monkeypatch):
    installed, foreign = _seed_toolchain_fp(
        update_scenario, monkeypatch, active_fp="fp-same", rec_fp="fp-same")
    update_scenario.run(_make_args(), installed=installed, foreign=foreign)
    captured = capsys.readouterr()
    assert "toolchain drift" not in (captured.out + captured.err).lower()


def test_missing_recorded_fingerprint_no_drift(update_scenario, capsys,
                                               monkeypatch):
    # Entry built before Q9: no recorded fingerprint → never flagged.
    installed, foreign = _seed_toolchain_fp(
        update_scenario, monkeypatch, active_fp="fp-new", rec_fp=None)
    update_scenario.run(_make_args(), installed=installed, foreign=foreign)
    captured = capsys.readouterr()
    assert "toolchain drift" not in (captured.out + captured.err).lower()


def test_explain_drift_renders_same_variant_reason(update_scenario, capsys,
                                                   monkeypatch):
    installed, foreign = _seed_toolchain_fp(
        update_scenario, monkeypatch, active_fp="fp-new", rec_fp="fp-old")
    update_scenario.run(
        _make_args(explain_drift=True), installed=installed, foreign=foreign)
    out = capsys.readouterr().out
    assert "toolchain rebuilt since build (same variant: pgo_llvm)" in out


def test_explain_drift_renders_different_variant_reason(update_scenario, capsys,
                                                        monkeypatch):
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.record("htop", "3.4.1", "1", toolchain_variant="stock_llvm")
    monkeypatch.setattr("sysforge.update.get_toolchain_variant",
                        lambda s: "pgo_llvm")
    monkeypatch.setattr("sysforge.update.get_toolchain_fingerprint",
                        lambda s: "fp-new")
    update_scenario.run(
        _make_args(explain_drift=True),
        installed={"htop": "3.4.1-1"}, foreign={"htop": "3.4.1-1"})
    out = capsys.readouterr().out
    assert "different variant than active (pgo_llvm)" in out


def test_rebuild_on_toolchain_drift_promotes_fingerprint_drift(update_scenario,
                                                               monkeypatch):
    installed, foreign = _seed_toolchain_fp(
        update_scenario, monkeypatch, active_fp="fp-new", rec_fp="fp-old")
    builds = update_scenario.run(
        _make_args(rebuild_on_toolchain_drift=True, no_toolchain_preflight=True),
        installed=installed, foreign=foreign)
    assert len(builds) == 1
    pkgbuild_path = builds[0][0][0] if builds[0][0] else builds[0][1]["pkgbuild_path"]
    assert "htop" in str(pkgbuild_path)


def test_toolchain_drift_promotion_forces_the_rebuild(update_scenario,
                                                     monkeypatch):
    """3.0.0-B9: a drift-promoted rebuild happens at an unchanged pkgver, so
    without -f makepkg exits 13 and the stale artifact gets reinstalled."""
    installed, foreign = _seed_toolchain_fp(
        update_scenario, monkeypatch, active_fp="fp-new", rec_fp="fp-old")
    builds = update_scenario.run(
        _make_args(rebuild_on_toolchain_drift=True, no_toolchain_preflight=True),
        installed=installed, foreign=foreign)
    assert "-f" in builds[0][1]["options"].extra_flags


def test_flag_drift_promotion_forces_the_rebuild(update_scenario):
    installed, foreign = _seed_flag_drift(update_scenario)
    builds = update_scenario.run(
        _make_args(rebuild_on_flag_drift=True, no_toolchain_preflight=True),
        installed=installed, foreign=foreign,
    )
    assert "-f" in builds[0][1]["options"].extra_flags


def test_ordinary_version_rebuild_is_not_forced(update_scenario):
    """Only drift promotion forces: a genuine pkgver bump has no stale
    artifact to trip over, and -f would defeat the resume case."""
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.record("htop", "3.3.0", "1")
    builds = update_scenario.run(
        _make_args(no_toolchain_preflight=True),
        installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
    )
    assert builds, "expected a version-driven rebuild"
    assert "-f" not in builds[0][1]["options"].extra_flags


def test_explain_drift_lists_flag_drift_and_exits(update_scenario, capsys):
    installed, foreign = _seed_flag_drift(update_scenario)
    builds = update_scenario.run(
        _make_args(explain_drift=True), installed=installed, foreign=foreign,
    )
    assert builds == []  # --explain-drift exits before Phase 5
    out = capsys.readouterr().out
    assert "different flags" in out
    assert "htop" in out


def test_rebuild_on_flag_drift_promotes_to_rebuild(update_scenario):
    installed, foreign = _seed_flag_drift(update_scenario)
    builds = update_scenario.run(
        _make_args(rebuild_on_flag_drift=True, no_toolchain_preflight=True),
        installed=installed, foreign=foreign,
    )
    assert len(builds) == 1
    pkgbuild_path = builds[0][0][0] if builds[0][0] else builds[0][1]["pkgbuild_path"]
    assert "htop" in str(pkgbuild_path)


def test_rebuild_on_drift_umbrella_covers_flag_drift(update_scenario):
    """--rebuild-on-drift opts into both axes, so flag drift rebuilds too."""
    installed, foreign = _seed_flag_drift(update_scenario)
    builds = update_scenario.run(
        _make_args(rebuild_on_drift=True, no_toolchain_preflight=True),
        installed=installed, foreign=foreign,
    )
    assert len(builds) == 1


def test_update_config_rebuild_on_drift_triggers_both_axes():
    """[update] rebuild_on_drift = true, no CLI flag -> both axes rebuild."""
    args = _make_args(rebuild_on_drift=False, rebuild_on_toolchain_drift=False,
                       rebuild_on_flag_drift=False)
    assert _resolve_drift_axes(args, update_cfg={"rebuild_on_drift": True}) == (True, True, True)


def test_update_cli_flag_wins_when_config_false():
    """A CLI flag still wins even when the matching config key is false."""
    args = _make_args(rebuild_on_flag_drift=True)
    assert _resolve_drift_axes(args, update_cfg={"rebuild_on_flag_drift": False})[2] is True


def test_update_both_off_no_rebuild():
    """No CLI flags and no [update] section -> no axis is enabled."""
    args = _make_args()
    assert _resolve_drift_axes(args, update_cfg={}) == (False, False, False)


def test_update_config_loaded_from_sysforge_toml_not_profiles(monkeypatch):
    """Real load path: [update] section must come from sysforge.toml
    (load_sysforge_toml), not profiles.toml -- this is the bug the F30
    final-review Critical caught (config source was wired to the wrong
    loader, making the feature completely inert)."""
    args = _make_args(rebuild_on_drift=False, rebuild_on_toolchain_drift=False,
                       rebuild_on_flag_drift=False)
    monkeypatch.setattr(
        sysforge.update, "load_sysforge_toml",
        lambda: {"update": {"rebuild_on_drift": True}},
    )
    assert sysforge.update._resolve_drift_axes(args) == (True, True, True)


def test_flag_drift_rebuild_installs_through_phase6_filter(update_scenario):
    """A promoted flag-drift rebuild flows through Phase 5/6: built artifact is
    installed, gated by filter_pkgs_to_installed (htop is installed -> kept)."""
    update_scenario.use_pkgdest()
    installed, foreign = _seed_flag_drift(update_scenario)
    update_scenario.build_produces(
        "htop", {"htop-3.4.1-1-x86_64.pkg.tar.zst": "htop"},
    )
    update_scenario.run(
        _make_args(rebuild_on_flag_drift=True, no_toolchain_preflight=True),
        installed=installed, foreign=foreign,
    )
    installed_files = [f for call in update_scenario.installed_pkg_files() for f in call]
    assert any("htop-3.4.1-1" in f for f in installed_files)


def test_not_profiled_package_is_not_flag_drift_checked(update_scenario, capsys):
    """A pacman-mode (non-profiled) package is never a flag-drift candidate."""
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    # build_mode pacman (not source_built) -> outside flag-drift scope
    from sysforge.primitives.build_state import BuildState
    bs = BuildState(update_scenario.state_dir)
    bs.record("htop", "3.4.1", "1", "0", "htop",
              update_scenario.src_root / "htop", build_mode="pacman",
              flags_string="CFLAGS=-stale")
    bs.save()
    builds = update_scenario.run(
        _make_args(rebuild_on_flag_drift=True, no_toolchain_preflight=True),
        installed={"htop": "3.4.1-1"}, foreign={"htop": "3.4.1-1"},
    )
    assert builds == []
    assert "flag drift" not in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# Phase 4.3 fold — profiled build_state entries outside update's package walk
# (e.g. a repo-class package recorded by `sysforge build` with repo_mode
# unset). Coverage absorbed from the removed `converge` verb.
# ---------------------------------------------------------------------------

def _seed_out_of_walk_drift(scenario):
    """ripgrep: installed and source-built, but stage-owned (owner_stage), so
    update's walk filters it out — only the Phase 4.3 fold sees its profiled
    build_state entry. Recorded flags are stale -> drifted.

    (A plain source-built repo package is now *in* the walk under the
    build_state-is-authority model, so stage ownership is what keeps an entry
    genuinely out-of-walk here.)"""
    scenario.add_pkg("ripgrep", "pkgname=ripgrep\npkgver=14.0.0\npkgrel=1\n")
    scenario.record("ripgrep", "14.0.0", "1",
                    flags_string="CFLAGS=-this-is-stale",
                    owner_stage="toolchain")
    return {"ripgrep": "14.0.0-1"}, {}


def test_fold_reports_drift_for_out_of_walk_entry(update_scenario, capsys):
    installed, foreign = _seed_out_of_walk_drift(update_scenario)
    builds = update_scenario.run(
        _make_args(), installed=installed, foreign=foreign,
    )
    assert builds == []  # detect/report only
    captured = capsys.readouterr()
    assert "flag drift" in (captured.out + captured.err).lower()


def test_fold_explain_drift_lists_out_of_walk_entry(update_scenario, capsys):
    installed, foreign = _seed_out_of_walk_drift(update_scenario)
    builds = update_scenario.run(
        _make_args(explain_drift=True), installed=installed, foreign=foreign,
    )
    assert builds == []
    out = capsys.readouterr().out
    assert "different flags" in out
    assert "ripgrep" in out


def test_fold_entry_not_promoted_but_hinted(update_scenario, capsys):
    """--rebuild-on-flag-drift can't queue an out-of-walk entry (no result row
    to promote); it must say so and point at `sysforge build` instead."""
    installed, foreign = _seed_out_of_walk_drift(update_scenario)
    builds = update_scenario.run(
        _make_args(rebuild_on_flag_drift=True, no_toolchain_preflight=True,
                   verbose=1),
        installed=installed, foreign=foreign,
    )
    assert builds == []  # never queued — there is no walk entry to promote
    text = (lambda c: c.out + c.err)(capsys.readouterr())
    assert "outside this run's package walk" in text
    assert "sysforge build" in text


def test_fold_respects_pkgnames_filter(update_scenario, capsys):
    """`sysforge update htop` must not drag unrelated build_state entries
    into the drift report."""
    installed, foreign = _seed_out_of_walk_drift(update_scenario)
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    installed["htop"] = "3.4.1-1"
    foreign["htop"] = "3.4.1-1"
    builds = update_scenario.run(
        _make_args(pkgnames=["htop"]), installed=installed, foreign=foreign,
    )
    assert builds == []
    out = capsys.readouterr().out
    assert "ripgrep" not in out


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


def test_frozen_sync_denial_causes_nonzero_exit(update_scenario, capsys):
    """Important-4 (3.0.0-F2): a source-freeze denial is a blocker, not a
    skip. Unlike a plain PULL_FAILED (which lets the run finish cleanly),
    htop's FROZEN status must both appear in the summary and abort the run
    with a non-zero exit — silently printing nothing and exiting 0 is
    exactly the failure mode this feature exists to prevent. neovim
    genuinely needs a rebuild so the run doesn't take the earlier
    "Nothing to rebuild" exit before failed_pkgs is populated.
    """
    from sysforge.primitives.source_sync import STATUS_FROZEN
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=0.9.0\npkgrel=1\n")
    update_scenario.add_pkg("neovim", "pkgname=neovim\npkgver=0.9.1\npkgrel=1\n")
    update_scenario.fake_sync(
        {"htop": (STATUS_FROZEN, "source freeze active — refused: htop")}
    )
    with pytest.raises(RuntimeError, match="htop"):
        update_scenario.run(
            _make_args(offline=False),
            installed={"htop": "0.9.0-1", "neovim": "0.9.0-1"},
            foreign={"htop": "0.9.0-1", "neovim": "0.9.0-1"},
        )
    combined = "".join(capsys.readouterr())
    assert "source freeze active" in combined
    assert "1 source freeze" in combined


def test_frozen_only_run_still_exits_nonzero(update_scenario, capsys):
    """New-2 (review): every candidate frozen, NOTHING buildable.

    Without a genuinely-rebuildable neighbour, htop's FROZEN status strips it
    from ``to_build``, leaving ``to_build`` and ``pending_pacman_upgrade``
    both empty — the exact shape that used to hit the early "Nothing to
    rebuild." return before ``failed_pkgs``/the raise were ever reached.
    That earlier exit is precisely the silent "prints a line, exits 0" mode
    the freeze feature exists to close.
    """
    from sysforge.primitives.source_sync import STATUS_FROZEN
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=0.9.0\npkgrel=1\n")
    update_scenario.fake_sync(
        {"htop": (STATUS_FROZEN, "source freeze active — refused: htop")}
    )
    with pytest.raises(RuntimeError, match="htop"):
        update_scenario.run(
            _make_args(offline=False),
            installed={"htop": "0.9.0-1"},
            foreign={"htop": "0.9.0-1"},
        )
    combined = "".join(capsys.readouterr())
    # Must NOT be the silent early exit.
    assert "Nothing to rebuild" not in combined
    assert "source freeze active" in combined
    assert "1 source freeze" in combined
    assert "htop" in combined


def test_frozen_survives_review_gate_abort(update_scenario, monkeypatch, capsys):
    """New (review round 3): a THIRD early-return route — the PKGBUILD
    review-gate abort at build_core's ``outcome.aborted`` — must not swallow
    the freeze either. A mixed run: htop is frozen (excluded from
    to_build), neovim genuinely needs a rebuild and reaches the review gate,
    where the user aborts. Before this fix that abort's early ``return``
    happened before the frozen check, so the whole run quietly exited 0 with
    htop's denial dropped.
    """
    from sysforge.primitives.source_sync import STATUS_FROZEN
    import sysforge.build_core as _build_core
    from sysforge.primitives.pkgbuild_review import DECISION_ABORT

    monkeypatch.setattr(_build_core, "review_target", lambda *a, **k: DECISION_ABORT)

    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=0.9.0\npkgrel=1\n")
    update_scenario.add_pkg("neovim", "pkgname=neovim\npkgver=0.9.1\npkgrel=1\n")
    update_scenario.fake_sync(
        {"htop": (STATUS_FROZEN, "source freeze active — refused: htop")}
    )
    with pytest.raises(RuntimeError, match="htop"):
        update_scenario.run(
            _make_args(offline=False, review=True),
            installed={"htop": "0.9.0-1", "neovim": "0.9.0-1"},
            foreign={"htop": "0.9.0-1", "neovim": "0.9.0-1"},
        )
    combined = "".join(capsys.readouterr())
    # The per-package summary (Phase 4, ahead of the build/review phase)
    # already printed the frozen line before the review gate was ever
    # reached, so it survives the abort.
    assert "source freeze active" in combined
    assert "1 source freeze" in combined
    assert "Aborted at PKGBUILD review" in combined


def test_build_failure_exits_nonzero(update_scenario, monkeypatch, capsys):
    """3.0.0-B4: a build failure exits 1.

    Unlike the freeze path it does *not* raise — a reported build failure
    needs no sentinel recovery prompt on the next run — so the code travels
    back through ``cmd_update``'s return value into
    ``ExecResult.exit_code``.
    """
    import sysforge.primitives.makepkg_wrapper as _mw

    def _boom(*a, **k):
        raise RuntimeError("simulated build failure")

    monkeypatch.setattr(_mw, "run", _boom)

    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.run(
        _make_args(),
        installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
    )
    combined = "".join(capsys.readouterr())
    # Tightened oracle: assert the actual failure line, not just that "htop"
    # appears somewhere (which would pass even if the failure were reported
    # under the wrong label).
    assert "Build failed for 'htop'" in combined
    assert "simulated build failure" in combined
    assert update_scenario.exit_code == 1


def test_cleansrc_refusal_exits_nonzero(update_scenario, capsys):
    """3.0.0-B4: a cleansrc ``STATUS_PURGE_REFUSED`` denial rides in
    ``failed_pkgs`` alongside build failures and must exit non-zero for the
    same reason — the queue did not do what was asked. neovim genuinely
    needs a rebuild so the run doesn't take the "Nothing to rebuild" exit
    first."""
    from sysforge.primitives.source_sync import STATUS_PURGE_REFUSED
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=0.9.0\npkgrel=1\n")
    update_scenario.add_pkg("neovim", "pkgname=neovim\npkgver=0.9.1\npkgrel=1\n")
    update_scenario.fake_sync(
        {"htop": (STATUS_PURGE_REFUSED, "refused to purge untracked files")}
    )
    update_scenario.run(
        _make_args(offline=False),
        installed={"htop": "0.9.0-1", "neovim": "0.9.0-1"},
        foreign={"htop": "0.9.0-1", "neovim": "0.9.0-1"},
    )
    capsys.readouterr()
    assert update_scenario.exit_code == 1


def test_successful_run_exits_zero(update_scenario, capsys):
    """3.0.0-B4 guard-rail: the non-zero exit is scoped to failures — a run
    that rebuilds everything it set out to rebuild still exits 0."""
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.run(
        _make_args(),
        installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
    )
    capsys.readouterr()
    assert update_scenario.exit_code == 0


def test_nothing_to_rebuild_exits_zero(update_scenario, capsys):
    """3.0.0-B4: the early "Nothing to rebuild." return is a clean run."""
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.3.0\npkgrel=1\n")
    update_scenario.run(
        _make_args(),
        installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
    )
    assert "Nothing to rebuild" in "".join(capsys.readouterr())
    assert update_scenario.exit_code == 0


def test_dry_run_with_pending_rebuild_exits_zero(update_scenario, capsys):
    """3.0.0-B4: the read-only routes return before any failure tally
    exists and must keep exiting 0 — a pending rebuild is not a failure."""
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.run(
        _make_args(dry_run=True),
        installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
    )
    capsys.readouterr()
    assert update_scenario.exit_code == 0


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

    import sysforge.primitives.already_built as _ab
    with patch("sysforge.build_core.resolve_already_built",
               wraps=_ab.resolve_already_built) as routed:
        update_scenario.run(
            _make_args(),
            installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"},
        )
    assert routed.call_count == 1

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


def test_sync_status_frozen_maps_to_frozen(tmp_path):
    """Important-4 (3.0.0-F2): a source-freeze denial must reach a
    recognizable action, not silently fall back to PULL_FAILED (which would
    print the wrong message) or vanish."""
    result = _check_pkgbase_with_sync_status(tmp_path, "frozen")
    assert result is not None
    assert result.action == "FROZEN"


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


def test_result_summary_carries_version_pairs():
    from sysforge.update import _build_result_summary
    from sysforge.update_result import _UpdateResult

    results = [
        _UpdateResult(pkgbase="mesa", pkgnames=["mesa"], action="NEEDS_REBUILD",
                      installed_ver="24.0", pkgbuild_ver="24.1",
                      pkgbuild_path=None),
    ]
    summary = _build_result_summary(
        results=results,
        built_pkgs=["mesa"], failed_pkgs=[], pacman_upgrade_pkgs=[],
        installed_deps=[], pgo_skipped_pkgs=[], cleansrc_failures=[],
        install_only=False, pacman_upgrade_failed=False, skipped=0,
        stage_owned_updates=[],
    )
    assert summary.versions["mesa"] == ("24.0", "24.1")


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


def test_print_summary_colours_verdicts_by_action(capsys):
    """2.6.1-F29: the action tag carries the verdict's colour so the handful of
    packages actually eligible to rebuild stand out from the wall that are not."""
    from sysforge import log
    from sysforge.update import _print_summary

    args = SimpleNamespace(verbose=1)
    saved = log._COLOR_MODE
    try:
        log.set_color_mode("always")
        _print_summary(_make_summary_results(), args)
    finally:
        log.set_color_mode(saved)
    captured = capsys.readouterr().out

    assert "\033[32m[NEEDS_REBUILD]\033[0m" in captured   # green: act on this
    assert "\033[33m[UP_TO_DATE]\033[0m" in captured      # yellow: fine, skipped
    assert "\033[33m[DEVEL]\033[0m" in captured
    assert "\033[31m[RATE_LIMITED]\033[0m" in captured    # red: check failed


def test_print_summary_colours_a_downgrade_red(capsys):
    from sysforge import log
    from sysforge.update import _print_summary, _UpdateResult

    results = [
        _UpdateResult("bat", ["bat"], "DOWNGRADE", "0.25.0-1", "0.24.0-1",
                      Path("/tmp/bat/PKGBUILD")),
    ]
    args = SimpleNamespace(verbose=1)
    saved = log._COLOR_MODE
    try:
        log.set_color_mode("always")
        _print_summary(results, args)
    finally:
        log.set_color_mode(saved)

    assert "\033[31m[DOWNGRADE]\033[0m" in capsys.readouterr().out


def test_print_summary_plain_output_is_byte_for_byte_unchanged(capsys):
    """NO_COLOR must yield exactly the pre-F29 output — colour is presentation
    only and changes neither the action taxonomy nor the gutter alignment."""
    from sysforge import log
    from sysforge.update import _print_summary

    args = SimpleNamespace(verbose=1)
    saved = log._COLOR_MODE
    try:
        log.set_color_mode("never")
        _print_summary(_make_summary_results(), args)
    finally:
        log.set_color_mode(saved)
    captured = capsys.readouterr().out

    assert "\033[" not in captured
    assert "  [NEEDS_REBUILD]  htop: 3.3.0-1 \u2192 3.4.1-1" in captured
    assert "  [UP_TO_DATE]     neovim: 0.9.5-1" in captured


def test_print_summary_colour_preserves_gutter_alignment(capsys):
    """The padding is emitted outside the SGR run, so columns line up on visible
    width rather than byte length."""
    from sysforge import log
    from sysforge.update import _print_summary

    args = SimpleNamespace(verbose=1)
    for mode in ("never", "always"):
        saved = log._COLOR_MODE
        try:
            log.set_color_mode(mode)
            _print_summary(_make_summary_results(), args)
        finally:
            log.set_color_mode(saved)
        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if "htop" in ln)
        # Strip any SGR runs, then the visible column must be identical.
        visible = re.sub(r"\033\[[0-9;]*m", "", line)
        assert visible.index("htop") == len("  [NEEDS_REBUILD]  ")


def test_print_summary_unknown_action_colour_degrades_to_plain(capsys):
    """An action absent from _ACTION_COLORS prints plain rather than raising."""
    from sysforge import log
    from sysforge.primitives import render

    saved = log._COLOR_MODE
    try:
        log.set_color_mode("always")
        assert "\033[" not in render.tag_header("MYSTERY", color=None)
        assert "\033[" not in render.tag_header("MYSTERY", color="chartreuse")
    finally:
        log.set_color_mode(saved)


def test_action_colours_cover_every_action_format():
    """The two dicts are keyed by the same action strings; a new action that
    lands in _ACTION_FORMATS without a colour is a drift this catches."""
    from sysforge.update_summary import _ACTION_COLORS, _ACTION_FORMATS

    assert set(_ACTION_COLORS) == set(_ACTION_FORMATS)


def test_print_summary_frozen_always_shown_and_counted(capsys):
    """Important-4 (3.0.0-F2): a FROZEN result must not be silently dropped.

    Before the fix, ``_ACTION_FORMATS`` had no "FROZEN" entry, so the
    package vanished from both the totals header and the per-package lines
    — even under ``-v`` — leaving a refusal that prints nothing.
    """
    from sysforge.update import _UpdateResult, _print_summary
    results = _make_summary_results() + [
        _UpdateResult("mesa", ["mesa"], "FROZEN", None, None,
                      Path("/tmp/mesa/PKGBUILD")),
    ]
    args = SimpleNamespace(verbose=0)
    _print_summary(results, args)
    captured = capsys.readouterr().out
    assert "1 source freeze" in captured
    assert "[FROZEN]" in captured
    assert "mesa" in captured


# ---------------------------------------------------------------------------
# repo_mode = "build_from_source" → pacman-class fast path
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
    update_scenario.set_repo_mode("build_from_source")
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
    update_scenario.set_repo_mode("build_from_source")
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
    update_scenario.set_repo_mode("build_from_source")
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
    """repo_mode=build_from_source + a behavior-changing override (enable_build_from_source) →
    source path (real PKGBUILD parse + vercmp), NOT the pacman fast path:
    checkupdates is never consulted for a source-class package."""
    update_scenario.set_repo_mode("build_from_source")
    update_scenario.add_override("firefox", source="repo", enable_build_from_source=True)
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
    update_scenario.set_repo_mode("build_from_source")
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
# 3.0.0-F4: the trailing pacman -Syu as a standalone update-run option
# ---------------------------------------------------------------------------

def _up_to_date_scenario(scenario):
    """A run with one in-scope AUR package that needs nothing — the shape that
    otherwise takes the early "Nothing to rebuild." exit."""
    scenario.add_pkg("htop", "pkgname=htop\npkgver=3.3.0\npkgrel=1\n")
    return dict(installed={"htop": "3.3.0-1"}, foreign={"htop": "3.3.0-1"})


def test_sysupgrade_flag_fires_syu_without_pacman_class(update_scenario, capsys):
    """--sysupgrade runs the trailing -Syu on a default (repo_mode = "pacman")
    install, where no result ever carries NEEDS_PACMAN_UPGRADE."""
    kw = _up_to_date_scenario(update_scenario)
    update_scenario.fake_sync()
    update_scenario.run(_make_args(offline=False, sysupgrade=True), **kw)
    assert _syu_fired(update_scenario)
    # Flag-only route: pacman does its own resolution, no walk widening.
    assert not _checkupdates_called(update_scenario)
    assert update_scenario.exit_code == 0


def test_sysupgrade_config_default_fires(update_scenario):
    """[build] system_upgrade = true alone enables the trailing -Syu."""
    kw = _up_to_date_scenario(update_scenario)
    update_scenario.set_build_key("system_upgrade", True)
    update_scenario.fake_sync()
    update_scenario.run(_make_args(offline=False), **kw)
    assert _syu_fired(update_scenario)


def test_no_sysupgrade_flag_beats_config_default(update_scenario):
    """--no-sysupgrade wins over [build] system_upgrade = true (CLI precedence)."""
    kw = _up_to_date_scenario(update_scenario)
    update_scenario.set_build_key("system_upgrade", True)
    update_scenario.fake_sync()
    update_scenario.run(_make_args(offline=False, no_sysupgrade=True), **kw)
    assert not _syu_fired(update_scenario)


def test_sysupgrade_default_off(update_scenario):
    """Neither flag nor config → today's behaviour: no trailing -Syu."""
    kw = _up_to_date_scenario(update_scenario)
    update_scenario.fake_sync()
    update_scenario.run(_make_args(offline=False), **kw)
    assert not _syu_fired(update_scenario)


def test_sysupgrade_suppressed_when_offline(update_scenario, capsys):
    """--offline outranks --sysupgrade — no network transaction is dispatched."""
    kw = _up_to_date_scenario(update_scenario)
    update_scenario.fake_sync()
    update_scenario.run(_make_args(offline=True, sysupgrade=True), **kw)
    assert not _syu_fired(update_scenario)
    assert "Nothing to rebuild" in "".join(capsys.readouterr())


def test_sysupgrade_failure_sets_exit_code(update_scenario):
    """A failing flag-triggered -Syu is a run failure, same as the classified
    route (3.0.0-B4 exit-code discipline)."""
    kw = _up_to_date_scenario(update_scenario)
    update_scenario.fake_sync()
    update_scenario.fake_run.respond("pacman -Syu", returncode=1)
    update_scenario.run(_make_args(offline=False, sysupgrade=True), **kw)
    assert _syu_fired(update_scenario)
    assert update_scenario.exit_code != 0


# ---------------------------------------------------------------------------
# Stage-owned packages — kernel ownership filter
# ---------------------------------------------------------------------------

def test_kernel_owned_package_skipped_by_default(update_scenario, capsys):
    """linux-custom matched via kernel.toml bootstrap is skipped + info-logged.

    The package is set up as a would-rebuild (installed 6.12 < PKGBUILD 6.13),
    so an empty build list unambiguously proves the stage-owned skip rather than
    an up-to-date no-op.
    """
    update_scenario.add_pkg("linux-custom", "pkgname=linux-custom\npkgver=6.13\npkgrel=1\n")
    kernel_path = update_scenario.src_root.parent / "kernel.toml"
    kernel_path.write_text(
        'enabled = true\npkgname = "linux-custom"\n'
        f'pkgbuild_src_dir = "{update_scenario.src_root}"\n')
    with patch("sysforge.primitives.stage_ownership.KERNEL_PATH", kernel_path):
        builds = update_scenario.run(
            _make_args(),
            installed={"linux-custom": "6.12-1"},
            foreign={"linux-custom": "6.12-1"},
        )

    assert builds == []  # stage-owned → skipped (the would-rebuild package never built)
    captured = capsys.readouterr()
    assert "kernel-stage package" in captured.err
    assert "linux-custom" in captured.err


def test_kernel_owned_via_build_state_marker_skipped(update_scenario):
    """The owner_stage marker in build_state is honored even without kernel.toml."""
    update_scenario.add_pkg("linux-custom", "pkgname=linux-custom\npkgver=6.13\npkgrel=1\n")
    # The kernel stage stamped owner_stage="kernel"; no kernel.toml bootstrap.
    update_scenario.record("linux-custom", "6.13", "1",
                           source="local", owner_stage="kernel")
    with patch("sysforge.primitives.stage_ownership.KERNEL_PATH",
               update_scenario.src_root.parent / "nope.toml"):
        builds = update_scenario.run(
            _make_args(),
            installed={"linux-custom": "6.12-1"},
            foreign={"linux-custom": "6.12-1"},
        )

    assert builds == []  # build_state marker → skipped


def test_include_stage_owned_flag_includes_kernel_package(update_scenario):
    """--include-stage-owned overrides the skip → the package builds."""
    update_scenario.add_pkg("linux-custom", "pkgname=linux-custom\npkgver=6.13\npkgrel=1\n")
    update_scenario.record("linux-custom", "6.13", "1",
                           source="local", owner_stage="kernel")
    with patch("sysforge.primitives.stage_ownership.KERNEL_PATH",
               update_scenario.src_root.parent / "nope.toml"):
        builds = update_scenario.run(
            _make_args(include_stage_owned=True),
            installed={"linux-custom": "6.12-1"},
            foreign={"linux-custom": "6.12-1"},
        )

    assert len(builds) == 1  # skip overridden → the would-rebuild package builds


def test_explicit_pkgname_overrides_stage_owned_skip(update_scenario):
    """Naming a stage-owned package on the CLI opts it back in for that run."""
    update_scenario.add_pkg("linux-custom", "pkgname=linux-custom\npkgver=6.13\npkgrel=1\n")
    update_scenario.record("linux-custom", "6.13", "1",
                           source="local", owner_stage="kernel")
    with patch("sysforge.primitives.stage_ownership.KERNEL_PATH",
               update_scenario.src_root.parent / "nope.toml"):
        builds = update_scenario.run(
            _make_args(pkgnames=["linux-custom"]),
            installed={"linux-custom": "6.12-1"},
            foreign={"linux-custom": "6.12-1"},
        )

    assert len(builds) == 1  # explicit pkgname opts back in → builds


# ---------------------------------------------------------------------------
# Stage-owned packages — toolchain ownership filter (LLVM suite)
# ---------------------------------------------------------------------------

def test_toolchain_owned_llvm_skipped_by_default(update_scenario, capsys):
    """An LLVM-suite package matched via the toolchain.toml (enabled + llvm)
    bootstrap fallback is skipped + info-logged, even with no owner_stage stamp.

    Would-rebuild (installed 22.1.5 < PKGBUILD 22.1.6) so an empty build list
    proves the skip.
    """
    update_scenario.add_pkg("llvm", "pkgname=llvm\npkgver=22.1.6\npkgrel=1\n")
    toolchain_path = update_scenario.src_root.parent / "toolchain.toml"
    toolchain_path.write_text('enabled = true\ncompiler = "llvm"\n')
    with (
        patch("sysforge.primitives.stage_ownership.KERNEL_PATH",
              update_scenario.src_root.parent / "nope-kernel.toml"),
        patch("sysforge.primitives.stage_ownership.TOOLCHAIN_PATH", toolchain_path),
    ):
        builds = update_scenario.run(
            _make_args(),
            installed={"llvm": "22.1.5-1"},
            foreign={"llvm": "22.1.5-1"},
        )

    assert builds == []  # toolchain-owned → skipped
    captured = capsys.readouterr()
    assert "toolchain-stage package" in captured.err
    assert "run `sysforge run toolchain`" in captured.err


def test_toolchain_owned_via_build_state_marker_skipped(update_scenario):
    """The owner_stage="toolchain" marker is honored even without toolchain.toml."""
    update_scenario.add_pkg("llvm", "pkgname=llvm\npkgver=22.1.6\npkgrel=1\n")
    update_scenario.record("llvm", "22.1.6", "1", owner_stage="toolchain")
    with (
        patch("sysforge.primitives.stage_ownership.KERNEL_PATH",
              update_scenario.src_root.parent / "nope-kernel.toml"),
        # Nonexistent toolchain.toml so the bootstrap fallback is inactive —
        # only the build_state marker should drive the skip.
        patch("sysforge.primitives.stage_ownership.TOOLCHAIN_PATH",
              update_scenario.src_root.parent / "nope-toolchain.toml"),
    ):
        builds = update_scenario.run(
            _make_args(),
            installed={"llvm": "22.1.5-1"},
            foreign={"llvm": "22.1.5-1"},
        )

    assert builds == []  # build_state owner_stage marker → skipped


def test_toolchain_gcc_compiler_does_not_skip_llvm(update_scenario):
    """Dual-toolchain parity: with toolchain.toml compiler="gcc" the fallback is
    inactive (register-only path owns no LLVM), so the LLVM package is NOT
    skipped — it flows through to the build set."""
    update_scenario.add_pkg("llvm", "pkgname=llvm\npkgver=22.1.6\npkgrel=1\n")
    toolchain_path = update_scenario.src_root.parent / "toolchain.toml"
    toolchain_path.write_text('enabled = true\ncompiler = "gcc"\n')
    with (
        patch("sysforge.primitives.stage_ownership.KERNEL_PATH",
              update_scenario.src_root.parent / "nope-kernel.toml"),
        patch("sysforge.primitives.stage_ownership.TOOLCHAIN_PATH", toolchain_path),
    ):
        builds = update_scenario.run(
            _make_args(),
            installed={"llvm": "22.1.5-1"},
            foreign={"llvm": "22.1.5-1"},
        )

    assert len(builds) == 1  # gcc toolchain owns no LLVM → not skipped → builds


def test_include_stage_owned_includes_toolchain_llvm(update_scenario):
    """--include-stage-owned overrides the toolchain skip (compiler=llvm)."""
    update_scenario.add_pkg("llvm", "pkgname=llvm\npkgver=22.1.6\npkgrel=1\n")
    toolchain_path = update_scenario.src_root.parent / "toolchain.toml"
    toolchain_path.write_text('enabled = true\ncompiler = "llvm"\n')
    with (
        patch("sysforge.primitives.stage_ownership.KERNEL_PATH",
              update_scenario.src_root.parent / "nope-kernel.toml"),
        patch("sysforge.primitives.stage_ownership.TOOLCHAIN_PATH", toolchain_path),
    ):
        builds = update_scenario.run(
            _make_args(include_stage_owned=True),
            installed={"llvm": "22.1.5-1"},
            foreign={"llvm": "22.1.5-1"},
        )

    assert len(builds) == 1  # skip overridden → builds


def test_explicit_pkgname_overrides_toolchain_skip(update_scenario):
    """Naming the LLVM package on the CLI opts it back in for that run."""
    update_scenario.add_pkg("llvm", "pkgname=llvm\npkgver=22.1.6\npkgrel=1\n")
    toolchain_path = update_scenario.src_root.parent / "toolchain.toml"
    toolchain_path.write_text('enabled = true\ncompiler = "llvm"\n')
    with (
        patch("sysforge.primitives.stage_ownership.KERNEL_PATH",
              update_scenario.src_root.parent / "nope-kernel.toml"),
        patch("sysforge.primitives.stage_ownership.TOOLCHAIN_PATH", toolchain_path),
    ):
        builds = update_scenario.run(
            _make_args(pkgnames=["llvm"]),
            installed={"llvm": "22.1.5-1"},
            foreign={"llvm": "22.1.5-1"},
        )

    assert len(builds) == 1  # explicit pkgname opts back in → builds


def test_toolchain_owned_spirv_skipped_via_configured_list(update_scenario, capsys):
    """spirv-llvm-translator is NOT matched by is_llvm_pkgbase (prefix set), so
    only the toolchain.toml [packages] configured-set union skips it. This pins
    the ownership broadening: a configured-but-unmatched member is skipped."""
    from sysforge.primitives.pkgbuild_patcher import is_llvm_pkgbase

    pkgbase = "spirv-llvm-translator"
    assert not is_llvm_pkgbase(pkgbase)  # the gap the broadening closes

    update_scenario.add_pkg(pkgbase, f"pkgname={pkgbase}\npkgver=19.1.5\npkgrel=1\n")
    # toolchain.toml owns LLVM (enabled + llvm) and lists spirv in non_pgo.
    toolchain_path = update_scenario.src_root.parent / "toolchain.toml"
    toolchain_path.write_text(
        'enabled = true\ncompiler = "llvm"\n'
        '[packages]\npgo = ["llvm"]\n'
        'non_pgo = ["clang", "spirv-llvm-translator"]\nlib32 = []\n'
    )
    with (
        patch("sysforge.primitives.stage_ownership.KERNEL_PATH",
              update_scenario.src_root.parent / "nope-kernel.toml"),
        patch("sysforge.primitives.stage_ownership.TOOLCHAIN_PATH", toolchain_path),
    ):
        builds = update_scenario.run(
            _make_args(),
            installed={pkgbase: "19.1.4-1"},
            foreign={pkgbase: "19.1.4-1"},
        )

    assert builds == []  # configured-list ownership → skipped
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


def test_update_emits_restart_line_when_stale(monkeypatch):
    from sysforge import update
    from sysforge.primitives import restart_probe as rp

    rep = rp.StaleReport(
        entries=[rp.StaleEntry(pid=100, comm="cosmic-comp", tier=rp.TIER_RELOGIN,
                               package="cosmic-comp", path="/usr/bin/cosmic-comp")],
        highest_tier=rp.TIER_RELOGIN, partial=False)
    monkeypatch.setattr(update.restart_probe, "scan_stale_processes", lambda **kw: rep)

    lines = []
    update._emit_restart_notice(emit=lines.append)
    assert len(lines) == 1
    assert "cosmic-comp" in lines[0]
    assert "log out" in lines[0]


def test_update_restart_notice_names_kernel_not_some_packages(monkeypatch):
    """The kernel entry deliberately carries package=None; the notice must
    name the running kernel explicitly rather than degrading to the
    "some packages" fallback that hides the single highest-value message."""
    from sysforge import update
    from sysforge.primitives import restart_probe as rp

    rep = rp.StaleReport(
        entries=[rp.StaleEntry(pid=0, comm="kernel", tier=rp.TIER_REBOOT,
                               package=None, path="/usr/lib/modules/6.19.1-arch1-1")],
        highest_tier=rp.TIER_REBOOT, partial=False)
    monkeypatch.setattr(update.restart_probe, "scan_stale_processes", lambda **kw: rep)

    lines = []
    update._emit_restart_notice(emit=lines.append)
    assert len(lines) == 1
    assert "the running kernel" in lines[0]
    assert "some packages" not in lines[0]


def test_update_restart_notice_falls_back_when_package_unknown(monkeypatch):
    """A non-kernel entry with no owning package still falls back to "some
    packages" — only the kernel entry gets the special-cased wording."""
    from sysforge import update
    from sysforge.primitives import restart_probe as rp

    rep = rp.StaleReport(
        entries=[rp.StaleEntry(pid=1, comm="systemd", tier=rp.TIER_REBOOT,
                               package=None, path="/usr/lib/systemd/systemd")],
        highest_tier=rp.TIER_REBOOT, partial=False)
    monkeypatch.setattr(update.restart_probe, "scan_stale_processes", lambda **kw: rep)

    lines = []
    update._emit_restart_notice(emit=lines.append)
    assert len(lines) == 1
    assert "some packages" in lines[0]
    assert "the running kernel" not in lines[0]


def test_update_silent_when_nothing_stale(monkeypatch):
    from sysforge import update
    from sysforge.primitives import restart_probe as rp

    rep = rp.StaleReport(entries=[], highest_tier=None, partial=False)
    monkeypatch.setattr(update.restart_probe, "scan_stale_processes", lambda **kw: rep)

    lines = []
    update._emit_restart_notice(emit=lines.append)
    assert lines == []


def test_update_restart_notice_never_raises(monkeypatch):
    # A probe failure must not fail the update that already succeeded.
    from sysforge import update

    def boom(**kw):
        raise OSError("procfs unavailable")

    monkeypatch.setattr(update.restart_probe, "scan_stale_processes", boom)
    lines = []
    update._emit_restart_notice(emit=lines.append)
    assert lines == []


# ---------------------------------------------------------------------------
# Drift advisory wording (3.1.0-B2)
# ---------------------------------------------------------------------------

def _ui_lines(monkeypatch):
    """Collect _log.ui() output — the advisory is UI, not stdout."""
    from sysforge import log as _log_mod
    seen: list[str] = []
    monkeypatch.setattr(_log_mod, "ui", lambda tag, msg: seen.append(str(msg)))
    return seen


def test_flag_drift_advisory_omits_hint_when_rebuild_enabled(
        update_scenario, monkeypatch):
    """Telling the user to pass --rebuild-on-flag-drift in the same run that
    already rebuilt on it is what made same-version rebuilds read as spurious
    reinstalls (3.1.0-B2)."""
    installed, foreign = _seed_flag_drift(update_scenario)
    seen = _ui_lines(monkeypatch)
    update_scenario.run(
        _make_args(rebuild_on_flag_drift=True, no_toolchain_preflight=True),
        installed=installed, foreign=foreign,
    )
    out = "\n".join(seen)
    assert "flag drift:" in out
    assert "Pass --rebuild-on-flag-drift" not in out
    assert "rebuilding (--rebuild-on-flag-drift)" in out


def test_flag_drift_advisory_keeps_hint_when_rebuild_disabled(
        update_scenario, monkeypatch):
    installed, foreign = _seed_flag_drift(update_scenario)
    seen = _ui_lines(monkeypatch)
    update_scenario.run(
        _make_args(no_toolchain_preflight=True),
        installed=installed, foreign=foreign,
    )
    out = "\n".join(seen)
    assert "Pass --rebuild-on-flag-drift to rebuild" in out


# ---------------------------------------------------------------------------
# 3.0.0-F5: version-change report for the flag-triggered pacman -Syu
# ---------------------------------------------------------------------------

def _snapshot_around_syu(scenario, monkeypatch, before, after):
    """Stub the local-DB read so it flips once the -Syu transaction has run."""
    def _fake():
        return dict(after) if _syu_fired(scenario) else dict(before)
    monkeypatch.setattr("sysforge.update.get_all_installed_packages", _fake)


def test_sysupgrade_report_itemizes_version_changes(
    update_scenario, monkeypatch, capsys
):
    """The flag route now reports what pacman actually did, read back off the
    local DB either side of the transaction."""
    kw = _up_to_date_scenario(update_scenario)
    update_scenario.fake_sync()
    _snapshot_around_syu(
        update_scenario, monkeypatch,
        {"htop": "3.3.0-1", "mesa": "24.0-1"},
        {"htop": "3.3.0-1", "mesa": "24.1-1"},
    )
    update_scenario.run(_make_args(offline=False, sysupgrade=True), **kw)
    out = "".join(capsys.readouterr())
    assert "mesa: 24.0-1" in out and "24.1-1" in out
    assert "pacman resolved the transaction" not in out


def test_sysupgrade_report_opts_out_with_flag(update_scenario, monkeypatch, capsys):
    """--no-sysupgrade-report keeps the transaction and drops only the report,
    falling back to the opaque single line."""
    kw = _up_to_date_scenario(update_scenario)
    update_scenario.fake_sync()
    _snapshot_around_syu(
        update_scenario, monkeypatch,
        {"htop": "3.3.0-1", "mesa": "24.0-1"},
        {"htop": "3.3.0-1", "mesa": "24.1-1"},
    )
    update_scenario.run(
        _make_args(offline=False, sysupgrade=True, no_sysupgrade_report=True), **kw
    )
    assert _syu_fired(update_scenario)
    out = "".join(capsys.readouterr())
    assert "system upgrade (pacman resolved the transaction)" in out
    assert "mesa: 24.0-1" not in out


def test_sysupgrade_report_survives_a_probe_failure(
    update_scenario, monkeypatch, capsys
):
    """The capture is reporting-only: a failing local-DB read must never turn a
    successful upgrade into a failed run."""
    kw = _up_to_date_scenario(update_scenario)
    update_scenario.fake_sync()

    calls = {"n": 0}

    def _boom():
        # The version-check walk reads the install set first; only the report's
        # own snapshots fail, so the run itself is unaffected.
        calls["n"] += 1
        if calls["n"] == 1:
            return {"htop": "3.3.0-1"}
        raise RuntimeError("local db unreadable")
    monkeypatch.setattr("sysforge.update.get_all_installed_packages", _boom)
    update_scenario.run(_make_args(offline=False, sysupgrade=True), **kw)
    assert _syu_fired(update_scenario)
    assert update_scenario.exit_code == 0
    assert "system upgrade (pacman resolved the transaction)" in "".join(
        capsys.readouterr()
    )
