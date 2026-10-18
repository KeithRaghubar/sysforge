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

- **`3.1.0-F3` — a way to keep sysforge away from the AUR.** Set `enabled = false` in `sysforge.toml`'s `[aur]` section and sysforge never contacts the AUR: no clones, no lookups, no search results and no package-list download. A package you track from the AUR is reported as `AUR disabled` and left alone rather than failed, `build` refuses one up front, and a dependency that only the AUR has is named as such. Repo and local packages work as before, and a checkout whose `origin` is somewhere other than the AUR still syncs. Separately, `update` no longer downloads the AUR package list on every run: it does so only when you track something from the AUR, since only shell completion reads it.

---

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

---

- **`3.5.0-F1` — `bench toolchain` gets past an out-of-date sync database.** The bench downloads the stock `clang` and `llvm-libs` that your sync database names. When the repo rebuilt them after your last `pacman -Sy`, every mirror had already dropped those files, so each download failed with HTTP 404 and the bench stopped. It now notices that every mirror failed, downloads a fresh copy of the sync database into its own temporary directory, and compares against the build the repo carries now. It does this as your user with `fakeroot` (from `base-devel`), never with sudo, and your system's sync database is left alone, because syncing it without upgrading would leave the system partially upgraded. The report shows the stock version it used, so a newer stock build is flagged as a version difference, as before. If the refresh fails too, the error says so and suggests `pacman -Syu`. `--offline` never refreshes. The PGO guide describes the fallback.

---

- **`3.5.0-F4` — mesa built against the stock LLVM loads with sysforge's optimized LLVM.** The optimized libLLVM that `run toolchain` builds stopped providing some C++ string functions that the stock libLLVM happens to provide, so a mesa linked against the stock one (as the build sandbox does, for mesa's Rust parts) imported them from libLLVM and could not load against sysforge's. sysforge refused to install such a mesa, and rebuilding it did not help, because the sandbox links it against the stock libLLVM again. The toolchain build now gives libLLVM those functions back, as small forwarders to the C++ library's own copies, in a form that only already-built programs use: nothing newly built against the optimized libLLVM starts depending on them, so the toolchain's own programs keep using the C++ library directly. At `-v`, the toolchain build also lists any C++ library functions the stock libLLVM provides that the new one still lacks, when the stock `llvm-libs` of the same version is in the pacman cache. This applies to x86-64 builds linked with lld, which every optimized toolchain build is. A build reused with `--reuse-built` from before this release is rebuilt once.

---

- **`3.5.0-F7` — `run toolchain --bolt-only` reruns BOLT without rebuilding LLVM.** BOLT only needs the LLVM build that `run toolchain` leaves in your build directory, so `sysforge run toolchain --bolt-only` now rewrites that build, repackages llvm and installs it through the usual checks (the pre-install audit, the install snapshot and the post-install verification) in minutes instead of the hour a full build takes. It works even with `[bolt] enabled` off, using your other `[bolt]` settings, and never syncs sources. It refuses, saying why, when that build isn't the finished optimized build of the version you have checked out: for example after an interrupted run, a version change, or a non-PGO build. sysforge now keeps the original libraries next to the build, so every BOLT run, including a repeat `--bolt-only`, starts from them rather than rewriting BOLT's own output. If BOLT can't run, `--bolt-only` stops before installing anything. Before installing it asks whether to go ahead with BOLT and the install (not with a build), and it doesn't require the 40 GiB of free space a full build needs.

---

- **`3.5.0-F8` — `run toolchain --install-only` installs the last build without rebuilding it.** When a run built the toolchain but didn't install it (the install failed, or its sudo prompt timed out), the only way forward was another multi-hour build. `sysforge run toolchain --install-only` installs the packages that run left, BOLTed ones included, with the same checks, snapshot and verification as a full run. It refuses if the last build isn't the optimized build of the version you have checked out, or a package is missing. A failed install now suggests it.

## Changed

