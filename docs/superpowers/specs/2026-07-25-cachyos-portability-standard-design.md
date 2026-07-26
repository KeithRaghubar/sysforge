# Arch-derivative portability as an enforced standard (`2.6.1-STD1`)

**Status:** design approved 2026-07-25 · **Unblocked** — `2.6.1-F2` shipped 2026-07-25
(container harness + `doctor --distro`). Sub-invariant 3 and standards row 23 landed with it,
since the standards rule forbids a row lagging the behaviour that adopts its spec; what remains
here is sub-invariants 1 and 2, the release gate, and the user-facing tier docs.
**Roadmap ID:** `2.6.1-STD1` (allocated via `make next-id TYPE=STD`)

## Problem

SysForge runs correctly on CachyOS today by accident, not by assertion. There is
no distro detection anywhere in `sysforge/`, no code-level invariant that forbids
the assumptions which would break an Arch derivative, and no release gate that
would notice a regression. `README.md:24` claims "Arch Linux" and nothing more,
so users on a derivative have no statement of what is validated.

The goal is a durable, per-minor commitment to CachyOS compatibility, expressed
as an enforced standard rather than a convention that decays.

## Non-goals

- Supporting non-Arch-derived distributions. `pacman`/`makepkg` remain assumed.
- Distro-conditional *behaviour*. The standard forbids assumptions; it does not
  introduce per-distro code paths. If a genuine behavioural divergence is found,
  that is a new roadmap item, not part of this one.
- Validating the VM-tier concerns on CachyOS: bootstrap/archinstall, kernel
  staging, graphics/DKMS probes, and restart detection stay Arch-only, matching
  `2.6.1-F2`'s stated out-of-scope list.
- Absorbing `2.6.1-F2`. That item is a hard prerequisite, kept separate.

## Design

### 1. Standards row 23 — Arch-derivative portability

Added to `docs/design/21-standards.md`. Cited spec: `os-release(5)` — the only
external specification actually in play. Status: **enforced**.

Scope: repo, toolchain-default, and version-comparison assumptions on
Arch-derived hosts. The row asserts three sub-invariants, one per risk class
enumerated in `2.6.1-F2`:

1. **No hardcoded sync-repo names.** The `["core", "extra"]` literal at
   `pacman.py:93` is the sole allowlisted occurrence, and only as an I/O
   fallback when `/etc/pacman.conf` cannot be read. Any other module comparing a
   repo name against a literal is a violation. This is the `build_core.prepare_deps`
   repo-vs-AUR makedep split — the same failure class as the exit-8 regression
   documented at `build_core.py:268`.
2. **The system `makepkg.conf` is the merge baseline, never replaced.**
   `makepkg_conf.py:8` remains the single home for that merge. A derivative's
   `-march=x86-64-v3` / LTO defaults must survive profile-key override intact.
3. **Distro identity is read from `os-release(5)` through one primitive.**
   Never from `pacman.conf` sniffing, `/etc/arch-release`, or hostname
   heuristics. The primitive is delivered by `2.6.1-F2` alongside its `doctor`
   probe; this row makes it the sole home.

Enforcement: a new `check_standards` group `distro_portability`, registered in
`GROUPS` in `tools/check_standards.py`, plus cases in
`tests/test_standards_compliance.py`. Per CLAUDE.md, the row and its enforcement
land in the same commit — the row must never lag the behaviour.

The group's checks are static, in the style of the existing `run_seam` and
`privilege_seam` groups:

- sub-invariant 1: scan `sysforge/` for repo-name string literals outside the
  allowlisted `pacman.py` fallback;
- sub-invariant 2: assert `makepkg_conf.py` is the only module reading the
  system `makepkg.conf` path, and that its merge is baseline-preserving;
- sub-invariant 3: assert `os-release` is read from exactly one primitive.

### 2. Per-minor release gate

New section 9 in `.claude/skills/release-prep/scripts/preflight.sh`, modelled
line-for-line on section 8's `vm-smoke` policy:

- **WARN** when the CachyOS harness is unavailable (no container image, harness
  absent). Optional infrastructure must never block a release — this mirrors the
  explicit policy comment already at `preflight.sh:224-226`.
- **FAIL** when the harness runs and a check fails. A real portability break
  blocks the release.

It invokes the container arm delivered by `2.6.1-F2`, not a VM, so it costs
seconds and has no snapshot that can rot.

**Making "each minor" real.** WARN-on-absence means the script alone cannot
enforce the per-minor cadence. Rather than putting version-conditional logic in
`preflight.sh` — which would make its policy non-uniform across sections — the
cadence is enforced in `.claude/skills/release-prep/SKILL.md`: its checklist
gains an explicit line stating that for a **minor or major** bump, section 9
must be *green*, not merely non-failing. A yellow section 9 is acceptable for a
patch release only. This keeps the script's policy uniform and puts the
version-sensitivity where a human reads it.

### 3. Dependency on `2.6.1-F2`

`2.6.1-F2` delivers the container harness, its distro parameterization (Arch
base; Arch base + CachyOS repos and `makepkg.conf`), and the `os-release` probe
surfaced through `doctor`. `2.6.1-STD1` consumes all three and adds only the
row, the `check_standards` group, the tests, preflight section 9, and the docs.

Sub-invariant 3 is unimplementable until F2 ships the probe, so STD1 cannot be
started early, even partially. The ROADMAP entry for STD1 names F2 as a hard
blocker; F2's entry gains a back-reference so neither is picked up in the wrong
order.

### 4. User-facing documentation — tiered support matrix

Three tiers, mirroring the existing tested-hardware tier language:

- **Arch Linux** — primary. Full VM validation including kernel staging,
  graphics, and DKMS.
- **CachyOS** — validated each minor via the container tier: packaging
  invariants, dependency resolution, `makepkg.conf` merge, version compare and
  already-built fingerprints.
- **Other Arch derivatives** — expected to work, since no repo-name or
  toolchain-default assumptions are permitted, but unvalidated.

Files changed:

| File | Change |
|------|--------|
| `docs/design/21-standards.md` | Row 23 |
| `docs/design/20-scope.md` | The three tiers as a scope statement, naming what CachyOS validation does *not* cover |
| `README.md` (Requirements, line 24) | Replace the flat "Arch Linux" with the three-tier list |
| `man/sysforge.1.scd` | Short `DISTRIBUTIONS` section; regenerate with `make man` |
| `docs/release-notes/unreleased.md` | A `Changed` entry, inline-tagged `2.6.1-STD1`, in ascending ID order |
| `ROADMAP.md` | Remove the STD1 entry on implementation; add the F2 back-reference |

`make design` regenerates `DESIGN.md` from the design sources. Doc update order
per CLAUDE.md: `docs/design/*` → `README.md` → `CLAUDE.md`.

## Verification

- `make check-standards` passes, including the new `distro_portability` group.
- `make check-design` — `DESIGN.md` in sync after `make design`.
- `make check-shipped` — manpage parity after `make man`.
- `make test` — new cases in `tests/test_standards_compliance.py` pass.
- A deliberate violation of each of the three sub-invariants is caught by the
  new group (negative tests).
- `preflight.sh` section 9 reports WARN with the harness absent, and FAIL
  against a harness run that fails.
