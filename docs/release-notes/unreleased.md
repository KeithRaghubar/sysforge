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

- **`3.5.0-F5` — every line of the run log says when it was written.** Only the section headers in a log file (`# sysforge build llvm: 2026-10-18 08:39:04`) carried a time, so a finished `sysforge-run-toolchain.log` could not tell you how long a step such as the pre-install audit, the install or the consumer rebuild took. Every line in the run log and the per-package logs now starts with the time, for example `09:41:07 [SYSFORGE][INFO][TOOLCHAIN] …`. The rest of the line is unchanged, so searching for a tag still works. When a run passes midnight, a `# date:` line marks the new day. The terminal output is unchanged.

---

- **`3.5.0-F6` — the toolchain progress line says where the stage is.** Since the repos build clang, lld and the rest as part of the one `llvm` package, every step of `run toolchain` showed `[1/1] … llvm` for up to an hour, never an estimate of the time left, and only `toolchain` between the builds. The line now counts the stage's own steps and says which one is running, for example `toolchain 5/8 · optimize · llvm · ninja 74% · 1h34m · ~40m left`. The steps are preflight; instrument, bootstrap, train and optimize for a full PGO run (only optimize when the stored profile is reused, one build step without PGO); then the pre-install audit, install and verification, plus the rebuild of libLLVM's consumers and the bench when those run. The time is how long the stage has run, and the estimate is how long each remaining step took on your recent runs, kept in a new `stage_timings.toml` in the state directory (safe to delete), so it appears from the second run on. A build's progress now reads as a percentage instead of the file being compiled, which the terminal width usually cut off, and a second build inside the same package (llvm builds its runtimes partway through) is shown beside it by name instead of looking like the main build nearly done. This applies to `build` and `update` too. The audit and verification steps name the check that is running, and the section headers in the output name what each step builds now.

---

- **`3.5.0-F7` — `run toolchain --bolt-only` reruns BOLT without rebuilding LLVM.** BOLT only needs the LLVM build that `run toolchain` leaves in your build directory, so `sysforge run toolchain --bolt-only` now rewrites that build, repackages llvm and installs it through the usual checks (the pre-install audit, the install snapshot and the post-install verification) in minutes instead of the hour a full build takes. It works even with `[bolt] enabled` off, using your other `[bolt]` settings, and never syncs sources. It refuses, saying why, when that build isn't the finished optimized build of the version you have checked out: for example after an interrupted run, a version change, or a non-PGO build. sysforge now keeps the original libraries next to the build, so every BOLT run, including a repeat `--bolt-only`, starts from them rather than rewriting BOLT's own output. If BOLT can't run, `--bolt-only` stops before installing anything. Before installing it asks whether to go ahead with BOLT and the install (not with a build), and it doesn't require the 40 GiB of free space a full build needs.

---

- **`3.5.0-F8` — `run toolchain --install-only` installs the last build without rebuilding it.** When a run built the toolchain but didn't install it (the install failed, or its sudo prompt timed out), the only way forward was another multi-hour build. `sysforge run toolchain --install-only` installs the packages that run left, BOLTed ones included, with the same checks, snapshot and verification as a full run. It refuses if the last build isn't the optimized build of the version you have checked out, or a package is missing. A failed install now suggests it.

---

- **`3.5.0-F9` — config presets in `sysforge setup`.** `setup` now offers a preset before its preference questions, for when you have one goal and don't want to read every setting: `performance` (the LLVM toolchain and the `optimized` profile), `low-friction` (no plan confirmation, no source review, automatic PGP key fetching and toolchain build reuse) or `secure` (a new `hardened` flag profile, review and plan confirmation on, clean-chroot builds once a chroot exists, and kernel `hardening`). It shows each change before writing, and the values it writes are ordinary settings you can edit later. It never turns on the toolchain or kernel stages. `low-friction` warns that with review off, changed AUR and git code is installed with nobody having looked at it and is not listed in the update summary. `--preset NAME` applies one without asking, for scripted installs. The new `hardened` profile adds flags from the OpenSSF compiler hardening guide (zeroed stack variables, bounds-checked trailing arrays, libstdc++ assertions, a non-executable stack) on top of the hardening flags Arch already sets. If your own `profiles.toml` replaces the shipped one, `secure` prints the profile to paste in instead of selecting it. The zsh and bash completions and the man page include the new flag.

---

- **`3.5.0-F10` — build failures caused by an undeclared dependency or clang's C++20 `module` rule are named.** Some failures were reported only as "build() failed: could be upstream or a flag/toolchain incompatibility", although the build log named the cause. Two are now recognized and explained under the failure. When a package's build needs something its PKGBUILD never asks for, which your system happens to have but the clean build sandbox does not (pkg-config's `Package 'libmnl' not found`, or makepkg's `Cannot find the git package needed to handle git sources`), sysforge names the package to add to `makedepends` and says it is a PKGBUILD mistake to report to the package's maintainer. When clang rejects a line that starts with `module` as a misplaced C++20 module declaration (`module declaration must not come from an #include directive`), which recent clang enforces and GCC does not, sysforge suggests building that package with gcc, through the build-failure menu or a `[[rules]]` profile with `toolchain = "gcc"`. Both are recorded with the failure, so `sysforge state failed` shows them.

---

- **`3.5.0-F11` — flag drift leaves out packages whose build compiles nothing.** A change to your profile's flags reported flag drift for every package sysforge builds, including prebuilt packages it only unpacks and copies, such as `brave-bin` or `1password`, so `--rebuild-on-flag-drift` rebuilt them although no compiler ever saw the flags and the result could not change. sysforge now notes when it builds a package whether the build compiles anything, and a package that doesn't is no longer reported for flag changes. It only counts a package as compiling nothing when its PKGBUILD has no `build()` step, no build-system makedepends (such as `cmake`, `meson` or `cargo`) and no compile command such as `make` or `gcc`; anything less clear is treated as compiling, as before. The sandbox and build-input lines and toolchain drift still apply to such a package. A package built before this release is still judged on its flags until its next rebuild. The drift guide lists the new case.

---

- **`3.5.0-F12` — a per-package compiler swap switches the whole toolchain.** When a build failed and you picked gcc (or clang) from the build-failure menu, sysforge retried and remembered the choice in `profiles.toml`'s `[package_compiler_overrides]`, but only the C and C++ compiler and the linker changed: a gcc swap on an LLVM profile kept `llvm-ar`, `llvm-nm`, `llvm-ranlib`, `llvm-strip` and lld. A row can now say `toolchain = "gcc"` or `"llvm"`, which switches the whole set the way a `[[rules]]` profile's `toolchain` does, and the menu writes that form and retries with the whole set. At its linker question, leaving the answer blank now means the chosen toolchain's own linker. Rows that name `cc`, `cxx` and `ld` directly keep working and remain the way to name a compiler outside the two sets. When `sysforge build --cc`, `--cxx` or `--ld` overrides a row for a package, `-vv` says so. The comment above the table in `profiles.toml` lists which setting wins; `sysforge config merge` picks it up.


---

- **`3.5.0-F13` — `sysforge log --verb` pages the log a verb left.** `log` could page only the unified log and per-package logs, although `build`, `update`, `doctor`, `fetch`, `setup`, `uninstall`, `revert-to-stock`, `config merge`, `state repair`, `state forget` and each `run <stage>` leave their own `sysforge-<name>.log`, which the end of the run named once. `sysforge log --verb update` (or `build`, `state-repair`, `run-toolchain` and so on; Tab lists them) pages it, and `--log-dir` finds a log written with the same `--log-dir`, for verb, unified and per-package logs alike.

---

- **`3.5.0-F14` — choose a package's toolchain when you build it, and keep it.** `sysforge build PKG --toolchain gcc` (or `llvm`) builds the packages you name with that toolchain as a whole: compilers, `ar`/`nm`/`ranlib`/`strip` and linker together, the same swap the build-failure menu makes. Before, the only CLI choice was `--cc`/`--cxx`, which changed just the two compilers and left an LLVM profile's `llvm-ar` and `lld` in place. A `--cc gcc --cxx g++` or `--cc clang --cxx clang++` pair now does the same as `--toolchain`; any other pair still sets only the compilers, for a compiler sysforge does not bundle. Add `--save-toolchain` to keep the choice for those packages in `profiles.toml`, so later builds and `update` use it, without waiting for a build to fail first; `--reset-toolchain` removes it again. Dependencies built along the way keep their own profile. `run kernel` takes the same option under the same name (it was `--compiler`; see `3.5.0-F15`).

---

- **`3.5.0-F18` — find and fix retired or misspelt settings in your config files.** An upgrade never rewrites your config files, so a setting sysforge stopped reading stayed there, silently ignored, and a misspelt key was never reported. `sysforge config check` lists both, with the line (`--json` prints them as one JSON document for scripts): settings sysforge renamed or retired (such as `[git] pull_timeout`, the per-package `pkgbuild_patch`, `repo_mode = "profiled"` and `build_mode = "patched_pkgbuild"`) and keys it does not know. `sysforge config clean` gives retired settings their current name or value, keeping your comments, and with `--unknown` removes the unknown ones. `sysforge config reset FILE [KEY ...]` puts keys back to the shipped default, or the whole file, keeping the old one as `FILE.bak-<time>`. Both show the change first and ask (or take `--yes`; `--dry-run` only shows it). `sysforge doctor system --config`, part of the default sweep, reports the same findings. The shipped defaults are now installed with the package for this, and `toolchain.toml` documents `profile_store` and `profiles.toml` documents `extends_system`, both read but listed nowhere. A test keeps every setting sysforge reads documented in the shipped files, so `config check` never calls a working setting unknown.

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

