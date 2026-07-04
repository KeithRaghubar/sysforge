"""
test_llvm_state.py — unit tests for sysforge.primitives.llvm_state

Covers:
    is_llvm_in_scope            — pattern filter
    _classify_origin            — repo (gitlab + sentinel), aur, user, missing
    collect_llvm_state          — full snapshot for a synthetic tree, with
                                  pacman / build_mode / scheduler-cache mocks
    offline / probe_fetch       — probe_fetch=False does NOT shell out to
                                  git fetch (asserts via monkeypatch)
    evaluate_strict             — dirty + diverged + pgo-mismatch blockers,
                                  allow_dirty override, profdata-mismatch is
                                  not suppressible
    render_preflight            — header + per-package line + blockers block
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sysforge.primitives import llvm_state
from sysforge.primitives.llvm_state import (
    LlvmPackageState,
    LlvmPreflightReport,
    collect_llvm_state,
    evaluate_strict,
    is_llvm_in_scope,
    render_preflight,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_repo(
    path: Path, *, remote_url: str | None, with_tracking: bool = True,
) -> None:
    """Create a real git repo with one commit, an upstream tracking branch, and
    a (cosmetic) origin URL.

    ``with_tracking=False`` skips the upstream setup so the repo is treated as
    "no upstream tracking branch" by ``git_is_dirty``.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
    )
    (path / "PKGBUILD").write_text(
        "pkgname=llvm\npkgver=20.0.0\npkgrel=1\narch=('x86_64')\n"
    )
    subprocess.run(["git", "-C", str(path), "add", "PKGBUILD"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True,
    )

    if with_tracking and remote_url is not None:
        # Stand up a sibling bare repo so the working tree has a real upstream
        # to track (git_is_dirty treats "no upstream" as dirty by definition).
        bare = path.parent / f"{path.name}.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", str(bare)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "push", "-q", "-u", "origin", "main"],
            check=True,
        )
        # Rewrite origin URL to the cosmetic value the test wants to verify.
        subprocess.run(
            ["git", "-C", str(path), "remote", "set-url", "origin", remote_url],
            check=True,
        )
    elif remote_url is not None:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", remote_url],
            check=True,
        )


@pytest.fixture
def src_root(tmp_path):
    """A pkgbuild_src_dir-style directory."""
    root = tmp_path / "src"
    root.mkdir()
    return root


@pytest.fixture
def config(src_root):
    return {
        "paths": {"pkgbuild_src_dir": str(src_root)},
        "rules": [],
        "profiles": {},
        "defaults": {},
    }


@pytest.fixture(autouse=True)
def _stub_pacman(monkeypatch):
    """Default: nothing is installed. Tests can override per-call."""
    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_foreign_packages", lambda: {}
    )
    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_installed_version", lambda _name: None
    )


@pytest.fixture(autouse=True)
def _stub_scheduler(monkeypatch):
    """Default: empty SourceMetaCache so divergence falls through to ``unknown``."""
    class _FakeCache:
        def all(self):
            return {}

    class _FakeScheduler:
        cache = _FakeCache()

    monkeypatch.setattr(
        "sysforge.primitives.source_sync.get_scheduler", lambda **_: _FakeScheduler()
    )


# ---------------------------------------------------------------------------
# is_llvm_in_scope
# ---------------------------------------------------------------------------

def test_is_llvm_in_scope_filters_non_llvm():
    names = ["mesa-git", "llvm", "llvm-git", "linux-firmware", "clang", "lld"]
    assert is_llvm_in_scope(names) == ["llvm", "llvm-git", "clang", "lld"]


def test_is_llvm_in_scope_handles_lib32():
    assert "lib32-llvm" in is_llvm_in_scope(["lib32-llvm", "lib32-mesa"])
    assert "lib32-mesa" not in is_llvm_in_scope(["lib32-llvm", "lib32-mesa"])


