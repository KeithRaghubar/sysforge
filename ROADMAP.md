# SysForge Roadmap

Planned features and changes. This is the single tracked home for
forward-looking work; **`DESIGN.md` describes only implemented design** and
never carries roadmap IDs.

**Purposely-excluded and abandoned ideas live in
[`docs/ROADMAP-ABANDONED.md`](docs/ROADMAP-ABANDONED.md)**, together with their
rationale and reopen conditions. That file is history rather than backlog, but
its IDs remain part of this file's ID namespace — see `## ID scheme`.

**Shipped work is not recorded here** — it lives in `docs/release-notes/` and git
history (the commit that lands an item is its record).

## ID scheme

IDs are `<version>-<TYPE><n>`, e.g. `1.2.0-F1` (feature), `1.2.0-B1` (bug),
`1.2.0-Q1` (open question), `1.2.0-STD1` (standards), `1.2.0-DOC1` (user-facing
documentation), `1.2.0-DEV1` (contributor-only work). The version prefix is the
current `pyproject.toml` version. The per-type counter **resets to 1 only on a
major or minor version bump** (`X.Y.Z` → `(X+1).0.0` / `X.(Y+1).0`), never on a
patch bump — i.e. the counter is scoped to the minor-release cycle and stays
monotonic across patch releases within it. The version prefix keeps IDs globally
unique and records the cycle an item originated in. An item still open at release
time keeps its existing ID
(it records the cycle the item originated in, not its target). IDs appear only here,
in `docs/ROADMAP-ABANDONED.md`, and in release notes.

**Those three files are one ID namespace.** An abandoned ID stays spent — it is
never reissued — so `make next-id` reads all three, and `make check-standards`
rejects an ID listed both here and in `docs/ROADMAP-ABANDONED.md`.

**Never hand-pick an ID — derive it.** The open items above keep their origin-cycle
prefixes, so eyeballing a neighbour gives the wrong cycle (and the wrong counter)
right after a release. Run `make next-id TYPE=F` (or `B`/`DEV`/`DOC`/`Q`/`STD`) — it reads the
current `pyproject.toml` version, scopes to that cycle's counter, and prints the next
free ID (e.g. `2.4.0-F1`). `make check-standards` also flags collisions and
active-cycle sequence gaps.

Within each subsection, entries are kept in **ascending ID order** (by type
counter, then version) — sort on every add so the list stays scannable. Every
entry opens `- **`<ID>` — <title sentence>.**` and is separated from its
neighbour by a `---` rule, so a single item is readable on its own rather than
running into the next one. `docs/release-notes/` entries use the same two rules.

**`DOC` covers user-facing documentation only** — guides under `docs/guides/`,
`README.md`, the man page — so doc work stays visible in release notes without
being forced into `F` or `B`. A `DOC` entry always lands under `## Changed`: the
section drives the derived SemVer bump, and documentation changes no API (a
`DOC` under `## Added` would demand a minor release). `make check-standards`
enforces this. Internal docs (`docs/design/`, `CLAUDE.md`, this file) get no ID,
and wrong `--help`/man output is a `B`, since it is CLI behaviour.

**`DEV` covers contributor-only work** — a fix or feature that touches only
`tests/`, `tools/`, the `Makefile`, `.claude/` or dev-only docs (`tools/vm/README.md`
and the like). Anything under `sysforge/` or a shipped file stays `B`/`F` even when a
dev problem prompted it, and changing a *rule* (a standards row, a process
convention) stays `STD`. A `DEV` item **files no release-note entry**: release notes
are user-facing and their sections drive the derived SemVer bump. Its squash-commit
subject carries the ID and is its only record, which `make next-id` and the gap
check read so a shipped `DEV` number is never reissued. `make check-standards`
rejects a `DEV`-filed release-note entry.

**Open questions (`Q`) must be resolved before any implementation.** A `Q`
entry is undecided by definition; investigation/spikes to inform the decision
are fine, but before writing production code the question must first be either
**promoted** to a proper `F`/`B`/`STD`/`DOC`/`DEV` entry (which then follows the normal
landing flow) or moved to `docs/ROADMAP-ABANDONED.md` with a rationale. Never
implement straight off a `Q`.

## Priority, effort & bump tags

Every **Planned** entry ends with a machine-readable tag of the form
`*Priority: <level> · Effort: <size> · Bump: <kind>* — <rationale>`:

- **Priority** ∈ `low` / `med` / `high` — impact *within this backlog*. The backlog
  is polish by construction (anything correctness-bearing is implemented, not parked),
  so judge relatively: `low` = cosmetic/churn/latent; `med` = observability, or a gap
  that is *currently active* on a real system; `high` = user-visible friction now.
- **Effort** ∈ `small` / `medium` / `large` — implementation cost, independent of
  priority. On a uniformly-low-impact backlog this is often the deciding axis
  (cheapest-to-land first). Effort is stable; it does not decay the way a per-host
  "is this live right now" flag would, which is why urgency folds into *priority*
  rather than a separate tag.
- **Bump** ∈ `patch` / `minor` / `major` — the SemVer impact the entry will carry
  when it lands (standards row 3). `major` means it changes or removes an
  existing contract and can therefore only ship in an `X.0.0` release; `minor`
  is purely additive; `patch` is a fix or an internal change with no surface
  effect. This is **planning-time** advice: an item is removed from ROADMAP by
  the commit that lands it, so by release time the authoritative record is the
  release-notes accumulator, from which `make next-bump` derives the required
  bump.

`docs/ROADMAP-ABANDONED.md` entries carry no tag — the tag is planning advice,
and an abandoned item has no landing to plan. The summary table below is **generated** from these
tags — run `make roadmap-table` after any add/remove/retag; `make check-roadmap-table`
(wired into `pre-release`) fails if the committed table has drifted.

The committed table is always in triage order (priority, then effort, then ID).
To read the backlog along another axis without touching the file, use `make
roadmap-view SORT=<triage|id|item|priority|effort|bump>` (add `REVERSE=1` to
flip it) — e.g. `SORT=effort` for cheapest-to-land first. That view is
read-only by construction, so it can never be committed in place of the
canonical ordering.

---

## Planned

<!-- BEGIN roadmap-table (generated by tools/gen_roadmap_table.py — do not edit by hand; run `make roadmap-table`) -->
| ID | Item | Priority | Effort | Bump |
|----|------|----------|--------|------|
| `3.0.0-F3` | update's PKGBUILD review gate is silent in exactly the unattended case | high | medium | major |
| `3.1.0-F4` | a first run should confirm before it changes anything, and setup should offer to persist that posture | high | medium | major |
| `3.3.0-B11` | on an unchanged kernel, every run says the merge-drift check and Gate 2 "did not run", although the tree that built the installed package is still on disk | med | small | patch |
| `3.3.0-B15` | completing a bare - under doctor system, doctor pkg, build, update or run toolchain prints a scrambled option listing; --help renders fine | med | small | patch |
| `3.3.0-B20` | a DKMS module whose pacman-hook build failed is reported as never built, and the finding sends the operator to rerun the command that just failed | med | small | patch |
| `3.3.0-F1` | the building bar's ETA swings because it averages packages of very different sizes | med | small | patch |
| `3.1.0-B12` | update --include-stage-owned co-schedules a toolchain rebuild with the packages it compiles, and stamps them all with the pre-rebuild fingerprint | med | medium | patch |
| `3.3.0-B21` | kernel installs keep none of pacman's output, so a failed DKMS hook or other post-transaction hook leaves no record | med | medium | patch |
| `3.1.0-F1` | a clean diagnostics axis reports nothing, so it reads as a broken axis | med | medium | minor |
| `3.1.0-F3` | no way to declare an AUR-free posture; update reaches for the AUR unconditionally | med | medium | minor |
| `3.2.0-F3` | Make update's phases functions, not comments | med | medium | patch |
| `3.2.0-F4` | Relocate makepkg_wrapper out of the leaf layer | med | medium | patch |
| `3.2.0-F9` | the mesa PGO rebuild suppresses its own staleness diagnostic, so a drifting profile is unobservable | med | medium | minor |
| `3.2.0-F10` | nothing verifies that a freshly installed locally-built mesa actually initialises | med | medium | minor |
| `3.2.0-F18` | build inputs applied outside the resolved profile never register as drift, so changing them leaves stale packages installed silently | med | medium | minor |
| `3.2.0-F19` | an interactive build's failure reason is never logged | med | medium | minor |
| `3.1.0-Q1` | should sysforge have an opinion about kernel hardening, or is that outside a build tool's remit? | med | medium | minor |
| `3.2.0-Q2` | what is the unit of "stop maintaining this": a pkgname, a pkgbase, or a policy decision that outlives both? | med | medium | minor |
| `3.2.0-F13` | Fence the remaining direct subprocess use behind the run seam | med | large | patch |
| `3.3.0-B12` | the toolchain PGO counters reset partway through a pass, so the final pass reads [1/1] and then [1/6] under the same PGO 4/4 label | low | small | patch |
| `3.3.0-B16` | under kernel.toml [fdo] mode, state profiles reports every applied kernel FDO store as 0d old, however old its profile is | low | small | patch |
| `3.3.0-B17` | capture's stripped-vmlinux refusal tells a round-2 operator to re-record without --propeller | low | small | patch |
| `3.3.0-B18` | a failed sidecar write can report the cleanup's error instead of its own, plus three loose ends from the kernel FDO rework | low | small | patch |
| `3.3.0-DOC4` | the PGO guide says an unchanged kernel FDO use run quietly reinstalls the package it already built; an interactive run asks first | low | small | patch |
| `3.3.0-F3` | a sudo password prompt sysforge raises itself does not pause or hide the progress bar | low | small | patch |
| `2.6.1-F27` | Install stage target-root change summary | low | medium | patch |
| `3.0.0-F1` | Preflight the Rust toolchain when the kernel fragment requests CONFIG_RUST | low | medium | patch |
| `3.2.0-F7` | Per-verb parser assembly on the Verb class | low | medium | patch |
| `3.3.0-F4` | a sudo prompt inside forwarded build output leaves the progress bar's clock running | low | medium | patch |
| `3.2.0-Q1` | should the container's config be an allowlist of what may cross, rather than a copy of the host's with known-bad keys subtracted? | low | medium | minor |
| `2.6.1-F21` | one home for replacing an existing config file | low | large | patch |
| `3.1.0-F10` | a sandboxed build links against repo versions, not the versions the host runs | low | large | minor |
<!-- END roadmap-table -->

