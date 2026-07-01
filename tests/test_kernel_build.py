"""
test_kernel_build.py — unit tests for kernel build mode behaviours:
  - emit_makepkg_conf flag key stripping (covered in test_system_conf.py)
  - LLVM env detection and injection via _run_build
  - kconfig patching toggled by interactive flag
  - _invoke_with_retry sudo timeout recovery
"""
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from sysforge.primitives.makepkg_wrapper import _find_built_packages, _invoke_with_retry, _run_build


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_profile():
    return {"batch": True}


def _minimal_pkgmeta():
    return {"globals": {"pkgname": "linux-custom"}}


@contextmanager
def _mock_build_context(tmp_path, profile=None, extra_env_out=None,
                        pkgbuild_text=None):
    """
    Set up a minimal _run_build call with the build machinery mocked out.
    Writes a PKGBUILD.sysforge into tmp_path.
    Captures the extra_env passed to _invoke_with_retry via extra_env_out list.
    """
    text = pkgbuild_text or "pkgname=linux-custom\npkgver=1\npkgrel=1\n"
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(text)
    patched = tmp_path / "PKGBUILD.sysforge"
    patched.write_text(text)

    captured = {}

    @contextmanager
    def fake_emit(*args, **kwargs):
        yield "/tmp/fake_makepkg.conf"

    def fake_invoke(pb, conf, rp, extra_env=None, extra_flags=None,
                    interactive=False, strip_flags=None, **kwargs):
        # **kwargs absorbs the keyword-only reemit_conf/pkgbase that the
        # interactive build-failure recovery path threads through.
        captured["extra_env"] = dict(extra_env or {})

    with (
        patch("sysforge.primitives.makepkg_wrapper.apply_patch_pkgbuild",
              return_value=patched),
        patch("sysforge.primitives.makepkg_wrapper.patch_pkgbuild_groups",
              return_value=patched),
        patch("sysforge.primitives.makepkg_wrapper.patch_noninteractive_kconfig"),
        patch("sysforge.primitives.makepkg_wrapper.emit_makepkg_conf",
              side_effect=fake_emit),
        patch("sysforge.primitives.makepkg_wrapper.resolve_env_vars",
              return_value={}),
        patch("sysforge.primitives.makepkg_wrapper._invoke_with_retry",
              side_effect=fake_invoke),
        patch("sysforge.primitives.makepkg_wrapper.cleanup_patch_artifacts"),
        patch("sysforge.primitives.makepkg_wrapper.handle_failure"),
    ):
        yield pkgbuild, captured


# ---------------------------------------------------------------------------
# LLVM detection
# ---------------------------------------------------------------------------

def test_kernel_clang_cc_override_injects_llvm(tmp_path):
    """cc_override=clang → LLVM=1 and LLVM_IAS=1 injected into env."""
    with _mock_build_context(tmp_path) as (pkgbuild, captured):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   cc_override="clang", kernel_build=True)
    assert captured["extra_env"].get("LLVM") == "1"
    assert captured["extra_env"].get("LLVM_IAS") == "1"


def test_kernel_clang_full_path_injects_llvm(tmp_path):
    """/usr/bin/clang in profile CC triggers LLVM injection."""
    profile = {**_minimal_profile(), "CC": "/usr/bin/clang"}
    with _mock_build_context(tmp_path) as (pkgbuild, captured):
        _run_build(pkgbuild, profile, {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   kernel_build=True)
    assert captured["extra_env"].get("LLVM") == "1"
    assert captured["extra_env"].get("LLVM_IAS") == "1"


def test_kernel_gcc_no_llvm_injection(tmp_path):
    """GCC toolchain: no LLVM env vars injected."""
    profile = {**_minimal_profile(), "CC": "gcc"}
    with _mock_build_context(tmp_path) as (pkgbuild, captured):
        _run_build(pkgbuild, profile, {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   kernel_build=True)
    assert "LLVM" not in captured["extra_env"]
    assert "LLVM_IAS" not in captured["extra_env"]


def test_kernel_no_cc_no_llvm_injection(tmp_path, monkeypatch):
    """No CC anywhere: no LLVM env vars injected."""
    monkeypatch.delenv("CC", raising=False)
    with _mock_build_context(tmp_path) as (pkgbuild, captured):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   kernel_build=True)
    assert "LLVM" not in captured["extra_env"]


def test_non_kernel_build_no_llvm_injection(tmp_path):
    """kernel_build=False: LLVM vars never injected even with clang."""
    with _mock_build_context(tmp_path) as (pkgbuild, captured):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   cc_override="clang", kernel_build=False)
    assert "LLVM" not in captured["extra_env"]


