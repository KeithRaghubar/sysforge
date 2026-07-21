# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
artifacts.py — inventory of user-authored system artifacts.

sysforge manages a *curated* set of personally-created artifacts (shell
scripts, systemd units, pacman hooks). The live system is input to curation,
never a template: discovery proposes candidates, the user adopts deliberately.

Three locations, three roles:

  * ``USER_DATA_DIR/artifacts/``  — authoritative content. Irreplaceable.
  * ``<state_dir>/artifacts.toml`` — registry metadata. Regenerable by
    re-hashing. Follows the same state-dir chain as ``build_state.py``
    (explicit arg > ``SYSFORGE_STATE_DIR`` > ``/var/lib/sysforge``).
  * the live filesystem — deploy target, owned by the OS.

The registry stores **metadata only**, never content, so the authoritative
copy has exactly one home and the two cannot silently diverge.

Status is *computed*, never stored — see :func:`status_of`. The filesystem is
the untrusted side; it must be read, not remembered.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sysforge import log
from sysforge.primitives import pacman, pacman_hooks, paths
from sysforge.primitives.config import load_sysforge_toml
from sysforge.primitives.privilege import run_privileged

# Artifact classes. Explicit registry field rather than an inference from the
# destination path, so adding a class is a table entry instead of a new branch
# in every code path.
CLASS_SCRIPT = "script"
CLASS_UNIT = "systemd-unit"
CLASS_HOOK = "pacman-hook"
ARTIFACT_CLASSES = (CLASS_SCRIPT, CLASS_UNIT, CLASS_HOOK)

_REGISTRY_NAME = "artifacts.toml"
_IGNORE_NAME = "artifacts-ignored.toml"
_DEFAULT_STATE_DIR = Path("/var/lib/sysforge")


class ArtifactError(Exception):
    """Raised for user-facing artifact operation failures (e.g. `adopt`)."""


class ArtifactRegistryError(ArtifactError):
    """The registry file exists but could not be read/parsed.

    Raised instead of silently treating the registry as empty: artifacts.toml
    is the only record of which artifacts are managed and their authoritative
    hashes, and a subsequent ``save()`` writes the full entry set — folding
    corruption into "empty" would make the next save permanently discard
    every managed artifact with no diagnostic ever shown.

    Subclasses :class:`ArtifactError` so the verb layer's ``except
    ArtifactError`` turns a corrupt registry into a clean exit-1 with this
    message rather than an uncaught traceback.
    """


def hash_bytes(data: bytes) -> str:
    """sha256 hex digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str | None:
    """sha256 of *path*, or None when absent/unreadable.

    Unreadable is folded into None deliberately: a root-owned file we cannot
    read is indistinguishable from an absent one for status purposes, and both
    mean "we have no trustworthy live hash".
    """
    try:
        return hash_bytes(Path(path).read_bytes())
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError, OSError):
        return None


@dataclass(frozen=True)
class Artifact:
    """One managed artifact's registry metadata. Never holds content."""

    name: str
    dest: Path
    cls: str
    auth_hash: str
    deployed_hash: str | None
    deployed_at: str | None


