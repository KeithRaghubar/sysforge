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
