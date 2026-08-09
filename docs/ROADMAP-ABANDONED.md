# SysForge Roadmap — Abandoned / decided against

Ideas that were considered and **purposely excluded**, with the rationale and,
where one exists, the condition that would reopen them. This is the second half
of `/ROADMAP.md`: that file carries forward-looking work (`## Planned`), this
one carries the decisions not to do something. `DESIGN.md` describes only
implemented design and never carries roadmap IDs.

An entry lives here so the reasoning survives the decision — the common failure
is re-proposing a rejected idea because nothing recorded why it was rejected.
Each entry therefore states what it would have done, why it was dropped, and
what would have to change for it to come back.

**Abandoned IDs stay spent.** They are part of the same ID namespace as planned
and shipped items, and `make next-id` reads this file alongside `/ROADMAP.md`
and `docs/release-notes/` so a retired number is never handed out twice — the
IDs recorded here are exactly the ones someone is most likely to look up later.
An ID may appear in `## Planned` **or** here, never both; `make check-standards`
enforces that across the two files, and fails if an `## Abandoned` heading
reappears in `/ROADMAP.md`.

Entries carry no `*Priority · Effort · Bump*` tag (that tag is planning advice
for live work) and are exempt from the generated summary table. Otherwise they
follow `/ROADMAP.md`'s conventions: each opens
``- **`<ID>` — <title sentence>.**`` and is separated from its neighbour by a
`---` rule. Ordering here is **reverse-chronological by decision date** (newest
first, undated entries last), not ROADMAP's ascending-ID order — these are
decisions, and the most recent reasoning is the one a reader is usually
checking against.

---

- **`1.2.0-F20` — rule `priority` auto-calculation from condition specificity — decided against
  2026-07-25.** Would have derived a baseline 0–99 score per `[[rules]]` entry from how many
  conditions it AND's together (CSS-specificity analogue), making `priority` optional and reserving
  it for tie-breaks. Rejected because its own deferral condition never came true and cannot: the
  entry was parked "until enough real rules exist to validate whether auto-priority causes ordering
  problems in practice", but `etc/sysforge/profiles.toml` ships **zero** live rules (lines 220–243
  are commented examples only), so there is no corpus of real ordering conflicts to weight against
  and no way to test a mis-ordering. Weighting ten match fields (`pkgnames` globs vs `depends_all`
  vs the `not_*` negations) without that evidence would fix the guesses as config-schema surface.
  Separately, the tiering it would provide largely exists: `config.py` bumps user rules by +100 on
  merge, which is the one precedence question that has actually come up, and a computed score would
  have to stay inside 0–99 to avoid punching through that tier. Cost of *not* doing it is one
  required integer per rule. **Reopens if** a shipped or field config accumulates enough rules to
  produce a genuine priority tie, or if manual priorities are observed being edited to fix ordering
  rather than to express intent — at which point the corpus supplies the weights. Implementation
  hazard on revisit: priority is read in three places (`profile.py:458` resolver,
  `resolve.py:64` explain view, `reconfigure.py:1351`) and all three must move together, or
  `sysforge resolve` reports an order the build does not use. Related: the cancelled
  `[env_precedence]` table below, rejected for the same "configurable precedence nobody needs"
  reason.

---

- **`2.3.0-F5` — declarative provisioning via `tmpfiles.d`/`sysusers.d` — decided against 2026-07-25.**
  Would have replaced the imperative provisioning in `fs_provision.py`/`stage_ownership.py` with a
  shipped `tmpfiles.d` snippet applied by `systemd-tmpfiles`, plus `sysusers.d` for service users.
  Rejected on three counts: it fixes no filed bug (`low · large`, the worst ratio in the backlog);
  it deferred to a `2.3.0-Q3` that exists nowhere in this file or `docs/design/`, so it was formally
  blocked behind a question nobody needs answered; and non-systemd hosts still need the imperative
  fallback, so the bespoke walk gains a second code path beside it rather than being replaced.
  **Reopens if** a provisioning bug traces to the imperative walk, packaging review requires a
  shipped snippet, or the non-systemd fallback leaves the support matrix — at which point delegation
  becomes a net removal and extends Standards row 2 (FHS). Scope on revisit: genuinely static
  provisioning only. **Spec:** `tmpfiles.d(5)`, `sysusers.d(5)`, `systemd-tmpfiles(8)`.