# ---------------------------------------------------------------------------
# kconfig patching toggle
# ---------------------------------------------------------------------------

def test_kernel_non_interactive_patches_kconfig(tmp_path):
    """kernel_build=True, interactive=False → patch_noninteractive_kconfig called."""
    with (
        _mock_build_context(tmp_path) as (pkgbuild, _),
        patch("sysforge.primitives.makepkg_wrapper.patch_noninteractive_kconfig") as mock_patch,
    ):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   interactive=False, kernel_build=True)
    mock_patch.assert_called_once()


def test_kernel_interactive_skips_kconfig_patch(tmp_path):
    """kernel_build=True, interactive=True → patch_noninteractive_kconfig NOT called."""
    with (
        _mock_build_context(tmp_path) as (pkgbuild, _),
        patch("sysforge.primitives.makepkg_wrapper.patch_noninteractive_kconfig") as mock_patch,
    ):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   interactive=True, kernel_build=True)
    mock_patch.assert_not_called()


def test_non_kernel_build_never_patches_kconfig(tmp_path):
    """kernel_build=False → patch_noninteractive_kconfig never called."""
    with (
        _mock_build_context(tmp_path) as (pkgbuild, _),
        patch("sysforge.primitives.makepkg_wrapper.patch_noninteractive_kconfig") as mock_patch,
    ):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile=None, pkgmeta=_minimal_pkgmeta(),
                   kernel_build=False)
    mock_patch.assert_not_called()


def test_kernel_build_applies_btf_guard(tmp_path):
    """kernel_build=True → patch_kernel_btf_guard called (gates vmlinux.h on BTF)."""
    with (
        _mock_build_context(tmp_path) as (pkgbuild, _),
        patch("sysforge.primitives.makepkg_wrapper.patch_kernel_btf_guard") as mock_btf,
    ):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   kernel_build=True)
    mock_btf.assert_called_once()


def test_non_kernel_build_never_applies_btf_guard(tmp_path):
    """kernel_build=False → patch_kernel_btf_guard never called."""
    with (
        _mock_build_context(tmp_path) as (pkgbuild, _),
        patch("sysforge.primitives.makepkg_wrapper.patch_kernel_btf_guard") as mock_btf,
    ):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile=None, pkgmeta=_minimal_pkgmeta(),
                   kernel_build=False)
    mock_btf.assert_not_called()


def test_kernel_build_always_applies_fragment_merge(tmp_path):
    """kernel_build=True always injects the sysforge.config fragment merge,
    threading the resolved interactive flag (both branches)."""
    for interactive in (True, False):
        with (
            _mock_build_context(tmp_path) as (pkgbuild, _),
            patch("sysforge.primitives.makepkg_wrapper.patch_noninteractive_kconfig"),
            patch("sysforge.primitives.makepkg_wrapper.patch_kernel_kconfig_apply") as mock_apply,
        ):
            _run_build(pkgbuild, _minimal_profile(), {}, [],
                       extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                       interactive=interactive, kernel_build=True)
        mock_apply.assert_called_once()
        assert mock_apply.call_args.kwargs["interactive"] is interactive


def test_non_kernel_build_never_applies_fragment_merge(tmp_path):
    with (
        _mock_build_context(tmp_path) as (pkgbuild, _),
        patch("sysforge.primitives.makepkg_wrapper.patch_kernel_kconfig_apply") as mock_apply,
    ):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile=None, pkgmeta=_minimal_pkgmeta(),
                   kernel_build=False)
    mock_apply.assert_not_called()


# ---------------------------------------------------------------------------
# _find_built_packages
# ---------------------------------------------------------------------------

def test_find_built_packages_empty(tmp_path):
    assert _find_built_packages(tmp_path) == []


def test_find_built_packages_finds_pkg_files(tmp_path):
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()
    (tmp_path / "foo-debug-1.0-1-x86_64.pkg.tar.zst").touch()
    result = _find_built_packages(tmp_path)
    names = {p.name for p in result}
    assert "foo-1.0-1-x86_64.pkg.tar.zst" in names
    assert "foo-debug-1.0-1-x86_64.pkg.tar.zst" in names


def test_find_built_packages_excludes_sig(tmp_path):
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst.sig").touch()
    result = _find_built_packages(tmp_path)
    assert len(result) == 1
    assert result[0].name == "foo-1.0-1-x86_64.pkg.tar.zst"


