"""
test_packages_repair.py — coverage for `sysforge packages repair-state`.

Verifies that build_state entries containing unexpanded shell variables are
rewritten using fresh PKGBUILD parses, that build history is preserved, and
that unrecoverable cases are skipped with a clear reason rather than
corrupting good state.
"""
from pathlib import Path

from sysforge.state_cmd import cmd_state_repair
from sysforge.primitives.build_state import BuildState
from cli_args import cli_args


def _write_broken_state(state_dir: Path, entries: dict[str, dict]) -> None:
    """Write a build_state.toml containing literal entries (bypasses BuildState)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# test build state", ""]
    for key, fields in entries.items():
        lines.append(f'["{key}"]')
        for k, v in fields.items():
            lines.append(f'{k} = "{v}"')
        lines.append("")
    (state_dir / "build_state.toml").write_text("\n".join(lines))


def _args(state_dir: Path, dry_run: bool = False):
    return cli_args("state", "repair", state_dir=str(state_dir), dry_run=dry_run)


def test_repair_simple_scalar_reference(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    pkgbuild_dir = tmp_path / "weston-git"
    pkgbuild_dir.mkdir()
    (pkgbuild_dir / "PKGBUILD").write_text(
        "_basename=weston\n"
        'pkgname="$_basename-git"\n'
        "pkgver=14.0.0.r754.gb44cf1b\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
    )

    state_dir = tmp_path / "state"
    _write_broken_state(state_dir, {
        "$_basename-git": {
            "pkgver": "14.0.0.r754.gb44cf1b",
            "pkgrel": "1",
            "epoch": "0",
            "pkgbase": "$_basename-git",
            "pkgbuild_dir": str(pkgbuild_dir),
            "build_mode": "source_built",
            "flags_string": "CFLAGS=-O2",
            "built_at": "2026-03-26T13:29:45Z",
        }
    })

    cmd_state_repair(_args(state_dir))
    out = capsys.readouterr().out
    assert "weston-git" in out

    bs = BuildState(state_dir)
    assert "$_basename-git" not in bs.all_packages()
    entry = bs.get("weston-git")
    assert entry is not None
    assert entry["pkgbase"] == "weston-git"
    assert entry["pkgbuild_dir"] == str(pkgbuild_dir)
    # build history preserved
    assert entry["built_at"] == "2026-03-26T13:29:45Z"
    assert entry["flags_string"] == "CFLAGS=-O2"
    assert entry["build_mode"] == "source_built"


def test_repair_split_package(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    pkgbuild_dir = tmp_path / "linux-custom"
    pkgbuild_dir.mkdir()
    (pkgbuild_dir / "PKGBUILD").write_text(
        "pkgbase=linux-custom\n"
        'pkgname=("$pkgbase" "$pkgbase-headers")\n'
        "pkgver=6.19.9.arch1\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
    )

    state_dir = tmp_path / "state"
    _write_broken_state(state_dir, {
        "$pkgbase": {
            "pkgver": "6.19.9.arch1", "pkgrel": "1", "epoch": "0",
            "pkgbase": "linux-custom",
            "pkgbuild_dir": str(pkgbuild_dir),
            "build_mode": "source_built",
            "built_at": "2026-03-23T18:15:35Z",
        },
        "$pkgbase-headers": {
            "pkgver": "6.19.9.arch1", "pkgrel": "1", "epoch": "0",
            "pkgbase": "linux-custom",
            "pkgbuild_dir": str(pkgbuild_dir),
            "build_mode": "source_built",
            "built_at": "2026-03-23T18:15:35Z",
        },
    })

    cmd_state_repair(_args(state_dir))

    bs = BuildState(state_dir)
    pkgs = bs.all_packages()
    assert "linux-custom" in pkgs
    assert "linux-custom-headers" in pkgs
    assert "$pkgbase" not in pkgs
    assert "$pkgbase-headers" not in pkgs
    assert pkgs["linux-custom"]["pkgbase"] == "linux-custom"
    assert pkgs["linux-custom-headers"]["pkgbase"] == "linux-custom"


def test_repair_dry_run_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    pkgbuild_dir = tmp_path / "weston-git"
    pkgbuild_dir.mkdir()
    (pkgbuild_dir / "PKGBUILD").write_text(
        "_basename=weston\n"
        'pkgname="$_basename-git"\n'
        "pkgver=14.0.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
    )

    state_dir = tmp_path / "state"
    _write_broken_state(state_dir, {
        "$_basename-git": {
            "pkgver": "14.0.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": "$_basename-git",
            "pkgbuild_dir": str(pkgbuild_dir),
        }
    })

    cmd_state_repair(_args(state_dir, dry_run=True))

    bs = BuildState(state_dir)
    # Broken entry must still be present after a dry run.
    assert "$_basename-git" in bs.all_packages()
    assert "weston-git" not in bs.all_packages()


def test_repair_skips_unresolvable_parameter_expansion(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    pkgbuild_dir = tmp_path / "1password"
    pkgbuild_dir.mkdir()
    (pkgbuild_dir / "PKGBUILD").write_text(
        "_tarver=8.10.0-1\n"
        "pkgname=1password\n"
        # Command substitution: the static parser cannot resolve this. (The
        # ${var//-/_} replace form used to live here, but the parser now
        # resolves it — see test_parser.py::test_scalar_parameter_expansion_*.)
        'pkgver="$(printf %s "$_tarver" | tr - _)"\n'
        "pkgrel=39\n"
        "arch=(x86_64)\n"
    )

    state_dir = tmp_path / "state"
    _write_broken_state(state_dir, {
        "1password": {
            "pkgver": "${_tarver//-/_}",
            "pkgrel": "39",
            "epoch": "0",
            "pkgbase": "1password",
            "pkgbuild_dir": str(pkgbuild_dir),
        }
    })

    cmd_state_repair(_args(state_dir))
    out = capsys.readouterr().out
    assert "expansion incomplete" in out or "cannot resolve" in out

    bs = BuildState(state_dir)
    entry = bs.get("1password")
    assert entry is not None
    assert entry["pkgver"] == "${_tarver//-/_}"


def test_repair_skips_missing_pkgbuild(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    state_dir = tmp_path / "state"
    _write_broken_state(state_dir, {
        "$_pkgname-git": {
            "pkgver": "1.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": "$_pkgname-git",
            "pkgbuild_dir": str(tmp_path / "does-not-exist"),
        }
    })

    cmd_state_repair(_args(state_dir))
    out = capsys.readouterr().out
    assert "PKGBUILD not found" in out

    bs = BuildState(state_dir)
    assert "$_pkgname-git" in bs.all_packages()


def test_repair_noop_when_state_is_clean(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    state_dir = tmp_path / "state"
    _write_broken_state(state_dir, {
        "htop": {
            "pkgver": "3.4.1", "pkgrel": "1", "epoch": "0",
            "pkgbase": "htop",
            "pkgbuild_dir": str(tmp_path / "htop"),
        }
    })

    cmd_state_repair(_args(state_dir))
    out = capsys.readouterr().out
    assert "No broken entries" in out


def test_repair_makes_the_state_repair_migration_promise_true(tmp_path, capsys, monkeypatch):
    """This is the test that makes Task 7's release note honest: a
    build_state.toml whose only defect is the pre-3.0.0 build_mode =
    "profiled" token (name/pkgbase/pkgver/pkgrel/epoch all fine, no PKGBUILD
    needed) is rewritten to "source_built" by `sysforge state repair`, with
    no dependency on the pkgbuild_dir existing.
    """
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    state_dir = tmp_path / "state"
    _write_broken_state(state_dir, {
        "mesa": {
            "pkgver": "24.0.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": "mesa",
            "pkgbuild_dir": str(tmp_path / "does-not-exist"),
            "build_mode": "profiled",
        }
    })

    cmd_state_repair(_args(state_dir))
    out = capsys.readouterr().out
    assert "legacy build_mode" in out

    bs = BuildState(state_dir)
    entry = bs.get("mesa")
    assert entry is not None
    assert entry["build_mode"] == "source_built"


def test_repair_leaves_unrecognized_build_mode_alone(tmp_path, capsys, monkeypatch):
    """An unrecognized-but-not-legacy build_mode (e.g. a typo) must not be
    coerced — only the closed set of known legacy tokens is rewritten.
    """
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    state_dir = tmp_path / "state"
    _write_broken_state(state_dir, {
        "htop": {
            "pkgver": "3.4.1", "pkgrel": "1", "epoch": "0",
            "pkgbase": "htop",
            "pkgbuild_dir": str(tmp_path / "htop"),
            "build_mode": "wat",
        }
    })

    cmd_state_repair(_args(state_dir))
    out = capsys.readouterr().out
    assert "No broken entries" in out

    bs = BuildState(state_dir)
    assert bs.get("htop")["build_mode"] == "wat"


def test_repair_still_preserves_valid_build_mode_in_shell_var_repair(tmp_path, monkeypatch):
    """The existing unexpanded-var repair path still carries a valid
    build_mode over verbatim (no legacy token involved).
    """
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    pkgbuild_dir = tmp_path / "weston-git"
    pkgbuild_dir.mkdir()
    (pkgbuild_dir / "PKGBUILD").write_text(
        "_basename=weston\n"
        'pkgname="$_basename-git"\n'
        "pkgver=14.0.0.r754.gb44cf1b\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
    )

    state_dir = tmp_path / "state"
    _write_broken_state(state_dir, {
        "$_basename-git": {
            "pkgver": "14.0.0.r754.gb44cf1b",
            "pkgrel": "1",
            "epoch": "0",
            "pkgbase": "$_basename-git",
            "pkgbuild_dir": str(pkgbuild_dir),
            "build_mode": "source_built",
        }
    })

    cmd_state_repair(_args(state_dir))

    bs = BuildState(state_dir)
    assert bs.get("weston-git")["build_mode"] == "source_built"


# ── 3.5.0-B42: repaired entries keep their provenance ───────────────────────

_STICKY = {
    "build_mode": '"pgo_llvm"',
    "flags_string": '"CFLAGS=-O2"',
    "built_at": '"2026-03-26T13:29:45Z"',
    "built_upstream_commit": '"abc123"',
    "source": '"aur"',
    "owner_stage": '"toolchain"',
    "toolchain_variant": '"pgo_llvm"',
    "toolchain_fingerprint": '"fp-1"',
    "build_inputs": '"python=3.14"',
    "build_seconds": '"10,20,30"',
    "sandboxed": "true",
}


def _write_raw_state(state_dir: Path, entries: dict[str, dict[str, str]]) -> None:
    """Like _write_broken_state, but values are raw TOML (bools, quoted strings)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for key, fields in entries.items():
        lines.append(f'["{key}"]')
        lines += [f"{k} = {v}" for k, v in fields.items()]
        lines.append("")
    (state_dir / "build_state.toml").write_text("\n".join(lines))


