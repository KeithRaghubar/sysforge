# sysforge (unreleased)

<!--
Running accumulator for the next release. Every landing commit that COMPLETES a
ROADMAP item appends its entry here (in the same commit that drops the item from
ROADMAP.md), under the matching Keep a Changelog section — one of Added, Changed,
Deprecated, Removed, Fixed, Security, in that order. An entry leads with its
roadmap ID, in the same shape ROADMAP.md entries use:

    - **`1.2.0-F35` — <title sentence>.** <body>

    ---

    - **`1.2.0-F36` — …

Entries are separated by a `---` rule and kept in ascending ID order within each
section. Flag breaking changes with a **Breaking:** prefix opening the body, plus
the migration path. At release time tools/release.sh (Phase 1) renames this file
to vX.Y.Z.md, stamps the `# ` title with the version and date, and reseeds a fresh
accumulator. Run the release-notes skill first to reconcile/lint the entries and
finalize the one-line summary below (drop this comment). Keep a Changelog:
https://keepachangelog.com/en/1.1.0/
-->

## Added

- **`3.1.0-F2` — `sysforge state failed --names` feeds last run's failures back into a retry.** `state failed` already knows exactly the set a user wants to retry after a partial `update`, and `build` takes several package names, but the two could not be composed: the verb rendered only a padded, paged table, and there is no machine-readable output mode anywhere in the CLI. The workarounds were scraping the table (coupled to column padding) or reading `build_state.toml` directly (a schema documented as internal). `--names` prints only the failed pkgbases, one per line, with no header and no pager, and nothing at all when nothing failed. `sysforge build $(sysforge state failed --names) --makepkg=-f --interactive` is now the supported retry. The mode is verb-local on purpose, since this is the one listing whose output is already a set of package names; a CLI-wide machine-output mode would be a separate decision. It has its own name because the global `--quiet` already means verbosity 0.

---

