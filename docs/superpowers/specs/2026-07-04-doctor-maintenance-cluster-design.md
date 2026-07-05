# Doctor maintenance-gap cluster (F14–F19) — design

**Status:** approved, pre-implementation
**Date:** 2026-07-04
**Roadmap items:** `1.2.0-F14`, `1.2.0-F15`, `1.2.0-F16`, `1.2.0-F17`, `1.2.0-F18`, `1.2.0-F19`

## Goal

Close the six `doctor` maintenance-gap items from the maintenance-gap audit. Each
is a read-only health check surfaced through the existing `doctor` axis framework:
a `_collect_<axis>_findings()` producer in `sysforge/doctor.py` delegating to a
`primitives/<axis>_probe.py` and returning `list[diagnostics.Finding]`. No axis
syncs pacman, writes `BuildState`, imports `pipeline`, or makes a network call.

## Axis mapping

| Item | Placement | Probe |
|------|-----------|-------|
| F14 live instrumented/record-only PGO build | fold into **state** axis | `state_probe.py` |
| F15 pkg-cache size | fold into **pacman** axis | `system_probe.py` |
| F16 mirror freshness | fold into **pacman** axis | `system_probe.py` |
| F17 disk space | **new `storage` axis** | new `storage_probe.py` (+ reconfigure reuse) |
| F18 fstab integrity | fold into **storage** axis | `storage_probe.py` |
| F19 broaden journal scan | extend **services** axis | `runtime_probe.py` |

Net: **one** new axis (`storage`), three extended probes, one extended probe for state.

## Components

### New axis: `storage` — `sysforge/primitives/storage_probe.py`

- `probe_free_space(path) -> tuple[float, float] | None`
  Extracted free-space primitive (the surgical half of F17). Returns
  `(free_gb, total_gb)` for the nearest existing ancestor mount of `path`, or
  `None` on `OSError`. **Sole home** for the `shutil.disk_usage` call.
  `reconfigure._step_disk` calls this for raw numbers and keeps its own AUR-count
  build-size estimate locally — the estimate heuristic is *not* moved.
- `_check_disk_space(config) -> list[Finding]` (F17)
  Resolve the build dir (`paths.pkgbuild_src_dir`), call `probe_free_space`, warn
  (`SEV_WARN`) when `free_gb < [doctor] disk_low_gb`. Remediation: clear build
  caches / `paccache` / free space. Info-level context line optional.
- `_check_fstab(config) -> list[Finding]` (F18)
  Parse `/etc/fstab`. For each real entry, resolve the fs-spec: `UUID=` via
  `/dev/disk/by-uuid/`, `LABEL=` via `/dev/disk/by-label/`, `PARTUUID=`/
  `PARTLABEL=` via the matching `by-*` dir, and a bare device path directly.
  Flag (`SEV_WARN`) any that no longer resolve. **Skip** pseudo filesystems
  (`proc`, `sysfs`, `tmpfs`, `swap` by-spec where irrelevant), comment/blank
  lines, network fs types (`nfs`, `cifs`, `sshfs`, …), and any entry whose
  options contain `nofail`. Read-only; never mounts.
- `collect_storage_findings(config) -> list[Finding]` composes both checks.

Registered in `doctor.py`:
- `_collect_storage_findings(config)` producer (mirrors the other `_collect_*`).
- `_SYSTEM_AXIS_ORDER`: insert `"storage"` after `"boot"`.
- `_AXIS_FLAGS`: `"storage": "storage"`.
- `_system_axes`: `diag.Axis("storage", "storage / filesystem", lambda: …,
  clean_msg="adequate free space; all fstab entries resolve")`.

### `pacman` axis extensions — `sysforge/primitives/system_probe.py`

- `_check_pkg_cache(config) -> list[Finding]` (F15)
  Resolve the cache dir through pacman's config (`pacman.get_cachedir()` or an
  equivalent already-present helper; **do not** hardcode
  `/var/cache/pacman/pkg`). Sum file sizes; warn (`SEV_WARN`) when total
  exceeds `[doctor] pkg_cache_warn_gb`. Remediation:
  `paccache -r` (keep last 3) / `paccache -ruk0` (drop uninstalled).
- `_check_mirror_freshness(config) -> list[Finding]` (F16)
  Read `/etc/pacman.d/mirrorlist`. Age = file mtime age, or if a
  `## last-sync`-style timestamp / newest `Server` metadata is cheaply
  available, the newest server sync age. Warn (`SEV_WARN`) when age exceeds
  `[doctor] mirrorlist_stale_days`. **No network call** — never probe latency.
- Both wired into `collect_system_findings()` (the pacman axis producer), so no
  `doctor.py` change is needed for F15/F16.

