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

## Changed

- **`3.5.0-DEV25` — an end-to-end scenario for the toolchain → kernel handoff.** `96-toolchain-kernel-handoff` runs a real `run toolchain` with `skip_build = true`, which registers a compiler without building it, and checks the result saved in `pipeline_state.toml`: `cc=/usr/bin/clang`, variant `stock_llvm` for `compiler = "llvm"`, and gcc after switching back. It then checks that `run kernel --dry-run`, with no compiler in `kernel.toml`, plans with the same compiler and variant. Before this, `95-stage-dry-run` previewed the two stages separately, so nothing checked what one leaves for the other.

---

- **`3.5.0-DEV28` — an end-to-end scenario for a stock package installed over a sysforge build.** `65-external-install` builds `tree` from source, reinstalls it with `sudo pacman -S tree`, and checks that the next `update` demotes its record to `pacman` without rebuilding it and consumes the hook's sentinel. It then installs `lld` and checks that `update -v` shows the toolchain reminder once and not again. The pacman hooks ran in every scenario before, but nothing checked what they did. Needs the mirrors.

---

- **`3.5.0-DEV30` — an end-to-end scenario for `config merge`.** `75-config-merge` leaves a `.sfnew` beside an edited `sysforge.toml`, sets `[ui] merge` to a script that takes the new file's added key, and runs `config merge` at a terminal: `[m]erge`, then `[r]emove`. It checks that the key arrived, the user's edit survived and the `.sfnew` is gone. `config merge` is no longer listed as uncovered. The scenario found `4.0.0-B3`.

---

- **`3.5.0-DEV31` — an end-to-end scenario for `build --interactive` and `update --interactive`.** `45-interactive` uses a new stand-in, `e2e-hello-alt`, which conflicts with `e2e-hello` without providing it. Without the flag, `update` and then `build` stop at pacman's conflict question and install nothing. With `--interactive`, the question shows on the terminal and a yes swaps the packages.

---

- **`4.0.0-DEV1` — releases go through a release PR instead of a push to `main`.** The `main` ruleset requires a pull request, so the push the release script asked for was rejected. `tools/release.sh` now cuts each release on a `release/vX.Y.Z` branch and asks for `git push -u origin release/vX.Y.Z && git push origin vX.Y.Z`. Resuming requires that branch, and the script refuses to start if it already exists. The release checklist's last stage opens the release PR and merges it with a merge commit, never a squash: a squash rewrites the signed `release:` commit, leaving the `vX.Y.Z` tag outside `main`'s history, so `git describe` in the `-git` package keeps reporting the previous version.

---

- **`4.0.0-DEV2` — the release script publishes long release notes and resumes after a failed publish.** GitHub rejects a release body over 125,000 characters, and the 4.0.0 notes are longer, so `gh release create` failed. A notes file over the limit is now published as its summary plus a link to the full file at the tag. That failure also showed the resume was not idempotent: Phase 3 had already rewritten `PKGBUILD`'s `sha256sums`, and `make release-resume` then refused the dirty tree. A resume now accepts a `PKGBUILD` whose only change is that array, and still refuses any other edit.

---

- **`4.0.0-DEV3` — `make e2e` no longer draws over the terminal running it.** `tools/e2e/answer.py` runs a scenario's command on a pseudo-terminal that has no window size, so sysforge assumed 80×24: its progress bar set a scroll region for rows 1 to 23 and drew on row 24. `answer.py` copied those bytes to its output, and through `podman exec` they reached the terminal running `make e2e` or `make pre-release`, where text overlapped on that row while the terminal's real bottom line stayed blank. `answer.py` now passes text, colour, carriage returns and erase-to-end-of-line, and drops the rest, including whatever is drawn between a cursor save and its restore. The command still runs on a terminal, so the scenarios test the same paths, and prompts are matched on the unfiltered output as before.

---

