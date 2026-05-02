"""
auto_repair.py — narrow, deterministic build-failure repair primitives.

Generalises the toolchain-mismatch retry pattern in `makepkg_wrapper`
into a registry of repair scenarios. On a CalledProcessError, the wrapper
walks ``REGISTRY``; if a scenario's ``detect`` matches the captured build
output (or pre-flight context), the corresponding ``repair`` runs and
the build is retried. Each scenario fires at most once per build so a
mis-detection cannot loop.

Four scenarios ship in v1.x (see DESIGN.md §1242–1264):

    vendored_deps_missing    auto_repair                meson wraps,
                                                        git submodules
    pgp_key_missing          auto_repair                gpg: No public key
    srcinfo_drift            auto_repair_with_warning   .SRCINFO out of sync
    checksum_mismatch        prompt_user                source sum mismatch

The first three are auto-applied silently or with a [WARN]; the fourth
requires explicit user consent before invoking ``updpkgsums`` because
silent auto-fix would mask supply-chain compromise (an attacker who
swapped an upstream tarball would have sysforge "fix" the checksum and
proceed). In batch mode the prompt-required scenario aborts rather than
running unattended.

Public API:
    REGISTRY: tuple[RepairScenario, ...]
    BuildOutputAccumulator
    RepairScenario(name, detect, repair, retry_phase, behaviour_key)
    MatchInfo
    apply_first_match(scenarios, accum, *, pkgbuild_dir, behaviour, batch,
                      already_repaired) -> RepairResult | None
    preflight_srcinfo(pkgbuild_dir, behaviour) -> bool

Reuses ``import_pgp_keys`` from ``sysforge.primitives.aur`` for the PGP
scenario; otherwise has no cross-module side effects beyond the package
build directory.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from sysforge import log

_log = log.get_logger("REPAIR")


# ---------------------------------------------------------------------------
# Output accumulator
# ---------------------------------------------------------------------------

@dataclass
class BuildOutputAccumulator:
    """The line-tee buffer captured by ``invoke_makepkg``.

    ``lines`` carries every stdout line (stripped) emitted by makepkg, in
    order. Detection functions read it directly; ``text`` joins on '\\n'
    when a regex needs the full body. ``srcdir`` is the resolved
    ``${srcdir}`` for the current build (or None if it can't be derived
    cheaply) — used by detectors that need to inspect on-disk state
    (e.g. ``.gitmodules``).
    """
    lines: list[str] = field(default_factory=list)
    srcdir: Path | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def add(self, line: str) -> None:
        self.lines.append(line)


# ---------------------------------------------------------------------------
# Scenario type
# ---------------------------------------------------------------------------

RetryPhase = Literal["incremental", "from_scratch", "preflight"]


@dataclass(frozen=True)
class MatchInfo:
    """Detection result. ``detail`` is opaque to the wrapper but passed back
    into ``repair`` so detection-specific extracts (e.g. a PGP key ID) can
    drive the fix without re-parsing."""
    detail: dict


@dataclass(frozen=True)
class RepairScenario:
    name: str
    detect: Callable[[BuildOutputAccumulator], MatchInfo | None]
    repair: Callable[[Path, MatchInfo], None]
    retry_phase: RetryPhase
    behaviour_key: str


@dataclass
class RepairResult:
    scenario: str
    retry_phase: RetryPhase
    repaired: bool
    aborted: bool = False
    skipped_reason: str | None = None


# ---------------------------------------------------------------------------
# Scenario 1: vendored_deps_missing
# ---------------------------------------------------------------------------

_MESON_WRAP_PATTERN = re.compile(
    r"Automatic wrap-based subproject downloading is disabled"
)


def _detect_vendored_deps(accum: BuildOutputAccumulator) -> MatchInfo | None:
    if _MESON_WRAP_PATTERN.search(accum.text):
        return MatchInfo(detail={"kind": "meson"})
    if accum.srcdir is None:
        return None
    # Empty-submodule heuristic: a .gitmodules file present but at least one
    # declared submodule path doesn't yet contain a .git pointer.
    for gm in accum.srcdir.rglob(".gitmodules"):
        try:
            txt = gm.read_text(errors="replace")
        except OSError:
            continue
        # Parse "path = <name>" lines (the simplest, format-agnostic surface).
        for line in txt.splitlines():
            line = line.strip()
            if not line.startswith("path"):
                continue
            _, _, target = line.partition("=")
            target = target.strip()
            if not target:
                continue
            sub = gm.parent / target
            if not (sub / ".git").exists() and not any(sub.iterdir() if sub.exists() else ()):
                return MatchInfo(detail={
                    "kind": "git_submodule",
                    "project_root": str(gm.parent),
                })
    return None


def _repair_vendored_deps(pkgbuild_dir: Path, info: MatchInfo) -> None:
    kind = info.detail.get("kind")
    if kind == "meson":
        # Run from the project root inside ${srcdir}. Without srcdir we
        # bail; the wrapper will fall through to the manual-correction path.
        # The most common shape: srcdir/<topdir>/meson.build — search
        # cheaply for the meson.build closest to ${srcdir}.
        srcdir = pkgbuild_dir / "src"
        targets = sorted(srcdir.glob("*/meson.build"))
        if not targets:
            raise RuntimeError("vendored_deps_missing repair: no meson project under srcdir")
        meson = shutil.which("meson")
        if meson is None:
            raise RuntimeError("vendored_deps_missing repair: meson binary not on PATH")
        project_root = targets[0].parent
        _log.info(f"repair: meson subprojects download (in {project_root})")
        subprocess.run([meson, "subprojects", "download"],
                       cwd=str(project_root), check=True)
    elif kind == "git_submodule":
        project_root = Path(info.detail["project_root"])
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("vendored_deps_missing repair: git binary not on PATH")
        _log.info(f"repair: git submodule update --init --recursive (in {project_root})")
        subprocess.run([git, "submodule", "update", "--init", "--recursive"],
                       cwd=str(project_root), check=True)
    else:
        raise RuntimeError(f"vendored_deps_missing repair: unknown kind {kind!r}")


VENDORED_DEPS = RepairScenario(
    name="vendored_deps_missing",
    detect=_detect_vendored_deps,
    repair=_repair_vendored_deps,
    retry_phase="incremental",
    behaviour_key="vendored_deps_missing",
)


# ---------------------------------------------------------------------------
# Scenario 2: pgp_key_missing
# ---------------------------------------------------------------------------

_PGP_KEY_RE = re.compile(
    r"gpg:\s+Can't check signature: No public key.*?([0-9A-Fa-f]{16,40})",
    re.DOTALL,
)


def _detect_pgp_key(accum: BuildOutputAccumulator) -> MatchInfo | None:
    m = _PGP_KEY_RE.search(accum.text)
    if not m:
        return None
    return MatchInfo(detail={"keyid": m.group(1).upper()})


def _repair_pgp_key(pkgbuild_dir: Path, info: MatchInfo) -> None:
    keyid = info.detail["keyid"]
    gpg = shutil.which("gpg")
    if gpg is None:
        raise RuntimeError("pgp_key_missing repair: gpg binary not on PATH")
    _log.info(f"repair: gpg --recv-keys {keyid}")
    # --keyserver lookup uses the user's gpg.conf default; falls back to
    # keys.openpgp.org via the dirmngr defaults if unset.
    subprocess.run([gpg, "--recv-keys", keyid], check=True)


PGP_KEY = RepairScenario(
    name="pgp_key_missing",
    detect=_detect_pgp_key,
    repair=_repair_pgp_key,
    retry_phase="from_scratch",
    behaviour_key="pgp_key_missing",
)


# ---------------------------------------------------------------------------
# Scenario 3: srcinfo_drift (pre-flight, not retry)
# ---------------------------------------------------------------------------

def _printsrcinfo(pkgbuild_dir: Path) -> str | None:
    """Return ``makepkg --printsrcinfo`` output, or None on failure."""
    makepkg = shutil.which("makepkg")
    if makepkg is None:
        return None
    try:
        r = subprocess.run(
            [makepkg, "--printsrcinfo"],
            cwd=str(pkgbuild_dir),
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.SubprocessError:
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def detect_srcinfo_drift(pkgbuild_dir: Path) -> bool:
    """Return True if .SRCINFO drift is present (and the file exists)."""
    srcinfo = pkgbuild_dir / ".SRCINFO"
    if not srcinfo.exists():
        return False
    fresh = _printsrcinfo(pkgbuild_dir)
    if fresh is None:
        return False
    try:
        existing = srcinfo.read_text()
    except OSError:
        return False
    return fresh.strip() != existing.strip()


def repair_srcinfo_drift(pkgbuild_dir: Path) -> bool:
    """Regenerate .SRCINFO. Returns True on success."""
    fresh = _printsrcinfo(pkgbuild_dir)
    if fresh is None:
        return False
    try:
        (pkgbuild_dir / ".SRCINFO").write_text(fresh)
    except OSError:
        return False
    return True


def preflight_srcinfo(pkgbuild_dir: Path, behaviour: str) -> bool:
    """Pre-build .SRCINFO drift check + regeneration.

    Returns True when drift was detected AND auto-repaired (the caller
    may want to log this); False otherwise. Behaviour values:
      ``auto_repair`` / ``auto_repair_with_warning`` — silent fix /
        WARN-then-fix. The default is ``auto_repair_with_warning``.
      ``abort`` — fail the build instead of regenerating.
      ``warn_and_fallback`` / anything else — log a WARN and continue.
    """
    if not detect_srcinfo_drift(pkgbuild_dir):
        return False
    if behaviour == "abort":
        raise RuntimeError(".SRCINFO drift detected; aborting per [failure_handling]")
    if behaviour in ("auto_repair", "auto_repair_with_warning"):
        if not repair_srcinfo_drift(pkgbuild_dir):
            _log.warn(".SRCINFO drift detected but regeneration failed")
            return False
        if behaviour == "auto_repair_with_warning":
            _log.warn(f".SRCINFO drift in {pkgbuild_dir.name} — regenerated")
        else:
            _log.info(f".SRCINFO drift in {pkgbuild_dir.name} — regenerated")
        return True
    _log.warn(f".SRCINFO drift in {pkgbuild_dir.name} — not repaired ({behaviour})")
    return False


# ---------------------------------------------------------------------------
# Scenario 4: checksum_mismatch (prompt_user; never silent)
# ---------------------------------------------------------------------------

_CHECKSUM_LINE_RE = re.compile(
    r"==> (?:ERROR: )?One or more files did not pass the validity check"
)


def _detect_checksum_mismatch(accum: BuildOutputAccumulator) -> MatchInfo | None:
    if not _CHECKSUM_LINE_RE.search(accum.text):
        return None
    # makepkg's preceding lines look like:
    #     <name> ... FAILED
    # Capture the failing source filenames so the prompt can show them.
    failed: list[str] = []
    for line in accum.lines:
        if line.endswith("FAILED") and "..." in line:
            # "    foo-1.0.tar.gz ... FAILED"
            name = line.split("...")[0].strip()
            if name:
                failed.append(name)
    return MatchInfo(detail={"failed_sources": failed})


def _repair_checksum_mismatch(pkgbuild_dir: Path, info: MatchInfo) -> None:
    """Prompt the user, then run updpkgsums on consent.

    Raises RuntimeError if the user declines or if updpkgsums isn't on PATH.
    The caller (auto-repair driver) only invokes this when behaviour is
    ``prompt_user`` AND the build is interactive — batch mode short-circuits
    earlier and never reaches this path.
    """
    failed = info.detail.get("failed_sources", [])
    print()
    print("=" * 70, flush=True)
    print(" CHECKSUM MISMATCH detected in PKGBUILD source verification.", flush=True)
    print(" Files that failed validity check:", flush=True)
    for name in failed:
        print(f"   - {name}", flush=True)
    print(
        " Updating sums silently would mask supply-chain compromise.\n"
        " If you trust the new upstream content, accept here to run\n"
        " `updpkgsums` and rebuild. Otherwise abort and investigate.",
        flush=True,
    )
    print("=" * 70, flush=True)
    try:
        answer = input("Run updpkgsums and retry? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in {"y", "yes"}:
        raise RuntimeError("checksum_mismatch repair: user declined")
    updpkgsums = shutil.which("updpkgsums")
    if updpkgsums is None:
        raise RuntimeError("checksum_mismatch repair: updpkgsums not on PATH "
                           "(install pacman-contrib)")
    _log.info(f"repair: updpkgsums in {pkgbuild_dir}")
    subprocess.run([updpkgsums], cwd=str(pkgbuild_dir), check=True)


CHECKSUM_MISMATCH = RepairScenario(
    name="checksum_mismatch",
    detect=_detect_checksum_mismatch,
    repair=_repair_checksum_mismatch,
    retry_phase="from_scratch",
    behaviour_key="checksum_mismatch",
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

REGISTRY: tuple[RepairScenario, ...] = (
    VENDORED_DEPS,
    PGP_KEY,
    CHECKSUM_MISMATCH,
)
# srcinfo_drift is intentionally NOT in REGISTRY — it's a pre-flight check
# (`preflight_srcinfo`), not a retry-on-failure scenario.


_ALLOWED_BEHAVIOURS = frozenset({
    "auto_repair", "auto_repair_with_warning", "prompt_user",
    "abort", "warn_and_fallback", "fallback", "error",
})


def _normalise_behaviour(behaviour: object) -> str:
    if isinstance(behaviour, str) and behaviour in _ALLOWED_BEHAVIOURS:
        return behaviour
    return "auto_repair"  # safe default for the three silent scenarios


def apply_first_match(
    scenarios: tuple[RepairScenario, ...],
    accum: BuildOutputAccumulator,
    *,
    pkgbuild_dir: Path,
    behaviour_for: Callable[[str], object],
    batch: bool,
    already_repaired: set[str],
) -> RepairResult | None:
    """Walk ``scenarios``; on the first match honour ``behaviour_for(key)``
    and either run ``repair`` (returning a RepairResult), abort (with
    ``aborted=True``), or skip (``skipped_reason`` set).

    ``already_repaired`` carries the names of scenarios that have already
    fired in this build; matched-but-already-repaired scenarios are skipped
    with ``skipped_reason='already-repaired'`` so the wrapper can fall
    through to its existing failure handling rather than looping.
    """
    for s in scenarios:
        if s.name in already_repaired:
            continue
        info = s.detect(accum)
        if info is None:
            continue
        behaviour = _normalise_behaviour(behaviour_for(s.behaviour_key))
        if behaviour == "abort":
            _log.warn(f"{s.name}: matched but [failure_handling] = abort")
            return RepairResult(scenario=s.name, retry_phase=s.retry_phase,
                                repaired=False, aborted=True)
        if behaviour == "prompt_user" and batch:
            _log.warn(
                f"{s.name}: matched but batch mode is active — "
                "prompt_user requires interactive consent; aborting"
            )
            return RepairResult(scenario=s.name, retry_phase=s.retry_phase,
                                repaired=False, aborted=True,
                                skipped_reason="batch-no-prompt")
        try:
            s.repair(pkgbuild_dir, info)
        except (RuntimeError, subprocess.CalledProcessError) as e:
            _log.warn(f"{s.name}: repair failed: {e}")
            return RepairResult(scenario=s.name, retry_phase=s.retry_phase,
                                repaired=False, skipped_reason=str(e))
        if behaviour == "auto_repair_with_warning":
            _log.warn(f"{s.name}: repaired, retrying ({s.retry_phase})")
        else:
            _log.info(f"{s.name}: repaired, retrying ({s.retry_phase})")
        already_repaired.add(s.name)
        return RepairResult(scenario=s.name, retry_phase=s.retry_phase,
                            repaired=True)
    return None