- **`3.2.0-F11` — `sysforge state profiles` lists and reclaims the shared profile store.** The PGO, AutoFDO, Propeller and BOLT profiles under `/var/cache/sysforge` were write-only from the user's side: nothing listed what was there, how old it was or which package it belonged to, and nothing reclaimed it. The purge path `makepkg_pgo`'s docstring promised was never written. `sysforge state profiles` lists every non-empty store with size, age and, where recorded (`3.2.0-B16`), the version a profile was collected against. `--purge <method>[/<target>]` deletes one store after a confirmation; it refuses an unknown method and refuses to run without a terminal, because a profile needs a workload to regenerate. Removing a package still never deletes its profile (the profile's presence is what keeps later rebuilds profiled), but `revert-to-stock` and `uninstall` now name any store they leave behind and the command that reclaims it. The stale docstring is corrected.

---

- **`3.2.0-F15` — a post-build lint names config a package places where its consumer never reads it.** `cosmic-greeter-git` once installed its PAM file under the *directory* `/etc/pam.d/cosmic-greeter/`, where PAM resolves a service to the flat file `/etc/pam.d/<service>`. PAM fell through to `pam.d/other`, `XDG_RUNTIME_DIR` was never created, and `cosmic-comp` panicked on every greeter restart, an unbootable desktop from a one-flag packaging slip. Authentication still succeeded, and nothing in the build, `pacman -Qkk` or the install logged anything. After every build, `primitives/payload_layout.py` now lists each package's members once and warns (non-fatally) on any file nested deeper than a non-recursive consumer looks: `/etc/pam.d`, `/etc/sudoers.d`, `/usr/lib/sysusers.d`, `/usr/lib/tmpfiles.d` and `/etc/ld.so.conf.d`. It deliberately takes no general `/etc` stance, so config trees that legitimately nest stay silent. Each finding is also repeated in the end-of-run summary of `update` and `build`, including single-package builds, so it is visible at default verbosity. New standards row 28 cites each directory's spec and is held to the lint's table by its tests.

---

- **`3.2.0-F17` — `base_config_merge = "overlay"` keeps your kernel config and adopts the packager's choices for what is new.** A non-default `base_config` was copied over the PKGBUILD's `.config` wholesale, so across a version bump every symbol the base did not mention (anything new in this release) resolved to the upstream Kconfig default instead of the PKGBUILD's value. The new `kernel.toml base_config_merge` key (CLI `--base-config-merge`) defaults to `"replace"`, so `base_config = "running"` still means *exactly my config*. `"overlay"` merges the base onto the PKGBUILD's config with `merge_config.sh -m`: the base wins every symbol it names, `# CONFIG_X is not set` included, and the PKGBUILD fills the rest before `olddefconfig`. One edge: a symbol missing from the base only because its dependencies were off takes the PKGBUILD's value once they are on. A PKGBUILD that runs its own `merge_config.sh` skips the seed entirely, and the `base cfg:` summary line now says the base is inert there. Adopt the commented default into a live config with `make sync-config`.

## Changed

- **`3.2.0-STD1` — the release preflight now runs lint and typecheck.** `typecheck` ran only in `make pre-release`, which is advisory; `tools/release.sh` preflight, the gate a release cannot skip, never called it. `v3.2.0` shipped four pyright errors, one of them the live BOLT crash (`3.2.0-B30`) the type-checker had named weeks earlier, and the 3.3.0 cycle reached eighteen. The preflight now runs `make lint` and `make typecheck` beside the five shared `check-*` gates and fails the release on either. The full test suite stays in the heavier tier by design. New standards row 27 (release-gate completeness) is enforced by a `check_standards` `release_gates` group that diffs `pre-release`'s prerequisites against the targets the script invokes, so `docs/RELEASE-CHECKLIST.md`'s gate table cannot silently diverge again. Because these gates now block a release, the tools they fetch per run are pinned (`PYRIGHT_VERSION = 1.1.414`, `REUSE_VERSION = 6.2.0` in the Makefile), and the same group fails on an unpinned `uv run --with` in any gate recipe. Bump a pin deliberately in its own commit.

## Fixed

- **`3.2.0-B12` — a `--pgo=record` build under the sandbox is now refused instead of silently losing its profiles.** `mesa_pgo.generate_flag` bakes `-fprofile-generate=<store>` into the flags. Under `[security] sandbox_builds` the instrumented package wrote its `.profraw` to that path inside the container, which `makechrootpkg -c` discards at teardown. The build succeeded, the profiles were gone, and the next `--pgo=use` found an empty store or reused a stale merge, with no diagnostic. The existing probes ask whether an *input* is present, which a generate flag (having none) always passes. `build_sandbox.refuse_profile_generate` now scans the conf and exports for the `-f[cs-]profile[-instr]-generate` family and raises `SandboxUnavailable` before any provisioning. The error explains that `--pgo=record` must run unsandboxed, after which `--pgo=use` may run sandboxed. Carrying profiles out at teardown or bind-mounting the store were considered and not taken: the first needs a hook that survives a failed build, and the second opens a host write channel inside the isolation boundary.

---

- **`3.2.0-B16` — a reused mesa PGO profile now says how old it is.** The `llvm-pgo` store records the LLVM major its profile was collected against (`clang.profdata.version`), but `pgo-mesa` wrote no sidecar, and its reuse check was a bare existence test. Every `sysforge update` rebuild therefore re-applied whatever profile was on disk with no bound on its age; on the development workstation, one profile collected against mesa `26.1.3` fed four later minor versions without a word. `mesa_pgo.merge_profraw` now writes `<pkgbase>.profdata.version` with the installed version of the instrumented build that produced the `.profraw`. On reuse, the `PGO (reuse)` line appends "profile collected against X, building Y; refresh with `sysforge build <pkg> --pgo=record`" when the versions differ, or "profile age unknown" for a profile collected before the sidecar existed. Unlike the LLVM gate, no verdict refuses the profile: mesa skew is gradual source drift, and discarding a profile that cost a workload to collect would be the worse regression.

---

- **`3.2.0-B21` — installing a libLLVM consumer outside the toolchain stage is now re-verified against the installed libLLVM.** Only the toolchain stage (after libLLVM changes under its consumers) and `doctor` asked `toolchain_safety.check_installed_consumer_symbols`. The reverse direction, a *consumer* changing on top of an unchanged libLLVM, had no post-install check, which is how the 2026-09-11 `mesa-sysforge` 26.2.2 install reached a reboot before anything named the cause. `3.2.0-B20` already refuses the known shape from the package archive before `pacman -U`. `build_core.build_and_install` now also runs the same symbol fact after its final install whenever an installed package is libLLVM or depends on it, and reports each finding as an error naming the consumer and how to roll back (pre-build snapshot or `pacman -U` of the previous package). It is the backstop for what an archive scan cannot see: a split member pulling another soname, or a dependency JIT-installed mid-batch.

---

- **`3.2.0-B22` — the `mesa_llvm_symbols` finding tells apart the two ways a consumer loses its libLLVM symbols.** `toolchain_safety.check_installed_consumer_symbols` explained every std:: drift finding as "the PGO libLLVM inlined away the weak std:: copies, so rebuild the consumer". That fits a libLLVM rebuilt under an existing consumer. On 2026-09-13, though, `doctor` said the same about a mesa that had been *built* against the repo libLLVM inside a sandbox container, where rebuilding the same way reproduced the identical broken package. Against the installed libLLVM, the finding now compares the consumer package's build date with the libLLVM package's install date (new `pacman.get_package_dates`). A consumer built after that install was linked against a different libLLVM build: the finding says so, points at how it was built (a sandboxed build must inject the host's libLLVM), and tells the user to restore the stock package meanwhile, without offering the rebuild. Otherwise, or when a date is unknown, the wording is unchanged.

---

- **`3.2.0-B24` — toolchain tests no longer take the host's real PGO build lock.** `pgo.pgo_lock_path` puts the lock beside `staging1`, which defaults to `/var/tmp`, and fifteen toolchain tests (four in `test_stage_toolchain.py`, eleven more through the shared `_pgo_setup` helper) overrode `pgo_staging` but not `pgo_staging1`. They locked `/var/tmp/sysforge-pgo.lock`, so they failed whenever a real `sysforge run toolchain` was running and could block one from starting. Every PGO test config now scopes `pgo_staging1`/`pgo_staging3` into `tmp_path`, and the shared autouse `_toolchain_gates_clean` fixture wraps `pgo.pgo_lock` to fail any test whose lock path falls outside `tmp_path`, so a future leak fails deterministically instead of only under load. Test isolation only; no user-visible change.

---

- **`3.2.0-B37` — Gate 2 and the kconfig drift check now read the kernel build tree the build actually used.** Both searched the *system* `makepkg.conf` `BUILDDIR` under the *checkout* name, while a profiled kernel build runs with the profile's `BUILDDIR` (`$HOME/builds`) and makepkg names the tree after the post-rename pkgbase (`linux-sysforge`). Every renamed kernel build therefore logged `Gate 2: resolved kernel .config not found in build tree` and installed with the boot-critical audit silently off. `makepkg_wrapper.run` now returns the tree it built in (`makepkg_env.build_root`: profile `BUILDDIR`, falling back to the system conf, keyed on the authoritative pkgbase), and the kernel stage passes it to Gate 2, the drift check, the kconfig history and Gate 3. A fresh build whose reported tree has no `.config` now aborts before install (override: `--skip-boot-audit`); only the AlreadyBuilt path, which has no fresh tree, keeps the warning.

---

- **`3.2.0-B39` — a profile flag change no longer reports the kernel as drifted.** Kernel builds strip `CFLAGS`/`CXXFLAGS`/`CPPFLAGS`/`LDFLAGS` and the `DEBUG_*` variants from the emitted conf (`profile.KERNEL_CLEAN_KEYS`; the kernel takes its optimisation from Kconfig), but flag drift still diffed the full serialized profile. Changing the `optimized` profile (`-O2` → `-O3 -fno-plt`, `--icf=all` in `LDFLAGS`) therefore flagged `linux-sysforge`, the most expensive rebuild in the system, although nothing about its build would change. `serialize_effective_flags(kernel_build=True)` now drops those keys on both the record and the replay side, and `flag_drift` strips them from an existing kernel record before diffing, so records written before this fix converge without one last false drift. Only keys that reach the kernel build (`CC`, `LD`, `RUSTC_WRAPPER`, …) can drift; non-kernel packages are unchanged.

---

- **`3.2.0-B40` — the flag-drift notice now says what it will actually rebuild.** With `--rebuild-on-flag-drift` on, the Phase 4.3 `ui()` line picked its hint from the flag alone (`… rebuilding (--rebuild-on-flag-drift)`), but only drifted packages in the run's package walk get promoted. Stage-owned packages (the kernel, the toolchain) are found by the build-state-wide fold and never queued, and the `warn()` saying so was hidden at default verbosity, so a default `update` announced it was rebuilding `linux-sysforge` and then did nothing. The line is now built from the same predicate the promotion uses (`update._partition_flag_drift`). It lists what is being rebuilt and, separately, what drifted but was not queued, each with the command that would rebuild it: `sysforge run <stage>` for a stage-owned package, `sysforge build <pkg>` otherwise. The now-redundant hidden warning is gone.

---

- **`3.3.0-B1` — switching `base_config` back to `"pkgbuild"` no longer keeps applying an old base config.** The kernel's base-config seed step runs whenever a `sysforge.base.config` file exists next to the PKGBUILD, and `write_base_config` returned early for `"pkgbuild"` (and for `"running"` when no running config could be read) without removing a seed an earlier run had written. The file lives in the untracked source checkout and is never cleaned, so after switching back to `"pkgbuild"` every build silently kept seeding the old running-kernel config, and the "falling back to the PKGBUILD base" warning was false. A run with no base text now removes both seed files (`sysforge.base.config` and `sysforge.base.overlay.config`), the same rule the hotplug fragment already follows. `--dry-run` still changes nothing.

## Security

- **`3.1.0-F8` — PGP keys are no longer fetched from a keyserver unattended.** `import_pgp_keys` ran `gpg --recv-keys` for every `validpgpkeys` fingerprint missing from the keyring, with no prompt, no fingerprint shown and no record. `validpgpkeys` exists so a person decides who the upstream signer is, so a tampered PKGBUILD that added its own key beside a re-signed tarball had it installed silently and then passed makepkg's verification. The fetch also bypassed `freeze_sources`. Keyserver fetches now go through one consent-gated home, `build_prep.fetch_pgp_keys`, which the build-failure PGP repair also uses. The fetch is its own egress kind (`key_fetch`) and obeys the source freeze. Keys land in a throwaway keyring first, and each one's fingerprint, owner and requesting package are shown before anything is imported. Import needs a TTY confirmation (default No). A non-interactive run imports nothing and reports the fingerprints and the manual `gpg --recv-keys` command. **Batch runs** that relied on unattended imports: set `[security] auto_fetch_pgp_keys = true` (the keys are still shown). Bundled `keys/pgp/*.asc` imports are unchanged.