def test_find_built_packages_finds_uncompressed(tmp_path):
    # PKGEXT='.pkg.tar' produces uncompressed package files. The .sig still
    # uses the .pkg.tar.sig form and must be excluded.
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar").touch()
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.sig").touch()
    result = _find_built_packages(tmp_path)
    assert len(result) == 1
    assert result[0].name == "foo-1.0-1-x86_64.pkg.tar"


def test_find_built_packages_ignores_unrelated(tmp_path):
    (tmp_path / "PKGBUILD").touch()
    (tmp_path / "src").mkdir()
    result = _find_built_packages(tmp_path)
    assert result == []


# ---------------------------------------------------------------------------
# install_built_packages — used by the kernel stage's split build/install
# ---------------------------------------------------------------------------

def test_install_built_packages_runs_pacman_U(tmp_path, monkeypatch):
    from sysforge.primitives import makepkg_wrapper as mw
    # PKGDEST-unset so _find_artifacts looks only in the PKGBUILD dir (the dev
    # machine's real PKGDEST would otherwise leak its built packages in).
    monkeypatch.setattr("sysforge.primitives.pacman.get_pkgdest", lambda: None)
    (tmp_path / "linux-custom-1-1-x86_64.pkg.tar.zst").touch()
    (tmp_path / "linux-custom-headers-1-1-x86_64.pkg.tar.zst").touch()
    calls = {}

    def fake_run(cmd, *a, **k):
        calls["cmd"] = cmd
        return SimpleNamespace(returncode=0)

    with patch("sysforge.primitives.makepkg_wrapper.subprocess.run", fake_run):
        pkgs = mw.install_built_packages(tmp_path)
    assert calls["cmd"][:4] == ["sudo", "pacman", "-U", "--noconfirm"]
    assert len(pkgs) == 2


def test_install_built_packages_scopes_to_pkgbuild_pkgnames(tmp_path, monkeypatch):
    """Only the PKGBUILD's own artifacts are installed, not all of PKGDEST.

    Regression: the kernel stage globbed the shared PKGDEST and handed every
    .pkg.tar* to ``pacman -U`` — so a populated PKGDEST dragged unrelated
    packages into the kernel install. The install set must be scoped to the
    PKGBUILD's pkgnames.
    """
    from sysforge.primitives import makepkg_wrapper as mw
    pkgdest = tmp_path / "pkgdest"
    pkgdest.mkdir()
    monkeypatch.setattr("sysforge.primitives.pacman.get_pkgdest", lambda: pkgdest)
    # Kernel's own artifacts...
    (pkgdest / "linux-custom-1-1-x86_64.pkg.tar.zst").touch()
    (pkgdest / "linux-custom-headers-1-1-x86_64.pkg.tar.zst").touch()
    # ...alongside unrelated packages a real PKGDEST would hold.
    (pkgdest / "firefox-130-1-x86_64.pkg.tar.zst").touch()
    (pkgdest / "ripgrep-14-1-x86_64.pkg.tar.zst").touch()
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgbase=linux-custom\n"
        "pkgname=('linux-custom' 'linux-custom-headers')\n"
        "pkgver=1\npkgrel=1\narch=('x86_64')\n"
    )
    calls = {}

    def fake_run(cmd, *a, **k):
        calls["cmd"] = cmd
        return SimpleNamespace(returncode=0)

    with patch("sysforge.primitives.makepkg_wrapper.subprocess.run", fake_run):
        pkgs = mw.install_built_packages(tmp_path)
    installed = {p.name for p in pkgs}
    assert installed == {
        "linux-custom-1-1-x86_64.pkg.tar.zst",
        "linux-custom-headers-1-1-x86_64.pkg.tar.zst",
    }
    assert not any("firefox" in arg or "ripgrep" in arg for arg in calls["cmd"])


def test_install_built_packages_no_artifact_raises(tmp_path, monkeypatch):
    from sysforge.primitives import makepkg_wrapper as mw
    monkeypatch.setattr("sysforge.primitives.pacman.get_pkgdest", lambda: None)
    with pytest.raises(RuntimeError, match="nothing to install"):
        mw.install_built_packages(tmp_path)


def test_install_built_packages_pacman_failure_raises(tmp_path, monkeypatch):
    from sysforge.primitives import makepkg_wrapper as mw
    monkeypatch.setattr("sysforge.primitives.pacman.get_pkgdest", lambda: None)
    (tmp_path / "linux-custom-1-1-x86_64.pkg.tar.zst").touch()
    with patch("sysforge.primitives.makepkg_wrapper.subprocess.run",
               lambda cmd, *a, **k: SimpleNamespace(returncode=1)):
        with pytest.raises(RuntimeError, match="pacman -U failed"):
            mw.install_built_packages(tmp_path)


