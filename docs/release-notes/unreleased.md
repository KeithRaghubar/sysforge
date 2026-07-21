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

- `make audit` scans the dev/build toolchain (hatchling, pytest, ruff, pyright,
  the coverage overlay) for dependencies with known CVEs via `pip-audit` in an
  ephemeral uv overlay. Dev-time only — nothing enters the shipped wheel or
  PKGBUILD — and kept out of the `pre-release` hard gate because it needs
  network and a fresh advisory shouldn't block a release. (`2.3.0-F3`)
- System-mutating verbs are now mirrored to the systemd journal as structured,
  queryable records (`journalctl -t sysforge`, `journalctl SYSFORGE_VERB=build`),
  additively alongside the unified run-log. No-op on non-systemd hosts. (2.3.0-F6)
- `doctor --restart` and an end-of-`update` advisory now report when an upgraded
  package has not taken effect because running processes still map the replaced
  files, classifying the fix as a unit restart, a re-login, or a reboot
  (`2.4.0-F2`).
- Artifact-drift alpm hook: a fourth libalpm PostTransaction hook records every
  pacman transaction so the next `sysforge update` reruns the artifact-inventory
  scans and nudges you to `sysforge artifact review` when a managed artifact
  drifted or a new adoptable candidate appeared. (2.3.0-F4)
- Artifact inventory: `sysforge artifact list [--unmanaged]` discovers and reports
  user-authored scripts, systemd units, and pacman hooks, excluding package-owned
  files and labelling sysforge's own hooks read-only. Warns when the configured
  script root is not on `PATH` (abstaining under `sudo`, where `secure_path`
  makes the process `PATH` unrepresentative). (`2.4.0-F4`)
- Artifact curation: `sysforge artifact adopt` / `edit` bring artifacts under
  management and track authoritative-vs-deployed drift with a three-way status
  (`ok`/`pending`/`drifted`/`conflict`/`missing`). (`2.4.0-F5`)
- Artifact deployment: `sysforge artifact deploy` / `remove` push managed content
  to the live system with per-class contracts (systemd `daemon-reload`,
  `disable --now` before unit removal). `deploy` refuses on external drift unless
  given `--force` or `--adopt-live`; `remove` likewise refuses on external drift
  without `--force`. (`2.4.0-F6`)
- Artifact offering: `sysforge artifact review` interactively offers discovered
  user-owned scripts, units, and hooks for adoption (`[a]dopt`/`[s]kip`/`[i]gnore`
  /`[q]uit`), remembering declines in a persistent ignore-list keyed by
  content-hash so a candidate re-surfaces only when its content changes. Off-TTY
  it lists candidates and the `artifact adopt` hint without prompting. (`1.2.0-F28`)

## Changed

- Logging re-levelling swept across every pipeline stage
  (`partition`/`install`, `hardware`, `configure`, `reconfigure`, `kernel`,
  `toolchain`): progress narration ("Probing hardware…", "Building kernel…",
  "[PGO] 3/4 complete", per-step "wrote/synced/installed" confirmations) is
  demoted from always-printed `ui()` to `info()` (`-vv`), so a default-verbosity
  run shows prompts, plan tables, section headers, dry-run previews, and check
  results without the intermediate chatter. File logs are unaffected (full
  verbosity always). A second golden-output guard now anchors the `configure`
  stage alongside the existing `packages` guard. (`1.2.0-F43`)
- `[build] cpu_quota` now **warns** when the resolved percentage exceeds the
  host's core count (`cpu_count*100`) — a typo or a config copied from a bigger
  box. The value is kept (systemd's effective cap clamps it harmlessly); the
  warning just gives the otherwise-silent overshoot a signal. Applies to both the
  absolute `"N%"` and the decimal-fraction forms, which now converge on a single
  resolved percentage before the check. (2.3.0-F7)
- Unrecognised string-valued config now has one policy via a shared
  `config.resolve_enum` seam. `[build] repo_track`'s previously-silent coercion
  to `"stable"` now **warns**. `resolve_repo_mode`'s defensive readers likewise
  **warn and fall back to `"pacman"`** instead of comparing an ambiguous raw
  value. Its authoritative load point (`packages.toml`) stays strict — an invalid
  `repo_mode` **hard-fails at load** rather than silently falling back, since that
  would drop the source builds the user configured. The legacy `profiled` →
  `build_from_source` alias is mapped before the enum check, so it is not
  flagged. (2.3.0-F8)
- `[build] mem_limit` is now enforced by a kernel-level cgroup `MemoryMax`
  (`systemd-run --scope`) even when `cpu_quota` is **unset**, wherever
  `systemd-run` is available — the escapable `RLIMIT_AS` preexec is demoted to a
  pure non-systemd fallback. A cgroup ceiling is hierarchical over makepkg's whole
  fork tree, whereas an rlimit on the single preexec child leaks across the fork
  tree. This also closes a gap where a `cpu_quota` set on a host without
  `systemd-run` silently dropped the memory cap: the rlimit fallback now owns it
  in that case. (2.3.0-F9)
