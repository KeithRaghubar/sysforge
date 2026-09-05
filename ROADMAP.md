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
| `3.1.0-F2` | no supported way to feed last run's failures back into a retry | med | small | minor |
| `3.1.0-F8` | missing validpgpkeys are fetched from a keyserver unattended, which turns a trust assertion into a rubber stamp | med | small | minor |
| `3.1.0-B12` | update --include-stage-owned co-schedules a toolchain rebuild with the packages it compiles, and stamps them all with the pre-rebuild fingerprint | med | medium | patch |
| `3.1.0-F1` | a clean diagnostics axis reports nothing, so it reads as a broken axis | med | medium | minor |
| `3.1.0-F3` | no way to declare an AUR-free posture; update reaches for the AUR unconditionally | med | medium | minor |
| `3.1.0-Q1` | should sysforge have an opinion about kernel hardening, or is that outside a build tool's remit? | med | medium | minor |
| `2.6.1-F27` | Install stage target-root change summary | low | medium | patch |
| `3.0.0-F1` | Preflight the Rust toolchain when the kernel fragment requests CONFIG_RUST | low | medium | patch |
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

### Open questions

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