---

- **`2.4.0-Q1` — machine-readable AI-inclusion disclosure — decided against 2026-07-17.**
  **No ratified standard as of mid-2026** — three competing conventions: the `Assisted-by:`/
  `Generated-by:` commit trailer (strongest convergence, kernel precedent), `SPDX-AI-Disclosure:`
  per-file line tags, and `AI-DECLARATION.md` + badge. Adopting one means a Standards row committing
  to a spec that may not win, and the file-tag variant would churn every source file when it loses.
  Existing README prose + the `Co-Authored-By:` trailer already disclose honestly.
  **Reopens** as a `STD` naming its target row once a convention is clearly dominant.

---

- **`2.2.0-Q1` — build-system cohesion audit — decided against 2026-07-10.**
  Premise doesn't hold. The load-bearing **low seam** (`makepkg_wrapper.run` /
  `primitives/makepkg_invoke.py` — flag scrubs, build throttle, PGO/FDO/BOLT rename, review gate,
  recovery menu) is already the single home used by every surface. The three stages bypass the
  **high seam** (`build_core.build_and_install`) *intentionally*, not by drift: `toolchain.py` is a
  5-pass build with no system-install for passes 1–3, and `kernel.py` is a single interactive
  package with an `nconfig` pause and local pkgbase rename — both violate its
  resolve→build→bulk-install assumption. **Reopens** as a narrow `F` scoped to `packages.py` if its
  partial re-implementation of `build_and_install` ever becomes a real maintenance cost.

---

- **`1.2.0-Q11` — proactive kernel driver-class filter — decided against 2026-07-03.**
  Deriving `=n` for built-in `=y` options from `hardware_profile` — the gap `localmodconfig`
  leaves, since it only touches unloaded `=m` modules — trades boot safety for marginal gain.
  A built-in driver costs image size but is inert at runtime; inferring it away can silently drop
  one the machine needs at *next* boot (new hardware, hotplug, rescue), against the stage's
  discipline that Gate-1/Gate-2 boot-safety stays authoritative. F37's opt-in `localmodconfig`
  path already serves users wanting a slimmer kernel, on the safe side of the trade.
  **Reopens under a new ID** only if a concrete boot-size or build-time problem motivates it.

---

- **`-sysforge` suffix on the PGO-built toolchain — scrapped 2026-06-24.** The PGO toolchain keeps
  stock names (`clang`, `llvm`, `llvm-libs`, …), per the invariant that the toolchain stage is an
  in-place system replacement. The rename would have broken five exact-pacman-name lookups
  (`_verify_llvm_install`/`_probe_cc` skew arms, `_installed_libllvm_soname` soname-bump gate, BOLT
  Pass 4a, `collect_llvm_state` provenance) plus a B5 rework — cosmetic provenance gain on a
  default-off path, on the highest-stakes path in the repo.

---

- **`[env_precedence]` config table — design cancelled.** Proposed a configurable priority stack
  (wrapper profile 100 / makepkg.conf 80 / shell 20 / PKGBUILD 10). Superseded by a simpler model:
  `invoke_makepkg` strips build-tool vars (`CC`, `CFLAGS`, `LDFLAGS`, …) from the inherited env, so
  the temp conf is the sole authority — bleed-through is prevented outright, not prioritized.
  SysForge's own vars (`SYSFORGE_STATE_DIR`, `SYSFORGE_CONFIG_DIR`) are exempt. The replacement
  model is documented in DESIGN.md.
