# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
sysforge.verbs — CLI verb framework.

Every top-level CLI verb is a :class:`Verb` subclass with three phases
(``pre_check``, ``execute``, ``post_validate``) dispatched through
:func:`run_verb`. See DESIGN.md §CLI Verb Framework.
"""
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb
from sysforge.verbs.runner import run_verb

__all__ = ["ExecResult", "PreCheckResult", "Verb", "run_verb"]
