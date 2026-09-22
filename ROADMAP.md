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
`1.2.0-Q1` (open question), `1.2.0-STD1` (standards). The version prefix is the
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
right after a release. Run `make next-id TYPE=F` (or `B`/`Q`/`STD`) — it reads the
current `pyproject.toml` version, scopes to that cycle's counter, and prints the next
free ID (e.g. `2.4.0-F1`). `make check-standards` also flags collisions and
active-cycle sequence gaps.

Within each subsection, entries are kept in **ascending ID order** (by type
counter, then version) — sort on every add so the list stays scannable. Every
entry opens `- **`<ID>` — <title sentence>.**` and is separated from its
neighbour by a `---` rule, so a single item is readable on its own rather than
running into the next one. `docs/release-notes/` entries use the same two rules.

**Open questions (`Q`) must be resolved before any implementation.** A `Q`
entry is undecided by definition; investigation/spikes to inform the decision
are fine, but before writing production code the question must first be either
**promoted** to a proper `F`/`B`/`STD` entry (which then follows the normal
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
| `3.2.0-B12` | a --pgo=generate build under the sandbox writes its profiles into the container and loses them at teardown | med | small | patch |
| `3.2.0-B16` | the mesa PGO store has no version sidecar, so an arbitrarily old profile is reused with no compatibility gate | med | small | patch |
| `3.2.0-B24` | four toolchain stage tests take the host's real PGO build lock | med | small | patch |
| `3.1.0-F2` | no supported way to feed last run's failures back into a retry | med | small | minor |
| `3.1.0-F8` | missing validpgpkeys are fetched from a keyserver unattended, which turns a trust assertion into a rubber stamp | med | small | minor |
| `3.2.0-F15` | a built package can place config where its consumer will never read it, and nothing looks | med | small | minor |
| `3.1.0-B12` | update --include-stage-owned co-schedules a toolchain rebuild with the packages it compiles, and stamps them all with the pre-rebuild fingerprint | med | medium | patch |
| `3.1.0-F1` | a clean diagnostics axis reports nothing, so it reads as a broken axis | med | medium | minor |
| `3.1.0-F3` | no way to declare an AUR-free posture; update reaches for the AUR unconditionally | med | medium | minor |
| `3.2.0-F3` | Make update's phases functions, not comments | med | medium | patch |
| `3.2.0-F4` | Relocate makepkg_wrapper out of the leaf layer | med | medium | patch |
| `3.2.0-F9` | the mesa PGO rebuild suppresses its own staleness diagnostic, so a drifting profile is unobservable | med | medium | minor |
| `3.2.0-F10` | nothing verifies that a freshly installed locally-built mesa actually initialises | med | medium | minor |
| `3.1.0-Q1` | should sysforge have an opinion about kernel hardening, or is that outside a build tool's remit? | med | medium | minor |
| `3.2.0-Q2` | what is the unit of "stop maintaining this": a pkgname, a pkgbase, or a policy decision that outlives both? | med | medium | minor |
| `3.2.0-F13` | Fence the remaining direct subprocess use behind the run seam | med | large | patch |
| `3.2.0-B21` | installing a libLLVM consumer outside the toolchain stage is never re-verified against the installed libLLVM | low | small | patch |
| `3.2.0-B22` | the mesa_llvm_symbols remediation misdiagnoses a consumer built against the wrong libLLVM | low | small | patch |
| `3.2.0-F11` | the profile store has no inspection or reclamation surface, and the purge path its own docstring promises does not exist | low | small | minor |
| `2.6.1-F27` | Install stage target-root change summary | low | medium | patch |
| `3.0.0-F1` | Preflight the Rust toolchain when the kernel fragment requests CONFIG_RUST | low | medium | patch |
| `3.2.0-F7` | Per-verb parser assembly on the Verb class | low | medium | patch |
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

- **`3.1.0-F2` — no supported way to feed last run's failures back into a retry.**
  `sysforge state failed` already knows the exact set a user wants to retry with different flags
  (`state_cmd.py:608`, `StateFailedVerb`), and both `build` and `update` take multiple positional
  package names — but the two cannot be composed, because `state failed` renders only a padded
  human table (PKGBASE / FAILED_AT / SIGNATURE / ERROR) through the pager. There is no
  machine-readable output mode anywhere in the CLI: no `--format`, `--json`, `--porcelain`, or
  `--plain` on any verb. The retry loop after a partial `update` — three `cosmic-*` packages fail
  on `makepkg` exit 4, and the user wants exactly those three rebuilt with `--makepkg=-f
  --interactive` — therefore has no supported spelling. The workarounds are both bad: scraping the
  table with `awk` couples a shell one-liner to column padding, and reading `build_state.toml`
  directly (`[packages.<pkgbase>]` entries carrying `failed_at`) couples it to a schema
  `primitives/build_state.py` documents as internal. Add a bare-names mode to `state failed` —
  pkgbases only, one per line, no header, no pager, implying `--no-pager` — so
  `sysforge build $(sysforge state failed --quiet)` is the supported spelling. Scope it to this
  verb rather than opening a CLI-wide `--format` axis: `state failed` is the one place whose output
  is *already* a set of package names, and a general machine-output standard is a much larger
  decision than this gap needs. The flag name matters — the global `--quiet` (verbosity 0) already
  exists, so reuse it only if the verb-local meaning composes with it, otherwise pick a distinct
  name rather than overloading it.
  *Priority: med · Effort: small · Bump: minor* — a gap that is currently active on a real system
  (three failures recorded right now with no supported retry path); new flag plus a render branch,
  no change to build_state or to the existing table.
  **Standards home on adoption:** none new — but if this ever grows into a CLI-wide machine-output
  mode, that *would* need a row; keep this one verb-local so it does not pre-empt that decision.

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

- **`3.1.0-F8` — missing `validpgpkeys` are fetched from a keyserver unattended, which turns a trust assertion into a rubber stamp.**
  `import_pgp_keys` (`primitives/build_prep.py:148-197`) runs a three-step strategy — bundled
  `keys/pgp/*.asc`, then a keyring check, then `gpg --recv-keys` for whatever is still missing —
  and the third step is unconditional: no prompt, no fingerprint echoed for review, no policy gate,
  and only a `warn` if it fails. `validpgpkeys` exists so a *human* decides that a given fingerprint
  is the upstream signer; importing every fingerprint the PKGBUILD happens to name inverts that.
  A tampered PKGBUILD that adds its own key alongside a re-signed source tarball gets that key
  silently installed, and makepkg's signature verification — the check the whole scheme rests on —
  then **passes**. That is the same PKGBUILD-tampering vector as the AUR supply-chain campaigns, and
  it is the mechanism sysforge's own `SECURITY.md` release-integrity story depends on: the AUR
  `sysforge` package ships a detached `.asc` and declares the maintainer key in `validpgpkeys`
  precisely so a mismatch aborts. The import is also permanent and unrecorded — the user's keyring
  grows with no log of what sysforge added, for which package, or how to undo it.
  Two seams already exist to hang the fix on. `[security]` (`sysforge.toml:243`) is the home for the
  policy key, and `net_policy.py:46-48` is the home for the egress kind: it splits `KIND_AUR_CLONE`
  from `KIND_REPO_CHECKOUT` "because a future policy may permit one while denying the other", and a
  keyserver fetch is a third kind that is currently *unclassified* — so `freeze_sources`, which is
  documented as refusing "all source downloads", does not actually stop it. Land it as: classify the
  fetch as its own egress kind so the freeze covers it; print the fingerprint, key owner, and
  requesting pkgbase before importing; and gate the import on confirmation by default, with a policy
  key for users who want the current unattended behaviour in batch runs. Bundled-key import (step 1)
  and the keyring check (step 2) are unaffected — only the network fetch needs consent.
  *Priority: med · Effort: small · Bump: minor* — a real weakening of a verification path rather
  than active friction, so not `high`; one new egress kind, one policy key, and a prompt at a single
  call site, with the surrounding strategy untouched.
  **Standards home on adoption:** the egress-kind vocabulary in `net_policy.py` is the existing home
  and this extends it, as `3.1.0-F3` also does; the consent prompt follows the established
  `primitives/prompt.py` TTY-only shape, so a non-TTY run must fail closed rather than auto-import.

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
  `3.2.0-B16` adds the pkgver sidecar, which answers "how far has the *version* moved". This entry
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

- **`3.2.0-F11` — the profile store has no inspection or reclamation surface, and the purge path its
  own docstring promises does not exist.**
  `makepkg_pgo.py:17` states that the optimization methods share one on-disk root "so a single
  `fs_provision.ensure_writable_dir` / purge path covers all of them". The provision half is real.
  **The purge half was never written** — there is no purge function in `makepkg_pgo`, no caller
  anywhere in the tree, and no CLI surface that reads or reclaims the store. The docstring describes
  an intent, not the code, and has done since the shared root was introduced; the shared-root helper
  it justifies (`resolve_profile_store_root`) currently has no consumer outside its own module.
  So the store is write-only from the user's side. Nothing lists what is in it, how old any of it is,
  or which package a given profile belongs to, and nothing reclaims it. On this workstation that is
  `llvm-pgo` 35M plus `pgo-mesa` 6.9M, with `autofdo`, `propeller` and `build-cache` seeded empty —
  modest, but the number is unknowable without `du`, which is the actual complaint.
  **The lifecycle question is settled and is not what this entry proposes.** Removing a package must
  *not* purge its profile: `mesa_pgo.reuse_profdata` treats the merged profile's existence as the
  durable record that this host opted into PGO, so deleting it on uninstall would silently downgrade
  the package to an unprofiled build on its next rebuild — precisely the regression that function
  exists to prevent. Reverting mesa to stock for a bisect is ordinary, and it must not cost hours of
  collected workload. `/var/cache` licenses staleness rather than obliging reclamation. Deletion
  therefore stays an explicit user decision, and this entry is about *making that decision
  possible*, not about automating it.
  Three pieces, in descending value. **(a) Inspection** — a read-only `sysforge state profiles`
  listing method, target, size and mtime per store, slotting beside the existing `state list` /
  `failed` / `repair` / `forget` verbs, which are already the home for "what does sysforge think it
  is tracking". Pairs naturally with `3.2.0-B16`'s sidecar: once collected-pkgver is recorded, the
  listing is where it becomes visible. **(b) Explicit reclamation** — `--purge <method>[/<target>]`
  over `resolve_method_store`, refusing an unknown method rather than guessing, and confirming
  before deleting anything a user cannot regenerate without a workload. **(c) An orphan warning** —
  when `revert`/`uninstall` drops a package whose store is non-empty, say so and name the path.
  Today that store goes silently orphaned; a one-line notice is the whole fix and needs none of (a)
  or (b) to land.
  Whatever lands, the stale docstring is corrected in the same change — either by the purge path
  existing or by the sentence no longer claiming it does. Related: `3.2.0-B16` and `3.2.0-F9` both
  make the store's *contents* legible; this makes the store itself legible.
  *Priority: low · Effort: small · Bump: minor* — low because nothing is broken and the disk cost is
  small, so this is discoverability rather than repair; small because (a) and (b) are thin readers
  over `resolve_method_store` and (c) is a notice at two existing call sites.
  **Standards home on adoption:** none new — `/var/cache` as the home for regenerable profile data is
  already the recorded FHS rationale in `makepkg_pgo`, and this adds a surface over that placement
  rather than changing it.

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

- **`3.2.0-F15` — a built package can place config where its consumer will never read it, and nothing
  looks.** `cosmic-greeter-git` at AUR commit `4268a07` installs its PAM file with `install -Dm644
  … -t "$pkgdir/etc/pam.d/cosmic-greeter/"`, so the payload carries the *directory*
  `/etc/pam.d/cosmic-greeter/` holding `cosmic-greeter.pam`, where PAM resolves a service name to the
  flat **file** `/etc/pam.d/<service>`. PAM found a directory, fell through to `/etc/pam.d/other`
  (bare `pam_unix`, no `pam_systemd`), `XDG_RUNTIME_DIR` was never created for uid 968, and
  `cosmic-comp` panicked `RuntimeDirNotSet` on every greeter restart until `start-limit-hit` — an
  unbootable desktop from a one-flag packaging slip. Authentication still *succeeded* the whole time,
  so nothing in the build, in `pacman -Qkk` (which verifies the payload against its own manifest, not
  against the consumer's lookup rules) or in the install logged anything.
  The generalisation is a **payload-layout lint**: a small table of directories whose consumer reads
  flat files only — `/etc/pam.d`, `/etc/sudoers.d`, `/usr/lib/sysusers.d`, `/usr/lib/tmpfiles.d`,
  `/etc/ld.so.conf.d` — checked for members nested a level deeper than the consumer will ever look.
  Deliberately *not* a general `/etc` opinion: the rule fires only where a spec says the directory is
  non-recursive, which is what keeps it free of false positives on the many config trees that
  legitimately nest.
  The seam already exists and wants no new machinery. `report_post_build_abi` is invoked non-fatally
  at `makepkg_wrapper.py:1519` over `_find_artifacts(...)`, and `abi_check._list_sos_in_pkg` already
  shells `bsdtar -t -v -f <pkg>` and parses the member table. This is the same listing with a
  different predicate — no extraction, no ELF work, one archive walk. It belongs beside the ABI check
  as a second non-fatal post-build report, warning and naming the offending member; it must not fail
  the build, because a layout the table does not model is a false positive and a build is expensive.
  Scope note: this catches the defect in *our own* build output before install. It does not and
  should not attempt to fix upstream — `-git` packages track a maintainer's HEAD, and the standing
  posture is to report upstream rather than carry a local PKGBUILD delta that conflicts on every
  push. The value here is that the next such slip is named at build time instead of diagnosed from a
  panicking compositor.
  *Priority: med · Effort: small · Bump: minor* — med because it is observability over a class that
  has now cost a real desktop outage once, not a correctness fix; small because the archive listing,
  the report seam and the non-fatal convention are all in place, leaving a directory table and a
  predicate.
  **Standards home on adoption:** new `21-standards.md` row naming the non-recursive config
  directories and their specs (`pam.d(5)`, `sudoers.d` via `sudoers(5)`, `sysusers.d(5)`,
  `tmpfiles.d(5)`, `ld.so.conf(5)`), enforced by the lint's own tests — the row is the table's
  citation, so it lands with the check rather than ahead of it.


### Bugs

- **`3.2.0-B12` — a `--pgo=generate` build under the sandbox writes its profiles into the container
  and loses them at teardown.**
  `3.2.0-B10` carries the profile-*use* files into the chroot: `host_profile_data` collects
  `-fprofile-use` / `-fprofile-instr-use` / `-fprofile-sample-use` and `provision_profile_data`
  mirrors each at its host path, so the flag already baked into the conf resolves inside the
  container. The `-fprofile-*generate` family was deliberately left out of that fix, because it is
  the opposite direction of travel: the value names an **output directory**, so there is nothing to
  copy *in*, and carrying it would have widened a bug fix into a feature. That leaves the gap this
  entry records. `mesa_pgo.generate_flag` returns `-fprofile-generate=<store>` with `<store>` an
  absolute host path under `[toolchain] profile_store_root`; under the sandbox the instrumented
  binary is told to write there, the path is inside the container, and `makechrootpkg -c` discards
  the working copy when the build ends. The raw profiles are gone before anything can merge them.
  **The failure mode is silence, which is what makes it worth an entry.** Nothing errors: the build
  succeeds, the package installs, and the subsequent `--pgo=use` finds an empty store and either
  falls back to an unprofiled build or reuses a stale `.profdata` from an earlier host-path run
  (`mesa_pgo.reuse_profdata` treats the *existence* of a merged profile as the durable opt-in
  signal, so a stale one is indistinguishable from a current one). The user asked for a profiled
  rebuild and got neither the profile nor a diagnostic. This is the same class the sandbox's
  refuse-rather-than-downgrade rule exists to prevent — `3.2.0-B4` rejected falling back to the
  chroot's gcc for exactly this reason — but the existing guards do not catch it, because every
  probe added so far asks "is this input present?" and a generate flag has no input to be absent.
  **The fix is a direction decision, not a patch.** Three options, in rising cost. *Refuse:* detect
  a `-fprofile-*generate` token when `[security] sandbox_builds` is on and raise
  `SandboxUnavailable` naming the conflict — honest, cheap, consistent with how the sandbox already
  treats what it cannot honour, and it leaves `--pgo=generate` simply unavailable under the sandbox.
  *Extract:* let the build write inside the container and copy the store back out before teardown —
  which needs a teardown hook the sandbox path does not currently have, and has to survive a
  **failed** build, since a partial profile is still worth keeping. *Bind-mount:* give the store a
  writable mount so the container writes straight to the host path, which is the only option that
  makes the sandboxed and host paths behave identically, and is also the one that puts a writable
  host directory inside the isolation boundary — a `[security]` opt-in that quietly opens a write
  channel needs its own justification, not an inherited one. Recommend refusing first and revisiting
  the other two only if `--pgo=generate` under the sandbox turns out to be wanted; do not implement
  the mount without settling the isolation question.
  Reproduce with `sysforge build mesa-sysforge --pgo=generate` under `[security] sandbox_builds =
  true`, then check `<profile_store_root>/pgo-mesa` for `.profraw` files.
  *Priority: med · Effort: small · Bump: patch* — small on the recommended (refuse) path: a token
  scan next to the existing `host_profile_data` probe plus a preflight refusal and its tests; the
  extract and mount options are medium and large respectively and are not what this is tagged for.
  **Standards home on adoption:** none new — this constrains when the existing sandbox refusal fires,
  and the profile-store contract already has its single home in `primitives/mesa_pgo.py`.

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

- **`3.2.0-B16` — the mesa PGO store has no version sidecar, so an arbitrarily old profile is reused
  with no compatibility gate.**
  The two instrumentation-PGO stores under the shared `/var/cache/sysforge` root are asymmetric in a
  way nothing justifies. `llvm-pgo` writes a `clang.profdata.version` sidecar at merge time
  (`pipeline/stages/toolchain.py:1635`, `_write_profdata_version`) recording the LLVM major the
  profile was collected against, and `makepkg_pgo._resolve_pgo_state` gates reuse on it — returning
  an explicit `("mismatch", "profdata is from LLVM N, building LLVM M")` rather than feeding a
  stale profile to the build. `pgo-mesa` writes no sidecar, and its reuse check is a bare
  `pd.is_file()` existence test (`mesa_pgo.reuse_profdata`).
  That existence test is load-bearing, which is what turns the missing sidecar from an inconsistency
  into a defect. `reuse_profdata` documents the presence of a merged profile as "the durable signal
  that this host opted into PGO", so a plain `sysforge update` rebuild silently re-applies
  `-fprofile-use` with whatever is on disk. There is no upper bound on how old that is, and no point
  at which the reuse becomes conditional.
  **Live on this workstation:** `/var/cache/sysforge/pgo-mesa/mesa.profdata` is dated `2026-06-26`,
  collected against mesa `1:26.1.3-2`. `pacman.log` records mesa-sysforge rebuilds at `26.1.4-1`
  (07-03), `26.1.5-1` (07-30), `26.1.6-1` (08-01) and `26.1.7-1` (08-19) — four minor versions
  consumed against that one profile, none of them mentioning it.
  The fix is to mirror the pattern that already exists rather than invent one: write
  `<pkgbase>.profdata.version` next to the merged profile in `mesa_pgo.merge_profraw` (the single
  place a merge is produced), recording the pkgver the `.profraw` was collected against, and consult
  it in `reuse_profdata`. The verdict should differ from the LLVM one deliberately — an LLVM major
  mismatch is a hard `mismatch` because the profile format tracks the compiler, whereas mesa skew is
  a gradual source-level drift, so this warns and proceeds, naming the collected and target versions
  and pointing at `sysforge build mesa --pgo=record`. A profile with no sidecar (every profile
  collected before this lands, including the live one above) must be treated as *unknown-age* and
  warned once, never as current and never as invalid — silently discarding a profile the user spent
  a workload collecting would be a worse regression than the one being fixed.
  Needs a test per verdict: sidecar matching the target pkgver, sidecar older, and sidecar absent.
  Related: `3.2.0-F9` covers the complementary axis (how much of the profile still *applies*, which
  the sidecar cannot answer); this one covers only how far the version has moved.
  *Priority: med · Effort: small · Bump: patch* — med because the outcome is a silently degraded
  optimized build rather than a broken one, and it is active here; the change mirrors an existing
  sidecar implementation into a second store, plus three tests.
  **Standards home on adoption:** none new — `makepkg_pgo`'s module docstring already declares itself
  the home for profile-store resolution and the profdata version sidecar; this makes the second
  tenant of that root obey the contract the first one already follows.

- **`3.2.0-B21` — installing a libLLVM consumer outside the toolchain stage is never re-verified
  against the installed libLLVM.**
  `toolchain_safety.check_installed_consumer_symbols` is the post-install fact that every libLLVM
  consumer still resolves its `LLVM_<ver>` symbols, but only two places ask it: the toolchain stage
  (after *libLLVM* changes underneath its consumers) and `doctor` (on demand). The reverse direction,
  where a *consumer* changes on top of an unchanged libLLVM, has no post-install check at all. That is
  how the 2026-09-11 `mesa-sysforge` 26.2.2 install reached a reboot: `sysforge update` installed it,
  nothing re-read the result, and the first signal was cosmic-comp failing to find an EGL device. The
  `doctor` run that named the cause (`mesa_llvm_symbols`) came after the desktop was already down.
  `3.2.0-B20` now refuses the known shape of this before `pacman -U`, from the package archive. This
  entry is the post-install backstop for what an archive scan cannot see: a consumer whose break
  depends on the *installed* file set, such as a split sub-package pulling a different soname or a
  JIT-installed dep landing mid-batch. **Fix shape:** after `build_core.install_built`, when any
  installed pkgname links `libLLVM` (or *is* a toolchain-owned package), run
  `check_installed_consumer_symbols` and surface a finding as an error naming the consumer and the
  snapshot/`pacman -U` of the prior version to restore. Needs a test that installs a consumer whose
  symbols are unresolved and asserts the error, plus one clean install that emits nothing.
  *Priority: low · Effort: small · Bump: patch* — low because `3.2.0-B20` blocks the observed case
  pre-install; this catches the residue, and the check already exists and only needs a second call
  site.
  **Standards home on adoption:** none new — reuses the existing toolchain post-install symbol fact.

- **`3.2.0-B22` — the `mesa_llvm_symbols` remediation misdiagnoses a consumer built against the
  wrong libLLVM.**
  `toolchain_safety.check_installed_consumer_symbols` explains every finding one way: "the PGO
  libLLVM inlined away the weak std:: copies the stock build globbed into the LLVM_<ver> node —
  rebuilding the consumer re-links them to libstdc++". That fits one cause, a libLLVM rebuilt under
  an existing consumer. On 2026-09-13 `doctor` said the same about a mesa that had instead been
  *built* against the repo libLLVM inside a sandbox container, and there "rebuild the consumer" is
  wrong advice. Before `3.2.0-B18`/`B19` it reproduced the identical broken package. The message
  should say which of the two happened: compare the consumer's build date with the installed
  libLLVM's install date from the local DB. If the consumer is newer, it was linked against a
  different build of libLLVM, so tell the user to check how it was built (sandbox dep injection) and
  restore the stock package in the meantime. Otherwise keep today's wording. Needs a test per branch.
  *Priority: low · Effort: small · Bump: patch* — diagnostic accuracy only; the finding itself is
  already correct and at error severity.
  **Standards home on adoption:** none.

- **`3.2.0-B24` — four toolchain stage tests take the host's real PGO build lock.**
  `pgo.pgo_lock_path` puts the lock in the parent of `staging1`, and `staging1` defaults to
  `/var/tmp/sysforge-llvm-stage1`. Four tests in `tests/test_stage_toolchain.py` override `staging`
  and `pgo_store` but not `staging1` (`..._four_passes`, `..._sidecar_persists_after_build_failure`,
  `..._redirects_dyld_when_clang_staged`, `test_validate_pgo_environment_runs_before_instrument`).
  So they lock the real `/var/tmp` file. While a real `sysforge run toolchain` was running on
  2026-09-19, all four failed with "Another sysforge PGO build is running (pid …)". The tests break
  whenever a real build is running, and a test run could also block a real build from starting.
  Fix: point `staging1` (and `staging3`) at `tmp_path` in those tests. Better, have the autouse
  isolation fixture in `tests/toolchain_helpers.py` redirect `pgo_lock_path` so no toolchain test
  can reach the host path. Add a guard test that fails if a resolved lock path falls outside
  `tmp_path`.
  *Priority: med · Effort: small · Bump: patch* — test isolation only; no user-visible change.
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
  subtraction list without changing the reason there is one. `3.2.0-B12` is the same shape arriving
  from the write side.
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