def test_is_llvm_in_scope_handles_minimal_git_variant():
    # The pattern matcher uses startswith(prefix + "-") so "llvm-minimal-git"
    # qualifies as an llvm variant.
    assert is_llvm_in_scope(["llvm-minimal-git"]) == ["llvm-minimal-git"]


# ---------------------------------------------------------------------------
# _classify_origin
# ---------------------------------------------------------------------------

def test_classify_origin_aur(src_root):
    pkg = src_root / "llvm-git"
    _init_repo(pkg, remote_url="https://aur.archlinux.org/llvm-git.git")
    origin, url = llvm_state._classify_origin(pkg)
    assert origin == "aur"
    assert url and "aur.archlinux.org" in url


def test_classify_origin_repo_via_url(src_root):
    pkg = src_root / "llvm"
    _init_repo(pkg, remote_url="https://gitlab.archlinux.org/archlinux/packaging/packages/llvm.git")
    origin, _url = llvm_state._classify_origin(pkg)
    assert origin == "repo"


def test_classify_origin_repo_via_sentinel(src_root):
    pkg = src_root / "llvm"
    _init_repo(pkg, remote_url="https://example.org/some/fork.git")
    (pkg / ".git" / "pkgctl-source").write_text("gitlab.archlinux.org\n")
    origin, _url = llvm_state._classify_origin(pkg)
    assert origin == "repo"  # sentinel wins over URL classification


def test_classify_origin_user_custom_remote(src_root):
    pkg = src_root / "llvm"
    _init_repo(pkg, remote_url="git@github.com:example/llvm-fork.git")
    origin, _url = llvm_state._classify_origin(pkg)
    assert origin == "user"


def test_classify_origin_user_no_remote(src_root):
    pkg = src_root / "llvm"
    _init_repo(pkg, remote_url=None, with_tracking=False)
    origin, url = llvm_state._classify_origin(pkg)
    assert origin == "user"
    assert url is None


def test_classify_origin_missing(tmp_path):
    origin, url = llvm_state._classify_origin(tmp_path / "does-not-exist")
    assert origin == "missing"
    assert url is None


# ---------------------------------------------------------------------------
# collect_llvm_state
# ---------------------------------------------------------------------------

def test_collect_state_missing_tree(config):
    """Names with no on-disk tree report origin=missing, divergence=missing."""
    report = collect_llvm_state(["llvm-git"], config)
    assert len(report.states) == 1
    s = report.states[0]
    assert s.source_origin == "missing"
    assert s.divergence == "missing"
    assert s.is_dirty is False
    assert s.install_origin == "not_installed"


def test_collect_state_clean_aur_tree(src_root, config):
    pkg = src_root / "llvm-git"
    _init_repo(pkg, remote_url="https://aur.archlinux.org/llvm-git.git")
    report = collect_llvm_state(["llvm-git"], config)
    s = report.states[0]
    assert s.source_origin == "aur"
    assert s.is_dirty is False
    assert s.divergence == "unknown"  # no cache entry, no probe
    assert s.pkgbuild_ver == "20.0.0-1"
    assert report.has_dirty is False
    assert report.has_diverged is False


def test_collect_state_dirty_uncommitted_changes(src_root, config):
    pkg = src_root / "llvm-git"
    _init_repo(pkg, remote_url="https://aur.archlinux.org/llvm-git.git")
    (pkg / "PKGBUILD").write_text(
        "pkgname=llvm-git\npkgver=20.0.0.r1\npkgrel=1\narch=('x86_64')\n"
    )
    report = collect_llvm_state(["llvm-git"], config)
    s = report.states[0]
    assert s.is_dirty is True
    assert s.dirty_reason == "uncommitted changes"
    assert report.has_dirty is True


