# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
sysforge.build — the build-orchestration layer (3.2.0-F4).

Sits between ``primitives/`` (the leaf layer) and the verbs and pipeline
stages. Its modules coordinate many primitives — the per-package makepkg
lifecycle in :mod:`makepkg_wrapper` (prepare, invoke, artifact discovery,
build-state recording, install) and the AUR dependency builds in
:mod:`aur_deps` — which is why they cannot live in ``primitives/``: a
primitive with thirty sysforge dependencies is a coordinator in the wrong
drawer, and every primitive it imports would be one edit away from a cycle.

Layering, enforced by ``tests/test_module_layering.py``: ``primitives`` never
imports ``sysforge.build``; ``build_core`` (the shared ``build``/``update``
engine) and the stages use it.
"""
