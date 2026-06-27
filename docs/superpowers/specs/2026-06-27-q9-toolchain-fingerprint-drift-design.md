# Q9 — Toolchain-fingerprint drift detection

**Date:** 2026-06-27
**Roadmap:** resolves `1.2.0-Q9` (Open questions → becomes a Feature on landing)
**Status:** design approved, pending implementation plan

## Problem

`update` Phase 4.25 detects toolchain drift by string-comparing each package's
recorded `build_state.toml` `toolchain_variant` (e.g. `pgo_llvm`) against the
active variant. If the toolchain is rebuilt with **fresh profdata but the same
variant name**, the recorded and active strings still match, so dependent
packages are never flagged — despite being built against a different
libLLVM/clang.

Scope of the gap (deliberately narrow):

- It is **not a correctness gap.** An ABI-changing toolchain rebuild bumps
  libLLVM's **soname**, which already forces consumer rebuilds via
  `assess_libllvm_soname_impact` / `_gate_soname_consumers`. The only thing
  Phase 4.25 misses is a **same-variant, same-soname, different-codegen**
  rebuild — ABI-compatible by construction, so consumers still link.
- It is **doubly opt-in.** Only fires when `run toolchain` is enabled (default
  `false`) *and* the variant is non-`system` (default `compiler = "gcc"`
  records no variant). On the common path the comparison never runs.

Because the upside is codegen-freshness rather than correctness, the chosen
resolution is **advise, don't auto-rebuild** — surfaced through the existing
drift advisory and honored by the existing opt-in rebuild flags. No new axis,
no new flag.

## Design

### Drift identity

Toolchain drift identity becomes the pair `(variant_name, toolchain_fingerprint)`
instead of `variant_name` alone. A package drifts when **either** component
differs from the active toolchain. This is a refinement of the Phase 4.25
comparison, not a new drift axis — it folds into the same `drifted` list and is
therefore already:

- printed by the existing one-line advisory (`_log.ui`, advisory by default),
- listed under `--explain-drift`,
- rebuilt only under `--rebuild-on-toolchain-drift` or the umbrella
  `--rebuild-on-drift`.

The advisory carries a reason so the two cases read distinctly:

- `built under a different variant than active (<variant>)` — name differs.
- `toolchain rebuilt since build (same variant: <variant>)` — name matches,
  fingerprint differs.

### Fingerprint methods (config-selected)

New config key **`[toolchain] drift_detect`** with two values:

- **`"fingerprint"` (default)** — `build_fingerprint.clang_identity(cc)`:
  path + binary size + nanosecond mtime + first line of `--version`. Fast, no
  hashing. Catches version bumps and reinstalls.
  - **Known tradeoff (documented in the shipped config comment):** mtime is
    included, so a pacman reinstall of byte-identical clang flips the
    fingerprint → a spurious advisory, and under `--rebuild-on-drift` a spurious
    rebuild. This is fail-safe (rebuilding is always safe) and advisory-only by
    default, so it is accepted rather than engineered around.

- **`"content_hash"` (opt-in)** — `hash_file` of the resolved **`libLLVM.so`**
  shared object (the real codegen carrier), mixed with the compiler `--version`
  line. Precise: catches a same-version libLLVM PGO rebuild that the stat-based
  method's version line would miss.
  - The clang **driver** binary is deliberately **not** the hash target: it
    links libLLVM dynamically, so a libLLVM rebuild changes the library bytes
    but often not the driver bytes. Hashing the driver would make the "precise"
    mode silently useless.
  - **Performance warning (documented in the shipped config comment):** hashes a
    ~100 MB+ shared object on every drift check.
  - libLLVM resolution reuses existing toolchain state (`llvm_state`); when no
    libLLVM is resolvable (e.g. a `gcc` variant), falls back to
    `clang_identity` so the mode never crashes.

### Stamping (build time)

When a consumer is built under a non-system variant, record
`toolchain_fingerprint` in `build_state.toml` next to `toolchain_variant`,
threaded through the same path as `toolchain_variant`:
`build_core.py` → `BuildOptions` (makepkg_wrapper) → `BuildState.record`.

- The value is the output of the **active** `drift_detect` method at build time.
- **Sticky** like `toolchain_variant` (preserved across re-records when not
  re-supplied).
- Added to `BuildState._serialize`'s key tuple (required for any new
  build_state field).

### Comparison (update time)

In `update.py`, where `active_variant` is resolved, also compute the **active**
toolchain fingerprint **once** (using the active `drift_detect` method). In
Phase 4.25:

- existing rule: flag when `rec_variant != active_variant`;
- new rule: additionally flag when `rec_variant == active_variant`
  **and** both `rec_fingerprint` and `active_fingerprint` are present
  **and** they differ.

### Compatibility & self-healing

- **Missing recorded fingerprint** (entries built before this feature) ⇒ never
  flagged on the fingerprint rule. Only compared when both recorded and active
  values are present.
- **Method flip self-heals.** The stamped value is an opaque string. Flipping
  `drift_detect` makes old strings mismatch the new method's output → packages
  read as drifted → a rebuild re-stamps with the new method. No migration code,
  no per-entry method tag. One fail-safe rebuild per package after a flip;
  documented, not engineered around.

## Lockstep obligations

- **Shipped config:** add `[toolchain] drift_detect` (with the tradeoff +
  performance comments) to `etc/sysforge` defaults and the
  `tests/data/etc/sysforge` fixture (`make check-shipped`).
- **Allowlists:** extend `_KNOWN_SECTIONS` / `_KNOWN_TOP_KEYS` in
  `tools/check_shipped.py` if `[toolchain]` / the key is not already covered.
- **build_state:** new `toolchain_fingerprint` field added to
  `_serialize`'s key tuple.
- **Design doc:** document `[toolchain] drift_detect` in the config-layer
  design source (`docs/design/*`) and run `make design`.
- **ROADMAP:** remove `1.2.0-Q9` from Open questions in the landing commit
  (shipped work is recorded by the commit + release notes, not ROADMAP).

## Testing

LLVM-path (variants are a toolchain-stage concept):

- same variant + **different** fingerprint ⇒ drift flagged.
- same variant + **identical** fingerprint ⇒ no drift.
- **missing** recorded fingerprint ⇒ no drift.
- `drift_detect = "content_hash"`: stamps and compares the libLLVM hash;
  libLLVM-unresolvable path falls back to `clang_identity` without raising.
- advisory message renders the same-variant reason distinctly from the
  different-variant reason.

## Out of scope

- No auto-rebuild outside the existing opt-in flags.
- No change to the soname-bump consumer-rebuild gate (already covers ABI).
- No new CLI surface (no completions/manpage changes).
