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
    Axis, run_axes(axes)
    render_axis(logger, label, findings, *, clean_msg, quiet)
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

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
    boot-fatal condition (preserved from ``KernelFinding``).
    """
    category: str
    severity: str
    check_id: str
    message: str
    remediation: str = ""
    fix_cmd: str | None = None
    auto_remediable: bool = False
    is_brick: bool = False

    @property
    def is_error(self) -> bool:
        return self.severity == SEV_ERROR or self.is_brick


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
    message, and a zero-arg callable returning its findings."""
    name: str
    label: str
    run: Callable[[], list[Finding]]
    clean_msg: str = "no issues detected"


def run_axes(axes: Iterable[Axis]) -> dict[str, list[Finding]]:
    """Run each axis, isolating failures so one broken probe can't abort the
    sweep. A raising axis yields a single WARN finding rather than propagating.
    Returns ``{axis_name: findings}`` preserving iteration order.
    """
    results: dict[str, list[Finding]] = {}
    for ax in axes:
        try:
            results[ax.name] = list(ax.run())
        except Exception as e:  # noqa: BLE001 — a probe must never abort the sweep
            _log.debug(f"axis '{ax.name}' raised: {e}")
            results[ax.name] = [Finding(
                category=ax.name,
                severity=SEV_WARN,
                check_id=f"{ax.name}:probe_error",
                message=f"could not run the {ax.name} probe: {e}",
                remediation="re-run with -v for the traceback, or file a bug",
            )]
    return results


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


def render_axis(
    logger: log.Logger,
    label: str,
    findings: list[Finding],
    *,
    clean_msg: str = "no issues detected",
    quiet: bool = False,
) -> int:
    """Render one axis section through ``logger`` and return its error count.

    Format matches the established doctor block:
        == <label> ==
          [SEV] check_id: message
              → remediation
        <label>: N finding(s), M error(s).
    A clean axis prints ``clean_msg`` (suppressed under ``quiet``). Returns the
    number of error-severity / brick findings for the exit-code reducer.
    """
    logger.newline()
    logger.ui(f"== {label} ==")
    if not findings:
        if not quiet:
            logger.ui(f"  {clean_msg}")
        return 0

    # Most-severe first so the important lines lead each section.
    ordered = sorted(findings, key=lambda f: severity_rank(f.severity), reverse=True)
    errors = 0
    for f in ordered:
        sev = _color_severity(f.severity)
        logger.ui(f"  [{sev}] {f.check_id}: {f.message}")
        if f.remediation:
            logger.ui(f"      {log.green('→')} {f.remediation}")
        if f.is_error:
            errors += 1
    logger.ui(f"{label}: {len(findings)} finding(s), {errors} error(s).")
    return errors
