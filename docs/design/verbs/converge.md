### `converge.py`

Implements `sysforge converge` — the flag drift detector. Algorithm:

1. Load `build_state.toml`. Group by `pkgbase`.
2. For each profiled package (`build_mode = "profiled"`): re-resolve the current profile via `parse_pkgbuild` → `match_rules` → `resolve_profile` → `serialize_flags`.
3. Diff the stored `flags_string` against the freshly resolved flags. Packages where the flags differ are `DRIFTED`; identical are `IN_SYNC`. Packages without a stored `flags_string` (built before this feature) are `NO_FLAGS`. Missing PKGBUILD → `NO_PKGBUILD`. Non-profiled packages are silently omitted.
4. Print a per-package summary with flag diffs for `DRIFTED` entries.
5. With `--apply`: rebuild all `DRIFTED` packages via `makepkg_wrapper.run()` with `update=False`. Post-build, the pkg-file set is run through `filter_pkgs_to_installed` before `batch_install_pkgs` so repairing drift on one split pkgname never installs sibling sub-packages the user never chose.

Without `--apply`, the command is read-only — it reports drift but does not rebuild. Positional `[PKG ...]` limits the drift check (and `--apply` rebuild) to the named pkgnames; names not in `build_state.toml` are warned and skipped, matching `update`'s positional behaviour. Flags: `--apply`, `--state-dir`, `--profile-conf`, `--no-pkg-log`, `--persist-log`, `--log-dir`, `--cache-report`.

