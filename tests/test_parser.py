#!/usr/bin/env python

import pprint
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

result = parse_pkgbuild("TEST_PKGBUILD")
pprint.pprint(result)