### Features

- **`3.0.0-F1` — Preflight the Rust toolchain when the kernel fragment requests `CONFIG_RUST`.**
  The kernel stage never authors kconfig itself — it merges hardware, device and manual
  `[[kconfig]]` entries into `sysforge.config` and lets the PKGBUILD's `prepare()` overlay them
  (`stages/kernel.py` module docstring). That is the right division of labour, and `CONFIG_RUST=y`
  should stay a user decision, not a sysforge default: the in-tree Rust drivers cover none of the
  tested-hardware scope (§Tested hardware — x86_64/Zen 3/NVMe), and the Nvidia path here is an
  out-of-tree DKMS module that kernel Rust does not touch. What is missing is the *precondition*
  check. `CONFIG_RUST` is unique among kconfig symbols in depending on host tooling rather than on
  the kernel source: `scripts/rust_is_available.sh` demands a `rustc` inside the version window the
  tree pins plus a matching `bindgen`, and when it fails kconfig drops the symbol during
  `olddefconfig` — the silent-loss shape the shipped `kconfig_plan.VERIFY` slot (`2.6.1-F23`)
  *detects*. This item is the other half: refuse to start the build rather than discover the loss
  from a warning after it. Two things make the
  failure likelier here than upstream assumes — `RUSTC_WRAPPER=sccache` and a rustup install that
  shadows the distro `rust` (the live workstation case, already documented in
  `primitives/rust_probe.py`), so the `rustc` the kernel resolves is frequently not the one the
  operator thinks. The seam exists: `primitives/toolchain_preflight.py` already probes rustc with
  pin awareness (`_probe_rust_native`, `rust_toolchain_pins`), and `doctor.py`'s `rust` axis already
  reports rustup-shadow provenance. Add a requirement token (`rust:kernel`) contributed when the
  merged fragment carries `CONFIG_RUST=y`, resolving to a `rustc` + `bindgen` presence and
  version-window probe, and surface the shadow provenance in the failure message rather than a bare
  "not found". Design decisions: **where the version window comes from** (parse the tree's
  `scripts/min-tool-version.sh` / `rust_is_available.sh`, versus a table sysforge maintains and must
  chase — the former is the only one that stays correct across kernel versions) and whether this is
  a hard preflight failure or a loud warn, given the stage's existing preference for warning over
  hard-failing a curated-table entry. Dual-toolchain parity applies: the check must behave
  identically on the gcc and llvm kernel paths, since `CONFIG_RUST` is orthogonal to `LLVM=1`.
  *Priority: low · Effort: medium · Bump: patch* — no behaviour change unless a fragment actually
  requests `CONFIG_RUST`; it converts a post-build silent drop into a pre-build refusal.
  **Standards home on adoption:** none new — this extends the existing toolchain-preflight seam.

---

- **`3.1.0-F1` — a clean diagnostics axis reports nothing, so it reads as a broken axis.**
  `sysforge doctor system --graphics` on a healthy NVIDIA/Wayland workstation prints one `[INFO]`
  line (`session_type`) and `1 finding(s), 0 error(s)` — indistinguishable from an axis whose
  probes all bailed out. That is not a graphics bug: every probe in
  `primitives/graphics_probe.py` returns `None` on success (`_check_nvidia_module_loaded`,
  `_check_multilib_enabled`, `_check_mesa_llvm_symbols`, …), and `_check_session_type` is the lone
  always-INFO probe, so the visible output is an accident of which check happens to be
  unconditional rather than a report. The shape is framework-wide — `diag.Axis`
  (`primitives/diagnostics.py:181`) is a bare callable returning findings, with no notion of which
  checks ran, which were vendor-gated out (`gpu_vendors` empty ⇒ silently skipped), and which
  passed; `render_axis` can therefore only print `clean_msg`. So the user cannot distinguish
  *checked and healthy* from *skipped because the probe found no `lspci`/`lsmod`/`pacman.conf`* —
  and the vendor-gated skips are the ones most likely to hide a real detection failure upstream of
  the check. Fix at the framework seam, not per-probe: let a probe report a `ran`/`skipped(reason)`
  outcome alongside its optional finding, have `Axis` accumulate the roster, and render it under
  `-v` (per the §Logging rubric — the roster is narration, the findings are the answer), leaving
  default output unchanged. Doing it in `graphics_probe.py` alone would fork the axis contract that
  `toolchain`, `hardware`, `cache`, `rust` and `gfxperf` all share.
  *Priority: med · Effort: medium · Bump: minor* — observability gap that is currently active on a
  real system; touches the shared `diagnostics` contract and every probe module's return type, but
  adds no new checks and changes no default-verbosity output.
  **Standards home on adoption:** none new — row 25 (logging levels) already governs where the
  roster prints.

---

- **`3.0.0-F3` — `update`'s PKGBUILD review gate is silent in exactly the unattended case.** The
  review gate (`primitives/pkgbuild_review.py`) is the codebase's existing supply-chain control: it
  diffs the full source tree from the recorded `reviewed_commit` to HEAD — catching changes hiding
  in `.install` files, patches and new sources, not just the PKGBUILD — and asks for a decision.
  But it auto-accepts on two paths, and their intersection is the hole: non-interactive runs (stdin
  or stdout not a TTY) auto-accept so an unattended `sysforge update` cannot hang, and `update`
  itself passes `interactive=False` by default so routine batch updates stay unattended. The result
  is that a cron/timer-driven `update` builds and installs arbitrary changed sources having only
  *logged* the decision. That is the precise scenario the recent AUR malware incidents exploit, and
  `3.0.0-F2` does not cover it — a freeze stops code *arriving*, this is code that already arrived.
  The fix is not to prompt in a non-TTY (that reintroduces the hang the auto-accept exists to
  prevent) but to make unattended runs **refuse** rather than assume: a policy knob whose strict
  setting turns a would-be prompt into a per-package blocker, reported in the summary and exited
  non-zero, so the operator reviews and re-runs. Design decisions to resolve first: **whether the
  strict setting is the new default** (a behaviour change for existing unattended users, hence the
  major-bump framing) or opt-in beside `[security] freeze_sources`; whether `--no-review` should
  remain able to disable the gate wholesale under a strict policy, or be demoted to a per-run lift
  like `--thaw`; and whether the blocker reuses `pkgbuild_review`'s existing `DECISION_*` vocabulary
  or earns its own status. Should reuse `3.0.0-F2`'s blocker-reporting path rather than growing a
  second one. *Priority: high · Effort: medium · Bump: major* — under the strict default an
  unattended `update` that previously completed now stops on any changed source, which is a
  breaking behaviour change for automation; it is also the larger of the two holes, since it fires
  on code already on disk.
  **Standards home on adoption:** none new — extends the existing review-gate seam.

---

- **`3.1.0-F3` — no way to declare an AUR-free posture; `update` reaches for the AUR unconditionally.**
  The per-package machinery for a repo-and-local-only user is already complete and deliberate:
  `source` is a first-class classification (`"repo" | "aur" | "git" | "local"`) settable in
  `packages.toml`, persisted in `build_state.toml` rather than re-derived each run
  (`primitives/build_state.py:184`), and honoured by the scheduler — `source_sync.py:279`
  short-circuits `"local"` to `STATUS_SKIPPED_LOCAL` because a hand-maintained PKGBUILD has no
  remote to sync against. `repo_mode = "build_from_source"` covers repo packages, and one
  `sysforge build <pkg>` is enough to put anything under `update`'s maintenance without it. What is
  missing is the *posture*: nothing lets a user say "never touch the AUR" once. The `[security]`
  section offers only `freeze_sources`, a blanket network freeze that also denies repo checkouts and
  source fetches — the wrong instrument; `[aur]` (`sysforge.toml:48`) tunes politeness
  (`min_fetch_interval_ms`, `rate_limit_abort_s`) but never abstention; and `--offline` gets there
  only by disabling every version check too. Worse, `update.py:646` calls `fetch_aur_name_cache()`
  on every non-`--offline` run *before* inspecting what is actually managed, so a user with zero
  AUR packages still generates AUR RPC traffic on every update. The policy half has a seam that
  already anticipates this: `net_policy.py:46-48` splits `KIND_AUR_CLONE` from
  `KIND_REPO_CHECKOUT` precisely because "a future policy may permit one while denying the other" —
  an AUR-free posture is that policy, not a new mechanism. The eager name-cache warm is the harder
  half, since it sits upstream of `NetPolicy.check()` entirely; gate it on whether anything managed
  actually carries `source = "aur"` (build_state already knows) rather than routing it through the
  policy. Filing this commits sysforge to the AUR-free user as a supported persona, which is the
  real decision here — the code is largely already behaving as if it were.
  *Priority: med · Effort: medium · Bump: minor* — additive posture plus one gating condition; no
  existing default changes and no package's routing behaviour moves.
  **Standards home on adoption:** none new — the egress-kind vocabulary in `net_policy.py` is the
  existing home, and this extends it rather than adopting an external spec.

