# Design — Basic package management verbs (`search` / `uninstall`)

**Roadmap ID:** `1.2.0-F42` (originated in the 1.2.0 cycle; keeps its ID though the
current `pyproject.toml` version is 2.2.0).

**Status:** Approved design, pre-implementation.

## Problem

SysForge is build-focused but lacks everyday package-lifecycle verbs. Two are missing:

- **`search`** — no way to look up a package by term from within sysforge.
- **`uninstall`** — no removal verb. A naive `pacman -R` is wrong for a sysforge-managed
  package: it leaves the `build_state.toml` record in place (so `sysforge update` keeps
  rebuilding it), and it does not know that an optimized build may be installed under a
  `-sysforge` renamed name.

Both are "largely pacman passthroughs" but must respect sysforge's state-authority and
rename invariants.

## Scope

Both verbs ship in this one spec. They share CLI/Verb wiring, completions, and manpage
work, so a single pass is efficient. `uninstall` carries the design nuance; `search` is a
mostly-passthrough read verb.

Out of scope: interactive package pickers, AUR *installation* from `search`, dependency
graphs. YAGNI.

## CLI surface

Two new top-level verbs, wired via `set_defaults(verb_cls=…)` (never `func=`):

```
sysforge uninstall [pacman-flags] <pkg> [<pkg>...]   # mutating; requires_sentinel=True
sysforge search <term>                               # read-only; requires_sentinel=False
```

## `uninstall` (`uninstall_cmd.py`, `UninstallVerb`)

### Behavior

1. **Resolve each named target to its actually-installed name** through the rename
   reverse-lookup currently inline in `revert_cmd.plan_revert` (`revert_cmd.py:66-100`).
   Extract that logic into a shared helper — `install_reconcile.resolve_installed_name(bs,
   name)` — returning the installed name, honoring `origin_pkgbase` so `uninstall mesa`
   finds `mesa-sysforge`. Both `revert_cmd` and `uninstall_cmd` call it; **no parallel
   resolver** (one-home discipline).
   - Plain `source_built` → stock name unchanged.
   - `conflict`/`coexist` optimized → the `-sysforge` renamed name.
   - Untracked / repo package → name unchanged.
2. **Print a plan** (packages to remove, flagging which are sysforge-tracked) before acting.
3. **Remove via `pacman -Rnsu <resolved-names>`** through `pacman.remove_pkgs`, extended to
   accept a flag set. `-n` drops `.pacsave`, `-s` recurses into now-orphaned deps, `-u`
   restricts recursion to packages nothing else needs (won't strand a required dep). Pacman
   runs its own confirmation prompt (passthrough); extra pacman flags may be forwarded.
4. **Clear build_state** for the removed packages via `cmd_state_forget` (handles
   split-package siblings via pkgbase matching for free), then run the
   `install_reconcile` reconcile as belt-and-suspenders — the exact composition
   `revert_cmd` uses at `revert_cmd.py:183`. **No parallel demotion path.**

### Decisions baked in

- Default `-Rnsu`; user may forward additional pacman flags.
- Reuse `plan_revert`'s reverse-lookup (extracted) and `cmd_state_forget`; never
  reimplement rename resolution or state demotion.
- Sentinel (`requires_sentinel=True`) gates only the mutating path.

## `search` (`search_cmd.py`, `SearchVerb`, `requires_sentinel=False`)

Three sections printed in fixed order **local → repo → AUR**, each with a header, each
skipped silently when it has no matches:

1. **Local** — `pacman -Qs <term>` passthrough (installed matches first).
2. **Repo** — `pacman -Ss <term>` passthrough. Native pacman colour/layout preserved by
   letting pacman write to stdout.
3. **AUR** — new `aur.aur_search(term)` helper hitting RPC v5
   `/search/{term}?by=name-desc` (mirrors `aur_info`'s request + error handling at
   `aur.py:108`). Rendered by sysforge in a format consistent with pacman's
   `repo/name version` line + indented description. `by=name-desc` matches pacman `-Ss`'s
   name+description matching, so the sections behave consistently for one term.

### Decisions baked in

- **Passthrough for local/repo, sysforge-rendered for AUR** — the pragmatic reading of
  "largely passthrough"; only AUR (no pacman equivalent) needs rendering.
- **AUR failure is non-fatal** — network/RPC error prints a dim
  `(AUR search unavailable: …)` note and still returns local+repo results. Search never
  hard-fails on the optional third source.
- **`aur_search` lives in the AUR primitive**, reusing its timeout/error conventions and
  sharing the JSON→dict extraction with `aur_info` (RPC v5 `search` returns the same result
  shape as `info`).

## Testing

Following TDD + Verb-framework conventions:

- **`uninstall`:**
  - Unit-test `resolve_installed_name` across all rename modes: plain `source_built`
    (stock name), `conflict` `-sysforge`, `coexist` `-sysforge`, untracked/repo (unchanged).
  - `UninstallVerb`: monkeypatch `remove_pkgs` / `cmd_state_forget` / reconcile seams
    (the pattern existing verb tests use); assert call order + args (`-Rnsu`, resolved
    names), including split-package forget. Assert `requires_sentinel=True`. Assert that
    name resolution + plan construction are pure (no mutation before the sentinel-gated
    `execute`). Note: there is no `--dry-run` flag; the actual removal confirmation is
    pacman's own prompt during passthrough.
- **`search`:**
  - Unit-test `aur_search` JSON parsing and the non-fatal error path (RPC exception →
    empty + note, not raise).
  - Test section ordering and omission of empty sections, with pacman calls monkeypatched.
- **No dual-toolchain parity test** — neither verb branches on the resolved compiler
  (gcc vs llvm), so that convention does not apply.

## Lockstep artifacts (same change)

- `completions/_sysforge` + bash completion — both new verbs and `uninstall` flag forwarding.
- Manpage (scdoc).
- `docs/design/*.md` (+ `make design`) → README.md → CLAUDE.md, in that order.
- Append a `docs/release-notes/unreleased.md` entry tagged `1.2.0-F42` (Keep a Changelog,
  ascending ID order).
- Remove `1.2.0-F42` from `ROADMAP.md` in the same commit that lands the feature.
- Extend any shipped-file allowlists if touched; keep `make check-shipped` /
  `make check-standards` / `make check-design` green.