def test_collect_state_dirty_no_upstream(src_root, config):
    pkg = src_root / "llvm-git"
    _init_repo(pkg, remote_url=None, with_tracking=False)  # no remote at all
    report = collect_llvm_state(["llvm-git"], config)
    s = report.states[0]
    assert s.is_dirty is True
    assert "upstream" in (s.dirty_reason or "")


def test_collect_state_detached_head_on_upstream_is_clean(src_root, config):
    """A source=repo checkout pinned to a release tag sits on a detached HEAD
    (no ``@{u}`` → ``no_tracking``) but its commit is still reachable from
    ``origin/main`` — that is upstream's own history, not local work, so it
    must NOT be reported as a dirty blocker.
    """
    pkg = src_root / "llvm"
    _init_repo(pkg, remote_url="https://aur.archlinux.org/llvm.git")
    # Detach HEAD onto the same commit that origin/main points at.
    subprocess.run(
        ["git", "-C", str(pkg), "checkout", "-q", "--detach", "HEAD"],
        check=True,
    )
    report = collect_llvm_state(["llvm"], config)
    s = report.states[0]
    assert s.is_dirty is False, s.dirty_reason
    assert report.has_dirty is False


def test_collect_state_offline_does_not_fetch(src_root, config, monkeypatch):
    """probe_fetch=False must NOT call git_fetch_and_compare."""
    pkg = src_root / "llvm-git"
    _init_repo(pkg, remote_url="https://aur.archlinux.org/llvm-git.git")

    calls: list = []

    def _boom(*_args, **_kwargs):
        calls.append(_args)
        raise AssertionError("git_fetch_and_compare must not be called when probe_fetch=False")

    monkeypatch.setattr(llvm_state, "git_fetch_and_compare", _boom)
    report = collect_llvm_state(["llvm-git"], config, probe_fetch=False)
    assert calls == []
    assert report.states[0].divergence == "unknown"


def test_collect_state_uses_cache_for_divergence(src_root, config, monkeypatch):
    pkg = src_root / "llvm-git"
    _init_repo(pkg, remote_url="https://aur.archlinux.org/llvm-git.git")

    head = subprocess.run(
        ["git", "-C", str(pkg), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    class _FakeCache:
        def all(self):
            return {"llvm-git": {"head_commit": head}}

    class _FakeScheduler:
        cache = _FakeCache()

    monkeypatch.setattr(
        "sysforge.primitives.source_sync.get_scheduler", lambda **_: _FakeScheduler()
    )

    report = collect_llvm_state(["llvm-git"], config)
    assert report.states[0].divergence == "up_to_date"


def test_collect_state_records_install_origin(src_root, config, monkeypatch):
    pkg = src_root / "llvm"
    _init_repo(pkg, remote_url="https://gitlab.archlinux.org/archlinux/packaging/packages/llvm.git")

    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_foreign_packages", lambda: {}
    )
    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_installed_version",
        lambda name: "20.0.0-1" if name == "llvm" else None,
    )

    report = collect_llvm_state(["llvm"], config)
    s = report.states[0]
    assert s.install_origin == "repo"
    assert s.installed_ver == "20.0.0-1"


def test_collect_state_records_foreign_install(src_root, config, monkeypatch):
    pkg = src_root / "llvm-git"
    _init_repo(pkg, remote_url="https://aur.archlinux.org/llvm-git.git")
    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_foreign_packages",
        lambda: {"llvm-git": "20.0.0.r1-1"},
    )

    report = collect_llvm_state(["llvm-git"], config)
    s = report.states[0]
    assert s.install_origin == "foreign"
    assert s.installed_ver == "20.0.0.r1-1"


def test_collect_state_skips_non_llvm():
    report = collect_llvm_state(["mesa-git", "linux-firmware"], config={})
    assert report.states == ()
    assert report.blockers == ()


# ---------------------------------------------------------------------------
# evaluate_strict
# ---------------------------------------------------------------------------