---

- **`3.1.0-F4` — a first run should confirm before it changes anything, and `setup` should offer to persist that posture.**
  A new user's first `sysforge update` can rebuild and reinstall an arbitrary number of packages
  with no upfront confirmation. Nothing in the CLI gates it: `--dry-run` shows the plan but is a
  separate invocation the user has to know to run first, and the `[build] review` gate
  (`packages.toml:90`, default `true`) is a *per-package* prompt that fires only when a package's
  source tree changed since the last accepted build — it is a supply-chain diff review, not a
  "here is the whole batch, proceed?" gate, and it says nothing about the packages whose sources
  are unchanged but which will still be rebuilt and reinstalled. Note also that `--interactive`
  is already spoken for and means two *different* things: on `build` it strips `--noconfirm` and
  hands stdout/stderr to makepkg's terminal (`cli.py:312`), on `update` it pauses on build
  failures for manual correction (`cli.py:441`). Neither is a confirmation gate, and a third
  meaning must not be hung on that flag name.
  Add a real gate — default on — that prints the resolved plan (what will be rebuilt, what will be
  installed, what the batched `pacman -Syu` will touch) and prompts once before any mutation, with
  the existing `--dry-run` output as its body since that computation already exists. Pair it with
  flipping `[security] freeze_sources` from `false` (`sysforge.toml:243`) to `true`, so the
  out-of-the-box posture is "ask before changing, and do not fetch unmediated sources" and the
  permissive behaviour is what a user opts into (`--no-frozen`, and a `--yes`/`--noconfirm`-style
  bypass for the gate).
  The second half is the escape hatch that makes the posture liveable: `setup` (and the
  `reconfigure` stage) should *ask* whether to persist the answers globally and write them to the
  live `sysforge.toml`, so an experienced user turns both off once instead of passing flags
  forever. Two constraints make that non-trivial and are why it belongs here rather than as a bare
  "add a prompt" item. **(a) There is no runtime TOML writer.** tomlkit is a dev-only dependency
  pulled into an ephemeral overlay by `tools/sync_config.py`; the only runtime precedent is
  `config.set_default_toolchain` / `_rewrite_profiles_default_toolchain`
  (`primitives/config.py:752-814`), an anchored line rewrite that exists specifically to avoid
  tomlkit at runtime. A second such key wants that generalised into one seam, not copy-pasted —
  the same "one home" argument as `2.6.1-F21`. **(b) The deprecation registry does not model a
  changed default.** `primitives/deprecations.py` has kinds for removed *surfaces* (`config_key`,
  `state_token`); a default whose *value* changed while the key remains valid has no kind, so
  either the registry grows one or the `freeze_sources` flip is carried by a release-note
  `## Changed` entry plus the prompt alone. Decide that before implementing, not during. `setup`
  currently takes only `--pacman-conf` and does no prompting at all, so the prompt seam is new
  even though `primitives/prompt.py` supplies the primitives (`prompt_choice`, `is_interactive`).
  Relates to `3.0.0-F3`, which fixes the *review* gate's silence under automation — the same
  "unattended runs must not silently consent" principle, one layer down.
  *Priority: high · Effort: medium · Bump: major* — a default-on confirmation gate and a flipped
  `freeze_sources` both change behaviour for every existing install on upgrade; the `setup` prompt
  is the migration path, not a separate convenience, which is why the parts ship together.
  **Standards home on adoption:** none new — but the runtime config-write seam from (a) is a
  candidate "one home" row if a third key ever needs it.

---

- **`3.1.0-F10` — a sandboxed build links against repo versions, not the versions the host runs.**
  The other half of the sandbox's dependency-scope limit, and the one `3.1.0-F9` (shipped) explicitly
  does not reach. Even with locally-built artifacts injected, everything *not* injected is resolved from the
  stock repos inside the container — so on a host whose LLVM is built from source ahead of `extra`,
  a sandboxed build compiles and links against the repo LLVM and the resulting package may not match
  what the host actually runs. This is not a lost optimization (a dependency's optimization lives in
  its own installed binary, which the built package still runs against); it is a **version-agreement**
  failure, and the failure mode is a broken package rather than a slower one.
  The standard fix is a local pacman repo: `repo-add` every artifact sysforge builds into a repo
  directory, and name that repo in the `pacman.conf` the chroot is created with (`mkarchroot -C`),
  so the container's dependency resolution sees the host's own builds by name and version. Nothing
  in the tree maintains such a repo today — `resolve_repo_mode`'s `build_from_source` is about where
  *PKGBUILDs* come from, not about publishing artifacts — so this is a genuinely new surface: a repo
  directory under the state dir or `PKGDEST`, a `repo-add` call at the same seam that records a
  successful build, a chroot `pacman.conf` template, and a decision about pruning (a `repo-add`-ed
  archive grows without bound).
  Weigh against the alternative of simply leaving the sandbox scoped at AUR leaves: the local repo
  is the difference between a per-profile opt-in and a mechanism that could reasonably default on.
  `3.1.0-F9` shipped first as planned and closed the more common failure; its artifact selection is
  now `makepkg_artifacts.find_artifacts(..., exact_ver=)`, which is what a `repo-add` pass calls to
  decide *which* build of a package to publish.
  **Observed break (2026-09-11).** This gap has already caused a broken package, not just a missed
  optimization. A sandboxed `mesa-sysforge` linked against the repo `llvm-libs 22.1.8-2` while the
  host ran a PGO `llvm-libs` with **the same pkgver** and different exports. `libgallium` failed to
  load, and the desktop did not come up after reboot. The immediate cause was injection losing the
  whole closure (`3.2.0-B18`/`B19`, fixed), and `3.2.0-B20` now blocks the install. That leaves a
  pkgver-identical skew outside the injection set detected only after a full build. A local repo
  prevents it at the source, so this entry's "buys reach rather than fixing an active break"
  rationale is weaker than when it was tagged.
  *Priority: low · Effort: large · Bump: minor* — the sandbox is usable without it and default-off,
  so this buys reach rather than fixing an active break; effort is a new artifact-publishing surface
  plus chroot provisioning and a retention policy, none of which exists today.
  **Standards home on adoption:** deferred to implementation — a local repo would be the first
  artifact-*publishing* surface in the tree, so if it lands it needs its own row covering repo
  layout and the `repo-add`/signature story, rather than extending an existing one.

---

- **`2.6.1-F21` — one home for replacing an existing config file.** Six sites hand-roll the same
  stage-to-temp-then-install shape with three different spellings and two different privilege
  idioms: `env_persist.apply_write`, `reconfigure._save_sysforge_toml_ui`, the `makepkg.conf` writer
  in the same module (line ~1181), `makepkg_conf.py`, `pacman_hooks.py`, and `artifacts.write_live`
  — the last of which alone uses the §22 seam correctly (`run_privileged` + `install -Dm`) while the
  first three call `subprocess.run(privileged_argv(["cp", …]))` raw. None of them is atomic: every
  one truncates-then-writes, so a kill or power loss mid-write leaves a truncated file. Add
  `primitives/atomic_write.py` as the sole seam for *replacing a file that already exists*: resolve
  symlinks first (`write_text` writes through a symlink, `os.replace` would clobber a dotfile-manager
  link with a regular file), stat the destination for mode/owner, stage the temp **in the
  destination's directory** so `rename(2)` stays same-filesystem, then `os.replace` on the direct
  path and `run_privileged(["install", "-m/-o/-g", …])` + rename on the escalated one. Convert the
  first four writers together — fixing one and not the others leaves the weaker guarantee in place
  while implying otherwise — and fold their raw `privileged_argv` calls into `run_privileged`.
  Deliberately **out of scope: `artifacts.write_live`**, which creates new files with a class-defined
  mode rather than replacing user-owned config, so it has no destination mode to preserve and no
  symlink to honour; folding it in would force the helper to grow a second mode. Add the
  symlink-preservation and mode/owner-preservation regression tests none of these writers has today.
  Severity note for whoever implements: the file carrying irreplaceable content here is the **user's
  shell rc**, not `/etc/environment` — pam_env parses the latter line-oriented and skips malformed
  lines with a warning, so a truncated `/etc/environment` silently loses env vars rather than
  blocking login. *Priority: low · Effort: large · Bump: patch* — six call sites plus coverage that
  does not exist yet; behaviour-preserving on every success path. **Standards home on adoption:**
  none new — §22's privilege seam row already covers the `run_privileged` conversion.

---

- **`2.6.1-F27` — Install stage target-root change summary.** Builds on `2.6.1-F24`; **built last,
  and deferrable.** The install stage pacstraps into a target root via `archinstall --silent`, so
  it has no before-state and every row is an addition (`— → ver`) — a manifest plus a total size.
  Two obstacles: `pacman.get_all_installed_packages()` (`pacman.py:735`) queries the live root with
  no `root=` parameter and needs an optional one; and **no target-root path is modelled anywhere in
  sysforge** — `archinstall_config.py` describes only per-partition `mountpoint` values, and neither
  `install.py` nor `archinstall_invoke.py` records the mount root, so resolution falls to an explicit
  `--target-root`/config value or a `findmnt` probe at stage end. The real risk is timing rather than
  path resolution: the after-snapshot must run while the target is still mounted, and the current
  code does not establish whether archinstall leaves it mounted when `install.py` returns. On any
  failure this emits F24's `UNKNOWN` outcome with an explicit reason — never a silent no-op, never a
  fabricated count. If the mount lifetime proves hostile, drop this item; F24–F26 stand alone.
  *Priority: low · Effort: medium · Bump: patch* — the one piece carrying implementation
  uncertainty, isolated here so it cannot hold up the rest.

