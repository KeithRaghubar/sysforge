#!/usr/bin/env python
import pprint
import sys
from pathlib import Path

from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

TESTS_DIR = Path(__file__).parent
PKGBUILD = TESTS_DIR / "data/PKGBUILDs/complex2.PKGBUILD"

result = parse_pkgbuild(PKGBUILD)
pprint.pprint(result)