- **`3.0.0-F3` — `update` no longer builds a changed source nobody looked at.** The PKGBUILD review gate, which diffs a package's whole source tree since the last build you accepted, used to accept every change during `update` with a logged line, and accepted silently whenever there was no terminal, so a timer-driven `update` built and installed whatever had arrived. Now a changed source is shown to you at a terminal (view, accept, skip or abort, as `--review` did), and with no terminal (a timer, cron, a pipe) that package is refused: the rest of the run still builds, the summary lists it under "Refused at review" and the run exits 1. Changes from Arch's own packaging repos are accepted with a logged line, since the same people publish the binary packages you already install; `[build] review_trusted_sources` in `packages.toml` changes that list, and `--review` reviews everything. `build` and the toolchain stage's rebuild of libLLVM consumers follow the same rules, and `build` gains `--review`. To keep the old unattended behaviour, set `[build] review = "accept"` (every accepted change is still listed in the summary) or pass `--no-review`. The config comment explains the risk: a build sandbox limits what a build can read, but it does not inspect what gets installed.

---

- **`3.1.0-F4` — `update` asks before it changes anything, and `setup` can save your answer.** A first `sysforge update` could rebuild and reinstall any number of packages without asking. At a terminal, `update` now shows its plan (what it will rebuild and why, what it will upgrade from the repos, and whether a system upgrade follows) under a line stating the protections in force: review mode, build sandbox, source freeze and AUR. It then asks `Proceed? [Y/n]`. Enter means yes; `n` or Ctrl-D stops with nothing changed. Packages that will stop at the review prompt are marked, and so is any package whose profile turns the build sandbox on or off against your global setting. `--dry-run` prints the same plan. Pass `--yes`, or set `confirm_plan = false` in `packages.toml`'s `[build]` section, to skip the question; a run with no terminal never asks. `sysforge setup` now also asks whether to confirm updates, how to treat changed AUR/git sources, whether to use the AUR and, when devtools and a chroot are set up, whether to build in the sandbox, and saves only the answers you change.

---

- **`3.2.0-F19` — an interactive build's output now reaches the logs.** An interactive build (`sysforge build --interactive`, and the kernel stage, which is interactive by default) handed makepkg your terminal directly, so nothing it printed reached sysforge. When it failed, the run log and the per-package log said only that the build had failed, and finding out why meant reconstructing the build by hand. makepkg now runs on a terminal sysforge relays: what you see and type is unchanged, so prompts and the kernel's `nconfig` menu work as before and Ctrl-C still stops the build, but every line is also copied, without colour codes, into the per-package log. When the build fails, its last lines, centred on the error, are repeated in the run log as well, and sysforge's failure diagnosis now reads them too. The kernel guide mentions where to find them.

---

- **`3.4.0-F5` — every `doctor` check now says whether it ran or was skipped.** With `-vv`, `doctor` lists the checks each axis ran and the ones it skipped, with the reason, but only the `graphics` and `gfxperf` axes kept that list. Every other axis printed `roster: not reported by this axis` at `-vvv`, and a check that could not run looked exactly like one that passed: `services` read as clean when your user cannot read the journal, `audio` when there was no user session bus under `sudo`, `network` when `ip` was missing, `storage` when `/etc/fstab` was unreadable, and `pacman` when the mirrorlist could not be read. All seventeen axes now report the list, and each of these cases appears as a skip with its reason, such as `missing_firmware (journal not readable by this user; add it to the systemd-journal group)`. A few checks that do not apply are skips too: `boot_entries` with a boot loader other than systemd-boot, `kernel_boot_config` when the running kernel does not expose `/proc/config.gz`, and `rust`'s pin check when no package is named. Output at the default verbosity, and `doctor`'s exit code, are unchanged.

---

- **`3.4.0-F10` — `bench toolchain` alternates which compiler it times first.** Each timed round compiled the workload with the stock clang and then with the sysforge build, always in that order. Interleaving the rounds already spread slow drift (thermal, background load) over both compilers, but anything that depends on running first or second within a round, such as caches the first compile warms for the second or a CPU boost budget the first one spends, always landed on the same side. The order now flips every round (stock first, then sysforge first, and so on), which cancels such an effect without adding any runs. The warm-up and the number of rounds per compiler are unchanged, so a result can differ slightly from one measured before.

---