def _state(**kwargs) -> LlvmPackageState:
    base = dict(
        pkgbase="llvm-git",
        pkgbuild_dir=None,
        variant="llvm-git",
        source_origin="aur",
        remote_url=None,
        is_dirty=False,
        dirty_reason=None,
        divergence="up_to_date",
        head_short=None,
        upstream_short=None,
        install_origin="foreign",
        installed_ver="20.0.0-1",
        pkgbuild_ver="20.0.0-1",
        build_mode=None,
        pgo_profdata_mismatch=False,
    )
    base.update(kwargs)
    return LlvmPackageState(**base)


def _report(*states) -> LlvmPreflightReport:
    return LlvmPreflightReport(
        states=tuple(states),
        blockers=(),
        has_dirty=any(s.is_dirty for s in states),
        has_diverged=any(s.divergence == "diverged" for s in states),
        has_pgo_profdata_mismatch=any(s.pgo_profdata_mismatch for s in states),
    )


def test_evaluate_strict_clean_passes():
    assert evaluate_strict(_report(_state())) == []


def test_evaluate_strict_dirty_blocks():
    blockers = evaluate_strict(_report(_state(is_dirty=True, dirty_reason="uncommitted changes")))
    assert len(blockers) == 1
    assert "dirty" in blockers[0]


def test_evaluate_strict_diverged_blocks():
    blockers = evaluate_strict(
        _report(_state(divergence="diverged", head_short="abc1234567", upstream_short="def1234567"))
    )
    assert len(blockers) == 1
    assert "diverged" in blockers[0]


def test_evaluate_strict_allow_dirty_suppresses_dirty_and_diverged():
    blockers = evaluate_strict(
        _report(
            _state(is_dirty=True, dirty_reason="x"),
            _state(pkgbase="llvm", divergence="diverged"),
        ),
        allow_dirty=True,
    )
    assert blockers == []


def test_evaluate_strict_pgo_mismatch_not_suppressible():
    blockers = evaluate_strict(
        _report(_state(pgo_profdata_mismatch=True)),
        allow_dirty=True,  # MUST NOT suppress profdata-mismatch
    )
    assert len(blockers) == 1
    assert "profdata" in blockers[0]


# ---------------------------------------------------------------------------
# render_preflight
# ---------------------------------------------------------------------------

def test_render_empty_returns_empty_string():
    report = LlvmPreflightReport(
        states=(), blockers=(),
        has_dirty=False, has_diverged=False,
        has_pgo_profdata_mismatch=False,
    )
    assert render_preflight(report) == ""


def test_render_includes_header_and_state_line():
    out = render_preflight(_report(_state()))
    assert "[LLVM]" in out
    assert "LLVM source pre-flight" in out
    assert "llvm-git" in out
    assert "origin=aur" in out
    assert "clean=clean" in out


def test_render_dirty_marks_state_loudly():
    out = render_preflight(_report(_state(is_dirty=True, dirty_reason="2 unpushed commit(s)")))
    assert "DIRTY" in out
    assert "unpushed" in out


def test_render_blockers_block_listed_at_end():
    report = LlvmPreflightReport(
        states=(_state(is_dirty=True, dirty_reason="uncommitted changes"),),
        blockers=("llvm-git: dirty (uncommitted changes)",),
        has_dirty=True, has_diverged=False,
        has_pgo_profdata_mismatch=False,
    )
    out = render_preflight(report)
    assert "blockers:" in out
    assert "llvm-git: dirty (uncommitted changes)" in out


# A repo-origin lib32 package installed from the binary repo with no source
# build_mode: every source-state column is empty/unknown. Nothing the pre-flight
# reports is actionable, so the row is pure noise (1.2.0-Q8).
def _repo_origin_noise_state(**kwargs):
    base = dict(
        pkgbase="lib32-llvm",
        source_origin="repo",
        install_origin="repo",
        is_dirty=False,
        divergence="unknown",
        build_mode=None,
        pgo_profdata_mismatch=False,
    )
    base.update(kwargs)
    return _state(**base)


