"""
test_paths.py — XDG Base Directory + FHS compliance for sysforge's own paths.

Covers the user-side path resolution in ``primitives/paths.py``:
  * XDG-correct defaults when no env vars are set
  * honouring $XDG_CONFIG_HOME / $XDG_CACHE_HOME / $XDG_STATE_HOME
  * BOOTSTRAP_PATH following $SYSFORGE_CONFIG_DIR (FHS nit)
  * migrate_legacy_user_dirs() moving the legacy consolidated dirs into their
    XDG homes (reversal of the old consolidation), with the no-clobber /
    source-absent / never-raise guarantees.

The module computes its constants at import time, so each test reloads the
module under a controlled environment via the ``reload_paths`` fixture, which
restores the real module on teardown.
"""
import importlib

import pytest

import sysforge.primitives.paths as paths

_XDG_VARS = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "SYSFORGE_CONFIG_DIR")


@pytest.fixture
def reload_paths(monkeypatch):
    """Reload paths.py under a controlled env; restore the real module after."""
    def _reload(env: dict):
        for k in _XDG_VARS:
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        importlib.reload(paths)
        return paths

    yield _reload
    # monkeypatch reverts the env; reload once more so the cached module matches
    # the real environment for any later test that imports paths.
    importlib.reload(paths)


# ---------------------------------------------------------------------------
# Defaults (no XDG vars set)
# ---------------------------------------------------------------------------

def test_user_dirs_default_to_xdg_spec(reload_paths, tmp_path):
    p = reload_paths({"HOME": tmp_path})
    assert p.USER_CONFIG_DIR == tmp_path / ".config/sysforge"
    assert p.USER_CACHE_DIR == tmp_path / ".cache/sysforge"
    assert p.USER_STATE_DIR == tmp_path / ".local/state/sysforge"


def test_cache_and_state_are_separate_roots(reload_paths, tmp_path):
    """Regression guard against the old single-root consolidation under config."""
    p = reload_paths({"HOME": tmp_path})
    assert ".config/sysforge" not in str(p.USER_CACHE_DIR)
    assert ".config/sysforge" not in str(p.USER_STATE_DIR)


# ---------------------------------------------------------------------------
# XDG env-var overrides
# ---------------------------------------------------------------------------

def test_user_dirs_honor_xdg_env(reload_paths, tmp_path):
    p = reload_paths({
        "HOME": tmp_path,
        "XDG_CONFIG_HOME": tmp_path / "cfg",
        "XDG_CACHE_HOME": tmp_path / "ca",
        "XDG_STATE_HOME": tmp_path / "st",
    })
    assert p.USER_CONFIG_DIR == tmp_path / "cfg/sysforge"
    assert p.USER_CACHE_DIR == tmp_path / "ca/sysforge"
    assert p.USER_STATE_DIR == tmp_path / "st/sysforge"


# ---------------------------------------------------------------------------
# CONFIG_DIR / config paths follow SYSFORGE_CONFIG_DIR directly
# ---------------------------------------------------------------------------

def test_config_dir_is_env_dir_directly(reload_paths, tmp_path):
    """SYSFORGE_CONFIG_DIR is the config dir itself, not an FHS root prefix —
    the TOML files live directly under it (no etc/sysforge subpath)."""
    p = reload_paths({"SYSFORGE_CONFIG_DIR": tmp_path})
    assert p.CONFIG_DIR == tmp_path
    assert p.BOOTSTRAP_PATH == tmp_path / "bootstrap.toml"
    assert p.PACKAGES_PATH == tmp_path / "packages.toml"
    assert p.KERNEL_PATH == tmp_path / "kernel.toml"
    assert p.TOOLCHAIN_PATH == tmp_path / "toolchain.toml"
    assert p.SYSFORGE_TOML_PATH == tmp_path / "sysforge.toml"
    assert p.CONFIG_PATHS[-1] == tmp_path / "profiles.toml"


def test_config_dir_default_is_fhs_etc(reload_paths):
    """SYSFORGE_CONFIG_DIR unset → the FHS system path /etc/sysforge."""
    p = reload_paths({})
    assert p.CONFIG_DIR == p.Path("/etc/sysforge")
    assert p.BOOTSTRAP_PATH == p.Path("/etc/sysforge/bootstrap.toml")


# ---------------------------------------------------------------------------
# migrate_legacy_user_dirs — reversal of the old consolidation
# ---------------------------------------------------------------------------

def test_migrate_moves_consolidated_dirs_to_xdg(reload_paths, tmp_path):
    p = reload_paths({"HOME": tmp_path})
    legacy_cache = tmp_path / ".config/sysforge/cache"
    legacy_state = tmp_path / ".config/sysforge/state"
    legacy_cache.mkdir(parents=True)
    legacy_state.mkdir(parents=True)
    (legacy_cache / "aur-packages.txt").write_text("pkg\n", encoding="utf-8")
    (legacy_state / "build_state.toml").write_text("x = 1\n", encoding="utf-8")

    p.migrate_legacy_user_dirs()

    assert (p.USER_CACHE_DIR / "aur-packages.txt").read_text(encoding="utf-8") == "pkg\n"
    assert (p.USER_STATE_DIR / "build_state.toml").read_text(encoding="utf-8") == "x = 1\n"
    assert not legacy_cache.exists()
    assert not legacy_state.exists()


def test_migrate_does_not_clobber_existing_target(reload_paths, tmp_path):
    p = reload_paths({"HOME": tmp_path})
    legacy_cache = tmp_path / ".config/sysforge/cache"
    legacy_cache.mkdir(parents=True)
    (legacy_cache / "aur-packages.txt").write_text("OLD\n", encoding="utf-8")
    # Target already populated (e.g. user already on an XDG-correct version).
    p.USER_CACHE_DIR.mkdir(parents=True)
    (p.USER_CACHE_DIR / "aur-packages.txt").write_text("NEW\n", encoding="utf-8")

    p.migrate_legacy_user_dirs()

    # Target wins; the legacy dir is left untouched (informational only).
    assert (p.USER_CACHE_DIR / "aur-packages.txt").read_text(encoding="utf-8") == "NEW\n"
    assert legacy_cache.exists()


def test_migrate_source_absent_is_noop(reload_paths, tmp_path):
    p = reload_paths({"HOME": tmp_path})
    # No legacy dirs at all → nothing created, no error.
    p.migrate_legacy_user_dirs()
    assert not p.USER_CACHE_DIR.exists()
    assert not p.USER_STATE_DIR.exists()


def test_migrate_never_raises_on_oserror(reload_paths, tmp_path, monkeypatch):
    p = reload_paths({"HOME": tmp_path})
    (tmp_path / ".config/sysforge/cache").mkdir(parents=True)

    def _boom(*_a, **_k):
        raise OSError("disk on fire")

    monkeypatch.setattr(p.shutil, "move", _boom)
    # Must swallow the error and not propagate.
    p.migrate_legacy_user_dirs()
