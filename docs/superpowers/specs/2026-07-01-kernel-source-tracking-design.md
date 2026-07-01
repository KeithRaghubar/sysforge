# Kernel-stage source tracking & local-rename (`1.2.0-F40`)

**Status:** design approved, pending implementation plan
**Date:** 2026-07-01

## Problem

The kernel stage's `source` field (`local` | `aur` | `git`) is effectively inert:

1. **Ordering defeats bootstrap.** `KernelStage` calls `_pkgbuild_path()` *before*
   `_presync_kernel_source()` (`kernel.py:1426-1427`). `_pkgbuild_path` raises when the
   PKGBUILD dir is missing (*"Clone the kernel PKGBUILD into `<dir>` first"*,
   `kernel.py:600-603`), so the sync — which the scheduler *can* satisfy by cloning
   (`source_sync._sync_one`: `needs_clone = not pkgbuild_dir.is_dir()`) — never gets the
   chance. For `source=aur|repo` the sync can only fetch/rebase an *already-present* clone;
   it can never do the initial clone.

2. **Naming forces `local`.** `_validate_pkgname_matches_pkgbuild` requires
   `kernel.toml pkgname` to equal the PKGBUILD's `pkgbase`. With the default
   `pkgname = "linux-sysforge"`, the only way that holds is if the user hand-edited an
   upstream PKGBUILD to rename its pkgbase — a hand-maintained tree — for which
   `source=local` correctly does nothing.

3. **`git` is a phantom.** `source_sync._clone` special-cases only `repo`
   (`pkgctl_checkout`); every other value falls to `aur_clone`. There is no URL field, so a
   bare `git` source cannot point at an arbitrary remote — it is dead config.

Net: `source` can only fetch a clone the user created by hand, and the default config path
guarantees a no-op. There is no supported way to say "track upstream `linux-zen`, build it
under a distinct name so it coexists with the official package."

## Goal

Decouple **what to pull** (upstream) from **what to build/install as** (local), and make the
pkgbase rename a patch sysforge applies rather than something the user does by hand — so
`source` becomes meaningful and an upstream kernel can be tracked without manual cloning or
renaming.

## Design

### Config schema (`[kernel]`)

| field | required? | meaning |
|---|---|---|
| `upstream_pkgname` | optional | Package sysforge pulls/tracks (e.g. `linux-zen`). If set, the stage clones/fetches it into `~/src/<upstream_pkgname>`. |
| `pkgname` | optional | **Local** name sysforge patches the pkgbase to, so the build coexists with an installed kernel. Defaults to `upstream_pkgname` when omitted. |
| `source` | optional | Pin the remote (`local` \| `repo` \| `aur`). Omitted → auto-resolve `local → repo → aur`. `git` removed. |

Two modes fall out:

- **Pure-local (today's default, unchanged):** `upstream_pkgname` unset, `source=local`,
  `pkgname=linux-sysforge` → build `~/src/linux-sysforge`, no sync, no rename. Byte-identical
  to current behavior; no migration.
- **Track-upstream (new):** `upstream_pkgname=linux-zen`, optional `pkgname=linux-zen-mine` →
  clone `linux-zen`, patch its pkgbase to `linux-zen-mine`, build. If `pkgname` omitted, builds
  as `linux-zen` and `_check_pkgname_repo_collision` warns before it can shadow the official
  package.

### Resolution & bootstrap flow

- **Reorder** the build entry: run source-sync **first**, resolve the PKGBUILD path **after**,
  so a missing tree is bootstrapped by the scheduler's existing clone-if-missing path.
- **Clone dir** = `~/src/<srcdir>`, where `srcdir` resolves as `kernel.toml srcdir` (existing
  explicit override, still honored) → `upstream_pkgname` → `pkgname`, first set wins. So
  track-upstream defaults the dir to `<upstream_pkgname>` and pure-local to `<pkgname>`, with
  `srcdir` overriding either.
- **Source resolution** (`local → repo → aur`), reusing existing machinery:
  - `source` set explicitly → honor it; `local` disables sync entirely.
  - `source` omitted:
    - **dir exists** → do not re-clone; the scheduler fetch/rebases via the tree's own git
      origin (non-git hand-maintained tree → treated as local). "Local first" = an existing
      tree is never clobbered by a re-clone.
    - **dir missing** → choose the clone remote via `is_repo_package(upstream_pkgname)` (the
      same pacman-sync-DB probe `_check_pkgname_repo_collision` already uses): in a sync repo
      → `repo` (pkgctl), else → `aur`. Network-cheap; no failed-clone-retry dance.
- `source_sync._clone` already dispatches `repo`→`pkgctl_checkout`, else→`aur_clone`; no change
  beyond dropping the phantom `git` from validation.

### pkgbase rename (generalize the one home)

- **Generalize the primitive.** Extract the core of `patch_package_suffix` into
  `patch_pkgbase_rename(patched_path, new_pkgbase, *, mode)` — rename `pkgbase=`, cascade
  `$pkgbase` references, rewrite literal tokens embedding the old base, inject
  `conflicts`/`replaces` per `mode`, return the same origin dict
  (`origin_pkgbase`/`origin_pkgnames`/…). `patch_package_suffix(path, suffix, mode)` becomes a
  thin wrapper computing `new = f"{origin}-{suffix}"` and delegating. **No behavior change** for
  existing FDO/PGO/mesa/llvm callers — their tests stay green — and the "rename is one home"
  invariant (CLAUDE.md) is preserved: one patcher, more general entry point.
- **Thread the kernel local-rename through the existing seam.** New `BuildOptions` field
  `rename_pkgbase_to: str | None`, set by the kernel stage when `pkgname != upstream_pkgname`.
  In `makepkg_wrapper` (same point as the suffix rename, `makepkg_wrapper.py:540-546`), apply
  `patch_pkgbase_rename(pkgbuild_path, options.rename_pkgbase_to, mode="coexist")` — **coexist**,
  because the purpose is to install alongside the official kernel without clobbering it. Apply it
  **before** the optional FDO `-sysforge` suffix so the layers stack orthogonally:

  ```
  linux-zen  (upstream, cloned)
    → linux-mine            (local pkgname rename, coexist)
    → linux-mine-sysforge   (only if --autofdo=use etc., coexist)
  ```

  The returned dict rides to `build_state` with `origin_pkgbase = upstream_pkgname`, so
  `sysforge update` keeps source-syncing the upstream tree.
- **No-rename case:** `pkgname == upstream_pkgname` (default) → no patch → builds under the
  upstream name.

### The `-sysforge` collapse (kept, documented)

`patch_package_suffix`'s existing idempotency guard (`pkgbuild_patcher.py:1343`,
`origin_pkgbase.endswith("-sysforge")` → no-op) means a local `pkgname` that already ends in
`-sysforge` does **not** get a second suffix from an optimized build:

