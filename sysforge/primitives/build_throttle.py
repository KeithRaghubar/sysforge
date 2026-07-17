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

  * ``nice`` / ``ionice`` / ``cpu_quota`` / ``mem_limit`` → :func:`wrapper_argv`
    builds an argv prefix prepended to the ``makepkg`` command at the subprocess
    chokepoint (``makepkg_invoke.invoke_makepkg``). A ``cpu_quota`` or a
    ``mem_limit`` wraps the build in a transient ``systemd-run --scope`` so the
    cgroup ``CPUQuota``/``MemoryMax`` applies kernel-hierarchically over the whole
    fork tree (the escapable ``RLIMIT_AS`` preexec is a non-systemd fallback for
    ``mem_limit``); the scope keeps the controlling TTY so interactive prompts
    still work.
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

import os
import re
import shutil
from dataclasses import dataclass

from sysforge import log

_throttle_log = log.get_logger("THROTTLE")

# Niceness applied by the "boost" run-override (2.1.0-F5). Negative == *higher*
# than default scheduling priority; it deliberately bypasses the unprivileged
# 0..19 clamp in ``_coerce_nice`` because the user asked to go faster. Lowering
# niceness needs privilege, so ``wrapper_argv``'s best-effort ``nice`` front-end
# simply runs at the current priority if the kernel refuses — never a hard fail.
_BOOST_NICE = -5

# Run-scoped throttle override set once at CLI startup (``set_run_override``),
# read by ``resolve_throttle`` when no explicit ``override`` is passed. Keeps the
# global --no-throttle/--turbo flags routed through this one throttle home rather
# than threading a parameter through every makepkg call site. One of:
#   None      — honour the configured throttle
#   "bypass"  — ignore the throttle entirely for this run (--no-throttle)
#   "boost"   — run at higher-than-default priority for this run (--turbo)
_RUN_OVERRIDE: str | None = None


def set_run_override(mode: str | None) -> None:
    """Set the process-global throttle override (``None``/``"bypass"``/``"boost"``).

    Called once from ``cli._main`` after argument parsing, mirroring
    ``log.set_color_mode`` / ``log.set_verbosity``.
    """
    global _RUN_OVERRIDE
    _RUN_OVERRIDE = mode

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

# A memory size: an integer or decimal with an optional binary suffix. Suffixes
# are binary (K=1024) to match systemd's MemoryMax / the RLIMIT_AS byte count;
# a bare number is already bytes. "B" is accepted (and no-op) for symmetry.
_MEM_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT]i?B?|B)?$", re.IGNORECASE)
_MEM_SUFFIX_POW = {"": 0, "b": 0, "k": 1, "m": 2, "g": 3, "t": 4}


@dataclass(frozen=True)
class BuildThrottle:
    """Resolved build-throttle settings (all ``None`` == unset / no throttle)."""

    nice: int | None = None
    ionice: str | None = None
    cpu_quota: str | None = None
    jobs: int | None = None
    mem_limit_bytes: int | None = None

    @property
    def is_noop(self) -> bool:
        return (
            self.nice is None
            and self.ionice is None
            and self.cpu_quota is None
            and self.jobs is None
            and self.mem_limit_bytes is None
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
    cores = os.cpu_count() or 1
    # Resolve both accepted forms to a single integer percentage so the
    # overshoot check (2.3.0-F7) guards one converged value — both the absolute
    # N% form and the fraction form can exceed the host's core count.
    pct: int | None = None
    if _CPU_QUOTA_RE.match(val):
        pct = int(val[:-1])
    elif "%" not in val and "." in val:
        # Relative form (2.1.0-F6): a decimal fraction of the host's total cores,
        # e.g. 0.5 on a 16-core box → 800%, so the same config is portable across
        # machines. Only a value carrying a decimal point is treated as a fraction;
        # a bare integer without "%" stays an error (too ambiguous vs. a percent).
        try:
            frac = float(val)
        except ValueError:
            frac = None
        if frac is not None:
            if frac <= 0:
                _throttle_log.warn(
                    f"[build] cpu_quota = {raw!r} (fraction of cores) must be > 0 — ignoring"
                )
                return None
            pct = max(1, round(frac * cores * 100))
    if pct is None:
        _throttle_log.warn(
            f"[build] cpu_quota = {raw!r} must look like \"600%\" (N%, 100%=one core) "
            "or a fraction of total cores (e.g. 0.5) — ignoring"
        )
        return None
    # Warn-only (2.3.0-F7): a quota above cpu_count*100 asks for more cores than
    # exist. systemd's own effective cap does the harmless clamping, so we keep
    # the value and just signal the likely typo / copied-from-a-bigger-box config.
    if pct > cores * 100:
        _throttle_log.warn(
            f"[build] cpu_quota = {raw!r} resolves to {pct}% but this host has only "
            f"{cores} core(s) ({cores * 100}%) — keeping it; systemd will clamp to "
            "the available CPUs"
        )
    return f"{pct}%"


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


def _coerce_mem_limit(raw) -> int | None:
    """Parse a per-build memory ceiling into bytes, or drop it with a warning.

    Accepts a bare byte count or a binary-suffixed size (``24G``, ``512M``,
    ``2Gi``); the ``i``/``B`` are cosmetic since suffixes are already binary.
    Junk, negative, or zero → ``None`` + warning (same drop-with-warning family
    as :func:`_coerce_cpu_quota` / :func:`_coerce_jobs`)."""
    if raw is None:
        return None
    m = _MEM_SIZE_RE.match(str(raw).strip())
    if not m:
        _throttle_log.warn(
            f"[build] mem_limit = {raw!r} must look like \"24G\" (K/M/G/T binary "
            "suffix) or a byte count — ignoring"
        )
        return None
    value = float(m.group(1)) * (1024 ** _MEM_SUFFIX_POW[(m.group(2) or "")[:1].lower()])
    n = int(value)
    if n <= 0:
        _throttle_log.warn(f"[build] mem_limit = {raw!r} must be > 0 — ignoring")
        return None
    return n


def resolve_throttle(
    resolved_profile, config: dict | None = None, override: str | None = None
) -> BuildThrottle:
    """Resolve build-throttle settings: ``sysforge.toml [build]`` defaults,
    overridden per-key by the resolved profile.

    ``resolved_profile`` may carry ``nice`` / ``ionice`` / ``cpu_quota`` / ``jobs``
    (sysforge-internal profile keys; see ``profile.SYSFORGE_KEYS``). A key present
    on the profile wins over the global default; an absent key falls back to it.

    ``override`` is the run-scoped override (2.1.0-F5); when ``None`` it falls back
    to the process-global ``_RUN_OVERRIDE`` set at CLI startup. ``"bypass"`` ignores
    the configured throttle entirely (no-op); ``"boost"`` runs at higher-than-default
    priority (negative niceness + best-effort IO, no CPU ceiling or job cap). Both
    short-circuit config/profile resolution — this is the sole throttle home.

    ``config`` is the parsed ``sysforge.toml`` dict; loaded on demand when ``None``
    (mirrors ``makepkg_env.resolve_build_python``). Never raises; each malformed
    value is dropped with a warning by its ``_coerce_*`` helper.
    """
    if override is None:
        override = _RUN_OVERRIDE
    if override == "bypass":
        return BuildThrottle()
    if override == "boost":
        # Constructed directly, bypassing _coerce_nice's 0..19 clamp — a boost is
        # an explicit request for *higher* than default priority. best-effort IO
        # (class 2) beats the usual idle throttle; no cpu_quota / job cap.
        return BuildThrottle(nice=_BOOST_NICE, ionice="best-effort")

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
        mem_limit_bytes=_coerce_mem_limit(pick("mem_limit")),
    )


