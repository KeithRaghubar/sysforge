"""
test_distro_portability.py — Arch-derivative portability (Standards row 23).

The *behavioural* half of row 23. `check_standards`' `distro_portability` group
catches structural regressions (a hardcoded repo name, a dropped baseline load);
these tests assert what actually has to hold on a derivative, using a synthetic
derivative system conf and pacman.conf rather than the host's own.

Fixtures model the shape of a derivative — extra sync repos ordered ahead of
`core`/`extra`, a raised-ISA + LTO `makepkg.conf` baseline, bumped `pkgrel`s on
core packages — with deliberately *synthetic* values. The point is that whatever
the derivative sets survives, not that any distro currently sets a particular
flag. (Verified against the real CachyOS image on 2026-07-26: `ID=cachyos`,
`ID_LIKE=arch`, sync repos `cachyos,core,extra,multilib`, `LTOFLAGS=-flto=auto`,
and a `pacman` pkgrel of `-4` where Arch ships `-2`. Its `makepkg.conf` uses
`-march=native`; the `x86-64-v3` level lives in its v3 *repos*, not that file.)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from sysforge.primitives import pacman
from sysforge.primitives.config import parse_system_makepkg_conf
from sysforge.primitives.makepkg_wrapper import emit_makepkg_conf
from sysforge.primitives.profile import merge_extends

# Narrowed mirror of the [profiles.*] tables in
# tests/data/etc/sysforge/profiles.toml — only what these tests override.
PROFILES = {
    "bare": {
        "BUILDDIR": "$HOME/builds",
    },
    "native": {
        "extends": "bare",
        "CFLAGS": "-march=native -O2 -pipe",
        "CXXFLAGS": "$CFLAGS",
        "LDFLAGS": "-Wl,-O1,--as-needed",
    },
}

# Synthetic derivative toolchain defaults: a raised baseline ISA level and LTO
# on by default — the shape of what a "replace the conf" bug silently discards.
DERIV_CFLAGS = "-march=x86-64-v3 -mtune=generic -O3 -pipe -fno-plt -flto=auto"
DERIV_LTOFLAGS = "-flto=auto"


def _write_derivative_conf(tmp: Path) -> Path:
    p = tmp / "makepkg.conf"
    p.write_text(
        'CARCH="x86_64"\n'
        'CHOST="x86_64-pc-linux-gnu"\n'
        f'CFLAGS="{DERIV_CFLAGS}"\n'
        'CXXFLAGS="$CFLAGS"\n'
        'LDFLAGS="-Wl,-O2,--as-needed"\n'
        f'LTOFLAGS="{DERIV_LTOFLAGS}"\n'
        'RUSTFLAGS="-Copt-level=3 -Ctarget-cpu=x86-64-v3"\n'
        'PACKAGER="Derivative Build <build@example.invalid>"\n'
        'PKGEXT=".pkg.tar.zst"\n'
    )
    return p


def _emit(system_conf: Path, profile: str = "bare", **kw) -> str:
    resolved = merge_extends(profile, PROFILES, conflict_groups={})
    active = frozenset({"makepkg", "env"})
    with emit_makepkg_conf(
        resolved, active, system_conf_path=str(system_conf), **kw
    ) as conf_path:
        return Path(conf_path).read_text()


# ---------------------------------------------------------------------------
# (b) The system makepkg.conf is the merge baseline, never replaced
# ---------------------------------------------------------------------------

def test_derivative_toolchain_defaults_survive_the_merge():
    """A derivative's raised -march and LTO defaults reach the emitted conf."""
    with tempfile.TemporaryDirectory() as d:
        text = _emit(_write_derivative_conf(Path(d)))
    assert f'CFLAGS="{DERIV_CFLAGS}"' in text
    assert "-march=x86-64-v3" in text
    assert f'LTOFLAGS="{DERIV_LTOFLAGS}"' in text


def test_unmanaged_derivative_keys_are_written_verbatim():
    """Keys sysforge does not manage pass through untouched — a derivative can
    set anything in its conf and sysforge must not editorialize."""
    with tempfile.TemporaryDirectory() as d:
        text = _emit(_write_derivative_conf(Path(d)))
    assert 'PACKAGER="Derivative Build <build@example.invalid>"' in text
    assert 'CHOST="x86_64-pc-linux-gnu"' in text
    assert 'CXXFLAGS="$CFLAGS"' in text  # shell reference preserved, not expanded