- **`4.0.0-DEV4` — the e2e image's package database no longer goes stale.** `podman build` cached the demo image's provision layer for as long as `provision.sh` was unchanged, so its `pacman -Syu` snapshot stayed at the day the image was first built. The mirrors keep only each package's current file, so once `lld` had a newer release, `65-external-install`'s `sudo pacman -S lld` asked for a file they no longer had and failed with 404. Packages that hadn't changed, such as `tree`, still installed. `make e2e` and `tools/record-demo.sh` now pass the date as a build argument that the provision step reads, so the layer rebuilds once a day, and they pull a newer base image when there is one.

---

- **`4.0.0-DEV5` — the design citation check no longer counts a name that only a comment or docstring still mentions.** A bare code name cited in `docs/design/` or `.claude/` had to appear somewhere outside the docs, and a renamed helper's old name usually still did: in a docstring nobody updated, or in a test's fixture string. Seven names the `3.2.0` split had made public were still cited in the toolchain design two months later. In a Python file a name now counts only where code uses it (an identifier, or a string literal outside `tests/`). The stricter check also found a `[cache]` table in design 15 that sysforge never read; that section now describes how ccache and sccache are actually controlled. Names from other software that had passed only because a comment quoted them are listed explicitly. The check also walks the tree once per run instead of once per cited module, which takes the `citations` group from about 18 s to under 4.

---

- **`4.0.0-DEV6` — TOML examples in the design docs are checked against the shipped config.** A ```` ```toml ```` block preceded by `<!-- shipped-example: etc/sysforge/<file>.toml -->` may show only keys the shipped file has, at their shipped values; a line commented `# example` keeps an illustrative value, and `complete` also requires every shipped key. The first run found the toolchain example's old package lists and eight missing keys, the kernel example's `interactive` (renamed `review_config`), and a bootstrap ESP default of 512 MiB where the code uses 1024. The root `CLAUDE.md` now asks a change to revise every design passage it makes untrue, not only add a paragraph about itself.

---

- **`4.0.0-DEV7` — the design's module tree is generated, and its installed layout is checked.** Design 04's hand-kept `sysforge/` tree had fallen 80 modules behind and still listed the toolchain and kernel stages as single files. `make design` now builds it from each module's first docstring paragraph, so `make check-design` fails on a new, removed or re-described module and a module without a docstring fails the build. A new `check_standards` group, `design_tree`, fails when the *Installed* layout omits a path the PKGBUILD installs, a directory tmpfiles.d creates, or a file the code keeps in the state dir. Rewriting the layout added the three missing shipped configs, the libalpm hooks, completions, man page and fifteen state files, and fixed the `/var/cache/sysforge` mode (`2775 root:sysforge`, not `0777`).

---

- **`4.0.0-DEV8` — commands and flags written in the design docs are checked.** The written-down-commands test now scans `docs/design/`, and reports a nested subcommand written without its namespace (`sysforge pipeline` for `run pipeline`). A new `check_standards` group, `design_flags`, requires a `--flag` the design names to be a real flag, a retired name, one the code passes to another program, or a listed external flag; docstrings don't count, since that is how a phantom flag reads as real. Their first runs found `sysforge pipeline` in five places, seventeen `sysforge doctor --<axis>` commands from before the `doctor system`/`doctor pkg` split, `update --check-drift` (the route is `--explain-drift`), `build --track-deps` (documented, never implemented) and a `--standalone` flag that is an options field.

---

