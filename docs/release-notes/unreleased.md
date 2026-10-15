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

- **`3.2.0-F18` — build inputs that never pass through a profile now count as flag drift.** Flag drift compared only your profile's flags, so several settings that change what a build produces went unnoticed, and changing them left stale packages installed with no warning. sysforge now records these inputs with every build and reports a change as flag drift, with its own diff line, promoted by `--rebuild-on-flag-drift` like any other: `python`, the Python minor version for a package that depends on Python at runtime, so a `[build] python` change or a distro Python upgrade from 3.13 to 3.14 shows up (a patch release does not); `mesa_gallium` and `mesa_vulkan`, mesa's driver lists when `[mesa] filter_drivers` is on, which change with `[mesa]` settings or a GPU swap; `llvm_targets`, LLVM's target list, which changes with `toolchain.toml [llvm] targets` or a GPU swap and is reported with a `sysforge run toolchain` hint because the toolchain stage owns LLVM; and `pgo_profile`, the PGO profile an `update` rebuild would reuse from the package's store, so a profile that appeared or was deleted since the build is reported. A package built with `--pgo=use` is now checked for these inputs too, though still not for its profile flags. Packages built before this release show no build-input drift until their next rebuild, so upgrading does not flag everything at once. The drift guide lists the new lines.

---

- **`3.4.0-F3` — an opt-in, DKMS-safe compile-time kernel hardening set.** Set `hardening = true` in `kernel.toml` and the kernel stage adds a curated set of hardening options, from the kernel's own self-protection recommendations, to the config fragment it merges into your kernel's config. The set covers read-only, non-executable kernel and module memory, address and stack randomisation, the stack protector and `FORTIFY_SOURCE`, hardened copies to and from user space, allocator hardening, zeroed stack and heap memory, checks that stop on a corrupted kernel list, and a smaller `/dev/mem` and system-call surface. Arch's config already turns most of these on; listing them keeps a different base config, or a step such as `localmodconfig`, from dropping them. Three are new: `BUG_ON_DATA_CORRUPTION`, and two that cost some speed, `INIT_ON_FREE_DEFAULT_ON` and `ZERO_CALL_USED_REGS`, which is why the set is off by default. The set has the lowest priority in the fragment, so any device, hardware or `[[kconfig]]` entry for the same option wins, with a warning, and that is how you turn a single option back off. It never makes the kernel require signed modules: module signature enforcement or a forced lockdown mode would stop unsigned DKMS modules such as `nvidia-open-dkms` from loading. The kernel stage now also refuses before the build, naming the modules, if your own `[[kconfig]]` entries enforce either while DKMS modules are registered. `hardening` with `kconfig_merge = false` is refused rather than building an unhardened kernel, and `--dry-run` shows `hardening: on` in the build plan. Runtime hardening, such as `sysctl.d` settings and kernel command-line options, is system policy and stays outside sysforge. The kernel guide has a new Hardening section.

---

- **`3.4.0-F4` — `update` no longer re-adopts a package you reverted or forgot.** `revert-to-stock` and `state forget` removed a package's build record, but `update` decides what to maintain afresh on every run, so the next run could pick the package straight back up: through a `packages.toml` entry for it, through `repo_mode = "build_from_source"` (which takes in every installed repo package, including one you just returned to stock), or, for an AUR package you forgot, simply because it is installed from the AUR. Both commands now also record the decision in a new file in the state directory, `opted-out.toml`, and `update` leaves those packages alone, telling you at `-v` which setting would have taken each one back so you can remove it. A split package opts out as a whole. Only an explicit request takes a package back: `sysforge build <pkg>`, `sysforge update <pkg>` (not with `--dry-run`) or `sysforge packages add <pkg>`. `sysforge state list` has a new *Opted out* section listing them, and an opted-out AUR package is no longer listed as untracked. `uninstall` records nothing, since reinstalling a package later is a new decision.

---

- **`3.4.0-F7` — `state list`, `doctor` and `update --dry-run` can answer in JSON.** A status bar, a notification hook or a timer wrapper that wanted to know what is drifted, what failed or what would rebuild had to parse output meant for people, which is reworded freely between releases. Each of these now takes `--json`: `sysforge state list --json`, `sysforge doctor --json` (also on `doctor system` and `doctor pkg`) and `sysforge update --dry-run --json`. Stdout then carries one JSON document and nothing else, even with `-v` or `-vv`; everything else sysforge would print goes to stderr. Every document has a top-level `"schema"` number, now 1, and a `"kind"`. A new field may appear in a minor release; a renamed or removed field bumps `"schema"`. `state list` lists the recorded, opted-out and untracked foreign packages; `doctor` lists each axis's findings and the checks it ran and skipped, with the reason; `update --dry-run` lists each package's verdict, both kinds of drift with whether this run would rebuild them, and any source-sync failures. Exit codes are the same as without `--json`. Because a real update or `doctor pkg --apply` changes the system, `update --json` without `--dry-run` and `doctor pkg --apply --json` are refused. The zsh and bash completions and the man page include the new flag.