---

- **`3.2.0-F3` — Make `update`'s phases functions, not comments.** `_cmd_update_body` is a
  790-line function; the numbered phases DESIGN describes (§update) exist in the code only as
  `# Phase N` comments and roadmap-ID annotations. The helpers are already extracted
  (`update_sync`, `update_version`, `update_assemble`, `update_summary`, `update_common`) — what
  remains inline is the *sequencing* itself. Introduce one function per phase taking and returning
  a small `UpdateRun` dataclass that carries the run's accumulated state (package set, sync results,
  drift verdicts, build outcomes, the `_UpdateResult`), and reduce the body to the ordered call
  list. "Which phase did this fail in" then becomes a stack frame rather than a comment search, and
  each phase gets a test that constructs the dataclass directly instead of driving the whole verb.
  Pairs with `3.2.0-F4`: the update phases and the wrapper's per-package lifecycle are the same
  story seen from both ends, and the dataclass boundary between them is where the two meet.
  *Priority: med · Effort: medium · Bump: patch* — behaviour-preserving; the existing
  `tests/test_update.py` (2709 lines) is the regression net.
  **Standards home on adoption:** none new.

---

- **`3.2.0-F4` — Relocate `makepkg_wrapper` out of the leaf layer.** It is filed under
  `primitives/` but has the fan-out of an orchestrator: thirty distinct sysforge imports (tied with
  `cli.py` and the toolchain stage), three function-level reaches into `pipeline.state`, and a
  437-line `_run_build` holding the whole per-package lifecycle (prepare, invoke, artifact discovery,
  build-state recording, install). A primitive with thirty dependencies is a coordinator in the
  wrong drawer, and every primitive it imports is a candidate cycle the moment that primitive needs
  the wrapper back. Two acceptable shapes, decide at implementation: (a) promote it to a new
  `sysforge/build/` layer sitting between primitives and verbs, alongside `build_core.py`, with the
  layering test extended to forbid `primitives → build`; or (b) keep the name but split the
  lifecycle into `prepare`, `invoke`, `record` steps where `record` is the *only* writer of
  `build_state` (the one-home invariant §`sysforge/CLAUDE.md` already implies). Either way
  `BuildOptions` stays the single argument surface and `build_core.build_and_install` remains the
  sole caller. `3.2.0-F1` (a) already landed, so the `resolve_state_dir` reach-up is gone and this
  no longer has a prerequisite.
  *Priority: med · Effort: medium · Bump: patch* — internal restructuring only.
  **Standards home on adoption:** none new.

---

- **`3.2.0-F7` — Per-verb parser assembly on the `Verb` class.** `cli.py` is 1645 lines of
  argparse construction with thirty distinct sysforge imports; `_add_run_parser` alone is 278
  lines. None of it is logic, but it is the one place completions parity (§CLI Verb Framework,
  `completions/_sysforge` lockstep rule) has to be checked by eye. Move each verb's flags into an
  `add_parser(sub)` classmethod on its `Verb` subclass (the class already owns `pre_check`/
  `execute`/`post_validate` and `requires_sentinel`), so `_build_parser` becomes a loop over the
  verb registry and the flag surface lives next to the code that reads `args.<flag>`. Second
  payoff: the `completions-cli-parity` audit and `help_cmd` can walk the registry rather than
  importing `_build_parser` from `cli` (the one remaining `help_cmd → cli` reach-up).
  *Priority: low · Effort: medium · Bump: patch* — behaviour-identical; verify with the completions
  parity audit before and after.
  **Standards home on adoption:** none new.

---

- **`3.2.0-F9` — the mesa PGO rebuild suppresses its own staleness diagnostic, so a drifting profile
  is unobservable.** `mesa_pgo.use_flags` appends `-Wno-profile-instr-out-of-date
  -Wno-profile-instr-unprofiled` to every `-fprofile-use` rebuild. The intent is sound and
  documented — mild instrumentation-vs-source skew is expected after upstream churn, and a mesa built
  with `-Werror` must not fail on it. The consequence is that the *only* signal distinguishing "a
  profile with minor drift" from "a profile that no longer describes this source tree" is discarded
  before anyone can see it, unconditionally and with no floor.
  This matters more than a suppressed warning normally would, because a badly stale profile is worse
  than no profile at all: `-fprofile-use` with counters that no longer map to the current functions
  biases inlining and block layout toward paths that have moved or disappeared, so the "optimized"
  build can be slower than the stock one while reporting complete success. The failure mode is
  silent by construction.
  `3.2.0-B16` added the pkgver sidecar (`<pkgbase>.profdata.version`), which answers "how far has the *version* moved". This entry
  answers the sharper question the sidecar cannot: how much of the profile still *applies*, which
  only the compiler knows. The two are complementary — a profile can be one pkgver old and badly
  skewed, or several old and still broadly valid.
  Direction: stop discarding the diagnostic and start counting it. Demote the blanket suppressions to
  `-Wno-error=profile-instr-out-of-date` / `-Wno-error=profile-instr-unprofiled` so the warnings are
  still emitted but cannot fail a `-Werror` build, then tally them at the existing per-line callback
  in `invoke_makepkg` (`primitives/makepkg_invoke.py:456`, already the home for build-output
  classification) and carry the counts into the update summary alongside the other per-package
  actions. Verify the `-Wno-error=` form against mesa's actual `-Werror` posture before committing to
  it — if mesa promotes these specifically, the fallback is to keep the suppression and derive
  coverage out of band from `llvm-profdata show`, which is weaker (it describes the profile, not the
  profile-against-this-source) but does not touch the build's warning configuration.
  Whatever the mechanism, the deliverable is the same: a rebuild that reused a profile says how well
  it fitted, and a skew past a threshold points at `sysforge build mesa --pgo=record`.
  *Priority: med · Effort: medium · Bump: minor* — med because it is pure observability over a
  currently-invisible correctness-adjacent regression, not a wrong result; the effort is the flag
  change plus a counting seam and its summary rendering, and the risk sits in the `-Werror`
  interaction rather than the code.
  **Standards home on adoption:** none new — the summary rendering follows the existing logging
  rubric in `docs/design/12-logging.md` (the count is narration behind `-v`, the skew warning is
  `warn()`); no external spec is adopted.

---

- **`3.2.0-F10` — nothing verifies that a freshly installed locally-built mesa actually initialises.**
  The graphics guards sysforge has all check *inputs* to the build or *static* properties of the
  result: `llvm_targets.py:108` refuses to drop the backends mesa links unconditionally,
  `_ensure_mesa_software_baseline` (`mesa_drivers.py:36`) keeps a software driver in the filtered
  list, and `graphics_probe._check_mesa_llvm_symbols` differs libgallium's `LLVMInitialize*` symbols
  against the installed libLLVM. Each is well-placed. Together they still stop short of the question
  the user actually has after `pacman -U` swaps their GL stack: does it load?
  Unresolved symbols are one way a locally-built mesa fails and the one already covered. A driver
  trimmed out of the gallium/vulkan lists, a mismatched `MESA_WHICH_LLVM`, a `-march` past what the
  running CPU provides, or a PGO rebuild that miscompiled a driver entry point all produce a mesa
  whose symbols resolve cleanly and which cannot create a context — and the first report of that is
  a black screen on the next session start, after the build has been declared successful and the
  stock package replaced.
  Direction: a post-install smoke check on mesa-family pkgbases (`profile.is_mesa_pkgbase` is the
  existing gate) that creates and tears down a context — `eglinfo` / `vulkaninfo` are the obvious
  probes and follow the `_run(...)`-returning-`None`-on-absence shape already used throughout
  `gfxperf_probe.py`, so a host without them degrades to "not checked" rather than failing. Report
  through the graphics axis so `sysforge doctor --graphics` gives the same answer on demand. It must
  be advisory: the package is already installed by the time this runs, so a failure's job is to name
  the problem and point at `sysforge revert mesa` while the user still has a session, not to attempt
  an automatic rollback.
  Scope caveat, stated up front rather than discovered later: this validates only on hardware with a
  working display path, and — like the rest of the graphics axis — is exercised here on
  Nvidia/x86_64 only. On a headless host or a build server there is no context to create, so the
  check must classify that as *skipped*, distinctly from *passed*; conflating the two would make the
  guard read as coverage it does not have.
  Related: `3.2.0-F9` covers the PGO-specific quality axis; this covers the load path regardless of
  how mesa was built, including a plain `source_built` mesa with no profile involved.
  *Priority: med · Effort: medium · Bump: minor* — med because the uncovered failure modes end in a
  black screen and the recovery window is the current session; effort is the probe plus its
  skipped/passed/failed classification and the headless path, not the invocation itself.
  **Standards home on adoption:** none new — this extends the existing graphics diagnostics axis and
  its findings vocabulary; the tested-hardware scope note belongs with the other hardware-tier
  qualifications in `docs/design/`, not in `21-standards.md`.

---

