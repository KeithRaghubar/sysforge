# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
net_policy.py — the source freeze: an enforced denial of code ingress.

``--offline`` *skips* network work as a convenience: it is threaded per-callsite
and unknown to the egress primitives themselves, so any path that forgets the
check reaches the network. This module is the opposite shape — a policy object
consulted *at the seam*, so code that does not know the gate exists still fails
closed.

The policy is resolved once at CLI entry and stored module-globally. The global
is deliberate: the seams sit at very different depths (``aur_clone`` under the
sync scheduler, the pkgver resolve under ``update_version``), and threading a
parameter through them reproduces the ``--offline`` weakness where a new call
site defaults *permissive*. A consulted global defaults *denied*. For a security
gate that asymmetry is the whole point.

``get_policy()`` returns a permissive policy when unset, so library use and
tests that never call ``set_policy`` are unaffected.

Public API:
    NetworkFrozen
    NetPolicy(frozen, thawed).check(kind, pkgbase)
    resolve_net_policy(args, cfg) -> NetPolicy
    set_policy(policy) / get_policy() / reset_policy()
"""
from __future__ import annotations

from dataclasses import dataclass

from sysforge import log
from sysforge.primitives.config import resolve_flag_default

_log = log.get_logger("FREEZE")


class NetworkFrozen(RuntimeError):
    """Raised when the source freeze denies an egress."""


# Egress kinds. Named constants rather than bare strings so a seam and its test
# cannot drift apart on a typo.
KIND_AUR_CLONE = "aur_clone"
# `pkgctl repo clone` from gitlab.archlinux.org. Deliberately distinct from
# KIND_AUR_CLONE: different origin, different trust story, and a future policy
# may permit one while denying the other.
KIND_REPO_CHECKOUT = "repo_checkout"
KIND_SOURCE_FETCH = "source_fetch"
KIND_VCS_PEEK = "vcs_peek"
KIND_VCS_RESOLVE = "vcs_resolve"


@dataclass(frozen=True)
class NetPolicy:
    """An immutable per-run decision about which egress is permitted."""

    frozen: bool
    thawed: frozenset[str]

    def check(self, kind: str, pkgbase: str | None) -> None:
        """Raise :class:`NetworkFrozen` if this egress is denied.

        A ``None`` pkgbase is always denied under freeze: ``--thaw`` is
        per-pkgbase, so an unnamed package can never match a lift, and denying
        keeps a seam that forgets to thread its pkgbase from becoming a silent
        bypass.
        """
        if not self.frozen:
            return
        if pkgbase is not None and pkgbase in self.thawed:
            return
        name = pkgbase or "<unknown>"
        raise NetworkFrozen(
            f"{name}: source freeze denied {kind} "
            f"(lift with --thaw {name}, or --no-frozen for the whole run)"
        )


_PERMISSIVE = NetPolicy(frozen=False, thawed=frozenset())
_policy: NetPolicy = _PERMISSIVE


def resolve_net_policy(args, cfg: dict) -> NetPolicy:
    """Resolve the run's policy from CLI flags and the ``[security]`` config.

    Precedence: ``--no-frozen`` > ``--frozen`` > ``[security] freeze_sources``
    > ``False``. The middle two rows go through the shared
    :func:`config.resolve_flag_default` seam; the explicit-off flag is a thin
    wrapper on top, since that seam has no "explicit false" concept.

    ``--thaw`` is a *lift*, never a switch — it narrows an already-active
    freeze and does not enable one.
    """
    frozen = resolve_flag_default(args, "frozen", cfg, "freeze_sources")
    if getattr(args, "no_frozen", False):
        frozen = False

    thawed: set[str] = set()
    for chunk in getattr(args, "thaw", None) or []:
        for name in str(chunk).split(","):
            name = name.strip()
            if name:
                thawed.add(name)

    return NetPolicy(frozen=frozen, thawed=frozenset(thawed))


def set_policy(policy: NetPolicy) -> None:
    """Install the run's policy. Called once, at CLI entry."""
    global _policy
    _policy = policy
    if policy.frozen:
        if policy.thawed:
            _log.warn(
                "source freeze ACTIVE — thawed: "
                + ", ".join(sorted(policy.thawed))
            )
        else:
            _log.warn("source freeze ACTIVE — no new sources will be downloaded")


def get_policy() -> NetPolicy:
    """The run's policy; permissive when never set."""
    return _policy


def reset_policy() -> None:
    """Restore the permissive default. For tests and long-lived processes."""
    global _policy
    _policy = _PERMISSIVE


def warn_ungated_sources(pkgbuild_dir) -> list[str]:
    """Report the ``source=()`` entries makepkg will fetch outside the gate.

    SysForge does not mediate makepkg's own network, so under freeze the honest
    thing is to name what is not covered. Only *remote and uncached* entries are
    reported: a warning that fires on every build is a warning nobody reads.

    ``source`` is not a member of ``pkgbuild_meta._ARCH_ARRAY_FAMILIES``, so
    ``source_<arch>`` arrays are never merged into the canonical key — they are
    read explicitly here. Under-reporting would be a silent gap in a security
    warning.

    Returns the reported entries (also useful to tests); logs at ``warn``.
    """
    from pathlib import Path

    from sysforge.primitives.pacman import get_srcdest
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    pkgbuild_dir = Path(pkgbuild_dir)
    pkgbuild = pkgbuild_dir / "PKGBUILD"
    if not pkgbuild.is_file():
        return []
    try:
        meta = parse_pkgbuild(pkgbuild)
    except Exception as e:  # a parse failure must never block a build
        _log.warn(f"{pkgbuild_dir.name}: could not read source=() ({e})")
        return []

    globals_ = meta.get("globals", {})
    entries: list[str] = []
    for key, val in globals_.items():
        if key == "source" or key.startswith("source_"):
            entries.extend(val if isinstance(val, list) else [val])

    try:
        srcdest = Path(get_srcdest())
    except Exception:
        srcdest = None

    reported: list[str] = []
    for entry in entries:
        # PKGBUILD(5): `local::url` renames; the cache key is the local name.
        local, sep, url = entry.partition("::")
        target = url if sep else entry
        if "://" not in target:
            continue  # a file shipped beside the PKGBUILD — no egress
        cache_name = local if sep else target.split("#", 1)[0].rstrip("/").split("/")[-1]
        if (pkgbuild_dir / cache_name).exists():
            continue
        if srcdest is not None and (srcdest / cache_name).exists():
            continue
        reported.append(entry)

    if reported:
        _log.warn(
            f"{pkgbuild_dir.name}: {len(reported)} sources makepkg will fetch "
            "outside the gate:"
        )
        for entry in reported:
            _log.warn(f"    {entry}")
    return reported
