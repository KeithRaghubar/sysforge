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

- **`2.6.1-F27` — the install stage now ends with a summary of what it installed on the new system.** The bootstrap install stage, which hands the disk to `archinstall --silent`, was the one package-changing stage with no change summary. It now reports a manifest of every package installed on the target, with versions and total size, all as additions since the disk was freshly partitioned. It reads the target's package database from the mounted install root, `/mnt`, which sysforge now passes to archinstall explicitly as `--mountpoint` so the two always agree. If the target can't be read, the summary says it is unavailable and why, rather than reporting no changes.

---

- **`3.0.0-F1` — `run kernel` refuses up front to build a `CONFIG_RUST=y` kernel this machine cannot build.** Rust support in the kernel depends on host tools rather than on the kernel source: kbuild requires `rustc` and `bindgen` at or above the versions the kernel tree pins, plus the Rust standard-library source (`rust-src`). When any of those is missing, kconfig quietly drops `CONFIG_RUST` while preparing the config, and the loss was only reported after a full build. When your kconfig entries request `CONFIG_RUST=y`, `run kernel` now checks those tools before building and refuses with a fix command if they fall short. The message names which `rustc` the build would actually use (the distro package, rustup and its active toolchain, or a user-local rustup) and any other `rustc` it shadows on `PATH`, since a rustup install shadowing the distro `rust` is the usual surprise. The minimum versions come from the kernel tree's own `scripts/min-tool-version.sh`, recorded from the most recently built tree. On a first run, before any tree has been built, only presence and `rust-src` are checked. The check is identical on the gcc and llvm kernel paths, and nothing changes unless you request `CONFIG_RUST`.

---

- **`3.1.0-F10` — a sandboxed build now links against the versions this host runs.** With `[security] sandbox_builds` on, the build container resolved every dependency sysforge hadn't built in the same run from the stock repos, even when the host ran its own build. On 2026-09-11 that produced a mesa linked against the repo's `llvm-libs 22.1.8-2` while the host ran an optimized build with the same version number, and the desktop did not come up after reboot. sysforge now publishes the packages you built from source, at the versions you have installed, into a local pacman repo, `[sysforge-local]`, under the state directory. The chroot lists it ahead of the stock repos (which is what wins a same-version tie) and mounts it read-only, so a build can read it but never change it, and only packages already installed on the host are ever published. Your own compiler build is also injected into the container, so the sandboxed build compiles with it. The repo holds one file per installed package and is reconciled once per run, with nothing to prune. It adds a `[sysforge-local]` block to the chroot's `pacman.conf`; other tools that share the chroot see the repo too, read-only, so keep sysforge's state directory in place while it's there. It is on by default whenever the sandbox is on; set `sandbox_local_repo = false` under `[security]` to turn it off, which removes the block on the next sandboxed build. If the repo can't be updated, the sandboxed build stops rather than building against the stock repos.

---

- **`3.3.0-F1` — the `building` bar's ETA now uses each package's own recorded build time, so it no longer swings when a large package finishes.** The live ETA projected the average duration of the packages finished so far onto the ones still to come. That works when items are similar in size, but in a build batch a 20-minute mesa sits next to 30-second libraries, so every large completion jumped the average and the countdown snapped to a new value. The `building` bar now gives each package its own median from its last recorded builds: remaining time is what each unfinished package usually takes, minus what the current one has already used. A package that has never been built falls back to the in-run average. With history, the ETA appears from the first package instead of after two. Recorded build times now also exclude time spent waiting at a prompt (the recovery menu, a sudo password), so a long wait no longer skews later estimates. Other bars are unchanged.

---

- **`3.3.0-F3` — a sudo password prompt sysforge raises itself now pauses and hides the progress bar.** `3.3.0-F2` paused the bar for every prompt that went through sysforge's prompt helpers, but sudo's own password prompt was not one of them. When cached credentials had lapsed, the prompt from the up-front credential check, or from a `pacman -U`/`-S` install (including the just-in-time install inside the `building` bar), appeared with the bar still drawn and its clock still counting. sysforge now checks first whether sudo would ask (`sudo -n true`). If it would, it runs `sudo -v` with the terminal handed over, so the bar is cleared and its clock paused until you answer. With credentials already cached, nothing changes.

---

