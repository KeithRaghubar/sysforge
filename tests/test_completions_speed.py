# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Guards that keep shell completion fast (3.5.0-B124).

``sysforge completions`` runs on every Tab press. These pin the reasons it is
fast rather than a wall-clock time, so they pass or fail the same on any
machine: the fast path imports no command module, a fresh ``packages`` cache
runs no pacman, output is capped by ``--limit``, and both completion scripts
use the filtered call. ``make bench-completions`` prints real timings."""
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

import sysforge.completions_data as completions_data

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "completions"

# Modules the fast path must never load: the registry and runner (which import
# every command), the heavy command modules themselves, the progress UI, and
# importlib.metadata (sysforge.__version__ resolves lazily for this).
_FORBIDDEN = (
    "sysforge.verbs",
    "sysforge.ui.progress",
    "sysforge.build_cmd",
    "sysforge.update",
    "sysforge.doctor",
    "sysforge.bench_core",
    "sysforge.pipeline",
    "importlib.metadata",
)

_LOADED_MODULES = """
import json, sys
sys.argv = ["sysforge", "completions", *sys.argv[1:]]
import sysforge.cli as cli
try:
    cli.main()
except SystemExit:
    pass
sys.stderr.write("\\n" + json.dumps(sorted(sys.modules)))
"""


@pytest.mark.parametrize("resource", ["local", "manifest", "state"])
def test_fast_path_imports_no_command_module(resource, tmp_path):
    env = {**os.environ, "SYSFORGE_STATE_DIR": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-c", _LOADED_MODULES, resource],
        capture_output=True, text=True, env=env, cwd=_ROOT, check=False,
    )
    loaded = json.loads(proc.stderr.rsplit("\n", 1)[-1])
    leaked = sorted(m for m in loaded
                    if any(m == f or m.startswith(f + ".") for f in _FORBIDDEN))
    assert not leaked, f"`completions {resource}` imported {leaked}"


# --- the `packages` cache ---------------------------------------------------

@pytest.fixture
def universe(monkeypatch, tmp_path):
    """A fake sync dir, AUR list and src dir, with pacman stubbed and counted."""
    sync = tmp_path / "sync"
    sync.mkdir()
    for db in ("core.db", "extra.db"):
        (sync / db).write_bytes(b"")
    aur = tmp_path / "aur-packages.txt"
    aur.write_text("aur-one\nrepo-one\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(completions_data, "_SYNC_DB_DIR", sync)
    monkeypatch.setattr(completions_data, "PACKAGES_CACHE", tmp_path / "cache.txt")
    monkeypatch.setattr("sysforge.primitives.aur.AUR_CACHE_PATH", aur)
    monkeypatch.setattr(completions_data, "load_config",
                        lambda: {"paths": {"pkgbuild_src_dir": str(src)}})
    calls = []

    def _probe(argv, **_kw):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="repo-one\nrepo-two\n",
                                     stderr="")

    monkeypatch.setattr("sysforge.primitives.run.probe", _probe)
    return types.SimpleNamespace(sync=sync, aur=aur, src=src, calls=calls)


def _emit(capsys, resource="packages", prefix="", limit=0) -> list[str]:
    completions_data.emit(types.SimpleNamespace(resource=resource, prefix=prefix,
                                                limit=limit))
    return capsys.readouterr().out.splitlines()


def _touch_later(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_fresh_cache_runs_no_pacman(universe, capsys):
    first = _emit(capsys)
    assert len(universe.calls) == 1
    assert _emit(capsys) == first == ["repo-one", "repo-two", "aur-one"]
    assert len(universe.calls) == 1  # second Tab: served from the cache


@pytest.mark.parametrize("change", ["sync-db", "new-repo", "aur-list", "src-dir"])
def test_changed_input_rebuilds_cache_once(universe, capsys, change):
    _emit(capsys)
    if change == "sync-db":
        _touch_later(universe.sync / "core.db")
    elif change == "new-repo":
        (universe.sync / "multilib.db").write_bytes(b"")
    elif change == "aur-list":
        universe.aur.write_text("aur-two\n", encoding="utf-8")
        _touch_later(universe.aur)
    else:
        (universe.src / "local-pkg").mkdir()
        (universe.src / "local-pkg" / "PKGBUILD").write_text("", encoding="utf-8")
        _touch_later(universe.src)
    _emit(capsys)
    _emit(capsys)
    assert len(universe.calls) == 2  # one rebuild, then cached again


def test_src_pkgbuilds_lead_the_universe(universe, capsys):
    (universe.src / "repo-two").mkdir()
    (universe.src / "repo-two" / "PKGBUILD").write_text("", encoding="utf-8")
    assert _emit(capsys) == ["repo-two", "repo-one", "aur-one"]


def test_unwritable_cache_still_prints(universe, capsys, monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")  # a file where the cache dir should be
    monkeypatch.setattr(completions_data, "PACKAGES_CACHE", blocker / "cache.txt")
    assert _emit(capsys) == ["repo-one", "repo-two", "aur-one"]


# --- output is capped -------------------------------------------------------

def test_prefix_and_limit_cap_the_output(universe, capsys):
    universe.aur.write_text("".join(f"a{i}\n" for i in range(5000)), encoding="utf-8")
    assert _emit(capsys, prefix="a1", limit=201) == [f"a{i}" for i in
        [1, *range(10, 20), *range(100, 200), *range(1000, 1090)]]
    assert len(_emit(capsys, limit=201)) == 201
    assert _emit(capsys, prefix="repo-") == ["repo-one", "repo-two"]


def test_prefix_applies_to_every_resource(monkeypatch, tmp_path, capsys):
    for name in ("cosmic-comp", "cosmic-term", "mesa"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "PKGBUILD").write_text("", encoding="utf-8")
    monkeypatch.setattr(completions_data, "load_config",
                        lambda: {"paths": {"pkgbuild_src_dir": str(tmp_path)}})
    assert _emit(capsys, "local", prefix="cosmic-", limit=1) == ["cosmic-comp"]


# --- the scripts use the filtered call --------------------------------------

@pytest.mark.parametrize("script", ["_sysforge", "sysforge.bash"])
def test_scripts_let_sysforge_filter_the_universe(script):
    text = (_SCRIPTS / script).read_text(encoding="utf-8")
    calls = [ln for ln in text.splitlines()
             if re.search(r"sysforge completions packages\b", ln)]
    assert calls, f"{script} no longer completes from `completions packages`"
    for ln in calls:
        assert "--prefix=" in ln and "--limit=" in ln, ln
        assert "| grep" not in ln, f"{script} filters in the shell again: {ln.strip()}"