- **`3.4.0-STD2` — the standards table no longer has two rows numbered 28.** The standards table in `docs/design/21-standards.md` numbers every row in one sequence shared by its two subsections, and code, tests and release notes cite rows by that number. Row 28 was used twice: by the `pam.d(5)` / `sysusers.d(5)` non-recursive directories row, and later by "Subprocess goes through `primitives/run`". The subprocess row took the number second, so it is now row 31 and notes its former number. The `pam.d(5)` row keeps 28, so the one published citation of row 28 (the v3.4.0 note for `3.2.0-F15`) still points at the right row. Every citation in code, tests and design docs now points at the right row. A new `make check-standards` group, `standards_rows`, rejects a repeated row number and checks that the table's "next row is N" counter is one past the highest row, since a stale counter is how the number got reused.

---

- **`3.5.0-DOC1` — the package description says what sysforge optimizes.** The `pkgdesc` shown by `pacman -Si` and on the AUR now names what people search for in one short line: "AUR helper that builds and tracks packages tuned to your CPU with PGO, BOLT and LTO", instead of a generic "build and maintenance suite" line. The README's opening paragraph says the same.

---

- **`3.5.0-DOC2` — the man page opens with what a running system uses.** Its name line now matches the package description, and its description of `sysforge run` leads with `run toolchain`, `run kernel` and `run packages`, then describes `run pipeline`, `run hardware` and `run reconfigure` as the fresh-install bootstrap.

---

- **`3.5.0-F2` — `run toolchain` builds the LLVM toolchain from upstream's single `llvm` package.** The repos now build clang, lld, polly, compiler-rt and openmp, along with lldb, libclc and others, as part of the `llvm` package. With PGO on, the whole set now goes through the profiling build together, which also means clang's own code (`libclang-cpp`) is optimized with the profile for the first time; before, only libLLVM was. Building them separately would have meant building the entire llvm source tree two extra times. When it installs, the toolchain stage now also replaces the other packages that come from the same build and that you already have installed (for example lldb and libclc), so they match the new libLLVM, and the rollback covers them too. Packages from that build you never installed, such as the WebAssembly runtimes, are built but not installed. Your `[packages]` lists in `toolchain.toml` keep working unchanged.

---

- **`3.5.0-F3` — `[bolt] enabled` can now run, and optimizes clang's library inside the toolchain build.** The BOLT option in `toolchain.toml` could never work: it tried to build the BOLT tools in a way the LLVM packages cannot support, then would have rewritten the 117 KB `clang` launcher instead of the library that holds clang's code, and it relied on a CPU feature (branch sampling) that Zen 3 processors lack. It now runs during `sysforge run toolchain`, right after the optimized LLVM is built and before anything is installed: it profiles `libclang-cpp` (and `libLLVM`, with `libllvm = true`) while compiling a sample of LLVM's own source, rewrites them, checks the result still compiles, and packages it. It uses the BOLT tools from the same build, so it needs the repos' single `llvm` package, and it no longer needs `perf`. Any failure warns and the toolchain installs without BOLT. Still experimental and off by default: it has not yet been through a full toolchain build.

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

---

- **`3.5.0-B5` — `update --dry-run` now lists the packages drift would rebuild.** With `--rebuild-on-flag-drift`, `--rebuild-on-toolchain-drift` or `--rebuild-on-drift`, a dry run answered before those packages were added to the rebuild list, so its report, including `--json`, showed them as up to date while the real run rebuilt them. The dry run now answers after them, and lists exactly what the real run would build.

---

- **`3.5.0-B6` — registering a toolchain now updates a root-owned `profiles.toml`.** When the toolchain stage set `[defaults] toolchain` to match the compiler it registered, it wrote `profiles.toml` directly, which fails on the root-owned copy in `/etc/sysforge` and is not atomic. It now writes the file the same way sysforge replaces every other config file: atomically, keeping the file's mode and owner, and asking for sudo when the file needs it.

---

- **`3.5.0-B7` — the review diff opens in your pager during `update`.** `update` sets `PAGER=cat` for its whole run so that pacman hooks and other subprocesses can't open a pager mid-update, but sysforge's own review prompt read the same setting, so choosing `[v]iew diff` printed the whole diff straight to the terminal. The review prompt now uses the `PAGER` and `LESS` you had before the run (or `less` when you have none), while subprocesses still get `cat`. Each paged diff also opens on a `[REVIEW]` line naming its package, and when several dependencies changed, each one's position (`2/5`), so you can tell where one package's diff ends and the next begins.