- **`3.4.0-F1` — makepkg never runs sudo itself any more, and `build`/`update` ask for your password once, up front.** When makepkg installed a package (`-i`/`--install`) or pulled in missing dependencies (`-s`/`--syncdeps`), it ran `sudo pacman` from inside the build. When credentials had lapsed, sudo's password prompt went straight to your terminal: sysforge could neither see it, pause the progress bar for it, nor keep the bar from drawing over it. On `build`/`update` this happened after any AUR dependency build that took longer than sudo's timeout. makepkg is no longer allowed to escalate on any path: AUR dependency builds, the toolchain passes, BOLT, the kernel stage and the bootstrap `packages` stage. sysforge installs missing repo dependencies before the build and the built package after it, through the same path as its other installs: any password prompt hides the bar and pauses its clock (`3.3.0-F3`), and pacman's output is kept in the run log (`3.3.0-B21`). `build` and `update` now ask for your password once, right before dependency preparation, and keep it fresh in the background until the run finishes, so a long batch never stops halfway on a prompt. If you can't authenticate, the run stops before building anything instead of failing at the install. Two side effects: an AUR dependency that is already built is now installed instead of silently left missing, and the bootstrap `packages` stage now installs every AUR package it builds, as its own documentation always said, even when the profile doesn't pass `--install`.

---

- **`3.4.0-F2` — the sandbox refuses a container conf that still names the host.** Before a sandboxed build starts, sysforge now checks the `makepkg.conf` it hands the container for anything that still points at the host: an absolute path that won't exist inside the chroot, or a compiler, linker, wrapper or `ccache`/`distcc` binary the chroot doesn't have. It checks the values the build will actually use (the last assignment of each key, and only the variables makepkg passes to build tools), after the toolchain and any PGO profile have been copied in. A problem stops the build with one message naming the setting, its value and which part of the sandbox should have corrected it, instead of failing minutes later with an error that mentions neither the sandbox nor the setting. Six earlier sandbox bugs would each have been caught this way (`3.2.0-B2`, `B4`, `B6`, `B7`, `B10`, `B12`). `3.2.0-B5` would not: that was a stale package database in the chroot, not a setting in the conf.

## Changed

- **`2.6.1-F21` — config files sysforge rewrites are now replaced atomically, and symlinks, permissions and ownership are kept.** sysforge rewrites several existing config files: your shell rc and `/etc/environment` (the editor setting), `sysforge.toml` (`reconfigure`) and `/etc/makepkg.conf` (`configure` and `reconfigure`). Each writer emptied the file and then wrote it, so a crash or power loss mid-write could leave it truncated. Your shell rc is the file that matters most here. A shell rc managed as a symlink by a dotfile manager could also be replaced by a regular file, and the privileged path copied with `sudo cp` and its own default permissions. All of these now go through one write path. It follows symlinks so the link is kept and the file it points to is replaced, keeps the existing permissions and owner, and writes the new content to a temporary file in the same directory before renaming it into place. The old or the new file is always intact. Writes to root-owned files still escalate through sudo, with the same guarantees.

---

- **`3.2.0-F3` — `update`'s phases are now functions instead of comments.** `sysforge update`'s main body was one function of about 1,000 lines, and its numbered phases (initialization, package-set assembly, source sync, version check, drift detection, build, install and summary) existed only as `# Phase N` comments. Each phase is now its own function taking one `UpdateRun` record that carries the run's state from phase to phase, and the body is just the ordered list of phases. A failure's traceback now names the phase it happened in, and each phase can be tested on its own. Internal restructuring only: no behaviour changes, confirmed by the existing `update` test suite.

---

- **`3.2.0-F4` — `makepkg_wrapper` moves out of the primitives layer into a new `sysforge/build/` layer.** `primitives/` is meant to be the leaf layer, but `makepkg_wrapper` coordinated the whole per-package build lifecycle across thirty other sysforge modules, so every primitive it imported was one edit away from an import cycle. It now lives in `sysforge/build/`, a layer between the primitives and the commands and pipeline stages. Building resolved AUR dependencies, which also drives the wrapper, moves with it as `sysforge/build/aur_deps.py`, while resolving them stays in the primitives layer. The layering test now forbids primitives from importing the build layer. Internal restructuring only: no behaviour changes.

---

- **`3.2.0-F13` — every external command sysforge runs now goes through one run seam, enforced by lint.** sysforge already had a seam for running external commands (`primitives/run.py`), which keeps them in the unified run log and lets `--dry-run`, resource limits and the sandbox apply uniformly. But about 145 call sites across the tree still called `subprocess` directly, and each one could bypass those guarantees. A tool leaking into the build sandbox (`3.2.0-B2`) came from exactly such a site. Probes now go through `run.capture`, or the new `run.probe`, which reports a missing tool as a failed command (status 127) where those sites previously crashed with a traceback. Commands that should fail loudly use `run_or_raise`. The few calls that must stay raw (a full-screen editor, the pager, `pacman -Syu`, privileged installs that need the terminal) are each marked with the reason. A new lint rule now bans `subprocess` outside the seam modules, so new direct calls can't creep back in. Internal hardening: commands behave as before, except that a missing tool on a probe path now reports as a failure instead of crashing.

