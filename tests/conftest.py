"""
conftest.py — shared pytest configuration for SysForge tests.

Sets SYSFORGE_CONFIG_DIR to point at tests/data before any module imports,
so load_config() and other path-sensitive functions resolve test fixtures
rather than /etc/sysforge.
"""
import os
from pathlib import Path

import pytest

# Must be set before importing any sysforge module that reads CONFIG_BASE
# at import time.
TESTS_DIR = Path(__file__).parent
TEST_DATA = TESTS_DIR / "data"

os.environ.setdefault("SYSFORGE_CONFIG_DIR", str(TEST_DATA))

# Force the subprocess fallback in primitives.pacman so existing tests that
# mock subprocess.run continue to drive the query. The pyalpm fast path is
# exercised explicitly in test_pacman_pyalpm.py.
os.environ.setdefault("SYSFORGE_PACMAN_NO_PYALPM", "1")

# Show all log messages in tests so assertions against log output work.
import sysforge.log as _sf_log
_sf_log.set_verbosity(2)


@pytest.fixture(autouse=True)
def _isolate_filesystem_soname_cache(monkeypatch):
    """
    `dep_analysis.soname_available` consults the real /usr/lib (and friends)
    when the supplied ldconfig set misses. Tests assert against synthetic
    state, so default-patch the filesystem probe to an empty set. Tests
    that want to exercise the fallback explicitly override the patch.
    """
    from sysforge.primitives import dep_analysis as _da
    monkeypatch.setattr(_da, "_filesystem_soname_set",
                        lambda lib32=False: frozenset())
