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
| `3.1.0-F1` | a clean diagnostics axis reports nothing, so it reads as a broken axis | med | medium | minor |
| `3.1.0-F3` | no way to declare an AUR-free posture; update reaches for the AUR unconditionally | med | medium | minor |
| `3.2.0-F9` | the mesa PGO rebuild suppresses its own staleness diagnostic, so a drifting profile is unobservable | med | medium | minor |
| `3.2.0-F10` | nothing verifies that a freshly installed locally-built mesa actually initialises | med | medium | minor |
| `3.2.0-F18` | build inputs applied outside the resolved profile never register as drift, so changing them leaves stale packages installed silently | med | medium | minor |
| `3.2.0-F19` | an interactive build's failure reason is never logged | med | medium | minor |
| `3.1.0-Q1` | should sysforge have an opinion about kernel hardening, or is that outside a build tool's remit? | med | medium | minor |
| `3.2.0-Q2` | what is the unit of "stop maintaining this": a pkgname, a pkgbase, or a policy decision that outlives both? | med | medium | minor |
| `3.2.0-Q1` | should the container's config be an allowlist of what may cross, rather than a copy of the host's with known-bad keys subtracted? | low | medium | minor |
| `3.1.0-F10` | a sandboxed build links against repo versions, not the versions the host runs | low | large | minor |
<!-- END roadmap-table -->

### Features

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

### Bugs

### Documentation

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