- **`3.2.0-F13` — Fence the remaining direct `subprocess` use behind the run seam.** `3.2.0-F5`
  added the probe form the seam was missing (`run.capture`) and migrated the toolchain package; the
  rest of the tree still calls `subprocess.run`/`Popen`/`check_output` directly, although
  `primitives/run.py`, `pty_runner.py` and the privilege seam (`privilege.run_privileged`,
  §Privilege-Escalation Seam) exist precisely so that the unified run-log, `--dry-run`, the throttle
  preexec (`resource_guard.make_child_preexec`) and the sandbox `BUILDENV` scrub apply uniformly.
  Every raw call is a site where one of those can be bypassed — the accelerator leak fixed as
  `3.2.0-B2` was exactly such a site. Migrate the remaining call sites in batches, each
  independently green, classifying each as a probe (`run.capture`), a raise-on-failure invocation
  (`run_or_raise`) or a privileged one (`run_privileged`). Then — **last**, because it fails until
  the batches are done — add a ruff `banned-api` rule (extending the `STD8`-era config) forbidding
  `subprocess.*` outside an allowlist (`run.py`, `pty_runner.py`, `privilege.py`,
  `sudo_session.py`). Calls that legitimately stay raw get named in that allowlist rather than a
  blanket exemption: streaming invocations, anything needing its own `preexec_fn`, and build
  invocations rather than probes are the shapes `capture()` deliberately does not cover — the four
  such calls left in the toolchain package are the worked example.
  *Priority: med · Effort: large · Bump: patch* — large by count, mechanical per site; ship in
  batches, each independently green.
  **Standards home on adoption:** new `21-standards.md` row "subprocess goes through `primitives/run`",
  enforced by the ruff `banned-api` entry plus `tests/test_standards_compliance.py`.

---

- **`3.2.0-F18` — build inputs applied outside the resolved profile never register as drift, so
  changing them leaves stale packages installed silently.** Flag drift (Phase 4.3) compares only
  `serialize_effective_flags`, and `SYSFORGE_KEYS` rightly excludes the scheduling/throttle keys
  that cannot change output. But several settings *do* change the built artifact and reach the build
  through seams the profile never sees: `sysforge.toml [build] python` (the interpreter pinned by
  `makepkg_env.resolve_build_python` — a Python package built against one minor version installs
  into that version's `site-packages`, so a pin change or a system Python bump breaks it with no
  signal), `[mesa] filter_drivers`/`gallium`/`vulkan` and `toolchain.toml [llvm] targets` (both
  patched into the PKGBUILD by `_maybe_patch_mesa_drivers` / `_maybe_patch_llvm_targets`, and both
  auto-detected from the hardware profile, so a GPU swap changes them too — the hardware stage
  reports the diff but nothing links it to a rebuild), and the PGO flag spliced in via
  `effective_flags_extra` (`--pgo=use` / store reuse), which bypasses `flags_string`. Record a
  second per-package field, `build_inputs`, at `_record_build_state` — the resolved values
  themselves, not a hash, so `--explain-drift` can render a per-key diff in `flag_drift.diff_flags`'
  `+added` / `-removed` vocabulary — and re-derive it at drift time through the same resolvers
  (`resolve_build_python`, `resolve_or_detect_mesa_drivers`, `resolve_or_detect_llvm_targets`), the
  B11 one-function-both-sides rule. For Python, record the interpreter's `major.minor` rather than
  its path so a patch-level system update does not drift every package. Surface it as a sub-axis of
  flag drift (same reporting, same `--rebuild-on-flag-drift` promotion) rather than a fourth CLI
  axis; a missing field on pre-existing entries reads as "unknown, not drifted" so the upgrade does
  not flag the whole build_state at once. Out of scope: `kernel.toml` kconfig inputs (stage-owned,
  rebuilt explicitly by `run kernel`, and already diffed request-vs-result by
  `_gate2_kconfig_drift`) and the LLVM PGO/BOLT settings the toolchain stage owns; LLVM's `targets`
  lands only in the build-state-wide fold, reported with the owning-stage hint like any other
  stage-owned entry. Tests: record/re-derive parity per input, the missing-field no-drift upgrade
  path, python `major.minor` normalisation, and one gcc-path + one llvm-path case for the PGO input.
  *Priority: med · Effort: medium · Bump: minor* — med because each case is a silent-stale-artifact
  class, the python one reachable by an ordinary distro update; medium because four inputs each need
  a record site and a re-derive site, plus a build_state schema field and the fold/explain plumbing.

---

- **`3.2.0-F19` — an interactive build's failure reason is never logged.** In interactive mode
  makepkg's stdout/stderr go straight to the terminal, so neither the unified run-log
  (`sysforge-run-kernel.log`) nor the per-package log (`sysforge_<pkg>.log`) records why a build
  failed. They record only `Build failed — leaving patched PKGBUILD in place` and `Kernel stage FAILED
  before applying changes`. The 2026-09-28 `3.2.0-B36` failure could only be diagnosed after the
  fact by reading `.config` generations in the build tree. A 2026-08-15 kernel run in the same log
  failed with no reason at all. Tee makepkg's combined output into the per-package log while keeping
  the terminal attached: run makepkg under a pty (the `pty_runner` seam the non-interactive path
  already uses) and copy the stream to the log, so prompts and `nconfig` still work. On failure, repeat
  the last N lines (after `strip_ansi`) in the unified log under `[BUILD]`, and feed them to
  `build_diag._MATCHERS` as the non-interactive path does. Needs a test that a failing interactive
  build leaves its last output lines in both logs.
  *Priority: med · Effort: medium · Bump: minor* — diagnosability; every interactive-stage failure
  currently needs forensic reconstruction.
  **Standards home on adoption:** none.

---

- **`3.3.0-F1` — the `building` bar's ETA swings because it averages packages of very different sizes.**
  The live ETA from `3.2.0-F14` (`ui/progress.py`, `tracker()._suffix`) projects
  `mean(completed item durations) × remaining − in-flight elapsed`. That works for batches whose items
  are roughly the same size (source sync, fetch, version check), but not for the `building` batch
  (`build_core.py`, `tracker(len(targets), "building")`), where a 20-minute mesa sits next to
  30-second libraries. Every time a large package finishes, the mean jumps. Between ticks the figure
  counts down and then snaps to a new value, and an overrun drops it and brings it back at a
  different value, so on the batch where the ETA matters most it reads as noise. The information to
  do better is already there: `build_seconds` records each package's own last-5 durations, and
  `build_estimate.estimate_seconds` already takes a median per pkgbase for the pre-build line. The
  tracker can't use it because it only knows a *count*, not which items are coming. Fix: `tracker()`
  takes optional per-item expected durations supplied by the caller (so `ui/progress` stays generic
  and no primitive has to import `ui`). `build_and_install` passes the per-pkgbase medians in build
  order. Remaining time = the expected durations of the items not yet finished, where the in-flight
  item counts as `max(0, expected − elapsed)`. Items with no history fall back to the in-run mean.
  With history, the ETA can show from the first item instead of waiting for two completions. Keep
  the never-overstate rules (5 s floor, drop on overrun) and leave every other batch on the current
  mean projection. Tests: a mixed-size batch whose ETA stays within its per-item medians across a
  large package's completion, a no-history batch that behaves exactly as today, and a split package
  counted once. **Depends on `3.3.0-F2`** (landed): the tracker is now a `_Tracker` record whose
  `expected` field is reserved for these per-item durations, and its clock is an *active* clock that
  stops while the terminal is yielded. The recorded `build_seconds` should subtract yielded time the
  same way, or a build that sat at a recovery prompt records the wait as build time and skews every
  later median.
  *Priority: med · Effort: small · Bump: patch* — med because it is the default-verbosity readout
  on the longest-running batch and currently misleads; small because the medians and the paint-time
  suffix seam already exist, so this is a new tracker argument plus one caller.

- **`3.3.0-F3` — a sudo password prompt sysforge raises itself does not pause or hide the progress bar.**
  `3.3.0-F2` pauses an open tracker's clock and yields the terminal for every prompt that goes
  through `primitives/prompt.py`, but sudo prompts are not among them. `sudo_session.authenticate()`
  (`sudo -v`, inherited stdio) and `privilege.run_privileged`/`privileged_argv` (the `pacman -U/-S`
  from `install_built`, including the just-in-time install inside the `building` tracker) prompt
  for a password whenever the cached credentials have lapsed, with the bar still painted and its
  clock still running. Fix, in the privilege seam: probe `sudo -n true` (an auth probe, which the
  privilege-escalation guardrail permits); if credentials are not cached, run `sudo -v` inside
  `progress_hooks.hooks().yield_terminal("prompt")` before the real command. Tests: an uncached
  probe yields around the `sudo -v` and pauses an open tracker; a cached probe yields nothing.
  *Priority: low · Effort: small · Bump: patch* — low because `keepalive` keeps credentials warm
  through the long stages, so the prompt is rare; small because it is one probe-then-yield at one
  seam.

- **`3.3.0-F4` — a sudo prompt inside forwarded build output leaves the progress bar's clock running.**
  `makepkg --install`/`--syncdeps` (toolchain passes use `--install`) run sudo *inside* the child
  whose output `pty_runner` forwards, so the password prompt arrives as bytes in the stream, where
  `3.3.0-F2` cannot yield. The bar keeps counting while the build waits on the user. Fix: set a
  `SUDO_PROMPT` sentinel in the child's environment, detect it in `pty_runner`'s partial-line
  buffer, and pause the tracker clock without yielding (`ui/progress.py` already has a depth-counted
  `_pause()`/`_unpause()` independent of yielding; expose it on `ProgressHooks`) until the next
  output. Caveat: a sudoers `passprompt_override` defeats `SUDO_PROMPT`. The alternative, pointing
  `PACMAN_AUTH` at `sudo -n` so the build never prompts and relies on `keepalive`, trades a
  prompt for a hard failure on lapsed credentials; the item must choose. Tests: a fake child
  printing the sentinel pauses the clock until its next output; a child without it never pauses.
  *Priority: low · Effort: medium · Bump: patch* — low for the same reason as `3.3.0-F3`; medium
  because detection has to survive chunk boundaries and the `PACMAN_AUTH` trade-off needs a decision.
  **Standards home on adoption:** none.

