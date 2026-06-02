"""Smoke tests for the centralized external-isolation fixtures (conftest.py).

These guard the single-seam guarantees the behavior-first rewrite relies on:
patching ``subprocess.run`` / ``shutil.which`` / ``builtins.input`` once
intercepts every caller, and ``state_dir`` wires ``SYSFORGE_STATE_DIR``.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_make_proc(make_proc):
    p = make_proc(returncode=1, stdout="x")
    assert p.returncode == 1
    assert p.stdout == "x"


def test_fake_run_records_and_responds(fake_run):
    fake_run.respond(["pacman", "-Q"], stdout="linux 6.9\n")
    out = subprocess.run(["pacman", "-Q"], capture_output=True, text=True)
    assert out.stdout == "linux 6.9\n"
    # Unmatched call falls through to the default (rc 0).
    other = subprocess.run(["true"])
    assert other.returncode == 0
    assert fake_run.commands == ["pacman -Q", "true"]


def test_fake_run_substring_match(fake_run):
    fake_run.respond("makepkg", returncode=0, stdout="built")
    out = subprocess.run(["env", "makepkg", "-s"], capture_output=True, text=True)
    assert out.stdout == "built"


def test_fake_run_check_raises_on_nonzero(fake_run):
    fake_run.respond("boom", returncode=2)
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(["boom"], check=True)


def test_fake_which_only_finds_registered(fake_which):
    assert shutil.which("clang") is None
    fake_which.add("clang", "lld")
    assert shutil.which("clang") == "/usr/bin/clang"
    # Basename resolution so absolute paths work too.
    assert shutil.which("/some/path/lld") == "/usr/bin/lld"
    assert shutil.which("ghc") is None


def test_fake_input_queue(fake_input):
    fake_input.push("y", "name")
    assert input("confirm? ") == "y"
    assert input("name? ") == "name"
    assert fake_input.prompts == ["confirm? ", "name? "]
    with pytest.raises(EOFError):
        input()


def test_state_dir_sets_env(state_dir):
    assert os.environ["SYSFORGE_STATE_DIR"] == str(state_dir)
    from sysforge.pipeline.state import resolve_state_dir

    chosen, source = resolve_state_dir()
    assert source == "SYSFORGE_STATE_DIR"
    assert Path(chosen).resolve() == state_dir.resolve()


def test_no_network_blocks(no_network):
    import urllib.request

    with pytest.raises(AssertionError):
        urllib.request.urlopen("https://example.invalid")