- **`3.5.0-DEV1` — the branches mutation testing showed unchecked are tested.** A mutation sweep over `abi_check`, `stage_sentinel`, `flag_drift`, `version` and `build_estimate` (comparisons, boolean operators, `not`, constants and return values flipped one at a time, each against every test file that imports the module) left mutants alive in every module. New tests kill all but six, which cannot change behaviour: `version`'s outer loop bound, whose inner loops re-check before indexing, and two `>`/`>=` swaps reached only when the operands already differ; `abi_check`'s `rsplit("/", 2)[-1]`, the same last element; and `stage_sentinel`'s branch its own comment marks unreachable. The new tests cover the ELF-class readers behind 32/64-bit matching, the verneed and `nm` parsers' malformed-input edges, the bsdtar failure path and the skew gate's symbol filter; the recovery prompt's metadata listing, declined and state-dir-mismatch returns, and invalid-record display; the flag-drift sub-axis guards and the compiler the build-inputs axis resolves with; `vercmp_local` against `vercmp(8)` for trailing separators, separator runs and alpha tails; and the duration and percent boundaries of the build-time estimate.

---

- **`3.5.0-DEV2` — the modules where the review found bugs are now covered.** The ten least-covered modules were the ones the 2026-10-18 review found bugs in. Their failure and resume paths now have tests: the packages stage's resume prompt and retry, each `artifact` verb's refusals, `packages list`, the pre-build snapshot's failure paths, the toolchain's PGO profdata gate and LLVM pre-flight, and `run reconfigure`'s makepkg, sudo, disk, network and GPG steps. Branch coverage rose from 89.6% to 91.2% overall (`snapshot.py` 53% → 99%, `packages_cmd.py` 60% → 93%, `reconfigure.py` 64% → 82%, `verbs/artifact.py` 70% → 95%, `dep_analysis.py` 71% → 82%, `pipeline/stages/packages.py` 72% → 93%, `configure.py` 78% → 90%, `makepkg_pgo.py` 77% → 99%, `toolchain/pkgbuilds.py` 80% → 88%, `makepkg_wrapper.py` 82% → 84%). The ratchet floor is re-stamped at 91.2% and now tracks these modules, with the two rows that pointed at the pre-package `kernel.py`/`toolchain.py` paths corrected. Writing the tests surfaced three bugs, filed for a later fix. Every pacman `-U` in the suite now records into a scratch sentinel dir, never the host's (`tests/conftest.py`).

---

- **`3.5.0-DEV3` — the toolchain rollback tests no longer depend on what the test machine has installed.** Since `3.5.0-B76` the toolchain stage's install snapshot keeps only the packages installed now, and it asks the system's pacman database. Three tests in `tests/test_stage_toolchain.py` (`test_gate3_failure_auto_restores_and_clears_sentinel`, `test_gate3_consumer_symbol_brick_triggers_rollback`, `test_install_and_snapshot_cover_installed_pkgbase_members`) therefore passed on a workstation with the LLVM packages installed and failed in the CI container, which lacks them; a fourth (`test_gate3_failure_restore_fails_keeps_sentinel`) passed there for the wrong reason, an empty snapshot rather than a failed restore. Each now states which packages count as installed.

---

- **`3.5.0-DEV4` — a test that reads the host's pacman database fails, naming where.** A test whose code ran a real `pacman` query passed or failed by what the machine had installed, which is how `3.5.0-DEV3`'s tests were green on a workstation and red in CI. `tests/conftest.py` now fails any such test with `HostStateError: test reached the host pacman DB via primitives/pacman.py:971 get_installed_version (pacman -Q llvm)`, and fails it at teardown even when the code under test catches the error, as the best-effort probes do. A stub at any level above the subprocess, or a pacman pointed at a fixture `--dbpath`, passes; `@pytest.mark.host_pacman` opts out the tests of the query functions themselves. The fence caught 67 tests in 8 files; each now stubs its answer as the clean CI container gives it. Eleven of them failed only in the container: the kernel stage's bootloader probe answers from `/boot` on a workstation and asks pacman only when `/boot` has no bootloader files, so the fence, like the original bug, needs a CI run to see everything. One had a stub that never applied: `test_dep_versions_excludes_build_set_members` patched a re-export the code doesn't call. And `tests/test_doctor.py`'s package-walk tests ran the opt-in rust and integrity axes, which probe the host's Rust toolchain and package files, because nothing stubbed them. `tests/test_host_fence.py` covers the fence.

---

- **`3.5.0-DEV5` — the CI run page names the gate that failed.** Each gate now runs in its own collapsible log group, and the run's summary page gets a table of every gate with pass or fail, so a red run no longer means searching ten minutes of log for the gate that broke. Every gate still runs after one fails, and the job still fails. `make ci-local` output is unchanged.

---

- **`3.5.0-DEV6` — CI reports test failures in full.** The suite in CI now prints each failure's assertion and a short traceback instead of a one-line summary, and writes a JUnit report from which each failing test is annotated on the run page with its file and line (`tools/junit_annotate.py`).

---

- **`3.5.0-DEV7` — the CI log opens with the container's environment.** Before the gates, `tools/ci.sh` prints the installed package count, whether LLVM, clang, lld, `python-tomlkit` and `pyalpm` are installed, and the Python, pytest and uv versions, so a failure seen only in CI can be compared with the local machine.

---

- **`3.5.0-DEV8` — `make test` is the complete suite, and `pre-release` and CI run one gate list.** `make test` ran the system pytest, so `tests/test_sync_config.py` skipped wherever `python-tomlkit` was not installed, and `make pre-release` ran fewer tests than CI on such a machine. It now runs pytest in a uv overlay with `tomlkit`, as `make coverage` does. The gate list is written once, as the Makefile's `GATES`: `pre-release` depends on it, and `tools/ci.sh` reads it through the new `make print-gates`, so CI now runs `test` like `pre-release` instead of `coverage`. The `release_gates` standards check expands `$(GATES)`, so it still compares each gate against the release script.

---

- **`3.5.0-DEV9` — the Makefile's comments and help match what it does.** `make help` now lists every variable a target reads (`TESTS=`, `NAME=`, `GUI=`, `DISTRO=`, `VM_USER=` and `preflight`'s `RUN_*` skips). `make dev` creates the project venv first, so it works on a fresh checkout; before, `uv pip install` used whatever venv `python` on `PATH` belonged to. `make clean` also removes `.ruff_cache/` and the generated `man/sysforge.1.scd`. `vm-ssh-root` is gone, since `vm-ssh` already connects as root. The `check-shipped` and `next-id` comments name the current check groups and ID types, and the `typecheck` comment says why it covers `sysforge/` only.

---

- **`3.5.0-DEV10` — a check that every `§Section` reference still resolves.** `make check-standards` gains a `design_refs` group: each `§` reference in the design docs, both CLAUDE.md files and code comments must name a DESIGN.md heading, a design file or a standards row. It found nine stale references, which are fixed: six pointed at a `§Reconvergence` section that is called *Drift detection*, one at a portability section that is standards row 23, one at a stale-profraw check under the wrong parent, and one at DESIGN.md line numbers.

---

- **`3.5.0-DEV11` — every written-down `sysforge` command is checked against the real parser.** The check that every `sysforge doctor …` the code prints still parses (`3.5.0-B3`) now covers every command, and reads comments and docstrings too, as well as the README, CLAUDE.md files, man page source, guides and shipped config comments. A long flag that a command does not have, or a subcommand name that is a near miss for a real one, fails the test with the file and line. It found nothing new; the two stale `doctor --toolchain`/`--suggest` comments it would have caught were fixed in `3.5.0-B125`.

---

- **`3.5.0-DEV12` — removed names cannot creep back.** `tools/retired_names.toml` lists the flags, tokens and functions this release removed, and `make check-standards` (group `retired_names`) fails if one appears anywhere outside release notes, ROADMAP, tests and the files that still read the old value to migrate it, comments included. Its first run found two stale passages in the design docs, now fixed: one citing a removed function, and the standards table still describing the `patched_pkgbuild` deprecation as live.

---

- **`3.5.0-DEV13` — tests build real options and arguments, not stand-ins.** A `SimpleNamespace` standing in for `BuildOptions`, `RunOptions` or a parsed command line broke whole test files each time the code read a new field, and would keep passing on a renamed one. A new test refuses them, and every test now builds a real `BuildOptions`, `RunOptions` or parsed command line (the last through a small `cli_args` helper), with no exceptions left: the last stand-in tested a fallback no caller needed, and both are gone. Converting them found a real bug (`3.5.0-B129`) and tests of `--dry-run` on two commands that have no such flag. The root CLAUDE.md now says to build `BuildOptions(...)`, `RunOptions(...)` or `build_parser().parse_args([...])`, and to add every removed name to `tools/retired_names.toml`.

---

- **`3.5.0-DEV14` — completions are checked command by command.** `make check-shipped` gains a `completion_commands` group. The old check looked only at top-level commands, so once group commands such as `state` and `run` stopped taking flags of their own, none of their subcommands' flags were checked, and a flag counted as present if it appeared anywhere in the file. Now each command's flags must be offered where that command is completed, in both zsh and bash; no flag the command lacks may be offered; every value a flag accepts must be offered; and lists copied from the code (desktops, profile-store methods) must match it.

---

- **`3.5.0-DEV15` — citations in the design docs and the Claude Code agents are checked.** The check that every file, module symbol and script CLAUDE.md cites still exists now covers the design docs and the agents, skills and rules under `.claude/`, and checks `make` targets too. It found and fixed a design passage on the prompt helpers that named a removed stage file and a removed function and gave call counts that were all wrong, a symbol cited under the wrong module, `make vm-install` (now `vm-install-stable`), and an agent and a skill citing files that no longer exist. A code name cited without its module must also exist somewhere in the code: that found 48 functions the design docs still called by names they lost in an earlier reorganization, a reconfigure prompt that changed, and a cache-invalidation setting the docs described but sysforge never had.

---

- **`3.5.0-DEV16` — the concept pages' rules are tested.** Each concept page must close with *Further reading*, citing at least one upstream source, then *In sysforge*, and must not use sysforge's own terms before that last section. The rule was in CLAUDE.md only; every page already followed it.