---

- **`3.3.0-DOC4` — the PGO guide now says what an unchanged kernel FDO `use` run does in each mode.** `docs/guides/pgo.md` § Keeping it applied said that when neither the profile nor the kernel changed, the run "reinstalls the package it already built". That is what an unattended run does. An interactive run asks first (install as built, rebuild, or abort), and an operator with `kernel.toml [fdo] mode` set meets that prompt on every routine `run kernel` with no guide text explaining it. The guide now describes both modes, and says rebuild is only needed to review the kernel config again, since a changed profile already forces a rebuild.

## Fixed

- **`3.1.0-B12` — `update --include-stage-owned` now builds toolchain packages first, then everything else against the new compiler.** Toolchain packages (llvm, clang, …) are normally skipped by `update` and left to `run toolchain`. With `--include-stage-owned`, or when one was named on the command line, they joined the same build batch as the packages they compile. A package is built *by* clang but almost never declares a dependency *on* it, so nothing ordered the batch around the compiler: some packages built against the old clang and some against the new one. Every package was also stamped with the toolchain fingerprint read before the batch, so once the new clang installed, the next run reported the whole set as toolchain-drifted and offered to rebuild packages that had just been built (with `--rebuild-on-drift`, a loop). Toolchain packages now build and install as their own pass first. The fingerprint is then read again, and the remaining packages build against the new toolchain and record its fingerprint. Runs without a toolchain package in the batch are unchanged.

---

- **`3.3.0-B11` — on an unchanged kernel, Gate 2 and the kconfig drift check now run against the reused package's own config instead of reporting "did not run".** When the kernel had not changed, makepkg skipped the build (exit 13) and the stage installed the package already in PKGDEST. Gate 2 and the drift check then looked for a build tree by guessing the system `BUILDDIR` under the checkout name, the guess `3.2.0-B37` had already removed from the fresh-build path, and on a profile with its own `BUILDDIR` that guess never matched. So every no-change `run kernel` printed the "Kconfig merge drift: check did NOT run" block and warned that the config "could not be validated before install", which left Gate 2, the brick backstop, off for every routine install. The stage now finds the tree the way a fresh build does (the profile's `BUILDDIR`, post-rename pkgbase) and uses it only when its `kernel.release` matches the release the PKGDEST package ships, since a later aborted build can leave a different `.config` there. If they don't match, it falls back to the `kconfig-history` archive for that release. The log names which source was checked, and the "did NOT run" block now appears only when neither exists. Reinstalling the same package no longer triggers the "matches the running kernel" warning either.

---

- **`3.3.0-B12` — the toolchain PGO progress counter no longer restarts partway through a pass.** `run toolchain`'s PGO pipeline splits some logical passes across several build calls: Pass 3 is the training build plus the optional corpus enrichment, and Pass 4 is llvm/llvm-libs, then the clang/lld suite, then lib32. Each call opened its own counter, so the final pass read `[1/1]` for llvm and then `[1/6]` for the rest, all under the same `PGO 4/4` label. Each logical pass now has one counter totalled over all its builds, so it runs `[1/7]` through `[7/7]`. The sub-pass names stay as section headers.

---

- **`3.3.0-B16` — `state profiles` no longer reports every applied kernel FDO store as `0d` old.** With `kernel.toml [fdo] mode` set, every routine `run kernel` reuses the already-built optimized kernel, and each reuse rewrote the store's `applied.toml`. `state profiles` dated a store by its newest file, so that bookkeeping write reset the age column the PGO guide tells you to use when deciding to re-profile. A `use` install now leaves `applied.toml` alone when it already holds the same profile fingerprint, so it means "last applied" rather than "last run". The listing also dates a store from its profile files only, ignoring the `round.toml` and `applied.toml` sidecars, so a later bookkeeping change cannot bring the drift back.

---

- **`3.3.0-B17` — a capture refusal in round 2 no longer sends the operator back to round 1.** When the only `vmlinux` matching the running kernel was the debug-stripped `-headers` copy, or the profiling build tree was gone, `--autofdo=capture` told you to re-run `sysforge run kernel --autofdo=record`. In round 2 that command builds a round-1 profiling kernel, so following it as printed cost a round. Every recovery command in a capture refusal now carries the round's own flags (`--autofdo=record --propeller` in round 2).