def _foo_pkgbuild(tmp_path: Path) -> Path:
    d = tmp_path / "foo"
    d.mkdir()
    (d / "PKGBUILD").write_text(
        '_base=foo\npkgname="$_base"\npkgver=1.0\npkgrel=2\narch=(x86_64)\n')
    return d


def test_repair_keeps_every_sticky_field(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    d = _foo_pkgbuild(tmp_path)
    state_dir = tmp_path / "state"
    _write_raw_state(state_dir, {"$_base": {
        "pkgver": '"1.0"', "pkgrel": '"2"', "epoch": '"0"',
        "pkgbase": '"$_base"', "pkgbuild_dir": f'"{d}"', **_STICKY,
    }})

    cmd_state_repair(_args(state_dir))

    entry = BuildState(state_dir).get("foo")
    assert entry["pkgbase"] == "foo"
    for key, raw in _STICKY.items():
        want = True if raw == "true" else raw.strip('"')
        assert entry[key] == want, key


def test_repair_keeps_an_unbroken_renamed_member(tmp_path, monkeypatch):
    """A -sysforge member the PKGBUILD's pkgname=() does not list was deleted
    with the broken group and never recreated."""
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    d = _foo_pkgbuild(tmp_path)
    state_dir = tmp_path / "state"
    _write_raw_state(state_dir, {
        "$_base": {"pkgver": '"1.0"', "pkgrel": '"2"', "epoch": '"0"',
                   "pkgbase": '"$_base"', "pkgbuild_dir": f'"{d}"'},
        "foo-sysforge": {"pkgver": '"1.0"', "pkgrel": '"2"', "epoch": '"0"',
                         "pkgbase": '"foo-sysforge"', "pkgbuild_dir": f'"{d}"',
                         "origin_pkgbase": '"foo"', "build_mode": '"pgo_mesa"'},
    })

    cmd_state_repair(_args(state_dir))

    pkgs = BuildState(state_dir).all_packages()
    assert "$_base" not in pkgs
    assert "foo" not in pkgs   # no phantom stock entry beside the rename
    renamed = pkgs["foo-sysforge"]
    assert renamed["origin_pkgbase"] == "foo"
    assert renamed["pkgbase"] == "foo-sysforge"
    # Carried from the old entry, through the same legacy-token rewrite as
    # pass 2: mesa's retired "pgo_mesa" comes back as "pgo" (3.5.0-F17).
    assert renamed["build_mode"] == "pgo"


def test_repair_rebuilds_a_broken_renamed_member_under_its_rename(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    d = _foo_pkgbuild(tmp_path)
    state_dir = tmp_path / "state"
    _write_raw_state(state_dir, {
        "foo-sysforge": {"pkgver": '"$pkgver"', "pkgrel": '"2"', "epoch": '"0"',
                         "pkgbase": '"foo-sysforge"', "pkgbuild_dir": f'"{d}"',
                         "origin_pkgbase": '"foo"', "owner_stage": '"kernel"'},
    })

    cmd_state_repair(_args(state_dir))

    pkgs = BuildState(state_dir).all_packages()
    assert set(pkgs) == {"foo-sysforge"}
    assert pkgs["foo-sysforge"]["pkgver"] == "1.0"
    assert pkgs["foo-sysforge"]["origin_pkgbase"] == "foo"
    assert pkgs["foo-sysforge"]["owner_stage"] == "kernel"


def _legacy_mesa_store(root: Path) -> Path:
    old = root / "pgo-mesa"
    old.mkdir(parents=True)
    (old / "mesa.profdata").write_text("merged", encoding="utf-8")
    (old / "7.profraw").write_text("raw", encoding="utf-8")
    return old


def test_repair_moves_the_legacy_mesa_store(tmp_path, monkeypatch, capsys):
    """3.5.0-F17: the pre-4.0.0 shared pgo-mesa store moves into pgo/mesa,
    even when build_state.toml needs nothing."""
    root = tmp_path / "profiles"
    monkeypatch.setenv("SYSFORGE_PROFILE_STORE", str(root))
    old = _legacy_mesa_store(root)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    cmd_state_repair(_args(state_dir))

    assert (root / "pgo" / "mesa" / "mesa.profdata").read_text() == "merged"
    assert (root / "pgo" / "mesa" / "7.profraw").exists()
    assert not old.exists()


def test_repair_dry_run_leaves_the_legacy_store(tmp_path, monkeypatch, capsys):
    root = tmp_path / "profiles"
    monkeypatch.setenv("SYSFORGE_PROFILE_STORE", str(root))
    old = _legacy_mesa_store(root)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    cmd_state_repair(_args(state_dir, dry_run=True))

    assert "Would move 2 file(s)" in capsys.readouterr().out
    assert (old / "mesa.profdata").exists()
    assert not (root / "pgo").exists()


def test_repair_rewrites_pgo_mesa_to_pgo(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "state"))
    state_dir = tmp_path / "state"
    _write_raw_state(state_dir, {
        "mesa-sysforge": {"pkgver": '"1.0"', "pkgrel": '"1"', "epoch": '"0"',
                          "pkgbase": '"mesa-sysforge"', "build_mode": '"pgo_mesa"'},
    })

    cmd_state_repair(_args(state_dir))

    assert BuildState(state_dir).all_packages()["mesa-sysforge"]["build_mode"] == "pgo"
