import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

import sysforge.primitives.makepkg_flags as mf
import sysforge.primitives.makepkg_invoke as mi


@pytest.fixture
def pkgbuild(tmp_path):
    p = tmp_path / "PKGBUILD"
    p.write_text("# pkgbuild\n")
    return p


def test_abort_returns_clean_outcome(monkeypatch, pkgbuild):
    monkeypatch.setattr(mi, "prompt_choice", lambda *a, **k: "a")
    out = mi._run_recovery_menu(
        pkgbuild, Path("/conf"), {}, extra_env=None, extra_flags=None,
        interactive=True, strip_flags=None, reemit_conf=None, pkgbase="htop",
    )
    assert out.action == "abort"
    assert out.overrides is None


def test_recover_prompt_names_failing_package(monkeypatch, pkgbuild):
    # 2.0.1-B2: the recovery menu header must name the failing package so a
    # batch update's prompt is unambiguous — "Recover <pkgbase>:", not "Recover:".
    seen = {}

    def capture(msg, choices, **k):
        seen["msg"] = msg
        return "a"

    monkeypatch.setattr(mi, "prompt_choice", capture)
    mi._run_recovery_menu(
        pkgbuild, Path("/conf"), {}, extra_env=None, extra_flags=None,
        interactive=True, strip_flags=None, reemit_conf=None, pkgbase="htop",
    )
    assert seen["msg"].startswith("Recover htop:")


def test_recover_prompt_falls_back_to_filename_without_pkgbase(monkeypatch,
                                                               pkgbuild):
    # No pkgbase → fall back to the PKGBUILD's parent-dir name, never a bare
    # "Recover:".
    seen = {}
    monkeypatch.setattr(
        mi, "prompt_choice",
        lambda msg, choices, **k: (seen.__setitem__("msg", msg) or "a"))
    mi._run_recovery_menu(
        pkgbuild, Path("/conf"), {}, extra_env=None, extra_flags=None,
        interactive=True, strip_flags=None, reemit_conf=None, pkgbase=None,
    )
    assert seen["msg"].startswith("Recover ")
    assert not seen["msg"].startswith("Recover:")


def test_summary_reports_effective_linker(monkeypatch, capsys, pkgbuild):
    # 1.2.0-B4: the "Toolchain used" line must surface LD alongside CC/CXX.
    # A profile carrying -fuse-ld=lld in LDFLAGS resolves to LD=lld.
    monkeypatch.setattr(mi, "prompt_choice", lambda *a, **k: "a")
    monkeypatch.setattr(mf.shutil, "which", lambda name: f"/usr/bin/{name}")
    mi._run_recovery_menu(
        pkgbuild, Path("/conf"),
        {"CC": "clang", "CXX": "clang++", "LDFLAGS": "-fuse-ld=lld -Wl,-O2"},
        extra_env=None, extra_flags=None, interactive=True, strip_flags=None,
        reemit_conf=None, pkgbase="htop",
    )
    out = capsys.readouterr().err
    summary = next(ln for ln in out.splitlines() if "Toolchain used" in ln)
    assert "CC=clang" in summary
    assert "CXX=clang++" in summary
    assert "LD=lld" in summary


def test_summary_linker_defaults_to_ld_when_unspecified(monkeypatch, capsys,
                                                         pkgbuild):
    monkeypatch.setattr(mi, "prompt_choice", lambda *a, **k: "a")
    mi._run_recovery_menu(
        pkgbuild, Path("/conf"), {}, extra_env=None, extra_flags=None,
        interactive=True, strip_flags=None, reemit_conf=None, pkgbase="htop",
    )
    out = capsys.readouterr().err
    summary = next(ln for ln in out.splitlines() if "Toolchain used" in ln)
    assert "LD=ld" in summary


def test_editor_path_snapshots_orig_and_retries(monkeypatch, pkgbuild):
    # Menu: choose [e], editor "succeeds", then build succeeds on retry.
    monkeypatch.setattr(mi, "prompt_choice", lambda *a, **k: "e")
    monkeypatch.setattr(mi, "resolve_editor", lambda: ("vim", "detected"))
    monkeypatch.setattr(mi, "editor_usable", lambda e: True)
    monkeypatch.setattr(mi, "run_tty_argv", lambda argv: 0)
    calls = {"n": 0}

    def fake_invoke(*a, **k):
        calls["n"] += 1  # first call inside menu retry → success (no raise)

    monkeypatch.setattr(mi, "invoke_makepkg", fake_invoke)
    out = mi._run_recovery_menu(
        pkgbuild, Path("/conf"), {}, extra_env=None, extra_flags=None,
        interactive=True, strip_flags=None, reemit_conf=None, pkgbase="htop",
    )
    assert out.action == "retry"
    assert (pkgbuild.parent / "PKGBUILD.orig").exists()
    assert calls["n"] == 1


def _both_toolchains_installed(monkeypatch):
    """Make gcc/g++/clang/clang++ all resolve on PATH so both coherent units
    are offered by the swap menu."""
    monkeypatch.setattr(mi.shutil, "which", lambda name: f"/usr/bin/{name}")


