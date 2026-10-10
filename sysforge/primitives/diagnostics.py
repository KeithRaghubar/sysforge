# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
diagnostics.py — one ``Finding`` type, one renderer, one axis runner.

Unifies the finding shapes that grew up independently across the probe
modules — ``GraphicsFinding`` / ``DeviceFinding`` / ``KernelFinding`` /
``ToolchainMismatchFinding`` (all already ``severity``/``check_id``/``message``/
``remediation``-shaped) plus the two outliers ``ToolchainCheck``
(``toolchain_preflight``) and ``FixSuggestion`` (``build_diag``) — into a
single :class:`Finding`.

``sysforge doctor`` is the user-facing front-end: it runs a set of *axes*
(each a callable returning ``list[Finding]``) and renders + exit-codes through
this module. The probes keep their own dataclasses and are converted at the
boundary by the ``adapt`` / ``from_*`` helpers, so no probe is rewritten and
the layering rule (``primitives`` never imports the ``pipeline`` layer) holds:
pipeline-layer checks are adapted by their *callers*, never imported here.

Public API:
    SEV_ERROR / SEV_WARN / SEV_INFO, normalize_severity, severity_rank
    Finding
    adapt(category, obj) / adapt_many(category, objs)
    from_toolchain_check(check, *, category) / from_fix_suggestion(s, *, category)
    error_count(findings)
    Skip, Roster, AxisResult, record(roster, check_id, result)
    Axis, run_axis(ax), run_axes(axes)
    render_axis(logger, label, findings, *, clean_msg, quiet, grouped, roster)
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sysforge import log

_log = log.get_logger("DIAG")


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

SEV_ERROR = "error"
SEV_WARN = "warn"
SEV_INFO = "info"

# Rank for ordering / "worst severity" reductions. Higher = more severe.
_SEVERITY_RANK = {SEV_INFO: 0, SEV_WARN: 1, SEV_ERROR: 2}

# Some probes spell the middle level "warning" (e.g. ToolchainMismatchFinding);
# fold those aliases onto the canonical tokens. Anything unrecognised degrades
# to a warning rather than silently becoming an error.
_SEVERITY_ALIASES = {
    "error": SEV_ERROR,
    "err": SEV_ERROR,
    "critical": SEV_ERROR,
    "warn": SEV_WARN,
    "warning": SEV_WARN,
    "info": SEV_INFO,
    "informational": SEV_INFO,
    "notice": SEV_INFO,
}


def normalize_severity(severity: str | None) -> str:
    """Fold a probe's severity string onto SEV_ERROR / SEV_WARN / SEV_INFO."""
    if not severity:
        return SEV_WARN
    return _SEVERITY_ALIASES.get(severity.strip().lower(), SEV_WARN)


def severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(normalize_severity(severity), _SEVERITY_RANK[SEV_WARN])