---

- **`3.5.0-DEV17` — a test that every flag reaches the code that uses it.** `run kernel --keep-hotplug-drivers` (`3.5.0-B129`) was read by the stage from an options object that never had the field, so the read always returned its default. Any such read now fails a test, as does a command flag that nothing reads. Both pass on today's code; the first would have caught B129.

---

- **`3.5.0-DEV18` — every user-facing change is recorded where reviewers look.** `tests/data/surface.json` is a generated list of everything a user can type, set or parse: commands and their flags and values, environment variables, journal fields, config keys, `--json` fields and log names. `make check-surface` fails when it is out of date, when something was added or removed without a release note naming it, or when a name disappears without being registered as retired. CI now blocks a merge on any failing check, `CONTRIBUTING.md` lists what to update for each kind of change, pull requests get a checklist, and `make install-hooks` adds an optional pre-commit hook running the fast checks.

---

- **`3.5.0-DOC1` — the package description says what sysforge optimizes.** The `pkgdesc` shown by `pacman -Si` and on the AUR now names what people search for in one short line: "AUR helper that builds and tracks packages tuned to your CPU with PGO, BOLT and LTO", instead of a generic "build and maintenance suite" line. The README's opening paragraph says the same.

---

- **`3.5.0-DOC2` — the man page opens with what a running system uses.** Its name line now matches the package description, and its description of `sysforge run` leads with `run toolchain`, `run kernel` and `run packages`, then describes `run pipeline`, `run hardware` and `run reconfigure` as the fresh-install bootstrap.

---

- **`3.5.0-DOC3` — a concept page on profile-guided and post-link optimization.** The guides say what to run; a new set of concept pages under `docs/concepts/` explains how the techniques behind them work, without assuming sysforge, and cites upstream sources rather than numbers measured on one machine. The README lists them beside the guides. This first one covers instrumented PGO, sampled profiles (AutoFDO), Propeller and BOLT: what each changes in a program, why the training workload decides the result, how profiles go stale, and when none of them pay off. The PGO guide's opening background moved here, leaving the guide the steps. The guide's toolchain section now quotes LLVM's own published compile-time figure instead of an unsourced range, and its BOLT section quotes the figure from LLVM's BOLT tutorial.

---

- **`3.5.0-DOC4` — a concept page on shared libraries and ABI.** Sonames, API versus ABI, why a library's soname change means rebuilding everything that uses it, symbol versioning, and how C++ standard-library functions can leak through another library's exports, with libLLVM as the worked case: two builds of the same version can export different symbols. The drift guide and the PGO guide's toolchain section link it.

---

- **`3.5.0-DOC5` — a concept page on how an Arch package is built.** PKGBUILD fields and arrays, makepkg and `makepkg.conf`, where builds and finished packages land (`BUILDDIR`, `PKGDEST`), pkgbase and split packages, `.SRCINFO`, `-git` packages and `pkgver()`, checksums and signatures, and clean-chroot builds.

---

- **`3.5.0-DOC6` — a concept page on compiler optimization basics.** What `-O2` and `-O3` change, what `-march=native` gains and the portability it gives up, full versus Thin LTO, the linker options in a typical profile (`-fno-plt`, `--as-needed`, full RELRO, `--icf=all`), and what each hardening flag buys and costs. The README and the profiles section of the shipped `profiles.toml` link it.

---

- **`3.5.0-DOC7` — a concept page on bootstrapping an optimized compiler.** Why a compiler is built in stages, what the instrumented, training and optimized builds each produce, how BOLT fits on top, and why an installed toolchain must only ever be replaced as a whole. The PGO guide's toolchain section links it.

---

- **`3.5.0-DOC8` — a concept page on kernel builds and boot basics.** The kernel config and `olddefconfig`, config fragments, `localmodconfig` and what it can miss, built-in versus module, the initramfs, systemd-boot entries and menu order, GRUB, and why a fallback kernel matters. The kernel and boot entries guides link it.

---

- **`3.5.0-DOC9` — a concept page on measuring performance honestly.** Why timings vary from run to run, warm versus cold caches, repeated and interleaved runs with the median and spread, what a before/after comparison has to hold fixed, and `perf stat` and `perf record` basics. The PGO guide's "Did it help?" section links it.

---

- **`3.5.0-DOC10` — a glossary.** One page defining the terms the guides and concept pages share (pkgbase, split package, `PKGDEST`, soname, LTO, profdata, training workload, pass, sentinel, drift and more), each linked to the page that covers it.

---

- **`3.5.0-DOC11` — a shorter, current sysforge(1).** The man page opened with a table of the `doctor` flags removed in 3.0.0, describing a hint that sysforge stopped printing in 3.1.0; it is gone. DESCRIPTION is now a short overview instead of repeating options each command documents itself, and the FILES entry for `toolchain.toml`/`kernel.toml` no longer carries a long explanation of individual keys (`kconfig_targets`, `hardening`, `[bench]`), which the shipped files already document in their comments.

---

- **`3.5.0-DOC12` — sysforge(1) lists shared options once.** An option several commands accept with the same meaning, such as `--state-dir`, `--profile-conf` or `--no-pager`, was printed in full under every one of them (`--state-dir` thirteen times). They now appear once in a new SHARED OPTIONS section, with the commands that accept each, and every command names its shared options in one line. `--packages` and `--cleansrc-force` now have the same help text everywhere, so they are shared too. Options that take one of a fixed set of values now show those values (`--pgo record|use`) instead of an internal name such as `PGO_MODE`. The page is about 180 lines shorter.

---

- **`3.5.0-DOC13` — the man page documents the journal records.** Commands that change installed packages or build state write a record to the systemd journal, queryable by field, but no user document said so. A new JOURNAL section names the fields (`SYSFORGE_VERB`, `SYSFORGE_TARGET`, `SYSFORGE_EXIT`), which commands write them, and two `journalctl` queries; it is generated from the field list the records are built from.

---

- **`3.5.0-F2` — `run toolchain` builds the LLVM toolchain from upstream's single `llvm` package.** The repos now build clang, lld, polly, compiler-rt and openmp, along with lldb, libclc and others, as part of the `llvm` package. With PGO on, the whole set now goes through the profiling build together, which also means clang's own code (`libclang-cpp`) is optimized with the profile for the first time; before, only libLLVM was. Building them separately would have meant building the entire llvm source tree two extra times. When it installs, the toolchain stage now also replaces the other packages that come from the same build and that you already have installed (for example lldb and libclc), so they match the new libLLVM, and the rollback covers them too. Packages from that build you never installed, such as the WebAssembly runtimes, are built but not installed. Your `[packages]` lists in `toolchain.toml` keep working unchanged.

---

- **`3.5.0-F3` — `[bolt] enabled` can now run, and optimizes clang's library inside the toolchain build.** The BOLT option in `toolchain.toml` could never work: it tried to build the BOLT tools in a way the LLVM packages cannot support, then would have rewritten the 117 KB `clang` launcher instead of the library that holds clang's code, and it relied on a CPU feature (branch sampling) that Zen 3 processors lack. It now runs during `sysforge run toolchain`, right after the optimized LLVM is built and before anything is installed: it profiles `libclang-cpp` (and `libLLVM`, with `libllvm = true`) while compiling a sample of LLVM's own source, rewrites them, checks the result still compiles, and packages it. It uses the BOLT tools from the same build, so it needs the repos' single `llvm` package, and it no longer needs `perf`. Any failure warns and the toolchain installs without BOLT. Still experimental and off by default: it has not yet been through a full toolchain build.

## Removed

- **`3.5.0-F15` — one spelling per flag across every command.** **Breaking:** these spellings no longer parse; each has a direct replacement.

  | Old | New | Commands |
  |-----|-----|----------|
  | `--no-pkg-logs` | `--no-pkg-log` | `run pipeline`, `run packages`, `run kernel` (now the same as `build` and `update`) |
  | `--no-confirm` | `--yes` | `doctor pkg`, `state orphans` |
  | `--force` | `--yes` | `revert-to-stock` (`--force` elsewhere overrides a guard; skipping a question is `--yes`, as on `update`) |
  | `--quiet`, `-q` | `--findings-only` | `doctor system`, `doctor pkg` |
  | `--non-interactive` | `--no-interactive` | `run kernel` |
  | `--list` | `--dry-run` | `config merge` (they were aliases) |
  | `--auto-pgo` | `--yes` | `run toolchain` |
  | `--force-retry` | `--retry-failed` | `run pipeline`, `run packages` |
  | `--skip-boot-audit` | `--allow-boot-audit-failure` | `run kernel` |
  | `--skip-build-space-check` | `--allow-low-build-space` | `run toolchain` |
  | `--prune` | `--delete` | `state orphans` |
  | `--purge STORE` | `--delete STORE` | `state profiles` (now also takes `--yes`, so it can run without a terminal) |
  | `--purge-log` | `--truncate-log` | `run pipeline` |
  | `--compiler gcc\|llvm` | `--toolchain gcc\|llvm` | `run kernel` (the same name and values as `build --toolchain`, `3.5.0-F14`) |
  | `sysforge doctor -q` / `--json` | `sysforge doctor system --findings-only` / `--json` | bare `doctor` |
  | `sysforge packages --packages FILE` / `--orphans` | `sysforge packages list --packages FILE` / `--orphans` | bare `packages` |

  The last two rows fix a bug as well as a spelling. An option given before a subcommand was silently dropped once the subcommand was named: `sysforge packages --packages /x.toml add foo` wrote to the default `packages.toml`, and `sysforge doctor --json system` printed text. Options now live only on the subcommand, the way `state` and `artifact` already worked, so a misplaced one is an error instead of being ignored. A bare `sysforge doctor` still runs the full system check and a bare `sysforge packages` still lists.

  `doctor`'s old `--quiet` shared its name with the global `--quiet` (show errors only), which sysforge therefore could not accept after the word `doctor`, or after a package named `doctor`. With the rename, the global `--quiet` works after any command. The two `--skip-` flags never skipped their check: it still runs, and the flag lets the install go ahead despite it, the same as the `--allow-` flags beside them, so they now say so. `state orphans --yes` now says it applies with `--delete`: its old help said `--no-confirm` implied pruning, but it never did. Help text across `artifact`, `revert-to-stock`, `uninstall` and `search` is now written as sentences, like every other command's. **Migration:** scripts using any old spelling fail with "unrecognized arguments"; replace it as above.