---

- **`3.5.0-B8` — an accepted review change counts as reviewed even if its build fails.** sysforge saved the commit you had reviewed only once the package built successfully. When a build failed after you accepted its change, for a missing dependency or a failed download for example, the next run showed the same diff again, plus anything newer, as if you had never seen it. The commit is now saved when you accept, so the next run asks only about what changed after it. The same applies to dependencies accepted as a batch, to changes accepted without asking (trusted sources, or `[build] review = "accept"`), and to packages you accepted earlier in a run you then aborted. Two smaller fixes come with it. A build run with the review gate off (`--no-review` or `[build] review = false`) used to count as reviewed, so the next reviewed run silently skipped everything it had built; it no longer does. The saved commit is also the one you were shown, not whatever the checkout held by the end of the build. A switch back to the stock package keeps the review. Commits saved by earlier versions still count until your next accept. The `[REVIEW]` header and the `--no-review` help now say "since last accepted review".

---

- **`3.5.0-B9` — Rust builds in the build sandbox show cargo's progress line again.** With `sandbox_builds = true`, the `Building [=====>    ] 120/431: <crate>` line that cargo draws above sysforge's progress bar never appeared; only the `Compiling` lines did. The sandbox's build tool always logs the build to a file, which sends cargo's output through a pipe, and cargo only draws its progress line when it is writing to a terminal. The sandbox now tells cargo to draw it anyway, at your terminal's width, whenever sysforge is passing the build's output to a terminal. Piped runs (`sysforge update | tee log.txt`) and `-vvv` runs are unchanged, and a profile that sets `CARGO_TERM_PROGRESS_WHEN` or `CARGO_TERM_PROGRESS_WIDTH` keeps its own setting. The width is taken when the build starts, so the line does not reflow if you resize the window mid-build. Builds outside the sandbox are not affected.

---

- **`3.5.0-B10` — `sysforge uninstall` tab-completes installed packages only.** Pressing Tab after `uninstall` offered every package sysforge knows about, including everything in the repos and the AUR, so most suggestions named packages that aren't installed and that `uninstall` can't remove. Both the zsh and bash completions now offer only installed packages, including sysforge's renamed `-sysforge` builds under the names they are installed as.

---

- **`3.5.0-B11` — the run log says which stage-owned package a version check was for.** At the end of `update`, sysforge checks whether the repos have a newer version of each package the toolchain or kernel stage owns, and with `-vv` (and always in `sysforge-update.log`) each check logged only `[VERSION] vercmp '23.1.3-2' '23.1.1-1' -> 1`, with no package name, no stage, and no plain answer. Each check now logs one line like `[UPDATE] toolchain-owned clang: repo 23.1.3-2 is newer than installed 23.1.1-1`, or `is not newer than` when there is nothing to do. The bare `vercmp` line is still in the log file but now appears only at `-vvv`, everywhere sysforge compares versions.

---

- **`3.5.0-B12` — `sysforge run toolchain` stops calling every run its first.** The "This is the first toolchain run on this install" notice showed on every `run toolchain` and `run kernel`, however many times the stage had completed, because running a single stage never recorded that it finished. Single-stage runs now record the stage as running, done or failed, the same way `run pipeline` does, so the notice shows only until the stage first completes. The same gap meant the toolchain a standalone `run toolchain` registered (its compiler paths and which build it is, stock or PGO) was never saved, so a later `sysforge run packages` neither pinned that compiler nor stamped the packages it built with it, and `update` could not tell when a toolchain rebuild had made them stale. It is saved now. If you have been running stages one at a time, the notice appears once more and then stops after the next successful run.

---

- **`3.5.0-B13` — `run toolchain` finds clang, lld and the rest in the `llvm` PKGBUILD again.** The repos now build clang, lld, polly, compiler-rt and openmp as part of the `llvm` package instead of as packages of their own. sysforge kept using the separate `clang`, `lld`, … checkouts already in your source directory, which stopped receiving updates at 23.1.1, so `run toolchain` failed with `pkgctl repo switch '23.1.3-2' failed: pathspec did not match` for five packages. sysforge now asks the repos which package builds each name and uses that checkout, and with `-v` it names each old checkout it skipped so you can delete it. This only fixes where the sources come from: building the LLVM toolchain with PGO also needs the build itself reworked for the merged package, which is still to come.