def test_no_install_option_default_false():
    from sysforge.primitives.makepkg_wrapper import BuildOptions, INSTALL_FLAGS
    assert BuildOptions().no_install is False
    # INSTALL_FLAGS is the set merged into strip_flags when no_install is set.
    assert "-i" in INSTALL_FLAGS and "--install" in INSTALL_FLAGS


# ---------------------------------------------------------------------------
# B8 — run()'s options.update source-sync honors the PKGBUILD's origin
# classification instead of defaulting every sync to source="aur".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("opt_source, expected_source", [
    ("local", "local"),   # hand-maintained (e.g. stock kernel PKGBUILD, modified pkgbase)
    ("git",   "git"),      # git-hosted PKGBUILD repo — must actually fetch, not no-op as AUR
    ("repo",  "repo"),
    (None,    "aur"),      # unset → preserve the historical AUR default
])
def test_run_update_sync_honors_source(tmp_path, monkeypatch, opt_source, expected_source):
    """run()'s options.update sync passes options.source to the scheduler (B8).

    Regression: the SyncRequest omitted source=, so a "local"/"git"/"repo" PKGBUILD
    was mis-synced as AUR — a spurious AUR RPC for local PKGBUILDs, and (worse) a
    git-hosted PKGBUILD repo was never fetched, yielding a stale build.
    """
    from sysforge.primitives import makepkg_wrapper as mw
    from sysforge.primitives.makepkg_wrapper import BuildOptions
    from sysforge.primitives.source_sync import STATUS_FAILED

    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text("pkgname=linux-custom\npkgver=1\npkgrel=1\n")

    captured = []

    class _FakeScheduler:
        def request(self, req):
            captured.append(req)
            # Abort right after the sync so we never reach the real build.
            return SimpleNamespace(status=STATUS_FAILED, error="stub-abort",
                                   pkgbase=req.pkgbase)

    monkeypatch.setattr(mw, "get_scheduler", lambda *a, **k: _FakeScheduler())

    opts = BuildOptions(update=True, source=opt_source, pkg_log=False)
    # fatal() on STATUS_FAILED calls sys.exit → SystemExit; we only care that the
    # SyncRequest was built before the abort.
    with pytest.raises(SystemExit):
        mw.run(pkgbuild, options=opts)

    assert len(captured) == 1
    assert captured[0].source == expected_source


# ---------------------------------------------------------------------------
# _invoke_with_retry — sudo timeout recovery
# ---------------------------------------------------------------------------

def _make_pkgbuild(tmp_path):
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text("pkgname=foo\npkgver=1\npkgrel=1\n")
    return pkgbuild


def test_invoke_retry_sudo_reauth_and_install(tmp_path):
    """'s' response with built packages → sudo -v + pacman -U, no rebuild."""
    pkgbuild = _make_pkgbuild(tmp_path)
    pkg_file = tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst"
    pkg_file.touch()

    profile = {}
    fail = subprocess.CalledProcessError(1, "makepkg")
    sudo_v_result = MagicMock(returncode=0)
    pacman_result = MagicMock(returncode=0)

    with (
        patch("sysforge.primitives.makepkg_invoke.invoke_makepkg", side_effect=fail),
        patch("sysforge.primitives.makepkg_invoke.subprocess.run",
              side_effect=[sudo_v_result, pacman_result]) as mock_run,
        patch("builtins.input", return_value="s"),
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", profile,
                           extra_flags=["--install"])

    calls = mock_run.call_args_list
    assert calls[0] == call(["sudo", "-v"])
    assert calls[1][0][0][0:3] == ["sudo", "pacman", "-U"]
    assert str(pkg_file) in calls[1][0][0]