def _choice_seq(monkeypatch, *answers):
    seq = iter(answers)
    monkeypatch.setattr(mi, "prompt_choice", lambda *a, **k: next(seq))


def test_swap_path_returns_overrides_on_success(monkeypatch, pkgbuild):
    # Menu: [c] → pick the gcc toolchain unit (coherent cc/cxx) → LD via
    # prompt_text; reemit_conf yields a conf; build succeeds → overrides returned.
    _both_toolchains_installed(monkeypatch)
    _choice_seq(monkeypatch, "c", "gcc")
    monkeypatch.setattr(mi, "prompt_text", lambda *a, **k: "bfd")

    @contextmanager
    def fake_reemit(cc, cxx, ld):
        assert (cc, cxx, ld) == ("gcc", "g++", "bfd")
        yield Path("/conf-new")

    monkeypatch.setattr(mi, "invoke_makepkg", lambda *a, **k: None)
    out = mi._run_recovery_menu(
        pkgbuild, Path("/conf"), {}, extra_env=None, extra_flags=None,
        interactive=True, strip_flags=None, reemit_conf=fake_reemit, pkgbase="htop",
    )
    assert out.action == "retry"
    assert out.overrides == {"cc": "gcc", "cxx": "g++", "ld": "bfd"}


def test_swap_menu_selects_coherent_toolchain_unit(monkeypatch, pkgbuild):
    # 2.1.0-B1: picking the "clang" unit must set BOTH cc=clang and cxx=clang++
    # — never a mixed gcc/clang++ pair. The user picks one toolchain, not two
    # independent free-text compilers.
    _both_toolchains_installed(monkeypatch)
    _choice_seq(monkeypatch, "c", "clang")
    monkeypatch.setattr(mi, "prompt_text", lambda *a, **k: "")  # keep LD

    @contextmanager
    def fake_reemit(cc, cxx, ld):
        yield Path("/conf-new")

    monkeypatch.setattr(mi, "invoke_makepkg", lambda *a, **k: None)
    out = mi._run_recovery_menu(
        pkgbuild, Path("/conf"), {"CC": "gcc", "CXX": "g++"},
        extra_env=None, extra_flags=None, interactive=True, strip_flags=None,
        reemit_conf=fake_reemit, pkgbase="htop",
    )
    assert out.overrides == {"cc": "clang", "cxx": "clang++", "ld": ""}


def test_swap_menu_omits_uninstalled_toolchain(monkeypatch, pkgbuild):
    # 2.1.0-B1: the menu enumerates only toolchains whose cc *and* cxx are on
    # PATH. With no clang installed, the offered choice set excludes it.
    monkeypatch.setattr(
        mi.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in ("gcc", "g++") else None)
    offered = {}

    def capture_choice(msg, choices, **k):
        choices = tuple(choices)
        if "gcc" in choices:  # the toolchain menu
            offered["choices"] = choices
            return "b"  # back out → re-show top menu
        if "c" in choices and "choices" not in offered:  # top menu, first pass
            return "c"  # enter the swap flow
        return "a"  # top menu, second pass → abort

    monkeypatch.setattr(mi, "prompt_choice", capture_choice)

    @contextmanager
    def fake_reemit(cc, cxx, ld):
        yield Path("/conf-new")

    mi._run_recovery_menu(
        pkgbuild, Path("/conf"), {}, extra_env=None, extra_flags=None,
        interactive=True, strip_flags=None, reemit_conf=fake_reemit, pkgbase="htop",
    )
    assert "gcc" in offered["choices"]
    assert "clang" not in offered["choices"]


def test_swap_applies_new_cc_cxx_to_retry_env(monkeypatch, pkgbuild):
    # Regression: the swapped CC/CXX must reach invoke_makepkg's extra_env,
    # not just the re-emitted conf (env.update wins last in invoke_makepkg, so
    # a stale CC/CXX in extra_env would clobber the freshly-emitted conf).
    _both_toolchains_installed(monkeypatch)
    _choice_seq(monkeypatch, "c", "gcc")
    monkeypatch.setattr(mi, "prompt_text", lambda *a, **k: "bfd")

    @contextmanager
    def fake_reemit(cc, cxx, ld):
        yield Path("/conf-new")

    recorded = {}

    def fake_invoke(pkgbuild_path, conf_path, resolved_profile,
                    extra_env=None, *a, **k):
        recorded["env"] = extra_env

    monkeypatch.setattr(mi, "invoke_makepkg", fake_invoke)
    out = mi._run_recovery_menu(
        pkgbuild, Path("/conf"), {"CC": "clang", "CXX": "clang++"},
        extra_env={"CC": "clang", "CXX": "clang++", "FOO": "bar"},
        extra_flags=None, interactive=True, strip_flags=None,
        reemit_conf=fake_reemit, pkgbase="htop",
    )
    assert out.action == "retry"
    assert recorded["env"]["CC"] == "gcc"
    assert recorded["env"]["CXX"] == "g++"
    assert recorded["env"]["FOO"] == "bar"   # unrelated env preserved
    assert out.overrides == {"cc": "gcc", "cxx": "g++", "ld": "bfd"}


