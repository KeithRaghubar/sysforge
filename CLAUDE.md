# SysForge — Claude Code Context

**DESIGN.md is the source of truth** (module layout, APIs, CLI, feature status, gaps) — read
before any architecture/API change. It is **generated**: edit `docs/design/*.md`, run `make
design`, never edit `DESIGN.md` directly (`make check-design` guards it). This file carries only
always-on process guardrails — don't re-inline design detail.

**Code-seam guardrails live in `sysforge/CLAUDE.md`** (gotchas, one-home invariants,
toolchain/kernel deep invariants) — loaded lazily when working under `sysforge/`. **Read it before
editing any code under `sysforge/`** if it isn't already in context. `make check-standards`
(group `claude_md`) verifies both files' path/symbol citations still resolve.

Repo: <https://github.com/KeithRaghubar/sysforge.git> — Python, TOML config.

## Commands (canonical: the Makefile — never invoke `pytest` directly)

```bash
make help            # every target, grouped (add `## desc` to a new target or the suite fails)
make dev-deps        # all dev system deps (dev-deps-list shows per-tier state)
make test / test-x   # full suite / stop on first failure
make lint            # ruff + shellcheck (lint-py / lint-sh individually)
make design          # regenerate DESIGN.md after editing docs/design/*
make check-design    # guard: DESIGN.md in sync with sources
make roadmap-view SORT=effort  # print Planned table sorted by a column (read-only)
make check-shipped   # guard: etc/sysforge, PKGBUILD*, hooks, completions, manpage parity
make check-standards
make sync-config     # adopt new shipped defaults into untracked live config (add-only)
make container-smoke[-cachyos]  # packaging + portability checks in a container (needs make vm-pkg-stable first)
make release-{major,minor,patch}
```

Shipped-file edits must pass `make check-shipped`; doc/design edits `make check-design`.

## Dev Environment

- Ryzen 7 5800X3D, RTX 5070, Arch Linux, COSMIC/Wayland, nvidia-open-dkms.
- `pkgbuild_src_dir = ~/src` (sources); builds at `~/builds`.
- `etc/sysforge/` = shipped defaults; `tests/data/etc/sysforge/` = git-tracked fixtures, kept in
  parity by `make check-shipped` (conftest forces `SYSFORGE_CONFIG_DIR` to it) — a shipped-default
  change updates fixtures too. Live config is a separate untracked dir (`make sync-config`).
  §Config Layer.

## Process Conventions

- **Doc update order**: `docs/design/*.md` (+ `make design`) → README.md → CLAUDE.md.
- **DESIGN = implemented only; `/ROADMAP.md` = planned + abandoned.** Roadmap IDs
  (`<version>-<TYPE><n>`, e.g. `1.2.0-F1`; counters reset on major/minor bump, not patch) live in
  ROADMAP + `docs/release-notes/`, never DESIGN. **Never hand-pick an ID for a new item — run `make
  next-id TYPE=F` (F/B/Q/STD)**, which derives the cycle from `pyproject.toml` (open items keep their
  origin-cycle prefixes, so copying a neighbour mis-numbers new items right after a release). Triage
  `notes.txt` into ROADMAP. Implementing an
  item **removes it from ROADMAP in the same commit** (git history is the record — drop the whole
  entry, no "done" marker) and **appends its release-note entry to `docs/release-notes/unreleased.md`**
  (Keep a Changelog section, inline roadmap ID). Keep entries in ascending ID order — re-sort on
  every add/remove. `tools/release.sh` Phase 1 renames the accumulator to `vX.Y.Z.md` + reseeds;
  the `release-notes` skill only reconciles before a release. `make check-standards` lints it.
- **Every Planned ROADMAP entry ends with a `*Priority: <low|med|high> · Effort: <small|medium|large> ·
  Bump: <patch|minor|major>*` tag** (§ROADMAP.md "Priority, effort & bump tags"). The `## Planned` summary table is **generated** from
  those tags — run `make roadmap-table` after any add/remove/retag; `make check-roadmap-table`
  (preflight-wired, mirrors `check-design`) fails on drift or an untagged/invalid entry. Abandoned
  entries carry no tag. `make roadmap-view SORT=<col> [REVERSE=1]` prints the same table sorted by
  any column (read-only — it never rewrites the committed triage ordering).
- **Completions stay in lockstep with the CLI** (`completions/_sysforge` + bash) in the same change.
- **CLI verbs go through the Verb framework**: `Verb` subclass dispatched by `verbs.runner.run_verb`
  (`pre_check`/`execute`/`post_validate`; `requires_sentinel=True` if it mutates). Wire via
  `set_defaults(verb_cls=…)`, not `func=`. §CLI Verb Framework.
- **Dual-toolchain test parity**: logic branching on resolved compiler (gcc vs llvm) ships both a
  gcc-path and an llvm-path test in the same change.
- **Cutting a release follows `docs/RELEASE-CHECKLIST.md`** — the standalone runbook (stages, exact
  commands, tick boxes). Note `make pre-release` and the `tools/release.sh` preflight overlap but
  neither is a superset, and coverage/audit/VM/container tiers sit in neither.
- **Releases are GPG-signed**: `tools/release.sh` signs commit+tag+tarball, gated by a signing
  preflight + sentinel-fingerprint publish gate. Stable PKGBUILD verifies via `validpgpkeys` + a
  `.asc` source (`SKIP`); `-git` exempt. §Release Process / §Standards row 16.
- **Shipped-file allowlists** (`_KNOWN_SECTIONS`/`_KNOWN_TOP_KEYS` in `tools/check_shipped.py`):
  extend in the same change. §Shipped-file pre-release checks.
- **Standards have one home**: `docs/design/21-standards.md`; enforced by `tools/check_standards.py`
  + `tests/test_standards_compliance.py`. User paths → `primitives/paths.py`; colour →
  `log.use_color()`. **Update the standards table in the same commit as any change that adopts,
  extends, or alters conformance to an external spec** (same in-commit rule as doc-update-order) —
  add/extend the row and wire its enforcement in that commit; never let a row lag the behaviour. A
  spec the code doesn't yet conform to stays a ROADMAP `Q`/`F`/`STD` (which names its target row),
  not a premature row.

## Interaction Preferences

- Be direct. Own mistakes and fix them immediately.
- When a design decision is made, update the relevant `docs/design/*.md` (+ `make design`) in the
  same turn — don't wait to be reminded.