### Bugs

- **`3.1.0-B12` — `update --include-stage-owned` co-schedules a toolchain rebuild with the packages
  it compiles, and stamps them all with the pre-rebuild fingerprint.**
  Stage-owned packages are filtered out of the walk by default (`update_assemble.py:120-124`), so
  the toolchain is normally rebuilt only by `sysforge run toolchain`. `--include-stage-owned` — and
  naming a toolchain package explicitly (`update_assemble.py:148`) — lifts that filter, putting
  `llvm`/`clang` into the same Phase 5 batch as everything they compile. Two things then go wrong
  in one process, no concurrency involved:
  **(a) the batch order is arbitrary with respect to the rebuild.** `build_core`'s intra-batch topo
  sort (`build_core.py:511-544`) keys on declared `depends`/`makedepends`/`checkdepends` edges
  resolved against in-batch providers. A package is *built by* clang but almost never *depends on*
  it — the compiler arrives via `CC`/`CXX` and `makepkg.conf`, not `depends=()`. On the live
  workstation only 11 of 86 non-toolchain source-built packages declare any edge on a toolchain
  package (all on `clang`, all COSMIC crates), so the other 75 keep insertion order relative to
  `llvm`: some build against the old compiler, some against the new, within one batch. The sort is
  working correctly — it has no edge to sort on.
  **(b) the fingerprint is snapshotted across the step that invalidates it.**
  `active_fingerprint` is computed once at `update.py:675`; `get_toolchain_fingerprint`
  (`pipeline/state.py:388-397`) takes the `cc` path from the toolchain stage result and fingerprints
  the binary **on disk at that moment** (`clang_identity` = path + size + mtime + version). Phase 5
  then installs a new clang, and every package built afterwards is stamped with the stale value at
  `update.py:1258`. The next run compares against a freshly-read fingerprint, finds a mismatch, and
  reports the whole set as toolchain-drifted — offering to rebuild packages that were just built
  correctly. `--rebuild-on-drift` makes that a loop.
  Not currently reachable in practice (the flag has never been used here), and the test surface
  stops at the assembly boundary: all three `include_stage_owned` tests
  (`tests/test_update.py:530, 2110, 2218`) assert only that the package enters the walk, nothing
  downstream of it. Reachable the moment the flag is used, which the `3.1.0-B11` re-hardening
  rebuild would have done.
  Fix direction is a scheduling decision, not a patch — resolve before implementing. The cheap,
  honest option is to refuse the co-schedule: detect a stage-owned toolchain package in `to_build`
  and either split it into its own pass (re-reading the fingerprint after it installs, so the
  remainder is stamped correctly) or decline the batch and point at `sysforge run toolchain`. A
  declared-edge fix is not available — the missing edges are absent by design. Whatever is chosen
  must extend the test surface past the gate.
  *Priority: med · Effort: medium · Bump: patch* — a scheduling rule plus fingerprint re-read; no
  change to what is built under the default (stage-owned-filtered) path.
  **Standards home on adoption:** none new — the toolchain-identity contract already lives with
  `get_toolchain_fingerprint` as its single canonical computation site; this constrains *when* it is
  read, not what it means.

---

- **`3.3.0-B11` — on an unchanged kernel, every run says the merge-drift check and Gate 2 "did not
  run", although the tree that built the installed package is still on disk.**
  When the kernel has not changed, makepkg exits 13 and the stage takes the AlreadyBuilt path with
  `built_dir = None` (`kernel/stage.py`, the `except AlreadyBuilt` branch). `resolve_built_config`
  (`kernel/gates.py:149`) then falls back to the **system** `BUILDDIR` keyed on the **checkout**
  name, which is exactly the guess `3.2.0-B37` removed from the fresh-build path. On the live
  workstation that guess is `/tmp/makepkg/linux`. The tree is really at the profile's `BUILDDIR`
  under the post-rename pkgbase, `~/builds/linux-sysforge/src/linux-7.2.7/.config`, and its
  `include/config/kernel.release` (`7.2.7-arch1-1-sysforge`) matches both the PKGDEST artifact and
  the running kernel. Two checks miss it on every no-change run: the advisory drift check prints
  the "Kconfig merge drift: check did NOT run" block, and Gate 2's boot audit warns that the config
  "could not be validated before install". `sysforge-run-kernel.log` shows the pair on 11 runs and
  no Gate 2 pass at all. The "did NOT run" wording from `2.6.1-B6` is correct when no tree exists.
  Here it is reported for a tree that sysforge could have found, and it shows up every run, so the
  warning becomes noise.
  A related gap: `<state_dir>/kconfig-history/` does not exist on the live system, because no fresh
  build in the logged history reached the archive step. The `2.6.1-F25` archive therefore can't
  serve as the only fallback.
  Fix: on AlreadyBuilt, derive the tree the same way the fresh path does
  (`makepkg_env.build_root(resolved_profile, pkgbase)`, post-rename pkgbase). Use it only if its
  `kernel.release` matches the release the PKGDEST artifact ships (`usr/lib/modules/<release>/`),
  because a later aborted build can leave a different `.config` in the same tree. When they match,
  run Gate 2 and the drift check against that tree, without the fresh-build hard-failure. When they
  don't match, use the `kconfig-history` archive for that release if one exists. Keep the "did NOT
  run" block only when neither source is available, and name which source was checked when it does
  run. Tests: matching tree → both checks run; mismatched release → falls through to the archive;
  no tree and no archive → the `B6` block unchanged; the fresh-build path is untouched.
  *Priority: med · Effort: small · Bump: patch* — med because Gate 2 is the brick backstop and is
  currently off on every no-change install on a real system; small because `build_root` and
  `built_kernel_release` already exist, so this is mostly a release match plus a fallback order.
  **Standards home on adoption:** none.

---

- **`3.3.0-B12` — the toolchain PGO counters reset partway through a pass, so the final pass reads
  `[1/1]` and then `[1/6]` under the same `PGO 4/4` label.**
  `passes.build_pass` opens its own `progress.tracker(total, label)` (`toolchain/passes.py:164`)
  with `total` set to that call's PKGBUILD-dir count. The pipeline splits one logical pass across
  several calls. Pass 4 is three of them: `PGO optimize · llvm/llvm-libs (PGO 4/4)`
  (`toolchain/pgo.py:716`), then `… clang/lld/... (PGO 4/4)` (`:762`), then `… lib32 (PGO 4/4)`
  (`:796`). Each one starts a fresh counter, so the operator sees `[1/1]` for llvm, then `[1/6]` for
  the rest of the suite, under one pass number. Pass 3 has the same shape (`train` and `corpus
  enrich` are both `3/4`, `:515`/`:557`), and the `reusing profdata` variant drops the pass number
  entirely (`:644`). Nothing is built wrong. The pass number and the item counter both look like
  overall progress, but they are scoped differently.
  Fix direction (pick one when implementing): **(a)** one tracker per logical pass, totalled over
  the union of its sub-maps, passed into `build_pass` as an optional `tick`. The sub-pass names
  become header lines, and the counter runs `[1/7]…[7/7]`. This needs care with
  `require_no_tracker` from `3.3.0-F2`, because `build_pass` must reuse the open tracker, not nest
  one. **(b)** keep the per-call trackers and label sub-passes `PGO 4/4 · 1/3 llvm`, `· 2/3
  clang/lld/...`, so the reset is explained instead of removed. (a) matches what the operator
  expects, and the ETA work in `3.3.0-F1` will want a per-pass total anyway. (b) is cheaper.
  Tests: a split Pass 4 produces one monotonic counter (or explicit sub-pass labels), and the
  dry-run pass headers are unchanged.
  *Priority: low · Effort: small · Bump: patch* — cosmetic, the build itself is correct; small
  because the sub-maps are already in hand at each call site.
  **Standards home on adoption:** none.

---

- **`3.3.0-B15` — completing a bare `-` under `doctor system`, `doctor pkg`, `build`, `update` or
  `run toolchain` prints a scrambled option listing; `--help` renders fine.**
  `sysforge doctor system -<TAB><TAB>` lays the option names out in one block and the descriptions
  in another, so no name sits beside its own description and `-q`/`-h` drift into a stray column.
  Reproduced under `zsh -f` with only `completions/` on `fpath`, so it is not a user style. The cause
  is two match groups with incompatible layouts: the verb's `_arguments` group, which contains a
  short/long alias pair (`'(-q --quiet)'{-q,--quiet}`, `-s/--suggest`, `-m/--makepkg`) that zsh
  renders as one aliased row, and the separate `-h`/`--help` group that `_sysforge_help_flag` adds
  through `_describe -o` on every verb (`completions/_sysforge`, `2.5.0-F2`). Neither group alone
  breaks: disabling `_sysforge_help_flag` makes the listing align with `--quiet -q` on one row, and
  verbs without an alias pair (`run kernel -`) or a `--` prefix (`doctor system --`) render
  correctly with the help group present. The affected leaves are exactly those with an alias pair:
  `_sysforge_doctor_system`, `_sysforge_doctor_pkg`, `_sysforge_build`, `_sysforge_update`,
  `_sysforge_run_toolchain`.
  Fix: keep one home for the help spec but deliver it inside `_arguments`. Define it once (for
  example an array `_sysforge_help_spec=('(-h --help)'{-h,--help}'[show this help message and exit]')`)
  and expand it into each leaf `_arguments` call, dropping the `_describe -o` group; or, if
  per-leaf expansion is judged too invasive, keep `_sysforge_help_flag` but skip it in leaves that
  already pass the help spec. Bash completion is unaffected (no descriptions). Update the
  `completions-cli-parity` expectations if the help flag moves into the specs.
  Tests: a zsh `zpty` listing test (skipped when zsh is absent) for `doctor system -` asserting each
  description line starts with its own option name; the existing completion parity checks stay
  green.
  *Priority: med · Effort: small · Bump: patch* — med because it is live on every interactive
  `doctor` completion, the verb most often completed by hand; small because the fix is one shared
  spec array and its expansion at five call sites.
  **Standards home on adoption:** none.