---

- **`3.5.0-F16` — `build_mode = "patched_pkgbuild"` is gone.** **Breaking:** the old name for `build_mode = "source_built"` in `profiles.toml`, deprecated since 2.0.0 and warned about on every run since 3.0.0, is no longer read as `source_built`. A profile that still uses it now stops the run with an error naming the replacement, so the package is never quietly built without its PKGBUILD's own flags. **Migration:** change `build_mode = "patched_pkgbuild"` to `build_mode = "source_built"`.

---

- **`3.5.0-F17` — mesa's PGO is no longer a special case.** **Breaking:** since `build --pgo` learned to profile any package, mesa had kept a store and a recorded mode of its own: profiles in `/var/cache/sysforge/pgo-mesa` (shared by mesa and lib32-mesa), and `build_mode = "pgo_mesa"` in the build state. Mesa now works like every other package, with its profiles in `/var/cache/sysforge/pgo/mesa` (and lib32-mesa's in `pgo/lib32-mesa`) and its optimized build recorded as `pgo`. `state profiles` lists it as `pgo/mesa`, so delete it with `state profiles --delete pgo/mesa`; `pgo-mesa` is no longer accepted. Neither old name is read any more, so `sysforge doctor system --state` reports them until you migrate. **Migration:** run `sysforge state repair` once. It moves your collected mesa profiles to their new stores and rewrites `pgo_mesa` in the build state; `--dry-run` shows what it would do. If you are part-way through recording, the instrumented mesa you have installed still writes to the old store, so rebuild it with `--pgo=record` before you go on. For Proton games, change `PRESSURE_VESSEL_FILESYSTEMS_RW` to `/var/cache/sysforge/pgo/mesa`.

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

- **`3.5.0-B9` — Rust builds in the build sandbox show cargo's progress line again.** With `sandbox_builds = true`, the `Building [=====>    ] 120/431: <crate>` line that cargo draws above sysforge's progress bar never appeared; only the `Compiling` lines did. The sandbox's build tool always logs the build to a file, which sends cargo's output through a pipe, and cargo only draws its progress line when it is writing to a terminal. The sandbox now tells cargo to draw it anyway, at your terminal's width, whenever sysforge is passing the build's output to a terminal. Piped runs (`sysforge update | tee log.txt`) and `-vvv` runs are unchanged, and a profile that sets `CARGO_TERM_PROGRESS_WHEN` or `CARGO_TERM_PROGRESS_WIDTH` keeps its own setting. The width is taken when the build starts; if the window gets narrower, the line is cut to fit (`3.5.0-B121`). Builds outside the sandbox are not affected.

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

- **`3.5.0-B35` — help text names the commands it means instead of "pipeline".** sysforge's help still read as if setup were the main job. `sysforge --help` and the zsh completion now list `run` by what you run routinely: "Build the toolchain, kernel or packages, or run the fresh-install bootstrap". `run pipeline` says it is the fresh-install bootstrap. `run kernel --toolchain` (then `--compiler`) refers to "the compiler the last `run toolchain` set up" rather than "toolchain-stage pipeline state", and `update --include-stage-owned` to packages owned by `run kernel` or `run toolchain`. The man page's state-directory entry says "stage progress", and the top-level description matches the package description.

---

- **`3.5.0-B36` — run logs name the command that ran.** Every standalone run's log opened with `# sysforge pipeline`, even `sysforge-run-toolchain.log`, and logged its failure under `[PIPELINE]`. Each log now opens with its command (`# sysforge run toolchain: …`), and a standalone run logs its own lines under the stage's tag. The kernel and packages stages say they took the compiler "from the last `run toolchain`" instead of "from pipeline" or "pipeline state".

---

- **`3.5.0-B38` — a staged edit to a `-git` package's PKGBUILD is no longer wiped.** For a VCS package, sysforge ignores makepkg's own `pkgver=` rewrite when deciding whether a checkout holds your work, but it compared the file with git's index rather than the last commit. An edit you had `git add`ed was invisible to that check, so with makepkg's bump on top the checkout looked clean, and a diverged sync reset it away or `--cleansrc` deleted it. The check now compares with the last commit, so a staged edit counts as your work. A staged bump with no other change is still treated as clean; it was previously counted as your work.

---

- **`3.5.0-B39` — `run toolchain --install-only` no longer installs an unfinished PGO build.** The PGO toolchain builds llvm four times, and the first three passes leave instrumented or bootstrap packages in PKGDEST. Each pass recorded the same toolchain variant and version as the final one, so after a run that stopped part way, `--install-only` (the retry the stage itself suggests) could install an instrumented libLLVM and clang to `/usr` as the optimized build. The first three passes are now recorded as intermediate, and `--install-only` and `--bolt-only` refuse such a record and say to finish the run with `sysforge run toolchain`. The PGO guide says so.

---

- **`3.5.0-B40` — `run pipeline --resume` builds a package that was interrupted mid-build.** The packages stage took each package off its to-do list when its build started and only put it on the built or failed list when the build ended, so a Ctrl-C, a kill or a power loss mid-build left it on neither. `--resume` then never built it and reported success. On resume, a package on none of the lists is now built again, in its original place in the order.

---

- **`3.5.0-B41` — `run pipeline --dry-run` no longer changes what the next real run does.** A dry run saved the pipeline's state file before it checked for `--dry-run`. With `--start-from packages`, it marked the earlier stages as skipped, so a later real `--resume` never ran install, hardware or configure. A plain dry run left a state file that made the next real run stop with "state file already exists". A dry run now writes no state.

---

- **`3.5.0-B42` — `state repair` keeps what it knew about each package it fixes.** When `state repair` rebuilt an entry from its PKGBUILD, it kept only the build mode, the flags and the build time. It lost which stage owns the package, the stock name of a `-sysforge` build, the source, the toolchain stamps, the recorded build inputs, the sandbox flag and the build times. With the owner gone, `update` started rebuilding packages that `run toolchain` or `run kernel` own; without the stock name, `revert-to-stock` picked the wrong package to go back to. A `-sysforge` package the PKGBUILD does not list by that name was deleted outright. Repaired entries now keep every field except the version and package base, which come from the PKGBUILD, and a `-sysforge` package stays under its own name.

---

- **`3.5.0-B44` — the first Ctrl-C during an install stops sysforge at the next safe point.** While installing, sysforge is meant to finish the current step on the first Ctrl-C and then stop, but nothing ever acted on it. sysforge silently ignored the first Ctrl-C (the build or pacman running at the time still received it), and only a second Ctrl-C stopped it, wherever it happened to be, possibly in the middle of `pacman -U`. The first Ctrl-C now prints a notice and stops `build`, `update` and `run packages` before their next package, with the interrupted-run record kept so the next run offers recovery. Toolchain and kernel installs have no safe point part way, so they finish the install and its checks, then stop. A second Ctrl-C still stops at once.

---

- **`3.5.0-B45` — saving a compiler swap from the build-failure menu no longer breaks `profiles.toml`.** When you keep a compiler or linker swap from the recovery menu, sysforge adds a row for the package to `profiles.toml`, but it wrote the package name and values unquoted. A name with `+`, such as `libstdc++`, made the file invalid, so every later command failed to load its config. A name with `.`, such as `python-ruamel.yaml`, was read as a nested table, so the swap never applied, and a `"` typed into the manual compiler entry broke the file too. Names and values are now quoted and escaped. A row an earlier version wrote is replaced, not duplicated, the next time you save a swap for that package. If a row already broke your file, quote its package name by hand.

---

- **`3.5.0-B46` — other commands no longer erase the record of an interrupted install.** sysforge offered to recover an interrupted install only before `build`, `update`, `run` and `setup`. Other commands that change the system, such as `uninstall`, `revert-to-stock`, `state repair` and the artifact commands, replaced the record with their own and deleted it when they finished. After an interrupted toolchain install, `sysforge uninstall foo` erased the only sign that the toolchain was half-installed, and the next `update` went ahead without offering recovery. Those commands now offer the same recovery first, and refuse to start, leaving the record in place, if you decline or it fails.

---

- **`3.5.0-B47` — `packages.toml` and `pacman.conf` are replaced atomically.** `packages add`, `packages remove`, `packages add-group`, the configure and reconfigure desktop steps, reconfigure's build-mode step and `setup`'s `IgnoreGroup` write all rewrote the file in place, so a crash or a full disk part way through could leave it empty, and an empty `pacman.conf` breaks pacman for the whole system. On the default layout, where `/etc/sysforge` belongs to root, `packages add`, `remove` and `add-group` stopped with a `PermissionError` traceback, and reconfigure's build-mode step failed with a warning shown only at `-v`. They now write a temporary copy next to the file and rename it into place, keeping the file's owner and mode and asking for sudo when the file is root's. `packages add` replacing an existing entry now writes the file once instead of twice.

---

- **`3.5.0-B48` — the first `build` opt-in creates a complete `packages.toml`.** When `packages.toml` did not exist, answering yes to `build`'s offer to build a repo package from source wrote a file holding only that package's entry, without the header comment and the `[build]` section with `pkgbuild_src_dir`. It now starts the file with both, as `packages add` does.

---

- **`3.5.0-B49` — the toolchain build without PGO always builds.** With `pgo = false` and `repo_mode = "build_from_source"`, `run toolchain` ran makepkg without `--force`, so a package of the same version already in PKGDEST, left by an earlier PGO run or a build with different `[llvm] targets`, made makepkg skip the build and sysforge installed that leftover package as the new build. The single-pass build now passes `--force`, as the PGO passes do.