---

- **`3.5.0-B14` — building LLVM with fewer targets no longer breaks lldb, bpftool, OpenCL and other programs.** `toolchain.toml [llvm] targets` (or the list detected from your hardware) builds the system libLLVM with only some of LLVM's backends. Some programs need all of them: lldb, bpftool (`bpf`), OpenCL (`opencl-mesa`) and faust all failed to start with `symbol lookup error ... undefined symbol: LLVMInitialize...` after such a build, and sysforge's safety checks only looked at the graphics drivers. sysforge now checks every installed program that uses libLLVM: the target list keeps every backend an installed program needs (with `-v` it names them), the toolchain build refuses to install, or rolls back, a libLLVM that would leave one unable to load, and `sysforge doctor --toolchain` reports programs that are already broken. Programs like lldb need every backend, so with one of them installed the filter now builds all targets. The same gap meant sysforge could not see most programs that use libLLVM at all, so the rebuilds it offers after a libLLVM version change found nothing to rebuild; they now find them. If your system is already affected, rebuild the toolchain, or set `[llvm] targets = []` before the next rebuild.

---

- **`3.5.0-B15` — `run toolchain` installs the llvm build's tools before reusing a saved profile.** When `run toolchain` reused the PGO profile from an earlier run, it skipped the first build pass, which was the only step that installed missing build dependencies. The single `llvm` package also needs `wasi-libc` and `wasm-component-ld`, so on a system without them the optimized build failed partway through, in the WebAssembly C++ library, with `reference to unresolved using declaration` errors about `mbstate_t`. sysforge now installs the missing repo build dependencies of every package in the toolchain build once, before the first pass, whether or not the profile is reused.

---

- **`3.5.0-B16` — rebuilding libLLVM's users after `run toolchain` no longer asks you to review official-repo packages.** After a toolchain install that changes what libLLVM exports, sysforge rebuilds the installed programs that use it. That rebuild didn't record that packages such as doxygen and faust come from Arch's own repos, so the PKGBUILD review gate treated them as AUR packages and stopped to show you their source diff, even though `[build] review_trusted_sources` trusts the official repos by default. A consumer whose checkout was cloned from Arch's packaging repo for that package, including split packages like `linux-tools` (bpftool, perf), now counts as a repo package and builds without a prompt. The check uses where the checkout came from, not the package names its PKGBUILD lists, so AUR and local consumers are still reviewed.

---

- **`3.5.0-B17` — sysforge can check out official split packages by their base name.** Some Arch packages are built together under one base name that isn't itself a package: bpftool and perf both come from `linux-tools`. Looking up `linux-tools` failed with "PKGBUILD not found", because the repos only list the packages it builds. So after `run toolchain`, rebuilding the programs that use libLLVM couldn't rebuild bpftool, and `sysforge build linux-tools` failed the same way. sysforge now recognises an official base name when you have one of its packages installed and the repos agree on that base, and checks it out from Arch's packaging repo like any other repo package.

---

- **`3.5.0-B18` — `run toolchain` no longer flags doxygen and lldb as broken by the new libLLVM.** Before installing a new libLLVM, sysforge checks that installed programs still find every symbol they import from it. That check counted every import tagged with an `LLVM_*` version as owed by libLLVM, but libclang, libclang-cpp and liblldb tag their own exports the same way. doxygen's libclang calls and lldb's own library calls were reported missing, so `run toolchain` rebuilt every libLLVM consumer it found and then reported the still-"missing" symbols as a failed install. An import is now counted against libLLVM only when none of the program's other libraries provides it.

---

- **`3.5.0-B19` — Failed rebuilds after `run toolchain` are recorded.** When a package that `run toolchain` rebuilds after installing the new libLLVM failed, sysforge logged "Failed to record build failure … not 'NoneType'" instead of saving the failure, so `sysforge state failed` never listed it. Failures are now saved to the usual state directory.

---