---

- **`3.3.0-B16` — under `kernel.toml [fdo] mode`, `state profiles` reports every applied kernel FDO
  store as `0d` old, however old its profile is.**
  `fdo.finish_fdo_build` (`kernel/fdo.py:341`) rewrites `applied.toml` after every successful `use`
  install, including an AlreadyBuilt reuse whose fingerprint is unchanged (`fdo.py:354`). With
  `[fdo] mode` set, every routine `run kernel` is such a reuse. `makepkg_pgo._store_entry` dates a
  store by the newest file in it (`makepkg_pgo.py:190`), so the bookkeeping write resets the age
  column that `docs/guides/pgo.md` tells the operator to use when deciding to re-profile.
  Fix: skip the write when `kernel_fdo.read_applied(store)` already equals the fingerprint (the
  sidecar then means "last applied", not "last run"), and date a store from its profile artifacts
  rather than its sidecars (`round.toml`, `applied.toml`), so a later bookkeeping change cannot
  reintroduce the drift.
  Tests: an unchanged-fingerprint finish leaves `applied.toml`'s mtime untouched; a changed one
  rewrites it; `list_profile_stores` age ignores sidecar mtimes.
  *Priority: low · Effort: small · Bump: patch* — the profile itself is applied correctly; only the
  age readout misleads, and both changes are local.
  **Standards home on adoption:** none.

---

- **`3.3.0-B17` — capture's stripped-vmlinux refusal tells a round-2 operator to re-record without
  `--propeller`.**
  When only a debug-stripped `-headers` copy of `vmlinux` matches the running kernel,
  `kernel_fdo.resolve_vmlinux` refuses with "re-run `sysforge run kernel --autofdo=record`"
  (`kernel_fdo.py:599-604`). In round 2 that command builds a round-1 profiling kernel (no pinned
  profile, no `CONFIG_PROPELLER_CLANG`), so following the message as printed sends the operator
  back a round. The guide's troubleshooting row adds "(add `--propeller` in round 2)", but the
  refusal is what the operator reads. `resolve_vmlinux` does not know the round; `run_fdo_capture`
  does.
  Fix: thread `propeller` into `resolve_vmlinux` (or have `run_fdo_capture` append the flag when it
  re-raises), so every recovery command in a capture refusal names the round's own flags; check the
  "build tree is gone" refusal for the same gap.
  Tests: both refusals, round 1 and round 2, assert the exact record command they print.
  *Priority: low · Effort: small · Bump: patch* — the guide already carries the right command; this
  makes the tool agree with it.
  **Standards home on adoption:** none.

---

- **`3.3.0-B18` — a failed sidecar write can report the cleanup's error instead of its own, plus
  three loose ends from the kernel FDO rework.**
  `kernel_fdo._write_sidecar` (`kernel_fdo.py:230-242`) unlinks its `.tmp` in `except
  BaseException` before re-raising. If that unlink itself raises (same directory, so the same
  permission or I/O fault is likely), the new `OSError` replaces the original as the raised
  exception, and `finish_fdo_build`'s warning names the cleanup failure rather than the write that
  failed (the original survives only as `__context__`). Fix: suppress errors from the cleanup
  (`contextlib.suppress(OSError)`) so the original always propagates; test with a store where both
  the write and the unlink fail.
  While there:
  - `fdo.building_pkgver`'s docstring says "Otherwise as `built_pkgver`" (`kernel/fdo.py:182`), but
    `built_pkgver` now prefers the patched build file and then the build manifest; say what
    `building_pkgver` actually reads (the static PKGBUILD).
  - `pkgbuild_patcher.PATCHED_PKGBUILD_NAME` exists, but three sites still spell the literal:
    `pkgbuild_patcher.py:2325`, `:2348` and `update_version.py:75`. Use the constant.
  *Priority: low · Effort: small · Bump: patch* — no observed failure; the masking only changes
  which error text a best-effort warning shows.
  **Standards home on adoption:** none.
---