---

- **`3.5.0-B50` — the kernel's boot check finds the disk under an encrypted, LVM or RAID root.** Before installing a custom kernel, sysforge checks that its config keeps the driver for the disk your root filesystem is on. It read the storage stack from `lsblk`, which prefixes every parent row with tree characters (`└─nvme0n1`), so when root sat on LUKS, LVM or RAID the disk row was never recognised and no driver was required. A config without NVMe support could then pass the check on a machine whose encrypted root is on an NVMe drive. sysforge now reads `lsblk`'s plain output and finds the disk through any of these layers.

---

- **`3.5.0-B51` — `update` exits non-zero when its only package was refused a `--cleansrc` purge.** When `--cleansrc` refused to delete a checkout holding your changes and that was the only package to rebuild, `update` printed "Nothing to rebuild." and exited 0, so a script saw success. The run now lists the refusal and exits 1, as it already did when other packages were rebuilt alongside.

---

- **`3.5.0-B52` — removing every kernel config entry removes them from the build.** When the kernel stage had no entries to merge (no `[[kconfig]]`, no hardening set, no hardware or device entries), it left the previous run's `sysforge.config` fragment in place, and the kernel's `prepare()` still merged it. Options you had removed kept applying, and because the stage thought there was no fragment, the checks that read it, such as the guard against forcing module signatures with DKMS modules installed, were skipped. The old fragment is now deleted, as it already was with `kconfig_merge = false`.

---

- **`3.5.0-B55` — a declined sudo prompt stops the kernel and toolchain builds before they start.** The kernel stage, the PGO toolchain build and `run toolchain --bolt-only` asked for your sudo password up front but carried on when it was refused or timed out. A background thread then retried `sudo -v` every minute, which could put a password prompt under the progress bar and make the stage hang at the end until it was answered. These now stop at once, with nothing built, when sudo is not authorized, and the background refresh never asks for a password: if the credential expires, it warns instead.

---

- **`3.5.0-B56` — `update --devel` rebuilds a VCS package whose last build was never installed.** For `-git` and other VCS packages, `update --devel` skips the version check when the upstream commit matches the one recorded at the last build. That commit was recorded when the build finished, before the install, so a build whose install never happened (a sudo timeout, an abort, a crash) made the package look current while the old version stayed installed, indefinitely. The same shortcut also missed a `pkgrel` or `epoch` bump that came with no new upstream commit, such as a rebuild for a library change. The shortcut now applies only when the recorded build is the installed one and the PKGBUILD's `pkgrel` and `epoch` match it.

---

- **`3.5.0-B57` — an interrupted `revert-to-stock` or `state forget` cannot be undone by the next update.** Both commands removed the package's build record first and wrote the "do not re-adopt" record second. If the second write failed or was interrupted, the package was forgotten but not opted out, so the next `update` could take it back and rebuild it from source. The opt-out is now written first; if it fails, the package stays tracked and you can run the command again.

---

- **`3.5.0-B58` — the toolchain's post-install check uses the new libLLVM and runs a real compile.** After installing a toolchain, sysforge checks that installed programs linked against libLLVM still find what they need. It checked against the alphabetically first `libLLVM.so.*`, which, with a compat package such as `llvm21-libs` installed, is the old library, so a missing backend that should have triggered a rollback could pass. It now checks the newest one. The check also ran only `clang --version` and `ld.lld --version`, which return before most of the compiler is loaded; it now also compiles and links a small program with clang and lld, as the check before the build does.

---

- **`3.5.0-B59` — `run kernel` checks a reused kernel package against its own config, or refuses it.** When makepkg found the kernel package already built and nothing tied a config to it, the boot-safety check (Gate 2) audited the newest `.config` in any build tree, which could belong to another build, or with none only warned, so a package the check had once rejected could be installed later after its build tree was wiped. The boot entry's version and the post-install DKMS check guessed the release the same way. A reused package is now checked against the config its own headers package ships when its build tree and archived config are gone, and is refused when no config can be tied to it (rebuild it, or pass `--allow-boot-audit-failure`). The boot entry and DKMS check use the release of the package being installed.

---

- **`3.5.0-B60` — reusing an already-built renamed package installs the right files.** When makepkg found a package of the same version already built and skipped the build, sysforge looked for it under its stock name. For a build renamed to `<name>-sysforge` (a `--pgo=use` build, a renamed kernel), a leftover stock build of the same version was installed instead, or the package failed with "no matching .pkg.tar" although the right files were there. sysforge now uses the list of files the build itself recorded, and never falls back to a stock-named leftover for a renamed build.

---

- **`3.5.0-B61` — the "build succeeded but install failed" recovery offers only this build's packages.** After a failed build, sysforge could offer to install the packages it found in the PKGBUILD's directory with `pacman -U`, which included any older version left there (a downgrade) and unrelated split members, while with makepkg's `PKGDEST` set (the usual setup) it found nothing. It now offers only the packages of the version being built, from wherever makepkg puts them, and only when makepkg actually ran with `--install`.

---

- **`3.5.0-B62` — `revert-to-stock` on one member of a split package reverts that member.** `sysforge revert-to-stock vulkan-radeon` reinstalled `mesa`, the package's base, leaving the source-built `vulkan-radeon` installed and no longer tracked. A renamed split set with only one member installed was replaced by the base's stock package instead of that member's, and a member's stock name did not find its `-sysforge` build at all. Reverting a member now reinstalls that member; naming the base reverts every installed member; and `vulkan-radeon` finds `vulkan-radeon-sysforge`.

---

- **`3.5.0-B63` — packages owned by `run kernel` or `run toolchain` stop drifting after an `update --include-stage-owned` rebuild.** `update` rebuilt such a package as an ordinary one, recording a different build setup from the one the owning stage uses, so every later drift check reported it changed and `--rebuild-on-flag-drift` rebuilt it on every run. The rebuild now runs and is recorded as the owning stage's build.

---

- **`3.5.0-B64` — every package sysforge installs is recognised as its own.** `update` stops rebuilding a package you reinstalled from the repo with `pacman -S`, and tells that apart from sysforge's own installs by a record sysforge writes at each install. Only one install path wrote it, so a package installed by the kernel stage, the toolchain stage's PGO passes, the retry after a sudo timeout, or the bootstrap's install of sysforge itself looked like your `pacman -S`, and `update` could switch it back to the repo build. The check also only asked whether sysforge had installed a package at all since the last `update`, so `sysforge build foo` followed by `pacman -S foo` kept `foo` on the source build and rebuilt it over your choice; it now compares which came last. `revert-to-stock` and `uninstall` ran the same check without `update`'s rule that only a package some repo carries can have been reinstalled from one, and now share `update`'s check.

---

- **`3.5.0-B65` — `--cleansrc` no longer deletes a checkout it cannot download again.** `update --offline --cleansrc` deleted every clean checkout and then skipped the packages because it was offline, and under a source freeze `--cleansrc` deleted a checkout before the freeze refused to clone it again, leaving the package unbuildable either way. `--cleansrc` now keeps the checkout when it is offline or the freeze would refuse the new clone, and says why.

---

- **`3.5.0-B66` — turning the AUR off also covers checkouts labelled `git`.** With `[aur] enabled = false`, sysforge skipped AUR checkouts labelled `source = "aur"` but still fetched from aur.archlinux.org for ones labelled `source = "git"`, which sysforge treats the same way. It now decides from where the checkout's `origin` points, whatever the label, so no AUR checkout is fetched while the AUR is off.

---

- **`3.5.0-B68` — injected cmake options stay on the cmake command.** sysforge adds options such as `-DLLVM_TARGETS_TO_BUILD` to a PKGBUILD's cmake call by appending them after the call. When that line ended in a comment, the added option landed inside the comment, so the build ran without it or ran it as a separate command; in `cmake .. && ninja` it went to ninja. The option is now inserted before a trailing comment or a following command, and the check that runs after patching refuses a PKGBUILD where an added option is not part of the cmake call.

---

- **`3.5.0-B69` — the automatic reset of a diverged checkout keeps patches you applied.** When a package's upstream rewrote its history, sysforge resets a checkout that holds no work of yours, and it judged that by each extra commit's author. A fix you applied with `git am` or cherry-picked from an AUR comment keeps its original author, so it counted as upstream's and was reset away, recoverable only from git's reflog. A commit you made yourself now counts as your work whoever wrote it, and blocks the reset.

---

- **`3.5.0-B70` — every kind of stage failure is recorded and offers `--resume`.** The pipeline handled only some errors at a stage boundary. A failed command in the configure stage, or a failed file copy or removal, ended in a Python traceback with the stage still marked as running and no hint on how to continue. Any error in a stage now marks it failed, names the error and prints the `--resume` hint.

---

- **`3.5.0-B74` — `run toolchain --dry-run` takes no lock and leaves no install record.** A dry run wrote the interrupted-install record and took its lock as if it were about to install, so it refused to start while a real run was installing, and an interrupted dry run left a record that made the next run offer a recovery for an install that never began. A dry run now does neither.

---

- **`3.5.0-B75` — the toolchain's repo installs check sudo before recording an install.** Installing the stock LLVM packages (`pgo = false` with `repo_mode = "pacman"`) and installing a missing bootstrap `clang` or `lld` both recorded an interrupted install before asking for the sudo password. A declined or timed-out password then left a record whose recovery offered to replace the toolchain although nothing had been installed. Both now stop before recording anything when sudo is not authorized, as the from-source install already did.

---

- **`3.5.0-B76` — a failed toolchain check can roll back when the build adds a package.** Before installing a toolchain, sysforge notes the cached package files of what is installed, so it can put the old toolchain back if the post-install check fails. A package the build added for the first time, such as a new subpackage or `lib32` member, had no cached file because it was never installed, and that made sysforge refuse the whole rollback and leave the install record for manual recovery. It now notes only packages that were installed before the run.

