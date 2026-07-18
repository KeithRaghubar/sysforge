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

- System-mutating verbs are now mirrored to the systemd journal as structured,
  queryable records (`journalctl -t sysforge`, `journalctl SYSFORGE_VERB=build`),
  additively alongside the unified run-log. No-op on non-systemd hosts. (2.3.0-F6)
- `doctor --restart` and an end-of-`update` advisory now report when an upgraded
  package has not taken effect because running processes still map the replaced
  files, classifying the fix as a unit restart, a re-login, or a reboot
  (`2.4.0-F2`).

## Changed

- `[build] cpu_quota` now **warns** when the resolved percentage exceeds the
  host's core count (`cpu_count*100`) — a typo or a config copied from a bigger
  box. The value is kept (systemd's effective cap clamps it harmlessly); the
  warning just gives the otherwise-silent overshoot a signal. Applies to both the
  absolute `"N%"` and the decimal-fraction forms, which now converge on a single
  resolved percentage before the check. (2.3.0-F7)
- `[build] mem_limit` is now enforced by a kernel-level cgroup `MemoryMax`
  (`systemd-run --scope`) even when `cpu_quota` is **unset**, wherever
  `systemd-run` is available — the escapable `RLIMIT_AS` preexec is demoted to a
  pure non-systemd fallback. A cgroup ceiling is hierarchical over makepkg's whole
  fork tree, whereas an rlimit on the single preexec child leaks across the fork
  tree. This also closes a gap where a `cpu_quota` set on a host without
  `systemd-run` silently dropped the memory cap: the rlimit fallback now owns it
  in that case. (2.3.0-F9)