```
pkgname = linux-sysforge  →  FDO suffix pass sees "-sysforge"  →  skips  →  linux-sysforge
```

Consequence: an optimized and a stock `linux-sysforge` build share one package name — the
optimized build **replaces** the prior one (in-place upgrade) rather than **coexisting**. To keep
an optimized and a stock kernel installed side-by-side (bootloader fallback), choose a local
`pkgname` that does **not** end in `-sysforge` (e.g. `linux-mine`), so the FDO layer appends
distinctly. This is retained as-is and **must be documented inline in the shipped
`etc/sysforge/kernel.toml`**.

### Validation & safety

- **Retarget `_validate_pkgname_matches_pkgbuild`** to validate the pre-rename name of the cloned
  tree: `upstream_pkgname` when set, else `pkgname`. Still catches a mis-cloned/typo'd tree; no
  longer forces a hand-rename.
- **Drop `git`** from `_resolve_source`'s accepted values; a stale `source = "git"` yields a clear
  validation error pointing at `local`/`repo`/`aur`.
- **`_check_pkgname_repo_collision` unchanged**, evaluated on the effective (post-rename) install
  name — the safety net for the `pkgname == upstream_pkgname` default over an official package.

## Scope / non-goals

- No arbitrary-git-URL source (phantom `git` removed, not resurrected). If needed later, a
  `git_url` field is a separate change.
- No change to the FDO/PGO/mesa/llvm rename semantics beyond the internal
  `patch_package_suffix` → `patch_pkgbase_rename` refactor.
- No change to the source-sync scheduler's clone/fetch mechanics — only the kernel stage's
  call ordering and source selection.

## Backward compatibility

Existing kernel configs (`pkgname` + `source = "local"`, no `upstream_pkgname`) resolve to
pure-local mode with identical behavior. No forced edits; `make sync-config` adds the new
commented `upstream_pkgname` default.

## Testing (TDD)

- `patch_pkgbase_rename` arbitrary rename (pkgbase, `$pkgbase` cascade, literal tokens,
  coexist = no conflicts/replaces); `patch_package_suffix` wrapper unchanged (existing tests
  stay green).
- Source resolution: dir-missing + `is_repo_package` true → `repo`; false → `aur`; dir-exists →
  fetch (no re-clone); explicit `source=local` → no sync.
- Bootstrap ordering: missing dir is cloned before path resolution (no premature abort).
- Validation retarget: `upstream_pkgname` vs pkgbase; pure-local `pkgname` vs pkgbase; `git`
  rejected.
- `-sysforge` collapse: `pkgname=linux-sysforge` + optimized build → single `linux-sysforge`
  (no double suffix); `pkgname=linux-mine` + optimized → `linux-mine` + `linux-mine-sysforge`.
- Pure-local backward-compat: unchanged config path builds as before, no sync.

## Docs & shipped-file updates

- `docs/design/07-pipeline-layer.md` (source-sync + naming) → `make design` → README →
  CLAUDE.md (rename one-home note: `patch_package_suffix` delegates to `patch_pkgbase_rename`).
- `etc/sysforge/kernel.toml` + `tests/data/etc/sysforge/…` parity (`make check-shipped`): new
  `upstream_pkgname`, source auto-resolution note, `-sysforge` collapse note.
- Completions only if a CLI flag changes (kernel config is TOML — verify during implementation).
- `docs/release-notes/unreleased.md` under **Added** with `(1.2.0-F40)`.
