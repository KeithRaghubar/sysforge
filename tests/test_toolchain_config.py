# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/config.py + constants.py — toolchain.toml parsing and defaults.
"""
import dataclasses
from pathlib import Path
from sysforge.pipeline.stages.toolchain.config import ToolchainConfig
from sysforge.pipeline.stages.toolchain.config import load_toolchain_config
from sysforge.pipeline.stages.toolchain.config import package_lists
from sysforge.pipeline.stages.toolchain.config import resolve_training_corpus
from sysforge.pipeline.stages.toolchain.constants import DEFAULT_LLVM_LIB32
from sysforge.pipeline.stages.toolchain.constants import DEFAULT_LLVM_NON_PGO
from sysforge.pipeline.stages.toolchain.constants import DEFAULT_LLVM_PGO
from sysforge.pipeline.stages.toolchain.constants import PGO_ALLOWED_MAKEPKG_FLAGS
from sysforge.pipeline.stages.toolchain.constants import PROFRAW_SETTLE_SECS
from sysforge.pipeline.stages.toolchain.profdata import do_profraw_merge
from sysforge.pipeline.stages.toolchain.profdata import profraw_merge_daemon
from unittest.mock import MagicMock
from unittest.mock import patch
import pytest
import threading

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    _make_old_profraw,
    _toolchain_gates_clean,
    fake_profdata_merge,
)


def test_load_toolchain_config_absent_returns_none(tmp_path):
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH",
               tmp_path / "nonexistent.toml"):
        result = load_toolchain_config()
    assert result is None

def test_load_toolchain_config_reads_toml(tmp_path):
    p = tmp_path / "toolchain.toml"
    p.write_text('compiler = "llvm"\npgo = false\n')
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", p):
        result = load_toolchain_config()
    assert result == {"compiler": "llvm", "pgo": False}

def test_load_toolchain_config_bad_toml_raises(tmp_path):
    p = tmp_path / "toolchain.toml"
    p.write_text("compiler = [[[bad toml")
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", p):
        with pytest.raises(RuntimeError, match="Failed to parse"):
            load_toolchain_config()

def test_resolve_pgo_store_default_is_var_cache(monkeypatch):
    from sysforge.primitives.makepkg_pgo import resolve_pgo_store
    monkeypatch.delenv("SYSFORGE_PGO_STORE", raising=False)
    assert resolve_pgo_store(None) == Path("/var/cache/sysforge/llvm-pgo")
    assert resolve_pgo_store({}) == Path("/var/cache/sysforge/llvm-pgo")

def test_resolve_pgo_store_env_override(monkeypatch):
    from sysforge.primitives.makepkg_pgo import resolve_pgo_store
    monkeypatch.setenv("SYSFORGE_PGO_STORE", "/tmp/env-pgo")
    assert resolve_pgo_store(None) == Path("/tmp/env-pgo")

def test_resolve_pgo_store_config_wins_over_env(monkeypatch):
    from sysforge.primitives.makepkg_pgo import resolve_pgo_store
    monkeypatch.setenv("SYSFORGE_PGO_STORE", "/tmp/env-pgo")
    assert resolve_pgo_store({"pgo_store": "/cfg/pgo"}) == Path("/cfg/pgo")

def test_resolve_profile_store_root_default(monkeypatch):
    from sysforge.primitives.makepkg_pgo import resolve_profile_store_root
    monkeypatch.delenv("SYSFORGE_PROFILE_STORE", raising=False)
    assert resolve_profile_store_root(None) == Path("/var/cache/sysforge")

def test_resolve_profile_store_root_env_then_config(monkeypatch):
    from sysforge.primitives.makepkg_pgo import resolve_profile_store_root
    monkeypatch.setenv("SYSFORGE_PROFILE_STORE", "/tmp/env-prof")
    assert resolve_profile_store_root(None) == Path("/tmp/env-prof")
    # config wins over env
    assert resolve_profile_store_root({"profile_store": "/cfg/prof"}) == Path("/cfg/prof")

def test_resolve_method_store_instr_pgo_aliases_legacy(monkeypatch):
    # instr-pgo must keep returning the legacy llvm-pgo location so the existing
    # pgo_store / SYSFORGE_PGO_STORE overrides stay authoritative.
    from sysforge.primitives.makepkg_pgo import resolve_method_store
    monkeypatch.delenv("SYSFORGE_PGO_STORE", raising=False)
    assert resolve_method_store(None, "instr-pgo") == Path("/var/cache/sysforge/llvm-pgo")
    monkeypatch.setenv("SYSFORGE_PGO_STORE", "/tmp/legacy")
    assert resolve_method_store(None, "instr-pgo") == Path("/tmp/legacy")

def test_resolve_method_store_sibling_subdirs(monkeypatch):
    from sysforge.primitives.makepkg_pgo import resolve_method_store
    monkeypatch.delenv("SYSFORGE_PROFILE_STORE", raising=False)
    assert resolve_method_store(None, "autofdo") == Path("/var/cache/sysforge/autofdo")
    assert resolve_method_store(None, "bolt") == Path("/var/cache/sysforge/bolt")

def test_resolve_method_store_target_namespacing(monkeypatch):
    from sysforge.primitives.makepkg_pgo import resolve_method_store
    monkeypatch.delenv("SYSFORGE_PROFILE_STORE", raising=False)
    assert resolve_method_store(None, "propeller", "linux-sysforge") == Path(
        "/var/cache/sysforge/propeller/linux-sysforge"
    )

def test_resolve_method_store_rejects_unknown_method():
    from sysforge.primitives.makepkg_pgo import resolve_method_store
    with pytest.raises(ValueError, match="unknown profile method"):
        resolve_method_store(None, "nonsense")

def test_package_lists_llvm_defaults():
    pgo, non_pgo, lib32 = package_lists({"compiler": "llvm", "pgo": True})
    assert pgo == DEFAULT_LLVM_PGO
    assert non_pgo == DEFAULT_LLVM_NON_PGO
    assert lib32 == DEFAULT_LLVM_LIB32

def test_package_lists_custom_override():
    tcfg = {
        "compiler": "llvm",
        "pgo": True,
        "packages": {
            "pgo": ["llvm", "clang"],
            "non_pgo": ["compiler-rt"],
            "lib32": [],
        },
    }
    pgo, non_pgo, lib32 = package_lists(tcfg)
    assert pgo == ["llvm", "clang"]
    assert non_pgo == ["compiler-rt"]
    assert lib32 == []

def test_training_corpus_default_is_llvm_only():
    # Default ["llvm"] means no *extra* targets — historical behaviour.
    assert resolve_training_corpus({}) == []
    assert resolve_training_corpus({"packages": {}}) == []

def test_training_corpus_strips_implicit_llvm_base():
    # "llvm" is the implicit base; only the extras come back.
    assert resolve_training_corpus(
        {"packages": {"training_corpus": ["llvm", "mesa"]}}
    ) == ["mesa"]

def test_training_corpus_mesa_only_without_llvm():
    # Omitting "llvm" still yields mesa (the self-build always trains LLVM
    # regardless, so the base is implicit either way).
    assert resolve_training_corpus(
        {"packages": {"training_corpus": ["mesa"]}}
    ) == ["mesa"]

def test_training_corpus_drops_unknown_members(capsys):
    out = resolve_training_corpus(
        {"packages": {"training_corpus": ["mesa", "qt6", "llvm"]}}
    )
    assert out == ["mesa"]  # unknown "qt6" dropped

def test_training_corpus_dedups_and_coerces_string():
    assert resolve_training_corpus(
        {"packages": {"training_corpus": "mesa"}}
    ) == ["mesa"]
    assert resolve_training_corpus(
        {"packages": {"training_corpus": ["mesa", "mesa", "llvm"]}}
    ) == ["mesa"]

def test_do_profraw_merge_adaptive_shrink_on_failure(tmp_path):
    """On merge failure, batch size halves and retries the same position."""
    for i in range(4):
        _make_old_profraw(tmp_path / f"p{i}.profraw")

    attempts = []
    call_n = [0]
    def fail_first_only(cmd, **kwargs):
        call_n[0] += 1
        raws = [a for a in cmd if a.endswith(".profraw")]
        attempts.append(len(raws))
        result = MagicMock()
        result.stderr = "OOM"
        result.returncode = 1 if call_n[0] == 1 else 0  # first call fails
        if result.returncode == 0:
            out_idx = cmd.index("--output")
            Path(cmd[out_idx + 1]).touch()
        return result

    with patch("sysforge.pipeline.stages.toolchain.constants.PROFRAW_MERGE_BATCH_MAX", 4), \
         patch("sysforge.pipeline.stages.toolchain.constants.PROFRAW_MERGE_BATCH_MIN", 1), \
         patch("subprocess.run", side_effect=fail_first_only):
        count, n_batches = do_profraw_merge(tmp_path, "test")

    # First attempt: batch=4 (max) → fails
    # Second attempt: batch=2 → succeeds, then another batch=2 → succeeds
    assert attempts[0] == 4   # first try at max batch
    assert attempts[1] == 2   # retry at half
    assert count == 4
    assert n_batches == 2

def test_profraw_merge_daemon_merges_on_wakeup(tmp_path):
    """Daemon merges profraw files when stop_event fires while raws exist."""
    _make_old_profraw(tmp_path / "a.profraw")
    stop_event = threading.Event()

    with patch("sysforge.pipeline.stages.toolchain.constants.PGO_MERGE_INTERVAL", 0), \
         patch("subprocess.run", side_effect=fake_profdata_merge):
        t = threading.Thread(target=profraw_merge_daemon,
                             args=(tmp_path, stop_event), daemon=True)
        t.start()
        t.join(timeout=2)
        stop_event.set()
        t.join(timeout=2)

    assert not (tmp_path / "a.profraw").exists()
    assert (tmp_path / "clang.profdata").exists()

def test_settle_secs_constant_is_positive():
    assert PROFRAW_SETTLE_SECS > 0

def test_pgo_allowed_flags_whitelist():
    assert "-f" in PGO_ALLOWED_MAKEPKG_FLAGS
    assert "--force" in PGO_ALLOWED_MAKEPKG_FLAGS


# ---------------------------------------------------------------------------
# ToolchainConfig — one home for every default (3.2.0-F6)
# ---------------------------------------------------------------------------

def test_from_toml_none_is_none():
    """No toolchain.toml keeps meaning 'stage is a clean no-op'."""
    assert ToolchainConfig.from_toml(None) is None


def test_empty_toml_gets_documented_defaults():
    cfg = ToolchainConfig.from_toml({})
    assert cfg.enabled is False          # opt-in
    assert cfg.compiler == "gcc"         # register-only path
    assert cfg.skip_build is False
    assert cfg.require_multilib is True
    assert cfg.min_build_free_gb == 40.0
    assert cfg.rebuild_soname_consumers == "prompt"
    assert cfg.pgo_pkgs == tuple(DEFAULT_LLVM_PGO)
    assert cfg.non_pgo_pkgs == tuple(DEFAULT_LLVM_NON_PGO)
    assert cfg.lib32_pkgs == ()          # lib32 is not in the toolchain pass


def test_compiler_default_is_the_same_everywhere():
    """The regression 3.2.0-F6 was written to prevent.

    ``compiler`` used to be defaulted independently at each read: the stage body
    fell back to "gcc" while Gate 1's sentinel fell back to "llvm". A config
    with no ``compiler`` key therefore ran the gcc path but stamped its sentinel
    "llvm". With one parse site there is one answer, and this pins it.
    """
    cfg = ToolchainConfig.from_toml({"enabled": True})
    assert cfg.compiler == "gcc"


def test_pgo_is_meaningless_without_the_llvm_path():
    """`pgo = true` under compiler = "gcc" is not a PGO run.

    The GCC path never builds anything, so the raw flag must not be read as
    "this is a PGO build" by any call site — hence a derived property rather
    than the raw field.
    """
    gcc = ToolchainConfig.from_toml({"compiler": "gcc", "pgo": True})
    assert gcc.pgo is True and gcc.pgo_enabled is False

    llvm = ToolchainConfig.from_toml({"compiler": "llvm", "pgo": True})
    assert llvm.pgo_enabled is True
    assert ToolchainConfig.from_toml(
        {"compiler": "llvm", "pgo": False}).pgo_enabled is False


def test_unknown_soname_mode_warns_and_falls_back_to_prompt(capsys):
    """A typo'd advisory knob warns; it does not abort a toolchain build."""
    cfg = ToolchainConfig.from_toml({"rebuild_soname_consumers": "of"})
    assert cfg.rebuild_soname_consumers == "prompt"
    assert "Unknown rebuild_soname_consumers" in capsys.readouterr().err


def test_soname_mode_accepts_each_documented_value():
    for mode in ("prompt", "auto", "off"):
        cfg = ToolchainConfig.from_toml({"rebuild_soname_consumers": mode})
        assert cfg.rebuild_soname_consumers == mode


def test_config_is_frozen():
    """A stage's configuration is decided at entry and cannot drift mid-run."""
    cfg = ToolchainConfig.from_toml({})
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.compiler = "llvm"


def test_raw_survives_for_the_two_whole_table_consumers():
    """resolve_pgo_store and the Pass-4 config_digest both take the raw table.

    The digest in particular must keep hashing the raw dict: hashing a field
    list instead would change every existing cache key and silently invalidate
    every user's reuse cache.
    """
    data = {"enabled": True, "pgo_store": "/tmp/store"}
    assert ToolchainConfig.from_toml(data).raw == data


def test_training_corpus_is_resolved_at_parse_time():
    """Extras only — "llvm" is the implicit base and is stripped."""
    assert ToolchainConfig.from_toml({}).training_corpus == ()
    cfg = ToolchainConfig.from_toml(
        {"packages": {"training_corpus": ["llvm", "mesa"]}})
    assert cfg.training_corpus == ("mesa",)
