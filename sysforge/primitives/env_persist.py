# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
env_persist.py — plan and apply environment-variable writes to system files.

The write-side counterpart to :mod:`sysforge.primitives.env_chain`, which is
read-only. Two targets are supported, and the syntax difference between them
is load-bearing: ``/etc/environment`` is parsed by pam_env and takes bare
``KEY=value`` lines (no shell, so ``export`` is meaningless there), while
``~/.zshenv`` is sourced by zsh and takes ``export KEY=value``. Writing the
wrong form produces a file ``env_chain`` cannot read back — see the
round-trip test in ``tests/test_env_persist.py``.

Planning is pure (:func:`plan_write`) and application is effectful
(:func:`apply_write`), so the whole conflict matrix is testable without a
filesystem, and a caller can show the user exactly what will change before
anything is written.

Public API:
    system_target() / user_target()  -> EnvTarget
    plan_write(target, variables, existing_text) -> WritePlan
    apply_write(plan) -> None
    format_assignment(name, value, syntax) -> str
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sysforge import log
from sysforge.primitives.privilege import privileged_argv

_log = log.get_logger("ENV")

# Values needing no quoting: an editor command is a bare word in every
# realistic case, but a user may configure "code -w".
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/@%+=,-]+$")

# Characters with no encoding every reader of these files agrees on. A quote
# survives shlex.quote as '…'"'"'…', which neither env_chain._strip_quotes nor
# pam_env parses; a newline in /etc/environment becomes a second, bogus
# assignment line. There is no "escape harder" answer — the file is read by
# pam_env, by env_chain and by the user's shell, and they disagree — so the
# primitive refuses instead of writing something ambiguous.
_UNENCODABLE = ("'", '"', "\n", "\r", "\0")


def _reject_unpersistable(name: str, value: str) -> None:
    """Raise when ``value`` has no form every reader parses identically.

    Called from :func:`format_assignment`, so rejection lands at *plan* time —
    before any file is touched — for every caller, not only the disciplined
    ones. Today's sole caller filters through ``shutil.which`` and can reach
    none of these, which is safety by caller discipline; this makes it safety
    by construction in a primitive whose write target PAM parses at login.
    """
    if not value:
        raise ValueError(f"{name} cannot be persisted: value is empty")
    if value != value.strip():
        raise ValueError(
            f"{name} cannot be persisted: value has leading or trailing "
            "whitespace, which readers strip"
        )
    for ch in _UNENCODABLE:
        if ch in value:
            raise ValueError(
                f"{name} cannot be persisted: value contains {ch!r}, which "
                "has no encoding every reader of this file agrees on"
            )


@dataclass(frozen=True)
class EnvTarget:
    """A file sysforge can persist environment variables into."""
    key: str
    label: str
    path: Path
    syntax: str          # "bare" | "export"
    scope_note: str
    needs_root: bool


def system_target() -> EnvTarget:
    return EnvTarget(
        key="system",
        label="system-wide",
        path=Path("/etc/environment"),
        syntax="bare",
        scope_note="all users, next login",
        needs_root=True,
    )


def user_target() -> EnvTarget:
    """``~/.zshenv`` for the invoking user.

    Resolved per call rather than as a module constant so a changed ``HOME``
    (tests, ``sudo -u``) is honoured instead of frozen at import time.
    """
    home = Path(os.environ.get("HOME", "~")).expanduser()
    return EnvTarget(
        key="user",
        label="this user",
        path=home / ".zshenv",
        syntax="export",
        scope_note="this user, next shell",
        needs_root=False,
    )


@dataclass(frozen=True)
class VarChange:
    """One variable's before/after within a :class:`WritePlan`."""
    name: str
    current: str | None
    new: str

    @property
    def is_change(self) -> bool:
        return self.current != self.new


@dataclass(frozen=True)
class WritePlan:
    """The complete result of applying ``variables`` to one target.

    Covers *all* requested variables, never one at a time: the caller applies
    a plan atomically, so a half-written state with a mismatched
    ``EDITOR``/``VISUAL`` pair is unrepresentable rather than merely avoided.
    """
    target: EnvTarget
    changes: tuple[VarChange, ...]
    action: str          # "create" | "append" | "replace" | "nochange"
    new_text: str