### `services` axis extension — `sysforge/primitives/runtime_probe.py` (F19)

- `_check_boot_errors() -> list[Finding]`
  One extra `journalctl -b -p err --no-pager -o cat` pass (current boot,
  error-priority). Surface failed-boot / core-dump / repeated unit-start-failure
  lines as a single deduped, capped `SEV_WARN` finding (same sample/`+N more`
  shape as `_check_missing_firmware`). Guarded like the other `_run` calls;
  absent/failed `journalctl` yields no findings. Wired into
  `collect_runtime_findings()`. Current-boot scoped, so the doctor `services`
  axis already carries the reboot-hint semantics where relevant.

### `state` axis extension — `sysforge/primitives/state_probe.py` (F14)

- `_check_instrumented_builds(state_dir) -> list[Finding]`
  Walk `build_state.toml`. For any package whose provenance shows a live
  record-stage PGO build — a `.profraw` store present with **no** merged
  `.profdata` — emit `SEV_WARN` pointing at the next step
  (`sysforge build <pkg> --pgo=use`, or roll back to the repo package).
  Detection is via the existing provenance trail (`build_state.toml` `build_mode`
  cross-checked against store state), **never** from inspecting binaries. Wired
  into `collect_state_findings()`.

## Config surface — new `[doctor]` section in `sysforge.toml`

```toml
[doctor]
pkg_cache_warn_gb     = 5.0    # F15: warn when pacman pkg cache exceeds this
mirrorlist_stale_days = 14     # F16: warn when mirrorlist older than this
disk_low_gb           = 10.0   # F17: warn when build-dir free space under this
```

- Add `"doctor"` to `_KNOWN_SECTIONS["sysforge.toml"]` in `tools/check_shipped.py`.
- Ship the section in `etc/sysforge/sysforge.toml` **and** the fixture
  `tests/data/etc/sysforge/sysforge.toml` in lockstep (`make check-shipped`).
- Each check reads its threshold via the config with the documented default as
  fallback, so a config without `[doctor]` behaves as the baked defaults.

## Registration lockstep (CLAUDE.md doctor invariant)

For the new `storage` axis only (F15/F16/F19/F14 fold into existing axes and
need no CLI/completion/manpage change):

- `sysforge/doctor.py` — producer + `_SYSTEM_AXIS_ORDER` + `_AXIS_FLAGS` +
  `_system_axes`.
- `sysforge/cli.py` — `--storage` axis flag on the `doctor` subparser.
- `completions/_sysforge` (zsh) + `completions/sysforge.bash` — `--storage`.
- scdoc manpage — document `--storage`.
- `_patch_axes_clean` (the doctor test helper) — include `storage`.

## Error handling

Every probe wraps external commands / filesystem reads so an absent tool,
permission error, or malformed file yields **no findings** rather than a crash
(`run_axes` isolates exceptions as a backstop, but probes fail soft locally).
Missing config → documented default. Unreadable `/etc/fstab` or mirrorlist →
skip that check silently.

## Testing

Unit test per probe with `tmp_path` fixtures — clean and dirty cases, asserting
on returned `Finding` lists (never captured stdout):

- `storage_probe`: fake fstab (resolving + dangling UUID entry, a `nofail`
  entry that must be skipped, a network-fs entry skipped); `probe_free_space`
  on a real tmp dir; disk-low threshold via a monkeypatched `probe_free_space`.
- `system_probe`: fake cache dir over/under threshold; fake mirrorlist with
  fresh/stale mtime; assert no network call is made.
- `runtime_probe`: monkeypatched `journalctl` output with/without error lines.
- `state_probe`: synthetic `build_state.toml` with a record-only PGO entry
  (dirty) and a merged-profdata entry (clean) — both cases.

No dual-toolchain branch here (none of these checks branch on gcc vs llvm), so no
gcc/llvm test-parity obligation. All read-only: no `pacman -Sy`, no
`BuildState.save`, no `pipeline` import, no network.

## Docs / roadmap landing

- Update `docs/design/*` doctor source (axes list + `[doctor]` config) and run
  `make design`; then README.md, then CLAUDE.md doctor-axes line if the axis
  list is enumerated there.
- Remove F14–F19 from `ROADMAP.md` in the landing commit (keep ascending order).
- Append `Added` entries to `docs/release-notes/unreleased.md` with inline IDs.

## Out of scope

- Live mirror-latency probing (F16 explicitly forbids network).
- Binary inspection for PGO detection (F14 uses provenance only).
- Moving reconfigure's build-size estimate heuristic (only the free-space probe
  is shared).
- Auto-remediation — every finding is advisory; `doctor` stays read-only.