def _toml_escape(value) -> str:
    """Escape a value for a TOML basic (double-quoted) string.

    Mirrors ``build_state._toml_escape`` (``\\``, ``"``, ``\\n``, ``\\r``) and
    additionally escapes ``\\t`` and every remaining C0 control character
    (0x00-0x08, 0x0B-0x1F, 0x7F) via ``\\uXXXX`` — the TOML spec requires every
    control byte except tab to be escaped in a basic string, and an
    unescaped one (e.g. a stray ``\\r`` in a name or dest path) would write a
    file ``tomllib.loads()`` then refuses to parse, silently corrupting the
    only record of which artifacts are managed. We hand-roll TOML writing
    because the runtime dependency surface is deliberately near-empty
    (tomlkit is dev-only; see pyproject).
    """
    out = []
    for ch in str(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch < "\x20" or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


class ArtifactRegistry:
    """Reads/writes ``artifacts.toml`` and locates authoritative content."""

    def __init__(self, state_dir=None, data_dir=None):
        if state_dir is not None:
            base = Path(state_dir)
        else:
            env = os.environ.get("SYSFORGE_STATE_DIR")
            base = Path(env) if env else _DEFAULT_STATE_DIR
        self._state_dir = base
        self._data_dir = Path(data_dir) if data_dir is not None else paths.USER_DATA_DIR

    @property
    def path(self) -> Path:
        """The registry TOML file."""
        return self._state_dir / _REGISTRY_NAME

    @property
    def content_dir(self) -> Path:
        """Directory holding authoritative artifact content."""
        return self._data_dir / "artifacts"

    def content_path(self, name: str) -> Path:
        """Authoritative content path for *name*."""
        return self.content_dir / name

    def load(self) -> dict[str, Artifact]:
        """Parse the registry.

        A genuinely missing (or missing-parent) file yields an empty
        registry — that's the expected first-run state. Anything else that
        prevents reading an existing file (corrupt TOML, permissions) raises
        :class:`ArtifactRegistryError` rather than being folded into "empty":
        this file is the sole record of managed artifacts, and ``save()``
        writes the complete entry set, so silently returning ``{}`` here
        would let the next save overwrite real data with nothing.
        """
        if not self.path.exists():
            return {}
        try:
            raw = tomllib.loads(self.path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ArtifactRegistryError(
                f"artifact registry at {self.path} is corrupt and could not be "
                "parsed as TOML; repair it by hand or remove it (this discards "
                "all managed-artifact records) before continuing"
            ) from exc
        except OSError as exc:
            raise ArtifactRegistryError(
                f"artifact registry at {self.path} exists but could not be read "
                f"({exc}); check permissions before continuing"
            ) from exc
        out: dict[str, Artifact] = {}
        for name, row in raw.items():
            if not isinstance(row, dict):
                continue
            out[name] = Artifact(
                name=name,
                dest=Path(row.get("dest", "")),
                cls=row.get("class", CLASS_SCRIPT),
                auth_hash=row.get("auth_hash", ""),
                deployed_hash=row.get("deployed_hash") or None,
                deployed_at=row.get("deployed_at") or None,
            )
        return out

    def save(self, entries: dict[str, Artifact]) -> None:
        """Write the registry atomically (temp file + replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = [
            "# sysforge artifact registry — metadata only, never content.",
            "# Managed by `sysforge artifact`; hand-edits are overwritten.",
            "",
        ]
        for name in sorted(entries):
            art = entries[name]
            lines.append(f'["{_toml_escape(name)}"]')
            lines.append(f'dest = "{_toml_escape(art.dest)}"')
            lines.append(f'class = "{_toml_escape(art.cls)}"')
            lines.append(f'auth_hash = "{_toml_escape(art.auth_hash)}"')
            if art.deployed_hash:
                lines.append(f'deployed_hash = "{_toml_escape(art.deployed_hash)}"')
            if art.deployed_at:
                lines.append(f'deployed_at = "{_toml_escape(art.deployed_at)}"')
            lines.append("")
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text("\n".join(lines))
        tmp.replace(self.path)


class IgnoreList:
    """Persistent record of *declined* discovery candidates: path -> content-hash.

    A sibling of :class:`ArtifactRegistry`, deliberately in its own file. The
    registry is documented as regenerable (rebuildable from managed content);
    this decline-intent is not — coupling them would let a registry rebuild
    silently forget every "no". Keyed by path + the content-hash seen at decline
    time so a candidate re-surfaces once its content changes.
    """

    def __init__(self, state_dir=None):
        if state_dir is not None:
            base = Path(state_dir)
        else:
            env = os.environ.get("SYSFORGE_STATE_DIR")
            base = Path(env) if env else _DEFAULT_STATE_DIR
        self._state_dir = base

    @property
    def path(self) -> Path:
        """The ignore-list TOML file."""
        return self._state_dir / _IGNORE_NAME

    def load(self) -> dict[Path, str]:
        """Parse the ignore-list, pruning entries whose file no longer exists.

        Missing file → empty (expected first-run). Corrupt/unreadable → raise
        :class:`ArtifactRegistryError` with repair guidance, mirroring the
        registry: this is the sole record of declines and ``save()`` writes the
        complete set, so folding a parse error into "empty" would let the next
        save erase real declines.
        """
        if not self.path.exists():
            return {}
        try:
            raw = tomllib.loads(self.path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ArtifactRegistryError(
                f"artifact ignore-list at {self.path} is corrupt and could not "
                "be parsed as TOML; repair it by hand or remove it (this "
                "re-offers every previously declined candidate) before continuing"
            ) from exc
        except OSError as exc:
            raise ArtifactRegistryError(
                f"artifact ignore-list at {self.path} exists but could not be "
                f"read ({exc}); check permissions before continuing"
            ) from exc
        out: dict[Path, str] = {}
        for row in raw.get("ignored", []):
            if not isinstance(row, dict):
                continue
            p = row.get("path")
            h = row.get("hash")
            if not p or not h:
                continue
            path = Path(p)
            if not path.exists():
                continue  # deleted file: legitimately re-offerable next scan
            out[path] = h
        return out

    def save(self, entries: dict[Path, str]) -> None:
        """Write the ignore-list atomically (temp file + replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = [
            "# sysforge artifact ignore-list — declined discovery candidates.",
            "# Managed by `sysforge artifact review`; hand-edits are overwritten.",
            "",
        ]
        for path in sorted(entries):
            lines.append("[[ignored]]")
            lines.append(f'path = "{_toml_escape(path)}"')
            lines.append(f'hash = "{_toml_escape(entries[path])}"')
            lines.append("")
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text("\n".join(lines))
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# Discovery — scan roots, three-stage filter
# ---------------------------------------------------------------------------

# Default scan roots as (path, class) pairs. Overridable via the [artifacts]
# table in sysforge.toml — adding a root is configuration, not code.
DEFAULT_ROOTS = (
    ("~/scripts", CLASS_SCRIPT),
    ("/etc/systemd/system", CLASS_UNIT),
    ("/etc/pacman.d/hooks", CLASS_HOOK),
)


def roots_from_config(config: dict | None = None) -> tuple:
    """Scan roots from ``sysforge.toml [artifacts] roots``, falling back to
    :data:`DEFAULT_ROOTS` when the table is absent or empty.

    *config* defaults to :func:`load_sysforge_toml` — pass an explicit dict in
    tests to avoid touching the live filesystem. Each row must carry ``path``
    and a ``class`` that is one of :data:`ARTIFACT_CLASSES`; a row missing
    either or naming an unrecognised class is skipped with a warning rather
    than crashing discovery or handing an invalid class downstream.
    """
    cfg = load_sysforge_toml() if config is None else config
    rows = (cfg or {}).get("artifacts", {}).get("roots", []) or []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            log.warn("artifacts", f"skipping malformed [artifacts] roots entry: {row!r}")
            continue
        path = row.get("path")
        cls = row.get("class")
        if not path or not cls:
            log.warn("artifacts", f"skipping [artifacts] roots entry missing path/class: {row!r}")
            continue
        if cls not in ARTIFACT_CLASSES:
            log.warn(
                "artifacts",
                f"skipping [artifacts] roots entry with unknown class {cls!r} "
                f"(expected one of {ARTIFACT_CLASSES}): {row!r}",
            )
            continue
        out.append((path, cls))
    if not out:
        return DEFAULT_ROOTS
    return tuple(out)

OWNER_YOU = "you"
OWNER_SYSFORGE = "sysforge"
OWNER_UNKNOWN = "unknown"

# Filename suffixes/prefixes excluded from discovery, each with its reason.
# These are documented rules, not a silent heuristic.
_EXCLUDED_SUFFIXES = {
    ".pacnew": "pacman conflict artifact",
    ".pacsave": "pacman conflict artifact",
    ".pacorig": "pacman conflict artifact",
    "~": "editor backup",
}
_EXCLUDED_PREFIXES = {
    ".#": "editor lock file",
}
# Subdirectories holding `systemctl enable` symlinks. These are systemd-owned
# *enablement state*, not authored files: adopting one into a copy-based
# registry yields a dead managed copy and a `remove` that fights
# `systemctl disable`.
_ENABLEMENT_DIR_SUFFIXES = (".wants", ".requires")


@dataclass(frozen=True)
class Candidate:
    """A discovered file: where it is, what class, and who owns it."""

    path: Path
    cls: str
    owner: str


def is_excluded(path) -> str | None:
    """Return the exclusion reason for *path*, or None when it is a candidate."""
    name = Path(path).name
    for prefix, reason in _EXCLUDED_PREFIXES.items():
        if name.startswith(prefix):
            return reason
    for suffix, reason in _EXCLUDED_SUFFIXES.items():
        if name.endswith(suffix):
            return reason
    return None


def _sysforge_owned_paths() -> set:
    """Destinations sysforge itself provisions — labelled, never hidden.

    Derived from ``pacman_hooks`` rather than a duplicated list, so shipping a
    fourth hook updates this guard automatically.
    """
    owned = {pacman_hooks.HOOK_DEST_DIR / n for n in pacman_hooks.HOOK_NAMES}
    owned.add(pacman_hooks.HELPER_DEST)
    return owned


def _sysforge_owned_names() -> set:
    """Basenames of sysforge-owned destinations: the scan root may differ
    from the install dir (/etc/pacman.d/hooks vs /usr/share/libalpm/hooks).

    Basename matching is scoped to :data:`CLASS_HOOK` candidates only (see
    ``scan``) — it exists solely to bridge that one root/install-dir mismatch.
    Applied to every class it would mislabel a user's own script or unit that
    happens to share a basename with a sysforge hook/helper as
    ``owner = "sysforge"`` in a directory that has nothing to do with pacman
    hooks, making it permanently unmanageable.
    """
    return {p.name for p in _sysforge_owned_paths()}


def scan(roots=None) -> list:
    """Discover candidate artifacts under *roots*.

    *roots* defaults to :func:`roots_from_config` (the shipped/user
    ``[artifacts] roots`` table in ``sysforge.toml``, falling back to
    :data:`DEFAULT_ROOTS`). Pass an explicit ``roots`` to scan something else.

    Three-stage filter: structural noise excluded by rule, package-owned files
    excluded (the OS already knows what it shipped), sysforge-owned files kept
    but labelled read-only. Only regular files at root level are considered.
    """
    pairs = roots if roots is not None else roots_from_config()
    found: list[tuple[Path, str]] = []
    for raw_root, cls in pairs:
        root = Path(raw_root).expanduser()
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir())
        except (PermissionError, OSError):
            continue
        for child in children:
            if child.is_dir():
                continue  # enablement dirs and any other nesting: not candidates
            if child.name.endswith(_ENABLEMENT_DIR_SUFFIXES):
                continue
            if child.is_symlink():
                # Symlinks in these roots are enablement/alias state, not
                # authored files — /etc/systemd/system holds `systemctl enable`
                # and alias links pointing at unit sources elsewhere. Adopting
                # one would copy the *target's* content into a managed regular
                # file and a later deploy would clobber the link with that copy.
                # Same rationale as the .wants/.requires exclusion.
                continue
            if not child.is_file():
                continue  # sockets, devices, dangling entries
            if is_excluded(child):
                continue
            found.append((child, cls))

    if not found:
        return []

    owners = pacman.owners_of([p for p, _ in found])
    sysforge_names = _sysforge_owned_names()

    out: list[Candidate] = []
    for path, cls in found:
        if cls == CLASS_HOOK and path.name in sysforge_names:
            out.append(Candidate(path=path, cls=cls, owner=OWNER_SYSFORGE))
            continue
        if path not in owners:
            out.append(Candidate(path=path, cls=cls, owner=OWNER_UNKNOWN))
            continue
        if owners[path] is not None:
            continue  # package-owned: the OS already knows what it shipped
        out.append(Candidate(path=path, cls=cls, owner=OWNER_YOU))
    return out


def iter_offerable(registry, ignore=None, roots=None) -> list:
    """Discovery candidates worth *offering* for adoption.

    The single composition point over :func:`scan`. Always excludes
    sysforge-owned candidates and any already present in *registry*. When
    *ignore* is a non-``None`` :class:`IgnoreList`, additionally drops a
    candidate whose current content-hash matches a recorded decline (a changed
    file re-surfaces). ``ignore=None`` skips that step — the shape
    ``artifact list --unmanaged`` uses, which still shows declined candidates.
    """
    managed_dests = {art.dest for art in registry.load().values()}
    ignored = ignore.load() if ignore is not None else {}
    out: list = []
    for c in scan(roots):
        if c.owner == OWNER_SYSFORGE:
            continue
        if c.path in managed_dests:
            continue
        if c.path in ignored and ignored[c.path] == hash_file(c.path):
            continue
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Status — three-way comparison, computed, never stored
# ---------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_PENDING = "pending"
STATUS_DRIFTED = "drifted"
STATUS_CONFLICT = "conflict"
STATUS_MISSING = "missing"


def status_of(registry, art) -> str:
    """Compute status from the three-way comparison. Never stored.

    The ``deployed_hash`` is what makes this attributable: with only
    authoritative-vs-live there is one bit of information ("same or
    different") and no way to tell which side moved. Knowing what we last
    wrote turns an ambiguous diff into a named state — which is what lets
    ``deploy`` refuse instead of guess.
    """
    del registry  # reserved: content-hash verification lands with adopt
    live = hash_file(art.dest)
    if live is None:
        return STATUS_MISSING

    auth_moved = art.auth_hash != art.deployed_hash
    live_moved = live != art.deployed_hash

    if art.deployed_hash is None:
        # Never deployed, but something exists at dest. No anchor to attribute
        # against — a human must decide.
        return STATUS_OK if live == art.auth_hash else STATUS_CONFLICT

    if not auth_moved and not live_moved:
        return STATUS_OK
    if auth_moved and not live_moved:
        return STATUS_PENDING
    if not auth_moved and live_moved:
        return STATUS_DRIFTED
    return STATUS_CONFLICT


# Map pacman_hooks provisioning states onto the unified status vocabulary.
# sysforge's own artifacts keep their own authority (pacman_hooks) — this is a
# presentation-layer join, not a second registry.
_HOOK_STATE_TO_STATUS = {
    pacman_hooks.STATE_OK: STATUS_OK,
    pacman_hooks.STATE_STALE: STATUS_DRIFTED,
    pacman_hooks.STATE_MISSING: STATUS_MISSING,
}


def unified_rows(registry, roots=None) -> list:
    """Rows for `artifact list`: managed entries + sysforge's own artifacts.

    sysforge-owned rows are rendered by delegating to
    ``pacman_hooks.diff_status()`` rather than being copied into the registry —
    each artifact keeps exactly one authority.
    """
    rows: list[dict] = []
    for name, art in sorted(registry.load().items()):
        rows.append({
            "name": name,
            "owner": OWNER_YOU,
            "cls": art.cls,
            "status": status_of(registry, art),
            "dest": art.dest,
        })
    try:
        hook_rows = pacman_hooks.diff_status()
    except Exception:  # noqa: BLE001 — a broken hook probe must not break list
        hook_rows = []
    for hook_art, state in hook_rows:
        rows.append({
            "name": hook_art.dest.name,
            "owner": OWNER_SYSFORGE,
            "cls": CLASS_HOOK,
            "status": _HOOK_STATE_TO_STATUS.get(state, "unknown"),
            "dest": hook_art.dest,
        })
    return rows


# ---------------------------------------------------------------------------
# Adoption — bring a live artifact under management
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def class_for_path(path, roots=None) -> str | None:
    """Infer an artifact class from the root containing *path*, else None.

    *roots* defaults to :func:`roots_from_config` — the same configured table
    :func:`scan` honors, so inference agrees with discovery when the user has
    customised ``[artifacts] roots``. Pass explicit ``roots`` in tests.
    """
    target = Path(path).expanduser().resolve()
    for raw_root, cls in (roots if roots is not None else roots_from_config()):
        root = Path(raw_root).expanduser()
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if target.parent == resolved:
            return cls
    return None


def adopt(registry, src, cls: str | None = None):
    """Copy *src* into the managed set and record it. Never moves the source.

    ``deployed_hash`` is seeded from the source: at adoption time the live file
    *is* the last-deployed state, so a fresh entry reads ``ok`` rather than
    ``pending``. The sysforge-owned basename guard is scoped to
    :data:`CLASS_HOOK` only, mirroring :func:`scan` — applying it to every
    class would make a user's own script permanently unadoptable merely for
    sharing a basename with a sysforge hook/helper.

    The recorded ``dest`` is absolutized (``resolve()``): a relative argument
    like ``./s.sh`` would otherwise be stored cwd-relative and every later
    ``status_of`` / ``deploy`` / ``remove`` would re-anchor it against whatever
    directory the command happened to run from.
    """
    src = Path(src).expanduser().resolve()
    if cls is None:
        cls = class_for_path(src)
    if cls not in ARTIFACT_CLASSES:
        raise ArtifactError(
            f"unknown class {cls!r} — expected one of {', '.join(ARTIFACT_CLASSES)}"
        )
    if cls == CLASS_HOOK and src.name in _sysforge_owned_names():
        raise ArtifactError(
            f"{src.name} is a sysforge-owned artifact — manage it with "
            "`sysforge setup` / `sysforge doctor --pacman`, not `artifact`"
        )
    try:
        data = src.read_bytes()
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        raise ArtifactError(f"{src} not found or unreadable: {exc}") from exc

    entries = registry.load()
    if src.name in entries:
        raise ArtifactError(f"{src.name} is already managed")

    registry.content_dir.mkdir(parents=True, exist_ok=True)
    registry.content_path(src.name).write_bytes(data)

    digest = hash_bytes(data)
    art = Artifact(
        name=src.name, dest=src, cls=cls,
        auth_hash=digest, deployed_hash=digest, deployed_at=_now_iso(),
    )
    entries[src.name] = art
    registry.save(entries)
    return art


# ---------------------------------------------------------------------------
# Script root PATH check
# ---------------------------------------------------------------------------


def rehash(registry, name: str):
    """Re-hash the managed copy of *name* after an edit and persist it.

    The live/deployed file is untouched — only ``auth_hash`` moves, so a
    plain edit surfaces as :data:`STATUS_PENDING` until the caller runs
    ``artifact deploy``.
    """
    entries = registry.load()
    art = entries.get(name)
    if art is None:
        raise ArtifactError(f"{name} is not managed")
    try:
        data = registry.content_path(name).read_bytes()
    except OSError as exc:
        raise ArtifactError(f"managed copy of {name} unreadable: {exc}") from exc
    updated = Artifact(
        name=art.name, dest=art.dest, cls=art.cls,
        auth_hash=hash_bytes(data),
        deployed_hash=art.deployed_hash, deployed_at=art.deployed_at,
    )
    entries[name] = updated
    registry.save(entries)
    return updated


def default_script_root():
    """The configured root for the `script` class, or None when unset.

    Reads the same :func:`roots_from_config` table as discovery, so the PATH
    warning names the user's actual script root rather than the shipped
    default when ``[artifacts] roots`` has been customised.
    """
    for raw_root, cls in roots_from_config():
        if cls == CLASS_SCRIPT:
            return Path(raw_root).expanduser()
    return None


def script_root_on_path(root=None) -> bool | None:
    """Whether the script root is on PATH.

    Returns True/False, or **None when undeterminable**. An escalated
    invocation (``SUDO_USER`` set) sees a PATH replaced by sudo's
    ``secure_path``, which is not the user's PATH — warning off that would be a
    confident false positive, so the check abstains instead.

    Comparison is on resolved paths: PATH may spell the root as
    ``$HOME/scripts``, an absolute path, with a trailing slash, or via a
    symlink.
    """
    if os.environ.get("SUDO_USER"):
        return None
    target = root if root is not None else default_script_root()
    if target is None:
        return None
    target = Path(target).expanduser()
    try:
        resolved = target.resolve()
    except OSError:
        return False

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            if Path(entry).expanduser().resolve() == resolved:
                return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------------------
# Deploy/remove — per-class live-filesystem contracts
# ---------------------------------------------------------------------------

# Live-file permission mode per class.
_LIVE_MODE = {
    CLASS_SCRIPT: 0o755,
    CLASS_UNIT: 0o644,
    CLASS_HOOK: 0o644,
}
# Classes whose destinations are root-owned system dirs and must go through
# run_privileged rather than a direct unprivileged write/unlink.
_PRIVILEGED_CLASSES = (CLASS_UNIT, CLASS_HOOK)


def unit_is_enabled(unit: str) -> bool:
    """True when systemd reports *unit* as enabled. False on any error.

    Queried unprivileged and non-raising — `systemctl is-enabled` needs no
    escalation, and an absent/failed systemctl (missing binary, unknown unit)
    is indistinguishable from "not enabled" for pre_remove's purposes.
    """
    try:
        cp = subprocess.run(
            ["systemctl", "is-enabled", "--quiet", unit],
            capture_output=True, text=True,
        )
        return cp.returncode == 0
    except OSError:  # e.g. systemctl missing entirely
        return False


def write_live(art, data: bytes) -> None:
    """Install *data* at the artifact's destination with its class mode.

    Scripts land in the user's own tree and must not escalate; units and
    hooks live in root-owned system dirs and go through ``run_privileged``.
    The privileged path stages *data* through a ``NamedTemporaryFile`` (owned
    by the invoking user, mode 0600) — that's fine, because the escalated
    ``install`` reads the temp file as root, which can always read a file the
    invoking user owns; the mode only matters for *other* unprivileged users,
    who must not be able to read it before install claims it.
    """
    mode = _LIVE_MODE[art.cls]
    if art.cls not in _PRIVILEGED_CLASSES:
        art.dest.parent.mkdir(parents=True, exist_ok=True)
        art.dest.write_bytes(data)
        art.dest.chmod(mode)
        return
    with tempfile.NamedTemporaryFile("wb", delete=False) as tf:
        tf.write(data)
        tmp = tf.name
    try:
        run_privileged(
            ["install", "-Dm", f"{mode:o}", tmp, str(art.dest)], tag="artifact"
        )
    finally:
        Path(tmp).unlink(missing_ok=True)


def post_deploy(art) -> None:
    """Class post-deploy action. Units need systemd told; nothing else does."""
    if art.cls == CLASS_UNIT:
        run_privileged(["systemctl", "daemon-reload"], tag="artifact")


def pre_remove(art) -> None:
    """Class pre-remove action. Stop/disable a live unit before unlinking it.

    Pacman hooks get no pre-action beyond a caller-side warning (Task 13) —
    there is nothing systemd-equivalent to quiesce for a hook.
    """
    if art.cls == CLASS_UNIT and unit_is_enabled(art.dest.name):
        run_privileged(
            ["systemctl", "disable", "--now", art.dest.name], tag="artifact"
        )


def remove_live(art) -> None:
    """Unlink the live file, escalating for root-owned classes."""
    if art.cls not in _PRIVILEGED_CLASSES:
        art.dest.unlink(missing_ok=True)
        return
    run_privileged(["rm", "-f", str(art.dest)], tag="artifact")


def deploy(registry, name: str, *, force: bool = False,
           adopt_live: bool = False) -> str:
    """Push the managed copy to the live destination.

    Refuses on ``drifted`` and ``conflict`` unless given an explicit
    resolution: ``force`` (managed wins) or ``adopt_live`` (live wins). There
    is deliberately no default — silently picking a side is data loss in one
    direction or the other.

    ``adopt_live`` overwrites the authoritative managed copy with live content,
    so its scope is fenced to the states where that is the point: ``drifted`` /
    ``conflict`` (the drift it resolves) and ``ok`` (a harmless no-op). It
    refuses on ``pending`` — where it would silently discard an undeployed
    managed edit — and on ``missing`` — where there is no live file to adopt.
    """
    if force and adopt_live:
        raise ArtifactError("--force and --adopt-live are mutually exclusive")

    entries = registry.load()
    art = entries.get(name)
    if art is None:
        raise ArtifactError(f"{name} is not managed")

    status = status_of(registry, art)

    if adopt_live and status == STATUS_PENDING:
        raise ArtifactError(
            f"{name} is pending: --adopt-live would overwrite the managed edit "
            f"you have not deployed yet with the older live file. Deploy the "
            f"pending change first (plain `deploy`), or re-edit the managed copy."
        )
    if adopt_live and status == STATUS_MISSING:
        raise ArtifactError(
            f"{name}: the live file is absent, so there is nothing to adopt. "
            f"Deploy without --adopt-live to (re)create it from the managed copy."
        )

    if status in (STATUS_DRIFTED, STATUS_CONFLICT) and not (force or adopt_live):
        raise ArtifactError(
            f"{name} is {status}: the live file changed outside sysforge. "
            f"Re-run with --force (managed copy wins, discards the live edit) "
            f"or --adopt-live (live file wins, updates the managed copy)."
        )

    if adopt_live:
        # Safe: missing already refused above, so dest exists here.
        live_data = Path(art.dest).read_bytes()
        registry.content_path(name).write_bytes(live_data)
        art = rehash(registry, name)

    data = registry.content_path(name).read_bytes()
    if status == STATUS_OK and not adopt_live:
        return STATUS_OK

    write_live(art, data)
    post_deploy(art)

    digest = hash_bytes(data)
    entries = registry.load()
    entries[name] = Artifact(
        name=art.name, dest=art.dest, cls=art.cls,
        auth_hash=digest, deployed_hash=digest, deployed_at=_now_iso(),
    )
    registry.save(entries)
    return STATUS_OK


def remove(registry, name: str, *, purge: bool = False, force: bool = False) -> None:
    """Remove the live artifact; with *purge*, also drop the managed copy.

    Without ``purge`` the managed copy and registry row survive, so the
    artifact can be redeployed — removal from the live system is not the same
    decision as discarding the content.

    Refuses on ``drifted`` / ``conflict`` unless *force*, symmetric with
    :func:`deploy`: the live file carries edits made outside sysforge that
    exist nowhere else, so unlinking it (privileged ``rm`` for units/hooks)
    would destroy them silently. ``--force`` proceeds anyway; to keep the live
    version, ``deploy --adopt-live`` pulls it into the managed copy first.
    ``ok`` / ``pending`` / ``missing`` remove without force — the live file is
    either reproducible from the managed copy or already gone.
    """
    entries = registry.load()
    art = entries.get(name)
    if art is None:
        raise ArtifactError(f"{name} is not managed")

    status = status_of(registry, art)
    if status in (STATUS_DRIFTED, STATUS_CONFLICT) and not force:
        raise ArtifactError(
            f"{name} is {status}: the live file changed outside sysforge and "
            f"removing it would discard those live-only edits. Re-run with "
            f"--force to remove anyway, or `deploy --adopt-live` first to save "
            f"the live version into the managed copy."
        )

    pre_remove(art)
    remove_live(art)

    if purge:
        registry.content_path(name).unlink(missing_ok=True)
        del entries[name]
    else:
        entries[name] = Artifact(
            name=art.name, dest=art.dest, cls=art.cls,
            auth_hash=art.auth_hash, deployed_hash=None, deployed_at=None,
        )
    registry.save(entries)