# ---------------------------------------------------------------------------
# The one finding type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    """A single diagnostic result.

    ``category`` is the axis tag (``abi``, ``depends``, ``graphics``,
    ``hardware``, ``toolchain``, ``kernel``, ``pacman``, ``state``, ``boot``,
    ``services``). ``fix_cmd`` / ``auto_remediable`` carry an optional
    remediation that a future ``--fix`` can execute; ``is_brick`` flags a
    boot-fatal condition (preserved from ``KernelFinding``). ``subject`` is an
    optional group key used only by ``render_axis(grouped=True)``; ignored in
    the default flat mode.
    """
    category: str
    severity: str
    check_id: str
    message: str
    remediation: str = ""
    fix_cmd: str | None = None
    auto_remediable: bool = False
    is_brick: bool = False
    #: Optional group key/label. Used only by ``render_axis(grouped=True)``;
    #: ignored in the default flat mode so existing axes are unaffected.
    subject: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == SEV_ERROR or self.is_brick


# ---------------------------------------------------------------------------
# Roster — which checks ran and which could not (3.1.0-F1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Skip:
    """A check that could not run: its tool is missing, its input unreadable,
    or it does not apply to this host (vendor-gated). Returned by a check in
    place of ``None`` so "checked and healthy" and "never checked" stop
    looking identical. ``reason`` is shown verbatim in the ``-vv`` roster."""
    reason: str


@dataclass
class Roster:
    """The checks an axis ran and the ones it skipped, with why."""
    ran: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def merge(self, other: "Roster") -> None:
        self.ran.extend(other.ran)
        self.skipped.extend(other.skipped)


@dataclass
class AxisResult:
    """What an axis returns when it reports a roster. ``roster=None`` means the
    axis has not been migrated to report one (``Axis.run`` returning a bare
    list is normalised to this)."""
    findings: list
    roster: Roster | None = None


def record(roster: Roster, check_id: str, result):
    """File one check's result into ``roster`` and return its finding, if any.

    ``Skip`` → skipped (returns ``None``); ``None`` → ran and passed; anything
    else → ran and produced that finding (returned for the caller to collect).
    """
    if isinstance(result, Skip):
        roster.skipped.append((check_id, result.reason))
        return None
    roster.ran.append(check_id)
    return result


# ---------------------------------------------------------------------------
# Adapters — convert the existing probe dataclasses to Finding
# ---------------------------------------------------------------------------

def adapt(category: str, obj) -> Finding:
    """Adapt any ``severity``/``check_id``/``message``/``remediation``-shaped
    object (GraphicsFinding, DeviceFinding, KernelFinding, ToolchainMismatchFinding)
    to a :class:`Finding`. Optional ``is_brick`` is carried when present.
    """
    return Finding(
        category=category,
        severity=normalize_severity(getattr(obj, "severity", SEV_WARN)),
        check_id=getattr(obj, "check_id", ""),
        message=getattr(obj, "message", ""),
        remediation=getattr(obj, "remediation", "") or "",
        is_brick=bool(getattr(obj, "is_brick", False)),
    )


def adapt_many(category: str, objs: Iterable) -> list[Finding]:
    return [adapt(category, o) for o in objs]


def from_toolchain_check(check, *, category: str = "toolchain") -> Finding:
    """Adapt a ``toolchain_preflight.ToolchainCheck`` (``ok``/``name``/``detail``/
    ``fix_cmd``/``auto_remediable``). A passing check is INFO; a failing one is
    ERROR and carries its (possibly auto-remediable) fix.
    """
    ok = bool(getattr(check, "ok", True))
    fix_cmd = getattr(check, "fix_cmd", None)
    return Finding(
        category=category,
        severity=SEV_INFO if ok else SEV_ERROR,
        check_id=getattr(check, "name", ""),
        message=getattr(check, "detail", ""),
        remediation=fix_cmd or "",
        fix_cmd=fix_cmd,
        auto_remediable=bool(getattr(check, "auto_remediable", False)),
    )


def from_fix_suggestion(suggestion, *, category: str = "build") -> Finding:
    """Adapt a ``build_diag.FixSuggestion`` (``signature``/``message``/``fix_cmd``).
    Build-failure diagnoses surface as warnings carrying their fix command.
    """
    fix_cmd = getattr(suggestion, "fix_cmd", None)
    return Finding(
        category=category,
        severity=SEV_WARN,
        check_id=getattr(suggestion, "signature", ""),
        message=getattr(suggestion, "message", ""),
        remediation=fix_cmd or "",
        fix_cmd=fix_cmd,
    )


# ---------------------------------------------------------------------------
# Reductions
# ---------------------------------------------------------------------------

def error_count(findings: Iterable[Finding]) -> int:
    """Count findings that should drive a non-zero exit (error severity or brick)."""
    return sum(1 for f in findings if f.is_error)


# ---------------------------------------------------------------------------
# Axes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Axis:
    """A named diagnostic axis: a label for the section header, a clean-state
    message, and a zero-arg callable returning its findings. ``run`` may return
    a bare finding list or an :class:`AxisResult` carrying a roster."""
    name: str
    label: str
    run: Callable[[], "list[Finding] | AxisResult"]
    clean_msg: str = "no issues detected"


def run_axis(ax: Axis) -> AxisResult:
    """Run one axis, isolating failures so one broken probe can't abort the
    sweep. A raising axis yields a single WARN finding (and no roster) rather
    than propagating; a bare finding list becomes ``AxisResult(list, None)``.
    """
    try:
        out = ax.run()
    except Exception as e:  # noqa: BLE001 — a probe must never abort the sweep
        _log.debug(f"axis '{ax.name}' raised: {e}")
        return AxisResult([Finding(
            category=ax.name,
            severity=SEV_WARN,
            check_id=f"{ax.name}:probe_error",
            message=f"could not run the {ax.name} probe: {e}",
            remediation="re-run with -v for the traceback, or file a bug",
        )])
    if isinstance(out, AxisResult):
        return AxisResult(list(out.findings), out.roster)
    return AxisResult(list(out))


def run_axes(axes: Iterable[Axis]) -> dict[str, list[Finding]]:
    """Run each axis via :func:`run_axis`; return ``{axis_name: findings}``
    preserving iteration order (the roster is dropped — callers that render
    it use :func:`run_axis`)."""
    return {ax.name: run_axis(ax).findings for ax in axes}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_SEVERITY_COLOR = {
    SEV_ERROR: log.red,
    SEV_WARN: log.yellow,
    SEV_INFO: log.dim,
}


def _color_severity(severity: str) -> str:
    """Return the upper-cased severity token, colourized by its level."""
    token = severity.upper()
    paint = _SEVERITY_COLOR.get(normalize_severity(severity))
    return paint(token) if paint else token


def _emit_roster(logger: log.Logger, roster: Roster | None) -> None:
    """The roster block: ``info`` (``-vv``), or ``debug`` (``-vvv``) when the
    axis reports none. Narration, never ``ui``."""
    if roster is None:
        logger.debug("  roster: not reported by this axis")
        return
    if roster.ran:
        logger.info(f"  ran:     {', '.join(roster.ran)}")
    if roster.skipped:
        logger.info("  skipped: " + ", ".join(
            f"{cid} ({why})" for cid, why in roster.skipped))


def render_axis(
    logger: log.Logger,
    label: str,
    findings: list[Finding],
    *,
    clean_msg: str = "no issues detected",
    quiet: bool = False,
    grouped: bool = False,
    roster: Roster | None = None,
) -> int:
    """Render one axis section through ``logger`` and return its error count.

    Format matches the established doctor block:
        == <label> ==
          [SEV] check_id: message
              → remediation
        <label>: N finding(s), M error(s).
    A clean axis prints ``clean_msg`` (suppressed under ``quiet``). Returns the
    number of error-severity / brick findings for the exit-code reducer.
    When ``grouped=True``, findings are grouped by their ``subject`` field,
    ordered by worst severity, then alphabetically. ``roster`` (3.1.0-F1) is
    printed at ``-vv`` after the findings (or the clean message); ``None``
    prints a not-reported line at ``-vvv``. It is narration about how the
    answer was produced, so ``info``/``debug`` per the logging rubric.
    """
    logger.newline()
    logger.ui(f"== {label} ==")
    if not findings:
        if not quiet:
            logger.ui(f"  {clean_msg}")
        _emit_roster(logger, roster)
        return 0

    errors = sum(1 for f in findings if f.is_error)

    def _emit(f: Finding, indent: str) -> None:
        sev = _color_severity(f.severity)
        logger.ui(f"{indent}[{sev}] {f.check_id}: {f.message}")
        if f.remediation:
            logger.ui(f"{indent}    {log.green('→')} {f.remediation}")

    if grouped:
        groups: dict[str, list[Finding]] = {}
        for f in findings:
            groups.setdefault(f.subject, []).append(f)
        # Groups ordered by their worst finding, then by name for stability.
        for subject in sorted(
            groups,
            key=lambda s: (
                -max(severity_rank(f.severity) for f in groups[s]),
                s,
            ),
        ):
            if subject:
                logger.ui(f"  {subject}")
            for f in sorted(groups[subject],
                            key=lambda f: severity_rank(f.severity),
                            reverse=True):
                _emit(f, "    " if subject else "  ")
    else:
        # Most-severe first so the important lines lead each section.
        for f in sorted(findings, key=lambda f: severity_rank(f.severity),
                        reverse=True):
            _emit(f, "  ")

    _emit_roster(logger, roster)
    logger.ui(f"{label}: {len(findings)} finding(s), {errors} error(s).")
    return errors