def wrapper_argv(throttle: BuildThrottle) -> list[str]:
    """Build the argv prefix that wraps the ``makepkg`` invocation for the
    scheduling/IO/quota knobs (``jobs`` is delivered separately, via MAKEFLAGS).

    A hard ``cpu_quota`` **or** ``mem_limit`` requires a cgroup, so either wraps the
    build in a transient ``systemd-run --scope --user`` carrying ``CPUQuota=`` and/or
    ``MemoryMax=`` (2.3.0-F9). ``nice`` / ``ionice``
    are *always* applied as front-end commands (``nice -n N ionice -c C makepkg``),
    nested inside the scope when one is present. They are **not** folded into
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

    # A systemd scope is the primary tier for *both* resource ceilings (2.3.0-F9):
    # a cgroup CPUQuota and/or MemoryMax is kernel-enforced hierarchically over
    # makepkg's whole fork tree, whereas an RLIMIT_AS preexec set on this client
    # leaks across the fork tree and is escapable. So we open a scope whenever
    # either ceiling is configured — not only for cpu_quota.
    wants_scope = throttle.cpu_quota is not None or throttle.mem_limit_bytes is not None
    if wants_scope:
        if shutil.which("systemd-run"):
            argv = ["systemd-run", "--scope", "--user", "--quiet"]
            if throttle.cpu_quota is not None:
                argv += ["-p", f"CPUQuota={throttle.cpu_quota}"]
            # MemoryMax reaches the scoped payload (a child of PID 1); the client's
            # preexec rlimit would not. resolve_child_mem_cap suppresses the rlimit
            # path here so the two mechanisms never double-apply.
            if throttle.mem_limit_bytes is not None:
                argv += ["-p", f"MemoryMax={throttle.mem_limit_bytes}"]
            return argv + front
        # No systemd-run: we cannot enforce a hard cgroup ceiling. A cpu_quota has
        # no fallback, so warn; a mem_limit degrades silently to the RLIMIT_AS
        # preexec (resolve_child_mem_cap owns that non-systemd fallback).
        if throttle.cpu_quota is not None:
            _throttle_log.warn(
                "cpu_quota set but systemd-run not found — cannot enforce a hard CPU "
                "ceiling; falling back to nice/ionice only"
            )

    return front


def _scope_owns_mem_cap(throttle: BuildThrottle) -> bool:
    """Whether the systemd scope (not the rlimit preexec) carries the memory cap.

    True iff a ``mem_limit`` is set *and* ``systemd-run`` is available — the same
    condition under which :func:`wrapper_argv` emits a ``MemoryMax`` scope. Keeps
    the two functions' decision in lockstep so the cap is applied exactly once."""
    return throttle.mem_limit_bytes is not None and shutil.which("systemd-run") is not None


def resolve_child_mem_cap(throttle: BuildThrottle) -> int | None:
    """The ``RLIMIT_AS`` byte cap for the makepkg-child preexec path, or ``None``.

    Returns ``None`` when the ``systemd-run --scope`` owns the ceiling via
    ``MemoryMax`` (i.e. :func:`_scope_owns_mem_cap`) — an ``RLIMIT_AS`` set in the
    client's ``preexec_fn`` would never reach the scoped payload (a child of PID 1),
    so applying it would be silently ineffective *and* risk double-counting. This
    is now keyed on scope emission, not on ``cpu_quota`` (2.3.0-F9): a ``mem_limit``
    set alone also earns a scope. Otherwise returns ``mem_limit_bytes`` (possibly
    ``None``) for the plain-child rlimit fallback — the non-systemd path. Pairs
    with the ``MemoryMax`` injection in :func:`wrapper_argv`."""
    if _scope_owns_mem_cap(throttle):
        return None
    return throttle.mem_limit_bytes


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