def test_render_suppresses_nonactionable_repo_origin_rows():
    # 1.2.0-Q8: a repo-origin row with no actionable state is dropped from the
    # rendered block entirely (it would otherwise read as noise).
    out = render_preflight(_report(_repo_origin_noise_state()))
    assert out == ""


def test_render_keeps_actionable_rows_alongside_noise():
    # The actionable row survives; the count reflects only what is shown.
    out = render_preflight(_report(
        _repo_origin_noise_state(),
        _state(pkgbase="llvm-git", is_dirty=True, dirty_reason="2 unpushed commit(s)"),
    ))
    assert "lib32-llvm" not in out
    assert "llvm-git" in out
    assert "(1 package)" in out


def test_render_verbose_shows_all_rows():
    # --verbose is the escape hatch: even non-actionable rows render.
    out = render_preflight(_report(_repo_origin_noise_state()), verbose=True)
    assert "lib32-llvm" in out


def test_render_keeps_repo_origin_when_source_built():
    # A repo-origin clone that sysforge *will* build from source is actionable.
    out = render_preflight(_report(
        _repo_origin_noise_state(build_mode="source_built"),
    ))
    assert "lib32-llvm" in out


def test_render_keeps_diverged_repo_origin():
    out = render_preflight(_report(
        _repo_origin_noise_state(divergence="diverged"),
    ))
    assert "lib32-llvm" in out


# ---------------------------------------------------------------------------
# detect_toolchain_config_mismatch (configured-vs-installed provenance)
# ---------------------------------------------------------------------------

def test_mismatch_gcc_config_returns_nothing():
    """gcc path: no LLVM toolchain configured → no findings, no probe."""
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch

    with patch("sysforge.primitives.llvm_state.collect_llvm_state") as mock_collect:
        result = detect_toolchain_config_mismatch(
            {}, toolchain_cfg={"enabled": True, "compiler": "gcc"})
    assert result == ()
    mock_collect.assert_not_called()


def test_mismatch_disabled_returns_nothing():
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch

    result = detect_toolchain_config_mismatch(
        {}, toolchain_cfg={"enabled": False, "compiler": "llvm"})
    assert result == ()


def test_mismatch_stock_install_flags_error():
    """llvm+pgo configured but stock repo LLVM installed → error finding."""
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch

    report = _report(_state(pkgbase="llvm", install_origin="repo"))
    with patch("sysforge.primitives.llvm_state.collect_llvm_state",
               return_value=report), \
         patch("sysforge.primitives.llvm_state._toolchain_built_packages",
               return_value=set()):
        result = detect_toolchain_config_mismatch(
            {}, toolchain_cfg={"enabled": True, "compiler": "llvm", "pgo": True})
    assert len(result) == 1
    assert result[0].check_id == "toolchain_stock_install"
    assert result[0].severity == "error"
    assert "PGO LLVM" in result[0].message


def test_mismatch_custom_install_is_clean():
    """llvm+pgo configured and a custom (foreign) LLVM installed → no findings."""
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch

    report = _report(_state(pkgbase="llvm", install_origin="foreign"))
    with patch("sysforge.primitives.llvm_state.collect_llvm_state",
               return_value=report):
        result = detect_toolchain_config_mismatch(
            {}, toolchain_cfg={"enabled": True, "compiler": "llvm", "pgo": True})
    assert result == ()


def test_mismatch_profdata_skew_flags_error():
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch

    report = _report(_state(pkgbase="llvm", install_origin="foreign",
                            pgo_profdata_mismatch=True))
    with patch("sysforge.primitives.llvm_state.collect_llvm_state",
               return_value=report):
        result = detect_toolchain_config_mismatch(
            {}, toolchain_cfg={"enabled": True, "compiler": "llvm", "pgo": True})
    assert [f.check_id for f in result] == ["toolchain_pgo_profdata_skew"]