- **`3.3.0-B20` — a DKMS module whose pacman-hook build failed is reported as never built, and the
  finding sends the operator to rerun the command that just failed.**
  Installing a kernel's `-headers` package fires `70-dkms-install.hook`, which runs `dkms install
  --no-depmod <mod>/<ver> -k <kver>` inside the transaction. A failed module build only makes the
  hook print a `WARNING: … exited <n>` line; pacman still exits 0, so the install looks clean.
  `kernel_safety.check_dkms_for_kernel` (`kernel_safety.py:672`, called by Gate 3 at
  `kernel/gates.py:432` and by `doctor` at `doctor.py:999`) sees only the end state (no
  `installed` row for `<kver>`) and says "not built … run `sudo dkms install …`". That reads as if
  nothing ran, and it gives no hint that a compile failed or where its output is. Observed
  2026-10-06 installing `linux-sysforge-profiling` 7.2.8 (and on 2026-09-28/29 for
  `linux-sysforge` 7.2.7): the hook build failed with exit 10, Gate 3 reported "not built", and a
  manual rerun succeeded. The cause is still unknown because the failing `make.log` was overwritten
  by the rerun and pacman's output was not kept (`3.3.0-B21`).
  Fix: when `<kver>`'s headers are present (`/usr/lib/modules/<kver>/build/include` exists), look
  in `/var/log/pacman.log` for that module/kernel's hook `exited <n>` line from the latest
  transaction. If there is one, the finding says the automatic build ran and failed, gives the exit
  code, and names `/var/lib/dkms/<mod>/<ver>/build/make.log`, warning that the next `dkms install`
  overwrites that log, so copy it before retrying. The retry command stays as the second step. With
  no headers, keep the "install headers" wording. With headers but no hook line, keep today's
  message.
  Tests: a pacman.log fixture with a failed hook line → "ran and failed" wording with exit code and
  log path; headers present with no hook line → today's wording; headers absent → install-headers
  wording; a failure line for a different kernel or module is ignored.
  *Priority: med · Effort: small · Bump: patch*: nvidia on a new kernel is a black screen at the
  next boot, and the current message hides the evidence the operator needs before rebooting.
  **Standards home on adoption:** none.

---

- **`3.3.0-B21` — kernel installs keep none of pacman's output, so a failed DKMS hook or other
  post-transaction hook leaves no record.**
  `makepkg_wrapper.install_built_packages` (`makepkg_wrapper.py:414`) runs `pacman -U` with
  inherited stdio so its prompts work. Everything pacman and its hooks print (DKMS builds,
  `mkinitcpio`, bootloader updates) goes only to the terminal, and is lost once the scrollback is.
  B7 added a "went to the terminal" note for a failing exit status, but hook failures exit 0 and
  get no note at all. The interactive path in `pacman.install_packages` (`pacman.py:661`) has the
  same gap. This is what made the `3.3.0-B20` hook failure impossible to diagnose after the fact.
  Fix: run the kernel install through `pty_runner.run_with_pty` (`reserve_bottom_rows=0` keeps
  stdin inherited, so the confirmation prompt still works) with a `line_callback` that tees each
  ANSI-stripped line into the run log, and promote `==> WARNING: … exited <n>` and `Error!` hook
  lines to `warn()` so they show up in the run summary. Extend the same capture to the interactive
  branch of `pacman.install_packages`.
  Tests: a fake pacman that prints a hook warning and exits 0 → the line is in the run log and
  raised to `warn()`; the prompt still works (stdin reaches the child); a non-zero exit still
  raises with the captured tail.
  *Priority: med · Effort: medium · Bump: patch*: the install itself is correct; this is about
  keeping evidence of brick-class hook failures (DKMS, initramfs).
  **Standards home on adoption:** none.

### Documentation

- **`3.3.0-DOC4` — the PGO guide says an unchanged kernel FDO `use` run quietly reinstalls the
  package it already built; an interactive run asks first.**
  `docs/guides/pgo.md` § Keeping it applied: "If neither the profile nor the kernel changed, it
  reinstalls the package it already built." That is what an unattended run does. An interactive
  run takes the AlreadyBuilt path through `source.resolve_already_built_action`, which shows the
  `3.2.0-B5` prompt (install as built / rebuild to review the kconfig / abort). An operator with
  `[fdo] mode` set meets that prompt on every routine `run kernel` and has no guide text explaining
  it. Fix: say both: unattended reuses the package, interactive asks, and "rebuild" there is only
  needed to review the kernel config (a changed profile already forces a rebuild, `3.3.0-B14`).
  *Priority: low · Effort: small · Bump: patch* — wording only; the behaviour is correct.
  **Standards home on adoption:** none.

### Open questions

- **`3.2.0-Q1` — should the container's config be an allowlist of what may cross, rather than a copy
  of the host's with known-bad keys subtracted?**
  `chroot_conf_text` derives the container's `makepkg.conf` by reading the *emitted host* conf and
  appending overrides, so every setting the host carries crosses the isolation boundary by default
  and is corrected only where someone has already enumerated it. The corrections are five disjoint
  sets in `primitives/build_sandbox.py`: `_CHROOT_DEST_KEYS` (host paths that must be rewritten),
  `_ENV_EXPORT_DENY` (env keys that must not travel), `_HOST_ONLY_BUILDENV` (accelerators naming
  host binaries), `_TOOLCHAIN_PACKAGE` (binaries to install instead of strip), and
  `_PROFILE_USE_PREFIXES` (data files to mirror in). Each is correct. Together they cover exactly
  the cases that have already failed.
  **The evidence that this is structural, not incidental, is the failure history.** `3.2.0-B2`
  (ccache/distcc in `BUILDENV`), `3.2.0-B4` (`CC=clang` naming an absent binary), `3.2.0-B5` (the
  chroot's own databases), `3.2.0-B6` and `3.2.0-B7` (`-fuse-ld` inside a flag string, and then in
  the conf rather than the exports) and `3.2.0-B10` (`-fprofile-use` naming a host *file*) are one
  bug six times: a host-only assumption riding into the container in the copied conf. They could
  only be found one at a time, because each was masked by the previous one failing earlier — the
  ccache error hid the missing compiler, which hid the stale databases, which hid the missing
  linker. Every fix was a new entry in one of the five sets, which is to say every fix widened the
  subtraction list without changing the reason there is one. `3.2.0-B12` was the same shape arriving
  from the write side (closed by refusing generate builds, another subtraction).
  **The question is whether inversion is worth its cost, and it is genuinely arguable.** An
  allowlist — name the keys the container may receive, drop everything else — makes a new host-only
  setting a *default-deny* rather than a silent leak, and turns the failure mode from "cryptic build
  error six layers in" into "sysforge did not pass X". That is the same refuse-rather-than-downgrade
  principle preflight already applies (`3.2.0-B1`), extended from availability to configuration.
  Against it: makepkg's conf surface is large and not stable across devtools releases, so an
  allowlist has its own maintenance burden and its own failure mode — a legitimate setting silently
  *not* crossing, which is quieter than the bug it replaces and would show up as an unexplained
  build-output difference rather than an error. It would also be a behaviour change for existing
  sandbox users, whose builds currently inherit conf keys nobody has enumerated on either list.
  A middle option exists and may be the real answer: keep the copy, but add a **preflight audit**
  that walks the emitted conf for absolute host paths and unresolvable binary names and refuses on
  anything unrecognised — default-deny on the *detectable* subset without having to enumerate the
  whole conf surface. That reaches the six bugs above (every one named a path or a binary) at a
  fraction of the cost.
  Resolve by deciding which of the three models the sandbox commits to, then promote to an `F`
  (allowlist or audit) or move to `docs/ROADMAP-ABANDONED.md` with the rationale if the current
  subtract-known-bad model is judged good enough. Do not implement straight off this entry. Note
  the sandbox is default-off and now works for the profiles it has been exercised against, so
  nothing forces the question today; the trigger to revisit is a seventh entry in this class.
  *Priority: low · Effort: medium · Bump: minor* — low because the current model is functional and
  the feature is opt-in; the effort is the model decision plus a preflight pass and its tests, not
  the token lists themselves.
  **Standards home on adoption:** none new — this changes how the container's configuration is
  derived, and `docs/design/11-makepkg-wrapper.md` already owns that description; the isolation
  boundary itself remains a `[security]` opt-in, not an external spec.

- **`3.1.0-Q1` — should sysforge have an opinion about kernel hardening, or is that outside a build tool's remit?**
  sysforge builds kernels from `kernel.toml` fragments, so the Arch wiki's
  [Security](https://wiki.archlinux.org/title/Security) *Kernel hardening* section is squarely inside
  the surface it already touches: `lockdown=integrity`, `module.sig_enforce=1` /
  `CONFIG_MODULE_SIG_ALL`, `kernel.kptr_restrict`, BPF hardening (`kernel.unprivileged_bpf_disabled`,
  `net.core.bpf_jit_harden=2`), and the ASLR sysctls (`vm.mmap_rnd_bits`). A shipped `hardened`
  fragment is mechanically trivial next to what the kernel stage already does — which is exactly why
  this is filed as a question rather than a feature: the cost is not implementation.
  Two things have to be decided first. **The DKMS conflict is real and load-bearing on the systems
  sysforge targets.** Signed-module enforcement blocks locally-compiled out-of-tree modules, which is
  the normal case for a workstation running DKMS drivers; shipping a fragment that silently makes the
  next boot lose its graphics driver is a worse outcome than shipping nothing. Any hardening fragment
  therefore has to either detect installed DKMS modules and refuse, or carry the signing-key
  machinery to enrol them — and the second is a substantially larger project than the fragment.
  **The scope question is the deeper one:** the sysctl half is not a build-time concern at all. Making
  it sysforge's business turns a build tool into a system-policy tool, and `20-scope.md` currently
  draws that line deliberately. There is a defensible middle — own the compile-time `CONFIG_*` half,
  since sysforge already decides kernel config, and stay out of `/etc/sysctl.d` entirely — and that
  split is the most likely resolution, but it is a scope call, not an implementation detail.
  Resolve by deciding the scope boundary first, then promote the surviving half to an `F` (or move
  this to `docs/ROADMAP-ABANDONED.md` with the rationale). Do not implement straight off this entry.
  Related: `3.0.0-F1` is the existing precedent for the kernel stage preflighting a `CONFIG_*`
  requirement rather than silently proceeding; the build sandbox (shipped) is the precedent for an
  opt-in `[security]` key that is refused rather than silently downgraded when unavailable.
  *Priority: med · Effort: medium · Bump: minor* — worth deciding rather than leaving implicit, and
  the likely landing is an additive opt-in fragment; effort is the decision plus the DKMS-detection
  guard, not the config tokens themselves.
  **Standards home on adoption:** deferred to promotion — if the `CONFIG_*` half lands, the Arch
  wiki *Security* page becomes a scope citation in `docs/design/20-scope.md` alongside
  `System_maintenance` and `General_recommendations`, **not** a `21-standards.md` row: that table's
  **enforced** column commits to a named mechanism, and the page is far too broad to enforce whole.

- **`3.2.0-Q2` — what is the unit of "stop maintaining this": a pkgname, a pkgbase, or a policy
  decision that outlives both?**
  `3.2.0-B15` (shipped) fixed the mechanical miss — the reconcile not matching a renamed or
  split-sibling key. It does not settle the model question underneath it, which is what "sysforge no longer owns this
  package" should actually mean, and the current answer is three different things in three places.
  **`state forget` is pkgbase-wide.** It deletes the named entry plus every entry whose `pkgbase`
  equals the name (`state_cmd.py`), so forgetting `mesa-sysforge` drops `mesa-docs-sysforge` too.
  **`revert` is pkgname-wide for the install and pkgbase-wide for the state.** `plan_revert`
  (`revert_cmd.py:61`) resolves exactly one target to one `RevertPlan` and reinstalls exactly one
  stock package, then calls `cmd_state_forget` with the renamed pkgname — which sweeps the siblings'
  *records*. So `sysforge revert mesa` leaves `mesa-docs-sysforge` installed on disk but no longer
  tracked: an untracked optimized artifact that no longer matches any stock package and that nothing
  will ever update. **`update` is neither** — it re-derives ownership from scratch each run
  (`update_assemble.py:110`) from four independent sources, of which build_state is only one.
  **The three candidate models are genuinely different, not refinements of each other.**
  *(1) pkgbase-atomic* — a revert plans the whole split set, reinstalling every stock counterpart
  that has one and removing every renamed member that does not. Matches the unit sysforge actually
  builds in, and is the only model under which the disk state after a revert is a state pacman could
  have produced on its own. Costs a plan that can fail partway through a multi-package transaction,
  where today each target is one atomic `pacman -S`.
  *(2) pkgname-precise, with refusal* — keep the per-package unit, but make `plan_revert` detect
  surviving siblings and refuse (or require `--pkgbase`) rather than leave a half-reverted split.
  Cheapest, and honest, but it hands the user a manual step for something sysforge knows how to do.
  *(3) a persistent opt-out* — record "user reverted this, do not re-adopt" and have assembly honour
  it, rather than relying on the absence of a build_state entry to mean the same thing. This is the
  only one of the three that also answers the policy leg: `state forget` and `revert` both clear
  *state* while `packages.toml` carries *policy*, and a non-inert entry there re-targets the package
  on the next run regardless of what was forgotten (`behavior_overridden`, `update_assemble.py:88`).
  Neither command reads `packages.toml`, and neither warns that an override will undo the forget.
  **Scope note, checked rather than assumed:** the policy leg is not currently live on this
  workstation — `~/sf-config/packages.toml` sets `repo_mode = "pacman"` and carries no `mesa` entry,
  so mesa is tracked purely through build_state and the shipped `3.2.0-B15` alone restored correct
  behaviour here. The leg is reachable for anyone who has used `packages add` on a package they later revert,
  and it is the reason model (3) is on the list at all; it is not what caused the observed failure.
  Resolve by choosing the unit first — that choice determines whether the policy leg needs a new
  persisted concept or only a warning — then promote to an `F` (pkgbase-atomic revert, or the opt-out
  record) or a `B` (refuse-on-surviving-sibling, if the precise model is kept). Do not implement
  straight off this entry. Related: `3.2.0-B15` was the mechanical prerequisite and has shipped
  regardless of which model wins; `uninstall_cmd` shares `resolve_installed_name` and would follow the
  same unit.
  *Priority: med · Effort: medium · Bump: minor* — med because the half-reverted split is reachable
  through the supported `revert` path today and leaves an artifact nothing maintains, but it is
  recoverable by hand and no data is lost; the effort is the model decision plus a revert planner
  that spans a pkgbase, not the plumbing.
  **Standards home on adoption:** none new — ownership and the rules-vs-state split are already
  described in `docs/design/`, and this refines what that split's boundary means rather than adopting
  an external spec.