def format_assignment(name: str, value: str, syntax: str) -> str:
    """Render one assignment line, or raise ``ValueError`` on a value that
    cannot round-trip through :mod:`sysforge.primitives.env_chain`."""
    _reject_unpersistable(name, value)
    rendered = value if _SAFE_VALUE.match(value) else shlex.quote(value)
    return f"{name}={rendered}" if syntax == "bare" else f"export {name}={rendered}"


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _assignment_re(name: str, syntax: str) -> re.Pattern[str]:
    """Every assignment form for ``name`` that ``env_chain`` reads back.

    The alternation order mirrors ``env_chain._parse_shell_init_file``: the
    split ``KEY=value; export KEY`` form is matched before the plain form on
    *both* syntaxes, because the reader applies it before its ``allow_bare``
    fallback too. Matching a strict subset of what the reader accepts is what
    makes the planner report ``currently unset`` beneath a chain display
    showing the real value, then append rather than replace.
    """
    n = re.escape(name)
    split = rf"{n}=(?P<split>.*?)\s*;\s*export\s+{n}\s*"
    plain = rf"{n}=(?P<plain>.*)" if syntax == "bare" else rf"export\s+{n}=(?P<plain>.*)"
    return re.compile(rf"^\s*(?:{split}|{plain})$")


def _captured(m: re.Match[str]) -> str:
    """The value from whichever assignment form matched."""
    split = m.group("split")
    return m.group("plain") if split is None else split


def plan_write(
    target: EnvTarget, variables: dict[str, str], existing_text: str | None,
) -> WritePlan:
    """Compute the file content that applies ``variables`` to ``target``.

    ``existing_text`` is ``None`` when the file does not exist. Comment lines
    are ignored. When a variable is assigned more than once, the **last**
    assignment is reported as ``current`` (that is the one a shell actually
    applies) while the **first** is the one rewritten and the rest dropped, so
    the result holds exactly one assignment per variable.
    """
    for name, value in variables.items():
        _reject_unpersistable(name, value)

    lines = [] if existing_text is None else existing_text.splitlines()
    patterns = {name: _assignment_re(name, target.syntax) for name in variables}

    # Locate every assignment per variable, ignoring comments.
    # Store (index, captured_value) tuples to avoid re-matching.
    hits: dict[str, list[tuple[int, str]]] = {name: [] for name in variables}
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        for name, pattern in patterns.items():
            m = pattern.match(line)
            if m:
                hits[name].append((idx, _captured(m)))

    changes = tuple(
        VarChange(
            name=name,
            current=(
                _strip_quotes(hits[name][-1][1])
                if hits[name] else None
            ),
            new=value,
        )
        for name, value in variables.items()
    )

    if all(not c.is_change for c in changes):
        return WritePlan(
            target=target,
            changes=changes,
            action="nochange",
            new_text=existing_text if existing_text is not None else "",
        )

    drop: set[int] = set()
    rewrite: dict[int, str] = {}
    appended: list[str] = []
    for name, value in variables.items():
        rendered = format_assignment(name, value, target.syntax)
        if hits[name]:
            rewrite[hits[name][0][0]] = rendered
            drop.update(idx for idx, _ in hits[name][1:])
        else:
            appended.append(rendered)

    out = [rewrite.get(i, line) for i, line in enumerate(lines) if i not in drop]
    out.extend(appended)
    new_text = "\n".join(out) + "\n" if out else ""

    if existing_text is None:
        action = "create"
    elif rewrite:
        action = "replace"
    else:
        action = "append"

    return WritePlan(target=target, changes=changes, action=action, new_text=new_text)


def apply_write(plan: WritePlan) -> None:
    """Write ``plan.new_text`` to its target.

    A ``nochange`` plan is a no-op — it never touches the file, so re-running
    the step does not churn mtimes. Otherwise the direct path is a plain
    in-place ``write_text`` (no write-temp-then-rename). On ``PermissionError``
    the content is instead staged to a chmod'd temp file and copied into
    place through the privilege seam, escalating for ``/etc/environment``.
    """
    if plan.action == "nochange":
        return

    path = plan.target.path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.new_text, encoding="utf-8")
        return
    except PermissionError:
        pass

    fd, tmp_name = tempfile.mkstemp(suffix=".sysforge-env")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(plan.new_text)
        tmp.chmod(0o644)
        _log.info(f"  Writing (sudo): {path}")
        rc = subprocess.run(privileged_argv(["cp", str(tmp), str(path)])).returncode
        if rc != 0:
            raise OSError(f"sudo cp exited {rc} — {path} unchanged")
    finally:
        tmp.unlink(missing_ok=True)