def test_profile_override_replaces_only_its_own_key():
    """A profile that overrides CFLAGS must not collaterally drop the
    derivative's other toolchain defaults."""
    with tempfile.TemporaryDirectory() as d:
        conf = _write_derivative_conf(Path(d))
        # `native` sets CFLAGS/CXXFLAGS/LDFLAGS; everything else is the baseline.
        text = _emit(conf, profile="native")
    assert "-march=native" in text            # the override landed
    assert f'LTOFLAGS="{DERIV_LTOFLAGS}"' in text   # baseline key untouched
    assert 'PACKAGER="Derivative Build <build@example.invalid>"' in text


def test_parse_reads_the_derivative_conf_verbatim():
    """parse_system_makepkg_conf is the one reader, and it does not normalize:
    the value text arrives exactly as the derivative wrote it."""
    with tempfile.TemporaryDirectory() as d:
        conf = _write_derivative_conf(Path(d))
        parsed = parse_system_makepkg_conf(str(conf))
    assert parsed["CFLAGS"] == f'"{DERIV_CFLAGS}"'
    assert parsed["LTOFLAGS"] == f'"{DERIV_LTOFLAGS}"'


# ---------------------------------------------------------------------------
# (a) Sync repos are read from pacman.conf, never assumed
# ---------------------------------------------------------------------------

def _write_pacman_conf(tmp: Path, body: str) -> Path:
    p = tmp / "pacman.conf"
    p.write_text(body)
    return p


def test_derivative_repos_resolved_in_file_order(monkeypatch):
    """A derivative puts its own repos AHEAD of core/extra so its rebuilds win.
    That order must survive into the registered sync DBs: registering core first
    would resolve a shadowed package to the Arch build."""
    with tempfile.TemporaryDirectory() as d:
        conf = _write_pacman_conf(Path(d), (
            "[options]\n"
            "HoldPkg = pacman glibc\n"
            "[cachyos-v3]\n"
            "Include = /etc/pacman.d/cachyos-v3-mirrorlist\n"
            "[cachyos-core-v3]\n"
            "Include = /etc/pacman.d/cachyos-v3-mirrorlist\n"
            "[core]\n"
            "Include = /etc/pacman.d/mirrorlist\n"
            "[extra]\n"
            "Include = /etc/pacman.d/mirrorlist\n"
            "[multilib]\n"
            "Include = /etc/pacman.d/mirrorlist\n"
        ))
        monkeypatch.setattr(pacman, "_PACMAN_CONF", conf)
        repos = pacman._read_sync_repo_names()
    assert repos == ["cachyos-v3", "cachyos-core-v3", "core", "extra", "multilib"]
    assert "options" not in repos


def test_repos_fall_back_only_when_conf_is_unreadable(monkeypatch, tmp_path):
    """The ["core", "extra"] literal is the allowlisted I/O fallback and nothing
    more — it must never override a readable conf."""
    monkeypatch.setattr(pacman, "_PACMAN_CONF", tmp_path / "absent.conf")
    assert pacman._read_sync_repo_names() == ["core", "extra"]


def test_repo_membership_is_asked_of_pacman_not_inferred(monkeypatch):
    """The repo-vs-AUR split (build_core.prepare_deps → aur.repo_packages) asks
    pacman, so a derivative carrying a slice of the AUR in its own sync DBs
    resolves those names as repo packages rather than AUR ones. This is the
    exit-8 failure class: an AUR name in the `pacman -S` transaction aborts it."""
    from sysforge.primitives import aur

    seen: dict[str, list[str]] = {}

    class _Proc:
        # A name the derivative carries in its own sync DB while Arch has it
        # only in the AUR — the shadowing case.
        stdout = "Name            : shadowed-tool\nVersion         : 2.1.0-2\n"
        stderr = ""
        returncode = 0

    def fake_run(argv, **_kw):
        seen["argv"] = argv
        return _Proc()

    monkeypatch.setattr(aur.subprocess, "run", fake_run)
    found = aur.repo_packages(["shadowed-tool", "definitely-not-packaged"])

    assert found == {"shadowed-tool"}
    assert seen["argv"][:2] == ["pacman", "-Si"]   # asked, not assumed