def test_mismatch_skip_build_suppresses_stock_install():
    """skip_build = true registers installed clang as-is — stock LLVM is a
    deliberate choice, not a mismatch; the probe is skipped like the gcc path."""
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch

    report = _report(_state(pkgbase="llvm", install_origin="repo"))
    with patch("sysforge.primitives.llvm_state.collect_llvm_state",
               return_value=report) as mock_collect:
        result = detect_toolchain_config_mismatch(
            {}, toolchain_cfg={"enabled": True, "compiler": "llvm",
                               "pgo": True, "skip_build": True})
    assert result == ()
    mock_collect.assert_not_called()


def test_mismatch_skip_build_suppresses_profdata_skew():
    """skip_build = true also suppresses the profdata-skew finding — the stage
    never rebuilds, so a stale profile is not actionable."""
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch

    report = _report(_state(pkgbase="llvm", install_origin="foreign",
                            pgo_profdata_mismatch=True))
    with patch("sysforge.primitives.llvm_state.collect_llvm_state",
               return_value=report):
        result = detect_toolchain_config_mismatch(
            {}, toolchain_cfg={"enabled": True, "compiler": "llvm",
                               "pgo": True, "skip_build": True})
    assert result == ()


def test_mismatch_collect_failure_is_silent():
    """Provenance reporting must never throw — a failed snapshot → no findings."""
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch

    with patch("sysforge.primitives.llvm_state.collect_llvm_state",
               side_effect=FileNotFoundError("pacman")):
        result = detect_toolchain_config_mismatch(
            {}, toolchain_cfg={"enabled": True, "compiler": "llvm", "pgo": True})
    assert result == ()


def test_mismatch_suppressed_when_toolchain_built():
    """In-place custom/PGO builds install stock-named packages (llvm, clang, …),
    which pacman classifies as repo (install_origin='repo') because their names
    exist in a sync DB. A toolchain-owned build_state record proves sysforge
    built them, so the stock-install finding must NOT fire (B5)."""
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch

    report = _report(_state(pkgbase="llvm", install_origin="repo"))
    with patch("sysforge.primitives.llvm_state.collect_llvm_state",
               return_value=report), \
         patch("sysforge.primitives.llvm_state._toolchain_built_packages",
               return_value={"llvm"}):
        result = detect_toolchain_config_mismatch(
            {}, toolchain_cfg={"enabled": True, "compiler": "llvm", "pgo": True})
    assert result == ()


def test_mismatch_still_flags_stock_when_never_built():
    """No toolchain build_state record (the stage was never run) → a stock
    install IS a genuine provenance mismatch and still fires."""
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch

    report = _report(_state(pkgbase="llvm", install_origin="repo"))
    with patch("sysforge.primitives.llvm_state.collect_llvm_state",
               return_value=report), \
         patch("sysforge.primitives.llvm_state._toolchain_built_packages",
               return_value=set()):
        result = detect_toolchain_config_mismatch(
            {}, toolchain_cfg={"enabled": True, "compiler": "llvm", "pgo": True})
    assert [f.check_id for f in result] == ["toolchain_stock_install"]


def test_mismatch_repo_mode_pacman_pgo_off_is_intended_stock():
    """pgo off + packages.toml repo_mode=pacman → the stage installs the stock
    LLVM suite from the repos on purpose, so a stock install is the chosen path,
    not a mismatch; the probe is skipped entirely."""
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch

    with patch("sysforge.primitives.llvm_state.collect_llvm_state") as mock_collect, \
         patch("sysforge.primitives.llvm_state._packages_repo_mode_is_pacman",
               return_value=True):
        result = detect_toolchain_config_mismatch(
            {}, toolchain_cfg={"enabled": True, "compiler": "llvm", "pgo": False})
    assert result == ()
    mock_collect.assert_not_called()
