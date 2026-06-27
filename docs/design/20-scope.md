## Scope & Non-Goals

SysForge's lane is **build/package optimization and the health of what it
builds**. It is a profiled package builder and the maintainer of the packages it
produces — not a general configuration-management, backup, or system-provisioning
tool. This boundary is what keeps the project focused; it is stated here (rather
than rediscovered per feature) so proposed work can be measured against it.

**In scope.** Anything that affects what SysForge builds/installs or the
steady-state health of a SysForge-managed system:

- full-system upgrade / partial-upgrade avoidance (owned by the `update` verb);
- orphan and unused-package cleanup, package-cache reclaim (`paccache`);
- `.pacnew` / `.pacsave` handling (the analogue of the `.sfnew` config-merge verb);
- failed systemd units and boot/journal errors surfaced read-only;
- mirror and keyring freshness — both directly affect what gets built/installed.

Read-only health checks of the above belong on `doctor` axes; anything that
mutates the system stays behind an explicit verb with the same sentinel/gate
discipline the rest of SysForge uses.

**Out of scope.** Concerns that belong to general system administration rather
than a package builder:

- backups, snapshot-as-policy, and disk-space *strategy* (the user's own
  btrfs/snapshot/backup tooling — the only adjacency is an optional pre-build
  snapshot);
- user-data hygiene;
- the broader general-recommendations territory — networking, user management,
  locale/input configuration, and security hardening as a whole.

**North star.** When deciding what SysForge should help *set up, monitor, and
debug*, the Arch wiki's
[System maintenance](https://wiki.archlinux.org/title/System_maintenance) and
[General recommendations](https://wiki.archlinux.org/title/General_recommendations)
pages are the reference for which maintenance topics are worth covering — filtered
through the in/out-of-scope boundary above. New maintenance work should trace back
to a concrete topic on those pages rather than diverging from them.
