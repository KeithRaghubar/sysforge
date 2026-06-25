import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

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


def test_swap_path_returns_overrides_on_success(monkeypatch, pkgbuild):
    # Menu: [c]; prompt_text supplies cc/cxx/ld; reemit_conf yields a conf;
    # build succeeds → overrides returned.
    monkeypatch.setattr(mi, "prompt_choice", lambda *a, **k: "c")
    answers = iter(["gcc", "g++", "bfd"])
    monkeypatch.setattr(mi, "prompt_text", lambda *a, **k: next(answers))

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


def test_swap_applies_new_cc_cxx_to_retry_env(monkeypatch, pkgbuild):
    # Regression: the swapped CC/CXX must reach invoke_makepkg's extra_env,
    # not just the re-emitted conf (env.update wins last in invoke_makepkg, so
    # a stale CC/CXX in extra_env would clobber the freshly-emitted conf).
    monkeypatch.setattr(mi, "prompt_choice", lambda *a, **k: "c")
    answers = iter(["gcc", "g++", "bfd"])
    monkeypatch.setattr(mi, "prompt_text", lambda *a, **k: next(answers))

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