def test_swap_unavailable_without_reemit(monkeypatch, pkgbuild):
    # reemit_conf=None → [c] is not offered; choosing retry as-is then aborting.
    seq = iter(["r", "a"])
    monkeypatch.setattr(mi, "prompt_choice", lambda *a, **k: next(seq))
    # First retry raises (still failing), second loop → abort.
    monkeypatch.setattr(
        mi, "invoke_makepkg",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, "makepkg")),
    )
    out = mi._run_recovery_menu(
        pkgbuild, Path("/conf"), {}, extra_env=None, extra_flags=None,
        interactive=True, strip_flags=None, reemit_conf=None, pkgbase="htop",
    )
    assert out.action == "abort"


def test_wrapper_persists_swap_overrides(monkeypatch, tmp_path):
    import sysforge.primitives.makepkg_wrapper as mw

    recorded = {}

    def fake_write(path, pkgbase, cc, cxx, ld):
        recorded.update(dict(path=path, pkgbase=pkgbase, cc=cc, cxx=cxx, ld=ld))
        return True

    monkeypatch.setattr(mw, "write_package_compiler_override", fake_write,
                        raising=False)
    # _invoke_with_retry returns None; take_last_recovery yields a swap outcome.
    monkeypatch.setattr(mw, "_invoke_with_retry", lambda *a, **k: None)
    monkeypatch.setattr(
        mw, "take_last_recovery",
        lambda: mw.RecoveryOutcome(action="retry",
                                   overrides={"cc": "gcc", "cxx": "g++", "ld": "bfd"}),
        raising=False,
    )
    mw._persist_recovery_overrides("htop")
    assert recorded["pkgbase"] == "htop"
    assert recorded["cc"] == "gcc"


def test_run_build_persists_swap_for_single_package_pkgbase(tmp_path, monkeypatch):
    """Regression (2.1.0-B2): a recovery-menu compiler swap on a single-package
    PKGBUILD (no explicit ``pkgbase=`` line — the common case) must persist to
    ``[package_compiler_overrides]``.

    The recovery menu succeeds under a swap, but ``_run_build`` fed the persist
    call the raw ``globals['pkgbase']`` (absent here) instead of the
    pkgbase-or-pkgname key ``resolve_profile`` reads back with, so the write
    silently no-op'd on the ``not pkgbase`` guard and the next update re-prompted.
    """
    from contextlib import contextmanager
    from unittest.mock import patch

    import sysforge.primitives.makepkg_wrapper as mw

    pb = tmp_path / "PKGBUILD"
    pb.write_text("pkgname=htop\npkgver=3.4.1\npkgrel=1\n")

    recorded = {}

    def fake_write(path, pkgbase, cc, cxx, ld):
        recorded.update(dict(pkgbase=pkgbase, cc=cc, cxx=cxx, ld=ld))
        return True

    def fake_invoke_with_retry(*a, **k):
        # Simulate a successful recovery-menu compiler swap (the menu stashes
        # the outcome in the _LAST_RECOVERY contextvar, drained by persist).
        mi._LAST_RECOVERY.set(
            mi.RecoveryOutcome(
                action="retry",
                overrides={"cc": "gcc", "cxx": "g++", "ld": "bfd"},
            )
        )
        return None

    @contextmanager
    def fake_emit(*a, **kw):
        yield "/tmp/fake_makepkg.conf"

    with (
        patch("sysforge.primitives.makepkg_wrapper.patch_pkgbuild_groups",
              return_value=pb),
        patch("sysforge.primitives.makepkg_wrapper.emit_makepkg_conf",
              side_effect=fake_emit),
        patch("sysforge.primitives.makepkg_wrapper.resolve_env_vars",
              return_value={}),
        patch("sysforge.primitives.makepkg_wrapper._invoke_with_retry",
              side_effect=fake_invoke_with_retry),
        patch("sysforge.primitives.makepkg_wrapper.write_package_compiler_override",
              side_effect=fake_write),
    ):
        mw._run_build(pb, {}, {}, [], extracted_profile=None,
                      pkgmeta={"globals": {"pkgname": "htop"}})

    assert recorded.get("pkgbase") == "htop", (
        "single-package recovery swap was not persisted "
        f"(recorded={recorded!r})"
    )
    assert recorded.get("cc") == "gcc"


def test_wrapper_persist_partial_overrides_never_raises(monkeypatch):
    import sysforge.primitives.makepkg_wrapper as mw

    called = {"n": 0}

    def fake_write(path, pkgbase, cc, cxx, ld):
        called["n"] += 1
        return True

    monkeypatch.setattr(mw, "write_package_compiler_override", fake_write,
                        raising=False)
    # Partial dict (CC-only swap) missing cxx/ld must not crash persistence.
    monkeypatch.setattr(
        mw, "take_last_recovery",
        lambda: mw.RecoveryOutcome(action="retry", overrides={"cc": "gcc"}),
        raising=False,
    )
    # Must not raise (best-effort contract): skips the write on an incomplete dict.
    mw._persist_recovery_overrides("htop")
    assert called["n"] == 0
