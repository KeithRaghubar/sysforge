#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

# Pre-commit hook (`make install-hooks`, 3.5.0-DEV18): the fast gates, so most
# CI failures show up before a push. The test suite stays in CI and
# `make pre-release`. Skip once with `git commit --no-verify`.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec make -s --no-print-directory lint check-shipped check-design check-standards check-surface
