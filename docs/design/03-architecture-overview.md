## Architecture Overview

Three layers:

```
┌─────────────────────────────────────────┐
│  Config                                 │
│  TOML profiles + hardware overlays      │
├─────────────────────────────────────────┤
│  Pipeline                               │
│  Python DAG orchestrator                │
│  checkpoint/resume across stages        │
├─────────────────────────────────────────┤
│  Primitives                             │
│  PKGBUILD parser, makepkg wrapper,      │
│  dep analysis, flag extraction          │
└─────────────────────────────────────────┘
```

**Import direction:** `cli.py` → `verbs/runner.py` → command modules (`update.py`, `packages_cmd.py`, `resolve.py`, …) → `primitives/*`. Each command module defines a `*Verb(Verb)` subclass alongside its existing helpers; the runner dispatches uniformly across them. No command module imports from another command module. See [CLI Verb Framework](#cli-verb-framework).

### Module & function decomposition

SysForge has no line-count lint for functions or modules; decomposition is
driven by **ownership and reuse**, not size. The governing rule is *one home per
concern*: a given decision (a path, a detection, an injection, a gate) is
computed in exactly one place, and every caller routes through it. The standing
list of these single-home invariants lives in `sysforge/CLAUDE.md` (the
lazily-loaded code-seam fragment: one-home invariants + the toolchain/kernel
deep invariants; the root `CLAUDE.md` carries only always-on process
conventions); this section is the *rubric* behind them — when to extract, and where the extracted code belongs.

**Promote logic to a `primitives/` function when** any of:

- **A second caller appears.** The moment two command modules (or a command
  module and a stage) need the same decision, it moves to `primitives/` — never
  copied. Two command modules must not import each other (above), so a shared
  primitive is the *only* way for them to share logic.
- **It is a policy-free fact.** Pure derivations — "which LLVM targets does this
  GPU need", "is this a musl-static build", "what is `PKGDEST`" — belong in a
  primitive that does guarded reads and degrades to a safe default rather than
  raising. The *stage/verb* owns the abort/warn/prompt policy; the primitive
  owns the facts. (`toolchain_safety.py`, `kernel_safety.py`, `flag_drift.py`,
  and the `pacman.get_*` path resolvers are the model — pure, never log.)
- **It guards an invariant a test must pin independently.** If a regression test
  needs to assert the behaviour in isolation (e.g. the lib32 flag scrub, the
  cmake-anchor finder), it wants a named, importable seam.

**Keep logic inline in the command module / stage when** it is policy
(sequencing gates, deciding to prompt vs abort), it has exactly one caller and
no test needs it in isolation, or extracting it would only relocate a single
straight-line block without removing duplication. Premature extraction that adds
an indirection with one caller is churn, not decomposition.

**Splitting an existing function** is warranted when a distinct, separately
*testable* responsibility is buried inside it (the classic "this 200-line
function has a 30-line pure sub-computation a test keeps reaching into via
monkeypatch"), or when two callers want different *prefixes/suffixes* around a
shared middle. Splitting purely to hit a line target is not — a long but linear,
single-responsibility function (a stage's gate sequence, a PKGBUILD render) is
more readable whole than fragmented across helpers that are each called once.

**Where extracted code lands:** a cross-cutting fact or operation → `primitives/`
(its own module if it owns a subsystem — `mesa_pgo.py`, `bolt.py`, `kernel_fdo.py`
— else an existing cohesive one); verb-specific orchestration → the command
module or `pipeline/stages/`; never a "utils" grab-bag. When adding a new
single-home concern, record it in `sysforge/CLAUDE.md` so the invariant is
discoverable, and cross-reference the owning DESIGN.md section. Cited
paths/symbols are kept fresh by the `check_standards` `claude_md` group.

---