---

- **`3.4.0-F8` — version changes are coloured by how big the jump is.** Every `old → new` row looked the same whether it was an epoch change, a major release or a rebuild, so in a long `update --devel` summary the few rows worth a second look were lost among dozens of `r1234 → r1240` VCS ticks. On a colour terminal each pair is now tinted by the first part of the version that changed: red for an epoch change or a downgrade, yellow for a major version, cyan for a minor one, and dim for a pkgrel-only rebuild or a VCS tick (only the trailing `rN` / commit hash moved). Patch-level changes, and versions whose shape can't be read reliably (such as a `YYYYMMDD` date), stay uncoloured. This applies to the `update` summary (built packages, the system upgrade, and stage-owned updates), the change summary after each pipeline stage, the toolchain stage's list of version changes before it builds, and the LLVM and toolchain pre-flights. With colour off (`--color=never`, `NO_COLOR`, or output piped elsewhere) the output is unchanged, and colour never reaches the log files.

---

- **`3.4.0-F9` — `bench toolchain` shows what it is doing while it runs.** At default verbosity the verb printed nothing between launch and its report: the download of the stock `clang` and `llvm-libs` (about 105 MB when they aren't cached), the extraction, and every timed compile round (about 40 seconds before any download) looked like a hang. The progress line now names each step: `fetching stock clang <version>`, then the same for `llvm-libs`, `unpacking stock clang`, the warm-up, and `round 3/5` for each timed round. The report replaces it when the bench finishes, and so does the error message if a step fails. Piped or non-interactive output shows the same steps as `[PROGRESS]` lines. The `-vv` narration is unchanged, and so is the output with `--quiet`.

## Changed

- **`3.2.0-F19` — an interactive build's output now reaches the logs.** An interactive build (`sysforge build --interactive`, and the kernel stage, which is interactive by default) handed makepkg your terminal directly, so nothing it printed reached sysforge. When it failed, the run log and the per-package log said only that the build had failed, and finding out why meant reconstructing the build by hand. makepkg now runs on a terminal sysforge relays: what you see and type is unchanged, so prompts and the kernel's `nconfig` menu work as before and Ctrl-C still stops the build, but every line is also copied, without colour codes, into the per-package log. When the build fails, its last lines, centred on the error, are repeated in the run log as well, and sysforge's failure diagnosis now reads them too. The kernel guide mentions where to find them.

---

- **`3.4.0-F5` — every `doctor` check now says whether it ran or was skipped.** With `-vv`, `doctor` lists the checks each axis ran and the ones it skipped, with the reason, but only the `graphics` and `gfxperf` axes kept that list. Every other axis printed `roster: not reported by this axis` at `-vvv`, and a check that could not run looked exactly like one that passed: `services` read as clean when your user cannot read the journal, `audio` when there was no user session bus under `sudo`, `network` when `ip` was missing, `storage` when `/etc/fstab` was unreadable, and `pacman` when the mirrorlist could not be read. All seventeen axes now report the list, and each of these cases appears as a skip with its reason, such as `missing_firmware (journal not readable by this user; add it to the systemd-journal group)`. A few checks that do not apply are skips too: `boot_entries` with a boot loader other than systemd-boot, `kernel_boot_config` when the running kernel does not expose `/proc/config.gz`, and `rust`'s pin check when no package is named. Output at the default verbosity, and `doctor`'s exit code, are unchanged.

---

- **`3.4.0-F10` — `bench toolchain` alternates which compiler it times first.** Each timed round compiled the workload with the stock clang and then with the sysforge build, always in that order. Interleaving the rounds already spread slow drift (thermal, background load) over both compilers, but anything that depends on running first or second within a round, such as caches the first compile warms for the second or a CPU boost budget the first one spends, always landed on the same side. The order now flips every round (stock first, then sysforge first, and so on), which cancels such an effect without adding any runs. The warm-up and the number of rounds per compiler are unchanged, so a result can differ slightly from one measured before.

---

- **`3.4.0-STD2` — the standards table no longer has two rows numbered 28.** The standards table in `docs/design/21-standards.md` numbers every row in one sequence shared by its two subsections, and code, tests and release notes cite rows by that number. Row 28 was used twice: by the `pam.d(5)` / `sysusers.d(5)` non-recursive directories row, and later by "Subprocess goes through `primitives/run`". The subprocess row took the number second, so it is now row 31 and notes its former number. The `pam.d(5)` row keeps 28, so the one published citation of row 28 (the v3.4.0 note for `3.2.0-F15`) still points at the right row. Every citation in code, tests and design docs now points at the right row. A new `make check-standards` group, `standards_rows`, rejects a repeated row number and checks that the table's "next row is N" counter is one past the highest row, since a stale counter is how the number got reused.

## Fixed

- **`3.4.0-B14` — `update --explain-drift` no longer syncs sources before it reports.** Its help promised an informational report with no source sync, but the report ran only after `update`'s normal source sync, so a plain `sysforge update --explain-drift` cloned and fetched every package in scope first, and only `--offline` avoided that. `--explain-drift` now implies `--offline`: both drift checks compare your local PKGBUILDs, config and toolchain against what `build_state.toml` recorded, so the report needs nothing from the network and makes no network request at all. The drift guide no longer tells you to add `--offline`.

---

- **`3.4.0-B15` — `--no-update`'s help no longer says it skips `git pull --rebase`.** sysforge stopped running `git pull --rebase` when source sync moved to its scheduler, which fetches and fast-forwards each checkout. The help for `--no-update` still described the old command on all six verbs that take it (`build`, `fetch`, and `run pipeline`, `toolchain`, `packages` and `kernel`), and on the `run` verbs it said "before each build", which is wrong for the kernel stage's single build. Each now says it skips the source sync and builds what is checked out, and `build` and `fetch` note that `--cleansrc` still syncs. The man page and zsh completions match. The flag itself is unchanged.

---

- **`3.4.0-B16` — stage numbers in the README, `--help`, shipped configs and messages now match the pipeline.** The pipeline runs seven stages: install, hardware, configure, reconfigure, toolchain, packages and kernel. It used to have eight, with partitioning and the base install as separate stages, and the old numbers outlived the merge. `kernel.toml` called the kernel stage 8 and `toolchain.toml` called the toolchain stage 6. `bootstrap.toml` placed system identity, the mirrorlist, the desktop and the makepkg defaults in stage 4. The README said the post-reboot run covers stages 5–8, `run pipeline --help` (and its man page entry and zsh completion) said "stages 1–8", and the live-ISO stop said "Bootstrap complete (stages 1–4 done)". All now use the registry's numbers: kernel is 7, toolchain is 5, system identity is applied by archinstall in stage 1 and the other `bootstrap.toml` sections in stage 3, the post-reboot run is stages 4–7, and the ISO stop reports stages 1–3. A new test keeps every one of these, and each stage module's own header, in step with the stage list.

---

- **`3.4.0-B17` — a typo in `toolchain.toml`'s `[bench]` table no longer crashes every toolchain command.** `[bench]` only configures `sysforge bench toolchain`, but `run toolchain` and `state profiles` read the same file, and a wrongly typed value there stopped all three with a Python traceback: `corpus = 5` raised `TypeError`, and `bench = "x"` in place of a `[bench]` table raised `AttributeError`. A malformed `[bench]` table, or a `corpus` that isn't a path or a list of paths, now prints a warning and the bench falls back to its built-in workload. Valid settings are read as before.

---

- **`3.4.0-B18` — rebuilding the toolchain at the same version now marks its bench result stale.** `state profiles` marked the GAIN figure `(stale)` only when the installed clang's version or the bench workload changed. Rebuilding the toolchain with a fresh profile (`run toolchain --rebuild-profdata`, a broader `training_corpus`, or new flags) keeps the same version, so the previous build's ratio kept showing as current. The bench now records when the clang it measured was built, and a different build time marks the result stale. A result recorded by an earlier sysforge has no build time and shows as stale until you run `sysforge bench toolchain` again.

---

- **`3.4.0-B19` — `bench toolchain` fetches the stock clang in cases where it used to give up.** It looked up the stock `clang` and `llvm-libs` with `pacman -Sp`, which checks dependencies against the installed system, so an unrelated conflict or versioned dependency on the host stopped the lookup even though the bench needs only the two archives; it now skips that dependency check. A download used only the first mirror in your mirrorlist, so a lagging mirror, or a sync database newer than that mirror, gave a 404 where pacman would have tried the next one; it now tries each of the repo's mirrors in turn, and if all fail the error names each one. And a cached archive that failed its checksum was a hard error; the bench now downloads a fresh copy into its temporary directory instead, leaving the pacman cache untouched. With `--offline` a corrupt cached archive is still an error, since there is nothing to fall back to.

---

- **`3.4.0-B20` — `state profiles` finds the bench result in the right place, without asking for sudo.** The GAIN column always read `bench.toml` from the default state directory, so a result recorded with `bench toolchain --state-dir DIR` never showed; `state profiles` now takes the same `--state-dir` flag. On a system where `/var/lib/sysforge` did not exist yet, looking up the result could try to create that directory and ask for your sudo password, from a command that only lists files. It now reads the state directory without creating it. And with a configured `[bench] corpus`, every listing re-read and hashed every source file to check whether the result was stale. It now first compares the files' paths, sizes and modification times with those recorded at measurement, and reads the sources only if one of those changed. The zsh and bash completions and the man page include the new flag.

---

- **`3.4.0-B21` — `bench toolchain` needs much less temporary space.** The bench unpacked the whole `usr/` tree of the stock `clang` and `llvm-libs` packages, about 440 MB, into `$TMPDIR`, and kept any downloaded package beside it until the run ended. On Arch `$TMPDIR` is usually `/tmp`, which is held in RAM, and a small `/tmp` made the extraction fail. It now unpacks only what the compiler needs (the `clang` driver, `libclang-cpp`, `libLLVM` and clang's built-in headers), about 260 MB, and deletes a downloaded package as soon as it is unpacked. A package taken from the pacman cache is never touched. The PGO guide now says how much space the bench uses and how to point `TMPDIR` elsewhere.

---

- **`3.5.0-B1` — colour no longer leaks into the run logs as raw escape codes.** Output sysforge prints as the answer to a command is also copied into the run log (`sysforge-<verb>.log`) and the per-package log, and that copy kept any colour codes. On a colour terminal, the end of a `build` run (the `Build complete` line and its `Built:`, `Not installed:`, `Failed:` and `Skipped:` rows) was written into the log as text like `^[[32mBuilt:^[[0m`, which reads as noise in a pager or editor and breaks a plain `grep` for the label. The log copy is now stripped of colour; the terminal keeps it.

---

- **`3.5.0-B2` — `update` no longer says it pulls with git.** sysforge's source sync fetches each checkout and fast-forwards it; it has not run `git pull` since sync moved to its scheduler. A package whose sync failed still showed in the `update` summary as `[PULL_FAILED] <pkg>: git pull failed (skipped)` and was counted as `pull failed`; it now reads `[SYNC_FAILED] <pkg>: source sync failed (skipped)` and counts as `sync failed`. The `update --offline` help and its zsh completion said it skips "git pulls" and now say it skips the source sync. When a build fails because related PKGBUILDs are at different versions, the hint told you to run `git pull --rebase` in each package directory, which bypasses sysforge's sync; it now points to `sysforge fetch <pkg>...`. The man page matches. Nothing changes in what any command does.

---

- **`3.5.0-B3` — commands sysforge suggests for `doctor` now run.** `doctor`'s axes moved under `doctor system` and `doctor pkg`, and the old flat `doctor --<axis>` form was removed in 3.1.0, but ten messages still told you to run it. Following them failed with `unrecognized arguments`. They include the advisory at the end of `update` (`... log out and back in to apply (details: sysforge doctor --restart)`), the mesa context check after an install, the hardware stage's boot-readiness hint, the `--graphics` hint after a failed build, the partial-coverage notes from the restart and integrity checks, and `artifact`'s pointer to `doctor --pacman`. Each now names the subcommand: `sysforge doctor system --restart`, and `doctor pkg --apply` for the one package-scoped flag. A new test runs every `sysforge doctor ...` command sysforge prints, or that the README, guides or man page show, through the real argument parser, so a future rename can't strand one again.

---

- **`3.5.0-B4` — the progress bar no longer leaves a copy of itself in the output when the terminal gets taller.** When the window gained rows during a run (closing the other tabs so the tab bar disappears, for example), the terminal pulled scrollback back onto the screen and moved everything down, bar included. sysforge then cleared the row where the bar used to be, which by then held a line of real output, and redrew the bar at the new bottom, which pushed the old copy up into the output. It now clears from the cursor down, which reaches the old bar wherever the terminal moved it. During a build, sysforge also used to redraw the bar up to a second after a resize, and in that time the build's output could scroll the old bar into view because the terminal had dropped the bar's reserved row. It now redraws before passing on any more of the build's output. Shrinking the window was not affected.
