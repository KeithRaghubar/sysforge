# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
build_throttle.py — CPU/IO throttling for makepkg builds

Resolves the build-throttling knobs (``nice``, ``ionice``, ``cpu_quota``,
``jobs``) from ``sysforge.toml [build]`` with per-profile overrides, and turns
them into either a command-prefix that wraps the ``makepkg`` invocation
(scheduling/IO priority and a hard CPU ceiling) or a rewritten ``MAKEFLAGS``
``-j`` token (parallelism).

Two delivery channels, by mechanism:

  * ``nice`` / ``ionice`` / ``cpu_quota`` → :func:`wrapper_argv` builds an argv
    prefix prepended to the ``makepkg`` command at the subprocess chokepoint
    (``makepkg_invoke.invoke_makepkg``). ``cpu_quota`` wraps the build in a
    transient ``systemd-run --scope`` so the cgroup ``CPUQuota`` applies; the
    scope keeps the controlling TTY so interactive prompts still work.
  * ``jobs`` → :func:`apply_jobs_to_makeflags` rewrites the ``-jN`` token in the
    ``MAKEFLAGS`` value written to the temp makepkg.conf
    (``makepkg_conf.emit_makepkg_conf``).

This module is the single home for build-throttle resolution and argv
construction — don't add a parallel ``nice``/``systemd-run`` path elsewhere.
The resolver is pure (no logging); :func:`wrapper_argv` logs only when it has
to drop a piece because the underlying tool is missing, so a throttling tool
that isn't installed degrades to "no throttle" rather than failing the build.
Owns the ``[THROTTLE]`` tag.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass

from sysforge import log

_throttle_log = log.get_logger("THROTTLE")

# ionice scheduling-class names accepted in config → the numeric class passed
# to ``ionice -c`` (3 = idle, 2 = best-effort). realtime (1) is intentionally
# unsupported: it needs CAP_SYS_ADMIN and can starve the rest of the system,
# the opposite of what this feature is for.
_IONICE_CLASSES = {"idle": "3", "best-effort": "2"}

# A CPUQuota is "N%" where N is a positive integer; 100% == one core's worth.
_CPU_QUOTA_RE = re.compile(r"^\d+%$")

# Matches a make ``-jN`` / ``-j N`` (we only rewrite the attached form) or a
# GNU long ``--jobs=N`` token, so an existing job count can be replaced in place.
_JOBS_RE = re.compile(r"(?:^|(?<=\s))(?:-j\s*\d+|--jobs=\d+)")


@dataclass(frozen=True)
class BuildThrottle:
    """Resolved build-throttle settings (all ``None`` == unset / no throttle)."""

    nice: int | None = None
    ionice: str | None = None
    cpu_quota: str | None = None
    jobs: int | None = None

    @property
    def is_noop(self) -> bool:
        return (
            self.nice is None
            and self.ionice is None
            and self.cpu_quota is None
            and self.jobs is None
        )


def _coerce_nice(raw) -> int | None:
    """Clamp a configured niceness into the unprivileged 0..19 range, or drop it."""
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        _throttle_log.warn(f"[build] nice = {raw!r} is not an integer — ignoring")
        return None
    if n < 0 or n > 19:
        clamped = max(0, min(19, n))
        _throttle_log.warn(
            f"[build] nice = {n} out of range 0..19 — clamping to {clamped} "
            "(negative niceness needs privilege and is not the intent here)"
        )
        n = clamped
    return n


def _coerce_ionice(raw) -> str | None:
    if raw is None:
        return None
    val = str(raw).strip().lower()
    if val in _IONICE_CLASSES:
        return val
    _throttle_log.warn(
        f"[build] ionice = {raw!r} is not one of {sorted(_IONICE_CLASSES)} — ignoring"
    )
    return None


def _coerce_cpu_quota(raw) -> str | None:
    if raw is None:
        return None
    val = str(raw).strip()
    if _CPU_QUOTA_RE.match(val):
        return val
    _throttle_log.warn(
        f"[build] cpu_quota = {raw!r} must look like \"600%\" (N%, 100%=one core) — ignoring"
    )
    return None


def _coerce_jobs(raw) -> int | None:
    if raw is None:
        return None
    try:
        j = int(raw)
    except (TypeError, ValueError):
        _throttle_log.warn(f"[build] jobs = {raw!r} is not an integer — ignoring")
        return None
    if j < 1:
        _throttle_log.warn(f"[build] jobs = {j} must be >= 1 — ignoring")
        return None
    return j


