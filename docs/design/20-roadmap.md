## Roadmap

Forward-looking enhancements that build on existing infrastructure. Each is a candidate, not a commitment, and none is required for current functionality. Shipped work is recorded in `docs/release-notes/`, not here.

- **Rule priority auto-calculation** — auto-calculate a baseline specificity score from rule conditions (mirrors CSS specificity: more AND'd conditions = higher weight), with manual `priority` override for ties. Deferred until enough real rules exist to validate whether auto-priority causes ordering problems in practice.
- **Configure stage additions** — btrfs snapshot before build runs, ccache/sccache initialisation check, estimated build time heuristic.
- **Graphics runtime debugging refinement** — tighten the graphics/doctor diagnostics surface (exact scope TBD). A candidate when revisiting graphics-related code; not blocking.
- **System maintenance scope expansion** — grow sysforge beyond build/package management into a unified system-maintenance helper: track and manage user-owned system artifacts that currently live ad-hoc across `~/scripts`, `/etc/systemd/system/`, `/etc/pacman.d/hooks/`, etc. Candidate primitives: inventory of tracked files, source-of-truth dir under repo control, install/sync command, drift detection vs filesystem, integration with the existing config/profile/manifest layers.

### System-maintenance scope (scoping pass)

The Arch wiki's [System maintenance](https://wiki.archlinux.org/title/System_maintenance)
and [General recommendations](https://wiki.archlinux.org/title/General_recommendations)
pages are the reference checklist for "keeping an Arch install healthy". This is
a deliberate *scoping* pass — which of their sections are a natural fit for
sysforge (as a verb or a `doctor` axis) vs. out of scope — not a commitment to
build any of it. SysForge's lane is **build/package optimization and the health
of what it builds**; it is not aiming to become a general config-management or
backup tool.

**In scope (already covered or a clean fit):**

- *Upgrading the system / partial-upgrade avoidance* — the `update` verb already
  owns the full-system upgrade path (source-built packages rebuilt, repo packages
  via `pacman -Syu`); partial upgrades are structurally avoided.
- *Orphans, unused packages, paccache* — overlaps the existing `cache` management
  and would extend naturally to a `doctor` axis reporting orphaned dependencies
  and reclaimable package cache, read-only.
- *`.pacnew`/`.pacsave` handling* — directly analogous to the existing config
  merge verb (`.sfnew` adoption); a `doctor` axis surfacing pending `.pacnew`
  merges is the obvious extension.
- *Failed systemd units / journal errors* — fits the read-only `doctor` Finding
  framework as a health axis (no mutation).
- *Mirror / keyring freshness* — a `doctor` axis warning on a stale mirrorlist or
  `archlinux-keyring` is in-lane (it directly affects what sysforge builds and
  installs).

**Out of scope (explicitly not sysforge's job):**

- Backups, snapshots-as-policy, disk-space *strategy*, user-data hygiene — these
  are the user's tooling (btrfs/timeshift/borg/etc.); sysforge's only adjacency
  is the optional pre-build snapshot already listed under *Configure stage
  additions* above.
- General `General recommendations` territory — networking, user management,
  desktop/locale/input config, security hardening as a whole — is outside a
  package-builder's remit and would dilute the tool.

The actionable near-term slice is therefore a set of **read-only `doctor` axes**
(orphans, `.pacnew`, failed units, mirror/keyring freshness) reusing the existing
Finding framework, plus the artifact-inventory primitive sketched in the bullet
above. Anything mutating stays behind an explicit verb with the sentinel/gate
discipline the rest of sysforge uses.