---

- **`3.3.0-B18` — a failed kernel FDO sidecar write reports its own error, not the cleanup's.** When writing `round.toml` or `applied.toml` failed, the temporary file was unlinked before re-raising. The unlink is in the same directory, so it tends to fail the same way, and when it did, its error replaced the original and the warning named the cleanup rather than the write. The cleanup's errors are now suppressed so the original always propagates. Also: `fdo.building_pkgver`'s docstring now says it reads the static PKGBUILD, and the three remaining `"PKGBUILD.sysforge"` literals use `pkgbuild_patcher.PATCHED_PKGBUILD_NAME`.

---

- **`3.3.0-B20` — a DKMS module whose automatic build failed during the kernel install is now reported as a failed build, with its log path.** Installing a kernel's `-headers` package runs the DKMS pacman hook. When the module build fails, the hook only prints a warning and pacman still exits 0, so Gate 3 and `doctor` saw only the end state and said the module was "not built", with a retry command, as if nothing had run. They gave no hint that a compile had failed or where its output was, and the retry overwrites that output. When the kernel's headers are installed and `/var/log/pacman.log` shows the hook's latest build of that module for that kernel exited non-zero, the finding now says the automatic build ran and failed, gives the exit status, and names `/var/lib/dkms/<module>/<version>/build/make.log`, telling you to copy it before retrying. Without headers, or with no hook failure in the log, the message is unchanged.

---

- **`3.3.0-B21` — kernel installs and interactive package installs now keep pacman's output, including the hooks'.** `run kernel`'s `pacman -U`, and an interactive `build`/`update` install, passed pacman's output straight through to the terminal so its prompts would work. Everything pacman's hooks printed (DKMS module builds, `mkinitcpio`, bootloader updates) was lost once it scrolled away, and a failed hook still exits 0, so nothing recorded it at all. That is why the `3.3.0-B20` DKMS failure could not be diagnosed afterwards. These installs now run on a pseudo-terminal that still forwards the output live and still takes your answer to the confirmation prompt, while each line is also written to the run log. A hook line reporting a failure (`==> WARNING: … exited <n>`, `Error!`, `==> ERROR:`) is raised as a warning so it shows in the run summary. A failed install now includes pacman's last lines in its error instead of saying the output went to the terminal.

---

- **`3.4.0-B1` — `update`'s trailing `pacman -Syu` no longer stops at a confirmation prompt.** Every other step of `update` runs unattended, but the system upgrade (`--sysupgrade`, `[build] system_upgrade`, or the `repo_mode = "build_from_source"` repo-package upgrade) only added `--noconfirm` when an option `update` does not have was set, so it always waited at pacman's `Proceed with installation?` and a run left alone stopped there. It now runs with `--noconfirm`, letting pacman's defaults answer: a package conflict still defaults to No, so the upgrade is cancelled and the summary reports it as failed rather than removing a package unasked. Pass `--interactive` to answer pacman's prompts yourself.

---

- **`3.4.0-B2` — `update` no longer asks for your sudo password again after a long system upgrade.** The build keeps sudo active only until its own installs finish. When the trailing `pacman -Syu` upgrades a kernel package, the end of the run removes sysforge boot entries whose kernel image is gone, which needs sudo. If the upgrade ran longer than sudo's timeout (a large download, a slow DKMS rebuild), that cleanup stopped at a password prompt after everything else had finished. `update` now keeps sudo active from just before the upgrade until that cleanup is done.

---

- **`3.4.0-B3` — a sandboxed build now records the compiler that actually built it.** With `[security] sandbox_builds` on, a package is compiled by the compiler inside the build container, which can be a different build than the host's (for example the repo's clang instead of a locally optimized one). Build state still recorded the host's compiler for every package, so toolchain drift compared the host with itself and could never report a sandboxed package as built with a different compiler. sysforge now identifies the container's compiler after each successful sandboxed build, without running it, and records the host's values only when it is the same package as the host's. Build state also gains a `sandboxed` field, and flag drift reports a package whose recorded sandbox state differs from what a build would do now, as a `sandboxed: False → True` line under `--explain-drift`. `--rebuild-on-flag-drift` rebuilds those packages, so turning the sandbox on or off can be followed by one rebuild of everything built the other way. Packages recorded before this change have no `sandboxed` value and are not reported.