---

- **`3.5.0-B78` — a renamed `-git` package keeps updating.** A `-git` package built with an optimization that renames it to `<name>-git-sysforge` was no longer recognised as a VCS package, so `update --devel` compared the PKGBUILD's placeholder version with the installed one, reported a downgrade and never rebuilt it. The `-sysforge` suffix is now ignored when deciding whether a package is VCS.

---

- **`3.5.0-B79` — a new kernel profiling round discards the previous round's profile.** `run kernel --autofdo=record` builds a new profiling kernel, but kept the profile captured from the previous one: in round 2 a `--autofdo=use --propeller` without a fresh capture then applied a Propeller profile recorded on another binary, and in round 1 an old `kernel.afdo` hid the warning that the profile came from a different kernel version. `record` now deletes the stale profile first, so `use` refuses until you capture again.

---

- **`3.5.0-B80` — numeric `[[kconfig]]` values apply.** A `kernel.toml` entry such as `CONFIG_NR_CPUS` with value `64` was written to the config fragment as `CONFIG_NR_CPUS="64"`, which the kernel's config tool rejects for number options, so the entry was silently dropped and only a drift warning mentioned it. Decimal and `0x` hexadecimal values are now written without quotes; other strings stay quoted.

---

- **`3.5.0-B81` — state files survive concurrent writers and power loss.** `build_state.toml` and sysforge's other state files (pipeline state, the source-sync cache, the install sentinel, the opt-out list, the artifact registry and ignore list, the toolchain build cache, the kernel FDO sidecars, the kernel module map and the hardware profile) were written to a fixed `<file>.tmp` and renamed into place without being flushed to disk. Two sysforge processes saving at once could publish each other's half-written file, and a power loss just after a save could leave an empty file; an empty `build_state.toml` makes `update` treat every package as a plain repo package, losing what sysforge built. Each write now uses its own temporary file, flushes it to disk before renaming it into place, and keeps the file's permissions.

---

- **`3.5.0-B82` — `sysforge fetch` names packages by their PKGBUILD and reports refusals.** `fetch` identified each package by its checkout's directory name, so a checkout in a renamed directory did not match `--thaw <pkg>` and was not recognised as a VCS package. It also exited 0 when the source freeze refused a fetch or `--cleansrc` refused to delete a checkout. It now uses the PKGBUILD's package base and exits 1 when a fetch is refused or fails.

---

- **`3.5.0-B83` — sysforge waits when the AUR asks it to slow down.** When the AUR's lookup service answered "too many requests", sysforge treated it as an ordinary failure and went straight on to fetch every package's checkout from the same server. A `Retry-After` from the lookup now pauses the fetches that follow too, as one from a fetch already did.

---

- **`3.5.0-B84` — `update --devel`'s shortcut works with makepkg's `BUILDDIR` set.** After a VCS build, sysforge records the upstream commit it built so the next `update --devel` can skip a full version check when nothing changed upstream. It looked for the source tree in the PKGBUILD's directory, but with `BUILDDIR` set (as in many setups) makepkg builds under `$BUILDDIR/<pkgbase>`, so no commit was ever recorded and every `--devel` run did the full check. It now looks where makepkg built.

---

- **`3.5.0-B85` — two packages from one split AUR package build once.** When a build needed two members of the same AUR package (for example `foo-git` and `foo-libs-git`), sysforge built the package for the first, then tried again for the second, found it already built and installed it a second time. Dependencies that share a PKGBUILD are now built and installed once.

---

- **`3.5.0-B86` — the missing PGP key repair offers the right key.** When a source signature could not be checked because its key was missing, sysforge offered to fetch a key, but took the first long hex string after the error message, which could be a commit or checksum from a later line. It now reads the key ID from makepkg's `FAILED (unknown public key …)` line, or from gpg's `using … key …` line, which name the key that signed the file. As before, nothing is imported without your consent.

---

- **`3.5.0-B87` — run logs keep lines written by background threads.** The sudo refresh and the PGO profile merge log from their own threads. A line written while the log was being closed could be lost, or land between the clearing of a successful run's log and its "log cleared" marker, and two lines could race the date marker added at midnight. Writes to the log files are now made one at a time.

---

- **`3.5.0-B89` — two flag clean-ups also apply to flags from your system `makepkg.conf`.** A PGO build removes `-flto` from the compiler flags, and a build linked with something other than lld removes lld-only options such as `--icf=`. Both looked only at flags your profile set, so an `-flto` or `--icf=` coming from `/etc/makepkg.conf` alone reached the build. Both now act on the flags the build actually gets, wherever they come from.

---

- **`3.5.0-B90` — a replacement package held by an exact-version sibling installs during a batch.** When one package in a batch replaces an installed one and another member pins it to an exact version, sysforge checks in advance whether the install may skip version checks for that pin. The check ran without the answer the real install gives to the replacement question, so it stopped at that question, found no pin, and the install then failed on it. The check now answers as the install does.

---

- **`3.5.0-B91` — the progress bar's build percentage restarts with each build.** The bar shows a ninja build's progress as a percentage and forgets the old totals when the step or item changes. A second build within one step, such as the repackaging in `run toolchain --bolt-only`, was measured against the larger total of the build before it and showed far too low a percentage. The totals now reset whenever a build starts.

---

- **`3.5.0-B92` — a declined artifact on an unmounted drive stays declined.** `sysforge artifact review` remembers the files you chose to ignore, and forgets one once the file is gone, so a recreated file is offered again. A file on a drive that was simply not mounted looked gone too, so its decline was dropped and the file was offered again once the drive returned. A missing file now keeps its decline unless its directory is there with other files in it.

---

- **`3.5.0-B93` — a failed revert of a renamed kernel leaves a recovery for the next run.** Reverting an AutoFDO or Propeller kernel removes the renamed package and then installs the stock one. If that install failed, sysforge printed the command to recover with but finished as if nothing were wrong, so the next run did not offer to recover, with the kernel package removed. It now stops with an error and keeps the interrupted-run record, so the next run offers to reinstall the stock package.

---

- **`3.5.0-B94` — unused internal helpers removed.** Twelve functions that nothing in sysforge called, and an unused shallow-clone option of the AUR clone that contradicted its full-history rule, were removed. Nothing you run changes. The AUR lookup's rate-limit helper, listed among them, is now used instead (`3.5.0-B83`).

---

- **`3.5.0-B95` — type-checking clean-ups.** Context managers carry the current return annotation, and checks the types show can never fire were removed, except two whose values can differ from their declared type at run time, which are kept and documented. Nothing you run changes.

---

- **`3.5.0-B97` — `update --dry-run` and the update reports no longer change the build record.** `--dry-run`, `--explain-drift` and `--versions` saved `build_state.toml`: they added a record for every newly installed package and moved packages you had reinstalled from the repos back to repo tracking, and they cleared the record of those reinstalls that the next real run needs. They now work out the same changes for their output without saving anything.

---

- **`3.5.0-B98` — the toolchain only promises a rebuild it will run.** After installing a toolchain, a C++ library mismatch in a program that uses libLLVM was reported as "healable by the queued consumer rebuild" even with `rebuild_soname_consumers = off` or the rebuild declined, when nothing was queued. The message now says no rebuild is queued and to rebuild the program with `sysforge build <pkg>`.

---

- **`3.5.0-B99` — `--cache-report` no longer shows `0/0 hits` for sandboxed builds.** A build in the build sandbox never uses your ccache or sccache, but the report measured the host's caches around it anyway and printed `ccache 0/0 hits (n/a) cache=5.0 GiB` for every package, which read as a cache that went unused. A sandboxed build is now listed as `n/a (sandboxed, host cache not used)` and left out of the totals. The `--cache-report` help says so.

---

- **`3.5.0-B101` — `setup`'s preset shows the profile you have now.** The `key: old → new` lines read only your own `profiles.toml`. With `extends_system = true`, a key your file leaves unset showed the shipped default instead of the system file's value, and a config with no `[defaults] profile` anywhere showed `"standard"` when builds actually use `bare`. The lines now show the merged value that builds use.

---

- **`3.5.0-B102` — the `secure` preset's paste-in note names the file it checked.** When the profiles your builds read have no `[profiles.hardened]`, `setup --preset secure` printed a block to paste and said your own `profiles.toml` was hiding the system one, also when there was no such file and the system `profiles.toml` was simply older than the profile (an upgrade saved as `.pacnew`). The note now says that the file your builds read does not define the profile, and gives its path.

---

- **`3.5.0-B103` — the `low-friction` preset no longer turns off the build sandbox.** `setup --preset low-friction` set `[security] sandbox_builds = false`, although the sandbox asks no questions and the preset's description, the README and its release note mentioned only plan confirmation and source review. The preset now leaves the sandbox as it is. If you applied it and want the sandbox back, run `sysforge setup` and answer yes to building in a clean chroot.

---

- **`3.5.0-B104` — `setup`'s preset step no longer crashes on an unexpected value.** A section written as a plain value (`build = "x"`) or a key holding a number or a list stopped `sysforge setup` with a traceback. Such a section now counts as unset, and an unusual value is shown as it is.

---

- **`3.5.0-B105` — sysforge never writes a config file it cannot read back.** `setup` and the toolchain stage change single keys in your config files line by line, and a top-level key could be placed inside a multi-line array or string, leaving a file that no longer parsed. The new text is now checked before it is written; if it would not parse, the file is left alone and `setup` prints the line to add by hand. None of the shipped files trigger this.

---

- **`3.5.0-B106` — `setup --preset` works without `/etc/pacman.conf` and reports a broken config.** Without a `pacman.conf`, `setup` printed "nothing to configure" and stopped, silently ignoring an explicit `--preset`. It now skips only the `IgnoreGroup` step and carries on with the hooks, the preset and the preferences. A config that fails to load, such as a missing `profiles.toml`, now prints one line naming the problem instead of a traceback.

