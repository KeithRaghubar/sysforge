"""
conftest.py — shared pytest configuration for SysForge tests.

Sets SYSFORGE_CONFIG_DIR to point at tests/data before any module imports,
so load_config() and other path-sensitive functions resolve test fixtures
rather than /etc/sysforge.
"""
import os
from pathlib import Path

# Must be set before importing any sysforge module that reads CONFIG_BASE
# at import time.
TESTS_DIR = Path(__file__).parent
TEST_DATA = TESTS_DIR / "data"

os.environ.setdefault("SYSFORGE_CONFIG_DIR", str(TEST_DATA))