def resolve_throttle(resolved_profile, config: dict | None = None) -> BuildThrottle:
    """Resolve build-throttle settings: ``sysforge.toml [build]`` defaults,
    overridden per-key by the resolved profile.

    ``resolved_profile`` may carry ``nice`` / ``ionice`` / ``cpu_quota`` / ``jobs``
    (sysforge-internal profile keys; see ``profile.SYSFORGE_KEYS``). A key present
    on the profile wins over the global default; an absent key falls back to it.

    ``config`` is the parsed ``sysforge.toml`` dict; loaded on demand when ``None``
    (mirrors ``makepkg_env.resolve_build_python``). Pure — never raises; each
    malformed value is dropped with a warning by its ``_coerce_*`` helper.
    """
    if config is None:
        from sysforge.primitives.config import load_sysforge_toml
        config = load_sysforge_toml()

    build_cfg = config.get("build") or {}
    prof = resolved_profile or {}

    def pick(key):
        # Profile override wins when the key is present (even if falsey); else
        # the global [build] default.
        return prof[key] if key in prof else build_cfg.get(key)

    return BuildThrottle(
        nice=_coerce_nice(pick("nice")),
        ionice=_coerce_ionice(pick("ionice")),
        cpu_quota=_coerce_cpu_quota(pick("cpu_quota")),
        jobs=_coerce_jobs(pick("jobs")),
    )


def wrapper_argv(throttle: BuildThrottle) -> list[str]:
    """Build the argv prefix that wraps the ``makepkg`` invocation for the
    scheduling/IO/quota knobs (``jobs`` is delivered separately, via MAKEFLAGS).

    A hard ``cpu_quota`` requires a cgroup, so it wraps the build in a transient
    ``systemd-run --scope --user`` carrying ``CPUQuota=``. ``nice`` / ``ionice``
    are *always* applied as front-end commands (``nice -n N ionice -c C makepkg``),
    nested inside the scope when a quota is present. They are **not** folded into
    ``systemd-run -p``: ``--scope`` runs the command in the caller's context (so the
    controlling TTY is kept for prompts), so systemd never execs it and rejects exec
    properties like ``Nice=`` / ``IOSchedulingClass=`` ("Unknown assignment").

    Every tool is guarded by :func:`shutil.which`; a missing tool drops just that
    piece with a warning — throttling is best-effort and must never fail a build.
    Returns ``[]`` when nothing applies.
    """
    # nice/ionice front-ends, shared by the quota and no-quota paths. With a
    # systemd scope these run *inside* it (the scope's command), since scope units
    # cannot carry Nice=/IOSchedulingClass= properties.
    front: list[str] = []
    if throttle.nice is not None:
        if shutil.which("nice"):
            front += ["nice", "-n", str(throttle.nice)]
        else:
            _throttle_log.warn("nice not found on PATH — skipping niceness throttle")
    if throttle.ionice is not None:
        if shutil.which("ionice"):
            front += ["ionice", "-c", _IONICE_CLASSES[throttle.ionice]]
        else:
            _throttle_log.warn("ionice not found on PATH — skipping IO-priority throttle")

    if throttle.cpu_quota is not None:
        if shutil.which("systemd-run"):
            return ["systemd-run", "--scope", "--user", "--quiet",
                    "-p", f"CPUQuota={throttle.cpu_quota}"] + front
        # No systemd-run: we cannot enforce the hard ceiling. Fall back to the
        # nice/ionice front-ends so the build is at least de-prioritised.
        _throttle_log.warn(
            "cpu_quota set but systemd-run not found — cannot enforce a hard CPU "
            "ceiling; falling back to nice/ionice only"
        )

    return front


def apply_jobs_to_makeflags(makeflags: str, jobs: int | None) -> str:
    """Return ``makeflags`` with its job count forced to ``jobs``.

    Replaces an existing ``-jN`` / ``--jobs=N`` token in place (normalising to the
    short ``-jN`` form, which make accepts), or appends ``-jN`` when none is
    present. Returns ``makeflags`` unchanged when ``jobs is None``.
    The input may contain shell tokens (e.g. ``-j$(nproc)``) — only an explicit
    numeric job token is rewritten; a ``$(nproc)`` form has no numeric match, so
    the literal ``-jN`` is appended and (being later) wins for make.
    """
    if jobs is None:
        return makeflags
    replacement = f"-j{jobs}"
    new, n = _JOBS_RE.subn(replacement, makeflags)
    if n:
        return new
    return f"{makeflags} {replacement}".strip() if makeflags.strip() else replacement