def test_invoke_retry_sudo_install_fails_then_abort(tmp_path):
    """pacman -U fails → inner loop prompts; 'abort' raises RuntimeError."""
    pkgbuild = _make_pkgbuild(tmp_path)
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()

    fail = subprocess.CalledProcessError(1, "makepkg")
    pacman_fail = MagicMock(returncode=1)

    with (
        patch("sysforge.primitives.makepkg_invoke.invoke_makepkg", side_effect=fail),
        patch("sysforge.primitives.makepkg_invoke.subprocess.run",
              side_effect=[MagicMock(returncode=0), pacman_fail]),
        patch("builtins.input", side_effect=["s", "abort"]),
        pytest.raises(RuntimeError, match="build_failed"),
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", {}, extra_flags=["--install"])


def test_invoke_retry_abort_with_built_packages(tmp_path):
    """'abort' response when packages are present → RuntimeError."""
    pkgbuild = _make_pkgbuild(tmp_path)
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()

    fail = subprocess.CalledProcessError(1, "makepkg")

    with (
        patch("sysforge.primitives.makepkg_invoke.invoke_makepkg", side_effect=fail),
        patch("builtins.input", return_value="abort"),
        pytest.raises(RuntimeError, match="build_failed"),
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", {}, extra_flags=["--install"])


def test_invoke_retry_enter_retries_build_with_packages(tmp_path):
    """Enter (empty) with built packages → full rebuild retry."""
    pkgbuild = _make_pkgbuild(tmp_path)
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()

    fail = subprocess.CalledProcessError(1, "makepkg")
    invoke = MagicMock(side_effect=[fail, None])

    with (
        patch("sysforge.primitives.makepkg_invoke.invoke_makepkg", invoke),
        patch("builtins.input", return_value=""),
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", {}, extra_flags=["--install"])

    assert invoke.call_count == 2


def test_invoke_retry_no_packages_prompts_fix_pkgbuild(tmp_path):
    """No built packages → existing 'fix PKGBUILD' prompt, no sudo option."""
    pkgbuild = _make_pkgbuild(tmp_path)

    fail = subprocess.CalledProcessError(1, "makepkg")
    invoke = MagicMock(side_effect=[fail, None])

    with (
        patch("sysforge.primitives.makepkg_invoke.invoke_makepkg", invoke),
        patch("builtins.input", return_value="") as mock_input,
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", {})

    assert invoke.call_count == 2
    prompt_text = mock_input.call_args[0][0]
    assert "sudo" not in prompt_text.lower() or "PKGBUILD" in prompt_text


def test_invoke_retry_batch_mode_ignores_packages(tmp_path):
    """batch=True → fails immediately even with built packages present."""
    pkgbuild = _make_pkgbuild(tmp_path)
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()

    fail = subprocess.CalledProcessError(1, "makepkg")

    with (
        patch("sysforge.primitives.makepkg_invoke.invoke_makepkg", side_effect=fail),
        pytest.raises(RuntimeError, match="build_failed"),
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", {"batch": True})


# ---------------------------------------------------------------------------
# rename_pkgbase_to — kernel local-rename seam (F40)
# ---------------------------------------------------------------------------

_ZEN_TEXT = (
    "pkgbase=linux-zen\n"
    'pkgname=("$pkgbase" "$pkgbase-headers")\n'
    "pkgver=6.10\npkgrel=1\n"
)


def test_run_build_rename_pkgbase_to_applies_coexist_rename(tmp_path):
    with _mock_build_context(tmp_path, pkgbuild_text=_ZEN_TEXT) as (pkgbuild, _):
        info = _run_build(pkgbuild, _minimal_profile(), {}, [],
                          extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                          kernel_build=True, rename_pkgbase_to="linux-mine")
    assert info is not None
    assert info["origin_pkgbase"] == "linux-zen"
    assert info["renamed_pkgbase"] == "linux-mine"
    assert info["mode"] == "coexist"
    patched = (tmp_path / "PKGBUILD.sysforge").read_text()
    assert "pkgbase=linux-mine" in patched


def test_run_build_rename_stacks_under_fdo_suffix(tmp_path):
    # Local rename first, FDO -sysforge suffix second: layers stack, and the
    # returned dict keeps the *upstream* origin so `update` syncs the right tree.
    with _mock_build_context(tmp_path, pkgbuild_text=_ZEN_TEXT) as (pkgbuild, _):
        info = _run_build(pkgbuild, _minimal_profile(), {}, [],
                          extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                          kernel_build=True, rename_pkgbase_to="linux-mine",
                          optimization_build_mode="autofdo_kernel")
    assert info is not None
    assert info["origin_pkgbase"] == "linux-zen"
    assert info["renamed_pkgbase"] == "linux-mine-sysforge"
    patched = (tmp_path / "PKGBUILD.sysforge").read_text()
    assert "pkgbase=linux-mine-sysforge" in patched


def test_run_build_rename_noop_when_names_match(tmp_path):
    # pkgname == upstream_pkgname → stage passes no rename → builds upstream name.
    with _mock_build_context(tmp_path, pkgbuild_text=_ZEN_TEXT) as (pkgbuild, _):
        info = _run_build(pkgbuild, _minimal_profile(), {}, [],
                          extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                          kernel_build=True, rename_pkgbase_to="linux-zen")
    assert info is None
    assert "pkgbase=linux-zen" in (tmp_path / "PKGBUILD.sysforge").read_text()