---

- **`3.5.0-B107` — the `hardened` profile warns about old C code.** The comment above `[profiles.hardened]` in the shipped `profiles.toml` now explains that `-fstrict-flex-arrays=3`, together with the `-D_FORTIFY_SOURCE=3` sysforge keeps from your system `makepkg.conf`, makes the fortified string functions treat an old-style `[0]` or `[1]` trailing array as its declared size, so a program that writes past one aborts at run time, and that a `[[rules]]` entry choosing another profile opts such a package out.

---

- **`3.5.0-B108` — split AUR packages whose base is not a package name update again.** sysforge looks up AUR versions by pkgbase, but the AUR's lookup matches package names only, so a split package whose pkgbase is none of its package names (`lib32-libdbusmenu`, which builds `lib32-libdbusmenu-glib` and `-gtk3`) was never found. When its PKGBUILD's version could not be read without running it, `update` skipped the package on every run ("unresolved shell expansion and no cached RPC version"), and its checkout was fetched every run even when nothing had changed. The lookup now also asks for each pkgbase's package names and files the answer under the pkgbase, and the version is usable in the same run.

---

- **`3.5.0-B109` — `lib32-*` packages build in the sandbox.** Arch builds its 32-bit packages in a build root that already holds the 32-bit compiler libraries (`multilib-devel`), so their PKGBUILDs never list them. sysforge's build sandbox installed only what the PKGBUILD lists, so a 32-bit build failed at link time with `libgcc_s.so.1 is incompatible with elf32-i386`. sysforge now adds `multilib-devel` to the sandbox for a `lib32-*` package, and only for one. If `[multilib]` is not enabled in `/etc/pacman.conf`, the build is refused with a message that says so.

---

- **`3.5.0-B110` — `[safety] llvm_preflight = false` turns the LLVM pre-flight table off.** The setting was documented in `sysforge.toml` but nothing read it, so only `--no-llvm-preflight` hid the table. `fetch`, `update` and `build` now honour it; the flag still works and wins over the setting.

---

- **`3.5.0-B111` — the shipped `optimized` profile keeps LTO under the default gcc toolchain.** The profile hardcoded `-fuse-ld=lld` while the shipped default toolchain is gcc, and gcc cannot link its LTO objects with lld, so sysforge turned LTO off for every package built with `optimized`. The profile no longer names a linker: the `llvm` toolchain still adds lld itself, and lld's `--icf=all` is still dropped when another linker is in use. An edited `/etc/sysforge/profiles.toml` is kept as it is (pacman writes the new one beside it as `.pacnew`), so remove `-fuse-ld=lld` from its `[profiles.optimized] LDFLAGS` by hand.

---

- **`3.5.0-B112` — the shipped config files no longer give stale advice.** The shipped profiles listed `--syncdeps`, `--install` and `--rmdeps` among their `makepkg_flags`, and `profiles.toml` recommended them, although sysforge installs dependencies and built packages itself and removes the first two from every build; they are gone, and the comment says why. `profiles.toml` called `[defaults] profile` required, but the `bare` profile is used when it is unset. Its example `CPPFLAGS = "-D_FORTIFY_SOURCE=2"` would define a macro Arch already sets in `CFLAGS`, and is replaced. `kernel.toml` and `profiles.toml` said `sysforge run kernel` needs a `profile = "kernel"` rule; it needs neither the rule nor the profile, which stays as an example for a kernel built with `sysforge build`. `sysforge.toml` and `bootstrap.toml` named a `converge` command that no longer exists and said `sysforge reconfigure` for what is now `sysforge run reconfigure`. A file installed with an earlier version keeps its text until you adopt the new defaults (`sysforge config merge`).

---

- **`3.5.0-B113` — a compiler swap kept from the build-failure menu is saved on the default layout.** Choosing to keep a working compiler swap writes it to `profiles.toml`, which on the default layout is root's `/etc/sysforge/profiles.toml`. sysforge tried to write a temporary file beside it as the build user, failed, and reported this only at `-v`, so the swap was never saved and the next build failed the same way. The write now asks for sudo when it needs to, keeps a symlinked `profiles.toml` a symlink, and keeps the file's mode and owner. If the save still fails, it is reported as an error.

---

- **`3.5.0-B114` — a failed toolchain install that cannot be rolled back says why.** When the new toolchain failed its post-install check and none of its packages had been installed before the run, as on a first toolchain install, there was no earlier version to put back, but the error said only that the automatic rollback "could not complete". sysforge now says that nothing was installed before, so there is no prior version to reinstall. The rest is unchanged: the recovery command is still printed, and the next run still offers it.

---

- **`3.5.0-B115` — the package installs its license.** sysforge is MIT-licensed, and MIT is not one of the licenses Arch provides for every package to share, so Arch packages under it install their own copy of the license text. sysforge's packages did not, which Arch's package checker reports. Both the `sysforge` and `sysforge-git` packages now install it as `/usr/share/licenses/sysforge/LICENSE` (`sysforge-git/LICENSE` for the VCS package). `make dev-install` does not link it, since nothing reads it at run time.

---

- **`3.5.0-B116` — the Python package no longer lists a dependency that can never apply.** sysforge's package metadata named `tomli`, a TOML reader for Python versions before 3.11, although sysforge requires Python 3.11 or newer, whose standard library reads TOML itself. The entry could never be installed and only made sysforge look as if it had a dependency. sysforge now declares none; `pyalpm` remains an optional extra.

---

- **`3.5.0-B117` — the raw pre-build snapshot is taken.** With `[build] pre_build_snapshot = true` and no snapper config covering `/`, sysforge takes a read-only btrfs snapshot under `/.snapshots`. It ran both steps as the build user, and both need root, so the snapshot always failed with a warning. They now run through sudo, asking for the password first when needed; a declined prompt is a warning and the build goes ahead. The snapper path is unchanged: it runs as you, so the snapper config's `ALLOW_USERS` must include you.

---

- **`3.5.0-B118` — `clean_builddir = true` no longer deletes the package's checkout.** The profile setting, which the shipped `batch` profile sets, is documented as clearing `src/` before a build. It deleted everything in the PKGBUILD's directory except the PKGBUILD: the checkout's `.git`, its `.SRCINFO`, and any patches or other files the PKGBUILD lists as sources, so the build failed and an AUR checkout could no longer sync. It now asks makepkg to clean its own source directory (`--cleanbuild`), wherever makepkg puts it, and leaves the checkout alone. A checkout already damaged this way needs a fresh clone (`sysforge fetch <package> --cleansrc`).

---

- **`3.5.0-B119` — `run reconfigure` no longer refreshes keys from a keyserver during a source freeze.** The GPG step's "Refresh all keys from keyserver?" ran `gpg --refresh-keys` without consulting the source freeze, so a frozen system still contacted the keyserver. Under a freeze the step now skips the offer and says why; a whole-keyring refresh names no package, so `--thaw` does not lift it. The sudo step's warning for a user outside `wheel` also no longer says `makepkg -si` will fail, since makepkg has not installed anything itself since 3.4.0; it names sysforge's own install step.

---

- **`3.5.0-B120` — a removed package's build failure is cleared.** A failed build stayed in `sysforge state failed`, `sysforge doctor` and the state summary until the package next built, so once the package was removed, whether by `sysforge uninstall`, a plain `pacman -R` or anything else, it was listed as failing indefinitely. sysforge now clears the failure when the package is removed and no other package built from the same PKGBUILD is still installed: `uninstall` clears it at once, a removal by pacman is cleared by the next `update`, and `state failed` and `doctor` already leave it out before then. `state forget` also clears the failure of a package it stops tracking. A package whose first build failed, and so was never installed, keeps its failure, since nothing removed it.

---

- **`3.5.0-B121` — Narrowing the window during a sandboxed Rust build no longer leaves cargo progress lines behind.** In the build sandbox cargo draws its progress line at the width your terminal had when the build started, because it cannot measure the terminal from inside. When the window got narrower, for example when a tiled window opened beside it, each frame wrapped onto two rows. Cargo redraws only the row it is on, so the first half of every frame stayed on screen with the next `Compiling` line written over it. sysforge now cuts each frame to the window's current width as it passes it on. Builds outside the sandbox were not affected, since cargo measures the terminal there itself.

---

- **`3.5.0-B122` — wrong or outdated help text.** `build --pgo` said it applied only to mesa and did nothing for other packages; it works on any package and warns for one outside mesa and `sysforge.toml` `[pgo] allow`. `doctor pkg --apply` referred to "v1.x". `config merge` said its companion files come from `make sync-config`, a developer command; it now names pacman's `.pacnew`/`.pacsave`. `doctor pkg --rust` read like a changelog entry. In sysforge(1), `log` said per-package logs live under the state directory; they live beside each PKGBUILD under `[paths] pkgbuild_src_dir`.

---

- **`3.5.0-B123` — shell completion gaps.** bash completed nothing for `sysforge log` (its flags or a package name); zsh completed nothing after `sysforge help bench`; and both offered only `pkgbuild`/`running` for `run kernel --base-config`, which also takes a path to a `.config` file, so they now offer files too.

---

- **`3.5.0-B124` — Tab completion is several times faster.** Every Tab press started a full `sysforge` that loaded every command before printing a single name, and completing a package name also ran `pacman -Ssq` and piped about 140,000 names through `grep`, about half a second per press. Completion lookups now skip the rest of the CLI, the merged repo and AUR name list is cached and rebuilt only after a sync database, the AUR name list or your PKGBUILD directory changes, and sysforge filters the list to what you have typed before printing it. A package-name lookup now takes about 0.1 s and the others well under that. Completing a package from the build state also no longer tries to create the state directory, which could ask for your password. For contributors, `make bench-completions` prints the time of each lookup.

---

