#!/usr/bin/env python
import os
import pprint
import sys
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

sys.path.insert(0, os.path.expanduser("~/src/sysforge"))

PKGBUILD = f"{sys.path[0]}/tests/data/PKGBUILDs/complex2.PKGBUILD"

result = parse_pkgbuild(PKGBUILD)
pprint.pprint(result)