- **`4.0.0-DEV9` — every TOML block in the design is marked, and line-number citations are refused.** A ```` ```toml ```` block now needs `<!-- shipped-example: … -->` or `<!-- illustrative -->`, so leaving one unchecked is a decision. Marking the rest found design 10's conflict-group example using `[conflict_groups]`, a table the loader ignores (`[append_conflict_groups]`), and design 05 giving `repo_mode = "build_from_source"` as the default (it is `"pacman"`). A commented-out default spanning several lines is now uncommented until its bracket closes, without which `packages.toml` could not be compared at all. A `file.py:NN` citation fails the `citations` group (cite `file.py::symbol`): both in the design pointed at moved code.

---

- **`4.0.0-DEV10` — the surface snapshot records every spelling of a flag.** `tools/surface.py` kept only the longest spelling, so `run kernel --headers`, `--docs` and `--keep-hotplug-drivers` sat behind their `--no-` forms, and dropping one would not have changed `tests/data/surface.json`. Each long spelling is now its own entry.

## Fixed

- **`4.0.0-B1` — `build --failed` no longer rebuilds a package you removed.** Replacing a failed `ethtool-git` with `pacman -S ethtool` left its failure recorded, so `build --failed` queued `ethtool-git` again. A removal cleared the failure only when sysforge read the pacman hook's record of that transaction, and that record can be gone by then. A failed package that sysforge tracks but that is no longer installed now counts as removed too, in `update`, `state failed`, `doctor --state` and `build --failed`; `update` checks it before pruning the entry. A package whose first build failed was never installed and is still retried. A failure left over from before this fix is cleared with `sysforge state failed --clear PKGBASE`.

---

- **`4.0.0-B2` — bare `sysforge doctor` runs the full sweep again, and `doctor system` reads your config.** The `--config` axis flag and the loaded config shared one attribute. Bare `sysforge doctor` took the loaded config for the flag and checked only the live config settings, so it printed one section and stopped. `sysforge doctor system` took the flag's unset value for a loaded config and ran every axis against an empty one, so the toolchain, cache, graphics and storage checks ignored your settings. The flag now has its own attribute; its spelling is unchanged.

---

- **`4.0.0-B3` — `config merge` can remove and overwrite files in `/etc/sysforge`.** Choosing `[r]emove` or `[o]verwrite` for a `.sfnew` or `.pacnew` crashed with `PermissionError`, because sysforge wrote the root-owned directory as your user. Removing a companion now escalates through `sudo`, and overwriting a live file goes through the same atomic replace as `config clean` and `config reset`, which keeps the live file's mode and owner.

---

- **`4.0.0-B4` — a non-interactive install no longer removes a package it conflicts with because another package in the batch names itself.** Some AUR packages declare `provides` and `conflicts` on their own name (`cbonsai` does). sysforge read that as a drop-in replacing the installed copy and passed `--ask=4`, which pacman applies to every conflict question in the transaction. An `update` that rebuilt `cbonsai` alongside an unrelated conflicting package then removed the installed side without asking, instead of stopping. A package no longer counts as replacing itself, so only a real drop-in (`replaces`, or `conflicts` plus `provides` naming another installed package) gets the auto-confirm. Found by `45-interactive` in CI.

---

- **`4.0.0-B6` — `update --versions` reports a new repo kernel again when the kernel is renamed.** With `[kernel] source = "repo"` and a local name such as `pkgname = "linux-sysforge"`, sysforge looked up the renamed package in pacman's sync database, which only carries `linux`. The lookup found nothing and the kernel was left out, so a pending 7.2.9 → 7.2.11 never appeared in `update --versions` or in the reminder at the end of `update`. The check now falls back to the stock name the build records (`origin_pkgbase`), the same name `run kernel` already uses to pick which repo version to build.

---

- **`4.0.0-B7` — a sandboxed build can depend on a package its repo has dropped.** `lib32-tdb` left `[multilib]` for the AUR. The host still ran the last repo build, but sysforge had never built it, so there was nothing in `PKGDEST` to pass into the container. sysforge warned that the container would use the repo version, but no repo carries the package now, so `lib32-libcanberra` failed inside the container with `target not found: lib32-tdb`. The local repo and the build's own package list now take such a package from pacman's cache when the signed repo file of the installed version is still there. A package sysforge built is never taken from the cache, which can hold the repo's build at the same version. A dependency that no repo carries and that has no build in `PKGDEST`, the local repo or the cache now stops the build before the container starts, naming the package and the command to build it (`sysforge build lib32-tdb`). Unit tests only: the precondition, a package installed from a repo that has since dropped it, can't be reproduced against the live mirrors, and the cache and local-DB lookups this fix relies on run for real in those tests.

---

- **`4.0.0-B8` — the `run toolchain` build plan says whether BOLT will run, and where.** The plan printed before a build showed the compiler, PGO, package counts, staging paths and gates, but not `[bolt]`, so you could not tell from it whether a multi-hour build would BOLT anything. It now has a `bolt:` row naming the libraries (`libclang-cpp`, plus `libLLVM` with `[bolt] libllvm`), the step (after Pass 4a, in the build tree before install) and the training workload. When BOLT will not run, the row says why: it is off, `[bolt] enabled` is set on a run without PGO, or clang is not built by the PGO PKGBUILD. `--bolt-only` shows its targets too. The plan also gains a `corpus:` row (the Pass-3 training packages), a `reuse:` row (`--reuse-built` or `reuse_unchanged`) and an `after:` row (the soname-consumer rebuild mode and `[bench] after_build`).

---

- **`4.0.0-B9` — the low-disk warning no longer suggests a command that does not exist.** `doctor system --storage` told you to free space with `sysforge clean`, which sysforge has never had, and assumed the build dir was `~/builds`. It now names the build dir it measured (`rm -rf <dir>/*`) beside `paccache -r`. Two docstrings that described flags sysforge does not have (`update --check-drift`, `run reconfigure --standalone`) are corrected with it.

---

- **`4.0.0-B10` — `run toolchain` builds every PGO pass on the host when the build sandbox is on.** The toolchain and kernel stages are meant to be exempt from `[security] sandbox_builds`, since they build against and install into the host they upgrade. The toolchain's exemption was keyed on a per-build marker that only the install-bearing builds carry, so the instrument, bootstrap and train passes ran in the container. A full 4-pass rebuild then failed at Pass 1 with `refusing to sandbox a profile-generate build`, after the stage had already emptied the PGO store. A run that reused existing profdata skips those passes and was unaffected. The exemption now covers the stage's whole build sequence: every pass, `--bolt-only` and the single-pass build. The soname-consumer rebuild after install builds ordinary packages and still follows `sandbox_builds`. Unit tests suffice: the defect was which policy the stage handed its own builds, and the tests run the real stage with the sandbox policy on.

---

- **`4.0.0-B11` — A failed `run toolchain` pass aborts the stage instead of offering the build-recovery menu.** When an LLVM build failed during `run toolchain`, sysforge showed the same menu as `sysforge build`: edit the PKGBUILD, retry with a different compiler, retry as-is, or abort. Accepting an edit or a compiler swap partway through the PGO sequence leaves the passes built from different recipes or compilers, so the profile no longer matches what gets shipped. Every toolchain pass now runs in batch mode, and a failure aborts the stage with the system unchanged. To recover, fix the config or PKGBUILD checkout and rerun. The kernel stage, `build` and `update` keep the menu. Unit tests suffice: the defect was the build options the stage passes to its own builds.

---

- **`4.0.0-B12` — `run toolchain` instruments LLVM without breaking the runtimes build.** Since the official llvm PKGBUILD merged the suite into one recipe, it builds compiler-rt, openmp, offload and libc++ with the clang it has just built. sysforge passed the PGO flags as `CFLAGS`, which makepkg hands to every compile in the recipe, so that fresh clang was asked to link a profile runtime it had not built yet. Pass 1 then failed in the runtimes configure with `Could NOT find Threads`, and no full PGO rebuild could finish. The LLVM build now takes its PGO through LLVM's own cmake options (`LLVM_BUILD_INSTRUMENTED`, `LLVM_PROFDATA_FILE`), which apply to the LLVM projects and leave the runtimes alone. A PKGBUILD the options cannot be added to fails the pass instead of building without instrumentation. The other toolchain packages still take `-fprofile-use` as compiler flags. A `--reuse-built` cache from an earlier run is not reused, so the next Pass 4 rebuilds once. Unit tests plus a `make test-live` canary that patches the current official llvm PKGBUILD.