- **`3.5.0-B125` — A package built with the other compiler was recorded as built by the active one.** Every source build recorded the toolchain stage's compiler, so a package kept on gcc on an LLVM system (through a `[package_compiler_overrides]` row, a profile, the recovery menu or the new `build --toolchain`) was recorded as `pgo_llvm`. The record now names the compiler that built it. `update` does not report it as toolchain drift while your config still picks that compiler for it, so it is not rebuilt with clang. A one-off `build --toolchain` build is still reported, since the next normal build undoes it. The [drift guide](../guides/drift.md) has a new section on building one package with the other toolchain.

---

- **`3.5.0-B126` — A kernel built with the other compiler is recorded as such, and stops warning about it.** `run kernel` recorded the system toolchain's variant whatever compiler built the kernel, so a kernel kept on gcc (`compiler = "gcc"` in `kernel.toml`, or `--toolchain gcc`) on an LLVM system was recorded as `pgo_llvm`. The record now names the compiler that built it, as `3.5.0-B125` does for other packages. The kernel stage's "Rebuilding will switch toolchains" warning now compares the record with the compiler this run will use rather than the system one, so it appears only when the kernel's compiler actually changes, and it names that compiler.

---

- **`3.5.0-B127` — bash completes the values of `run toolchain --rebuild-soname-consumers`.** zsh offered `prompt`, `auto` and `off` after the flag; bash offered nothing. It now offers the same three.

---

- **`3.5.0-B128` — the man page lists every environment variable sysforge reads.** Its ENVIRONMENT section named three of eleven. Now it also covers `SYSFORGE_MERGE` (the merge tool for `config merge`), `SYSFORGE_ASCII` (ASCII in place of decorative glyphs), `SYSFORGE_PGO_STORE` and `SYSFORGE_PROFILE_STORE` (where profiles are kept, the second documented nowhere before), and three diagnostic switches for bug reports. The `SYSFORGE_EDITOR` entry now gives the whole lookup order, which includes `sysforge.toml [ui] editor`, and the pager entry the right fallback flags. The section is generated from one list in the code, and a test fails on a variable missing from it.

---

- **`3.5.0-B129` — `run kernel --keep-hotplug-drivers` takes effect.** The flag and its `--no-` form were accepted and then dropped before the kernel stage read them, so the stage always used `kernel.toml`'s `keep_hotplug_drivers`. They now override it, as their help says. A new test checks that every flag of every `run` stage reaches the stage.

---

- **`3.5.0-B130` — cargo's progress line no longer leaves its end behind in the build sandbox.** In a sandboxed Rust build, a `Compiling` line written after cargo's `Building [=====>    ] 120/431: <crate>` line covered the bar but not what followed it, so text like `] 120/431: serde, syn` stayed at the end of the row. cargo clears its progress line before printing anything else only when it colours its output, and in the sandbox it writes to a pipe and doesn't colour it, so it just writes the next line over the old one. sysforge now clears the row itself before the next line. Builds outside the sandbox were not affected.

---

- **`3.5.0-B131` — `cpu_quota` and `mem_limit` now cap sandboxed builds.** With `sandbox_builds` on, the build runs in a container that systemd places under its own `devtools-<user>.slice`, outside the `systemd-run` scope sysforge opened to apply the limits, so neither limit reached the build and nothing said so. sysforge now sets them on that slice for as long as the build runs (`sudo systemctl set-property --runtime`) and removes them when it ends, including when it fails. If a run is killed before it can remove them, the next sandboxed build does, and a reboot always does. Limits you set on that slice yourself in `/etc` are left alone. Builds outside the sandbox were not affected.

---

- **`3.5.0-B132` — the shell prompt no longer lands on top of `update`'s output.** At the end of every `sysforge update` at a terminal, sysforge sends a reset in case a program it ran left the terminal on its alternate screen. In xterm and terminals built on xterm.js (VS Code's terminal among them), that reset also moves the cursor back to the last position the progress bar saved, even when nothing used the alternate screen, so the prompt and anything typed next were drawn over earlier lines of the run. sysforge now saves the current position just before the reset, so the cursor stays put, and a real return from the alternate screen still restores the cursor that program left.

## Security

- **`3.5.0-B37` — the root pacman hook no longer follows links in the state directory.** The hook helper runs as root in every pacman transaction and wrote into `/var/lib/sysforge/sentinels`, whose parent any `sysforge` group member can write to. A member could swap `sentinels` for a symlink to another directory, which the next transaction made group-writable, or symlink one of its files so the hook appended to any file as root. The helper now enters the directory and acts only if it is the real `sentinels`, owned by root. It appends only to a regular file, opened so that a symlink is refused and a planted FIFO cannot stall the transaction. As before, it never fails a transaction.

---

- **`3.5.0-B43` — the interrupted-install recovery no longer runs a command read from a shared file.** When an install-bearing run is interrupted, sysforge leaves a record in its state directory, and the next run offers to run the recovery command stored in it, often under `sudo`. Any member of the `sysforge` group can write that directory, so one member could plant a record and choose the command another member's next `update` or `build` offered to run. The record now holds only the kind of recovery and its package names or cached package files, and sysforge builds the command itself from a fixed form for each kind: reinstall packages, reinstall cached packages, or regenerate the initramfs. A package name must be a valid pacman name, and a cached package must be a root-owned file in pacman's package cache. A record owned by anyone but you or root, or one that is a symlink, is refused without a prompt. A record left by an older sysforge still shows its command, but only offers to clear the record; run the command yourself if it is right.

---

- **`3.5.0-B53` — a PKGBUILD's dependency names can no longer pass options to pacman.** sysforge installs a package's missing dependencies with `sudo pacman -S`, and checks them with `pacman -T` and `pacman -Si`, passing the names from the PKGBUILD as they were. A dependency written to start with `-` in a malicious PKGBUILD was read by the root pacman as an option. Names that do not follow the package-name rules are now ignored with a warning, and every pacman command marks where its options end before the package names.

---

- **`3.5.0-B54` — another local user can no longer run code in a PGO toolchain build.** The PGO build stages its intermediate compilers in fixed directories under `/var/tmp` (`sysforge-llvm-stage1`, `-stage2`, `-stage3`) and later runs what is in them. They were created without checking who owned an existing one, so another user on the machine could create one first, or put a symlink there, and have their own files run in your build. The directories are now created readable and writable by you only, and a build refuses to use one that is a symlink, belongs to someone else or that others can write to, naming the directory to inspect and remove.

---

- **`3.5.0-B67` — the source freeze covers the build-failure repair that downloads subprojects.** When a build failed because meson subprojects or git submodules were missing, sysforge downloaded them with `meson subprojects download` or `git submodule update` and retried, even with the source freeze on, so a frozen run could pull new code unattended. The freeze now refuses that download, and `--thaw <pkg>` lifts it for one package as for any other. The repair also looked for the source tree in the PKGBUILD's directory, so with makepkg's `BUILDDIR` set it never found the project; it now looks where makepkg built.

---

- **`3.5.0-B71` — the bootstrap install no longer opens root SSH login with a password.** Every install from `bootstrap.toml` set `PermitRootLogin yes` and `PasswordAuthentication yes` and enabled `sshd`, settings meant for the test VMs, so a real machine installed without passwords in `bootstrap.toml` was reachable over SSH as root. Root password login is now set only with `[system] ssh_root_login = true`, which also requires a `root_password`; otherwise sshd keeps its default, which refuses root password logins. The shipped `bootstrap.toml` documents the key.

---

- **`3.5.0-B72` — `bootstrap.toml`'s `username` and `target` are checked.** The configure stage builds paths it deletes, and a shell command it runs as the new user, from these two values without checking them, so a user name with `/`, `..` or shell characters pointed the deletion elsewhere or ran extra commands, and an empty or relative `target` made the deletions relative to the current directory. `username` must now use lowercase letters, digits, `_` and `-`, starting with a letter or `_`; `target` must be an absolute path other than `/`, and configure refuses to run until it is mounted.

---

- **`3.5.0-B77` — sysforge no longer uses sudo on a symlink planted in its shared state directory.** When a sysforge directory was not writable, sysforge created and fixed it with `sudo install -d`, `chgrp` and `chmod`, all of which follow symbolic links. Any member of the `sysforge` group could replace a directory in the shared state tree with a link, and another user's next run then changed the group and permissions of the link's target as root. sysforge now refuses before using sudo when the directory, or a parent of it inside a directory others can write to, is a symbolic link.

---

- **`3.5.0-B88` — `bootstrap.toml` is never readable by others in the new system.** The configure stage copied the configuration directory into the new system with each file's original permissions and made `bootstrap.toml`, which holds the install passwords in plain text, readable only by root afterwards, so for a moment it was readable by any user. It is now created readable only by root from the start, and the live ISO's copy is restricted too.

---

- **`3.5.0-B96` — the install stage refuses to wipe a disk that is in use.** The install stage asked for confirmation before partitioning `[partition] device`, but did not check what the device was, so a typo relied on that one prompt to avoid wiping the disk the live system runs from. It now refuses, before asking, a path that is not a block device or a disk with anything mounted from it, such as `/`, the ISO's boot medium or swap.

---

- **`3.5.0-B100` — C++ builds keep Arch's libstdc++ assertions.** Arch's `makepkg.conf` adds `-Wp,-D_GLIBCXX_ASSERTIONS` to `CXXFLAGS`, which turns on libstdc++'s bounds and precondition checks. Every shipped profile replaces `CXXFLAGS` with `$CFLAGS`, and the flag was not among those sysforge restores, so C++ packages built with the `standard` or `optimized` profile lost those checks. The flag is now restored for C++ only (in the shipped `profiles.toml`'s `[preserved_system_tokens]`), also when `CXXFLAGS` is just `$CFLAGS`. Because this changes the flags C++ packages build with, your next `update` reports flag drift for packages built before this release; `--rebuild-on-flag-drift` rebuilds them. A live `profiles.toml` gains the flag with `make sync-config` or by adding it to `[preserved_system_tokens] CXXFLAGS`.