- **`3.5.0-B20` — `run toolchain` no longer checks lldb and libclc against the libLLVM that replaces them.** Before and after installing a new libLLVM, sysforge checks that installed programs still find every symbol they import from it. It skipped the LLVM packages installed together with libLLVM, but since the toolchain build started producing lldb, libclc and llvm-lit from the same `llvm` package, the check also inspected the old copies of those three that the same install was about to replace. A missing symbol there could queue a needless rebuild or, for a missing LLVM backend, stop an install that would have fixed it. The check now skips every package the toolchain build produces.

---

- **`3.5.0-B21` — the check after a build looks only at the packages that build produced.** After a build, sysforge checks the packages it produced for library problems. It finds them in your package output directory (`PKGDEST`), and for most builds it matched them by package name only, so older builds of the same packages still in that directory were checked too: after mesa 26.2.5 built, findings were also reported for the 26.2.4 packages left from the previous build. It now keeps only the packages at the version the PKGBUILD names. The same fix applies to the package-layout check, and to the kernel stage's install, which finds its packages the same way. For a `-git` package whose PKGBUILD version can't be matched to its packages, the behaviour is unchanged.

---

- **`3.5.0-B22` — BOLT now runs when your sysforge profile sets the build directory.** With `[bolt] enabled = true`, `run toolchain` skipped BOLT with "no llvm build tree found" whenever your profile sets `BUILDDIR` (for example `$HOME/builds`) to something other than `/etc/makepkg.conf`: it looked for the llvm build tree only where the system config says, not where the build actually ran. It now looks first in the directory Pass 4a reported building in, then falls back to the system location as before. Until this fix, an enabled BOLT never ran on such a setup, and the toolchain installed un-BOLTed.

---

- **`3.5.0-B23` — Gate 2 no longer reports harmless libstdc++ exports as missing from libLLVM.** After a healthy `run toolchain`, Gate 2 warned that the built libLLVM "lacks 30 libstdc++ export(s) stock libLLVM has" (`_Temporary_buffer` constructors, `mersenne_twister_engine::seed`, …). None of them can break anything: they are template code every program that uses it compiles into itself, and stock libLLVM exports them only because it is built with GCC. The check now counts only symbols the system `libstdc++.so.6` exports itself, the ones a program can actually look for in libLLVM, so a healthy build reports no gap. If libstdc++'s exports cannot be read, the check is skipped and says why.

---

- **`3.5.0-B24` — a stage that succeeds no longer keeps the error from an earlier failed run.** After a successful `run toolchain`, `pipeline_state.toml` still showed the previous attempt's "rebuilding its consumers did not fully succeed" error next to `status = "done"`. A stage's error is now cleared when it starts again and when it completes; a failure still records one.

---

- **`3.5.0-B25` — the "first run" config notice no longer appears after a stage has completed before.** `run toolchain` printed "This is the first toolchain run on this install" on a machine where the stage had completed in the past, because the previous attempt had failed and only the latest result was kept. sysforge now remembers when a stage first completed, and the notice appears only for a stage that has never completed. A stage that last completed before this release and then failed shows it one final time.

---

- **`3.5.0-B26` — build logs show the real ccache size.** Every build logged `ccache … cache=0.0 B` however full the cache was, because ccache 4.x reports its size in KiB under a name sysforge did not read. The size is now read correctly, and the sccache line leaves the size out when sccache reports none, instead of ending in an empty `cache=`.

---

- **`3.5.0-B27` — the run log no longer repeats the same setup lines over and over.** One `run toolchain` logged the system `makepkg.conf` and all its keys 16 times (almost half the log), the state-directory choice 12 times, and a 1,000-character "keeping 17 LLVM target(s)" warning 3 times. Each is now logged once per run, and again only if what it reports changes. The LLVM targets warning is shorter too: it lists the targets once and the packages that need them once, instead of repeating the package list for every target. It stays a warning, since it overrides a target list you may have set yourself.

---

- **`3.5.0-B28` — several `run toolchain` log lines now say what actually happened.**
  - The stage header said `PGO: True` and `Packages: 8 total (2 pgo / 6 non-pgo)`, a split the build plan then contradicted (`7 pgo / 1 non-pgo`). It now reads `PGO: on` and gives the package total only; the build plan shows the real split.
  - The LLVM source pre-flight no longer lists old `clang`, `lld` or `compiler-rt` checkouts as packages with a downgrade arrow (`23.1.3-3 → 23.1.1-1`): those packages come from the llvm PKGBUILD now, so they are shown as part of llvm.
  - makepkg no longer receives `--cleanbuild` twice.
  - The "Overriding shell RUSTC_WRAPPER" warning (and the same warning for any variable) appears only when the value actually changes.
  - The log no longer says `CC overridden via --cc` when you passed no `--cc`.
  - The change summary lists a package rebuilt at the same version under **Rebuilt (same version)** instead of calling it updated. A reinstall that changes neither version nor size still isn't listed, since the summary compares what pacman has installed before and after.

