# sysforge (unreleased)

<!--
Running accumulator for the next release. Every landing commit that COMPLETES a
ROADMAP item appends its entry here (in the same commit that drops the item from
ROADMAP.md), under the matching Keep a Changelog section — one of Added, Changed,
Deprecated, Removed, Fixed, Security, in that order. Reference the roadmap ID
inline, e.g. (1.2.0-F35). Flag breaking changes with a **Breaking:** prefix and
the migration path. At release time tools/release.sh (Phase 1) renames this file
to vX.Y.Z.md, stamps the `# ` title with the version and date, and reseeds a fresh
accumulator. Run the release-notes skill first to reconcile/lint the entries and
finalize the one-line summary below (drop this comment). Keep a Changelog:
https://keepachangelog.com/en/1.1.0/
-->

## Added

- `doctor --distro` reports the running distribution and its support tier, read
  from `os-release(5)` (2.6.1-F2). Arch is the primary base and the bare sweep
  stays silent on it; an Arch derivative (`ID_LIKE=arch`) gets an `info` naming
  what is validated there (packaging, dependency resolution, `makepkg.conf`
  merge) and what is not (bootstrap, kernel staging, graphics/DKMS); anything
  else warns, as does an unreadable `os-release`. No finding on this axis is
  ever an error — a support tier cannot change doctor's exit code.
  `primitives/os_release.py` is now the single home for distro identity, which
  sysforge previously had nowhere to report at all.

- A container test tier for the packaging and portability checks, parameterized
  by distro (2.6.1-F2): `make container-smoke` (Arch) and
  `make container-smoke-cachyos`. It installs a locally-built package into a
  throwaway container and runs the same checks `make vm-smoke` does, in seconds
  instead of a boot and a snapshot. The derivative arm is the point: it covers
  repo/AUR shadowing, a different `makepkg.conf` baseline, and bumped `pkgrel`s
  on core packages — none of which a same-distro test can reach. Bootstrap,
  kernel staging, graphics/DKMS and restart detection stay with the VM tier.
  Requires `podman`; see `tools/container/README.md`.

## Changed

- **Standards row 23** (`os-release(5)`, enforced) records the distro-identity
  invariant adopted above (2.6.1-F2), guarded by the new `check_standards`
  `distro_identity` group: identity is read from `/etc/os-release` (then
  `/usr/lib/os-release`) through `primitives/os_release.py` alone, never
  inferred from `pacman.conf` section names, `/etc/arch-release`, or a hostname.

- The smoke checks moved from `tools/vm/smoke.sh` to `tools/smoke.sh` and gained
  a `--transport=ssh|podman` flag (2.6.1-F2), so the VM and container tiers
  share one copy of the checks rather than diverging. `make vm-smoke` is
  unchanged. Both tiers additionally assert the portability invariants — distro
  identity, sync-repo discovery against `pacman.conf`, the system
  `makepkg.conf` merge baseline, and version comparison on a live `pkgver` —
  each as a differential between the host's own `/etc` and what sysforge
  resolved. An unreachable target now exits `3` (was `1`), distinguishing
  absent optional infrastructure from a real failure for callers that warn
  rather than fail.

## Fixed

- `make vm-iso` no longer requires the installer ISO to be named `archlinux.iso`
  (2.6.1-B2). It now boots the single `*.iso` in the VM directory whatever its
  filename, or the one named by the new `SYSFORGE_VM_ISO` variable (bare filename
  or absolute path) when several are present; zero or ambiguous matches fail with
  remediation instead of a stale path. Lookup happens only in `--iso` mode, so
  other boot modes are unaffected. Combined with `SYSFORGE_VM_DIR`, a VM tree for a
  second Arch-derived distro now needs no code change. Only the Arch install stays
  automated — see `tools/vm/README.md`.

- Source sync no longer warns `working tree has local modifications — keeping local
  PKGBUILD` for every `-git` package carrying makepkg's routine `pkgver()` auto-bump
  (2.6.1-B1). The warning contradicted the very next log line, which reset the same
  tree to upstream after re-asking the question VCS-aware; it is now an `INFO` for
  VCS checkouts whose only working-tree change is the generated `pkgver`/`.SRCINFO`
  churn. Deliberate edits, and genuine upstream divergence, still warn as before.
  The sync outcome is unchanged.