---

- **`3.5.0-B29` — zsh completion describes `--rebuild-profdata` correctly.** The zsh completion for `sysforge run toolchain` described `--rebuild-profdata` as forcing a "3-pass" PGO build; the toolchain build has four passes, as `--help` says. It now reads "4-pass".

---

- **`3.5.0-B30` — a timed-out sudo prompt at install no longer leaves you facing a full rebuild.** `run toolchain --bolt-only` asked for sudo only when it reached the install, minutes after BOLT started, so an unattended run hit a password prompt that timed out. The run then left a recovery marker whose suggested fix reinstalls the stock LLVM packages, though nothing had been installed. `--bolt-only` now asks for sudo before BOLT and keeps it alive, as a full build does. Both now check sudo before installing: if it isn't granted, the run stops with nothing changed and no marker, and says to run `sysforge run toolchain --install-only`.

---

- **`3.5.0-B31` — `sysforge state list` shows which toolchain packages are BOLTed.** After BOLT, the run log said the packages were recorded as `bolt_llvm`, but nothing recorded it: the build history still called them plain source builds, so only `readelf` on the library could tell. The packages that carry a BOLTed library (`clang` by default, `llvm-libs` too with `libllvm = true`) are now recorded with build mode `bolt_llvm`, shown in `sysforge state list`. They are otherwise treated as the source builds they are: update, flag drift and `revert-to-stock` handle them as before.

---

- **`3.5.0-B32` — the package's optional dependencies list only what you install for LLVM.** The PKGBUILD listed `clang`, `lld`, `llvm` and `compiler-rt` as separate optional dependencies, and the README named all four. Installing `clang` already brings in `llvm` and `compiler-rt`, and `run toolchain` installs the compilers it needs itself. Both also pointed at a `sysforge run toolchain --compiler=llvm` flag that doesn't exist. The list is now `clang` and `lld`, and the README says to set `compiler = "llvm"` in `toolchain.toml` instead.

---

- **`3.5.0-B33` — saying no at the `run toolchain` prompt no longer points you at the setup pipeline.** Declining `sysforge run toolchain`'s confirmation printed `Resume command: sysforge run pipeline --resume`, which starts the fresh-install bootstrap, though nothing had been built and there was nothing to resume. On its own, `run toolchain` now just says nothing was built or installed. Inside `sysforge run pipeline`, the resume hint is unchanged.

---

- **`3.5.0-B34` — `--cache-report` help describes the run it is on.** Every `sysforge run` subcommand shares `--cache-report`, and its help said the summary prints "after the pipeline completes", including under `run toolchain`, `run kernel` and `run packages`. It now says "after the run completes", and the zsh completion matches.

---

- **`3.5.0-B35` — help text names the commands it means instead of "pipeline".** sysforge's help still read as if setup were the main job. `sysforge --help` and the zsh completion now list `run` by what you run routinely: "Build the toolchain, kernel or packages, or run the fresh-install bootstrap". `run pipeline` says it is the fresh-install bootstrap. `run kernel --compiler` refers to "the compiler the last `run toolchain` set up" rather than "toolchain-stage pipeline state", and `update --include-stage-owned` to packages owned by `run kernel` or `run toolchain`. The man page's state-directory entry says "stage progress", and the top-level description matches the package description.

---

- **`3.5.0-B36` — run logs name the command that ran.** Every standalone run's log opened with `# sysforge pipeline`, even `sysforge-run-toolchain.log`, and logged its failure under `[PIPELINE]`. Each log now opens with its command (`# sysforge run toolchain: …`), and a standalone run logs its own lines under the stage's tag. The kernel and packages stages say they took the compiler "from the last `run toolchain`" instead of "from pipeline" or "pipeline state".
