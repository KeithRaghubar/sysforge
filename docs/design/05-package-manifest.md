## Package Manifest

Two files split the responsibility cleanly, and keeping them distinct is what
makes the model legible:

- **`build_state.toml` — the registry of what sysforge maintains.** It is the
  **authority** for steady-state tracking. Any installed package sysforge built
  from source (its record has `build_mode != "pacman"`) is maintained — i.e.
  `sysforge update` rebuilds it from source as upstream advances — with no
  packages.toml entry required. `build_mode = "pacman"` records are inert
  install-markers written by `sync_with_installed` for everything else installed.
- **`packages.toml` — the declared manifest** of *intent*: the bootstrap install
  set, package groups, and per-package **build overrides**. It does **not** drive
  steady-state tracking; that is build_state's job.

`packages.toml` plays two roles depending on context:

1. **Bootstrap (pipeline `run packages` stage):** every entry is installed. The manifest *is* the install list, because the system has nothing installed yet beyond the pacstrap base.
2. **Steady-state (`sysforge update`, `sysforge build`):** entries act as **build-rule overrides** applied to the live install set. Pacman owns the install set; `build_state.toml` mirrors it and is the tracking authority. An entry whose package is not currently installed is an inert rule, not a "missing" item.

This dual role is intentional: the manifest captures your declared intent, but at steady-state we respect the live system rather than reconciling against the manifest.

### What `sysforge update` maintains (steady-state scope)

In one sentence: **sysforge maintains what it built.** Concretely, `update`'s
walk is the union of —
- everything sysforge source-built (build_state `build_mode != "pacman"`) — this
  is what makes `sysforge build mesa` *durable*: the optimized repo build is
  rebuilt from source on every update instead of being frozen behind
  `IgnoreGroup = sf-build`;
- every foreign package (`pacman -Qm` — AUR/local installed outside or before
  sysforge), with default rules and no overrides;
- repo packages explicitly opted in via a packages.toml override
  (`enable_build_from_source` / `cache` / `reason`) or globally via
  `repo_mode = "build_from_source"`.

Stage-owned packages (kernel/toolchain) are excluded from the default walk
(`--include-stage-owned` or naming them overrides). To **stop** maintaining a
package, drop its record with `sysforge state forget <pkg>` — the installed
artifact is left in place (still pinned by the `sf-build` group), so reverting to
the stock repo binary is a separate `pacman -S <pkg>`. Uninstalling a package
also auto-stops tracking (`sync_with_installed` prunes its record).

The orthogonality of the roles means:
- An entry for `mesa-git` can stay in the manifest even if you've rolled back to repo `mesa` — it's an inert rule at steady-state, but the next pipeline bootstrap of a fresh system would still install it.
- An installed AUR package without an entry uses default rules — `sysforge update` still walks it via `pacman -Qm`, just with no overrides applied.
- `profiles.toml` and the manifest stay orthogonal: sourcing/patching choices vs. compiler flag tuning.

Each entry overrides at most these fields (all optional except `name`):
- `source` — `repo` (pacman) vs `aur`/`git`/`local`. A **routing hint**: it tells the bootstrap/build paths how to obtain the package, falling through to pacman / AUR RPC inference if omitted. It does **not** by itself put a package under steady-state tracking — tracking comes from sysforge having built the package (build_state), not from a manifest entry. So an entry with only `name` + `source` has no override effect at steady-state, and `sysforge packages add` rejects it (pair it with a behavior-changing field — `enable_build_from_source`, `cache`, `reason` — for the entry to override anything). When sysforge builds a package, it records the resolved `source` in build_state, so the registry is self-describing without re-inferring origin later.
- `enable_build_from_source` *(bool)* — if `true`, this **repo** package is built from source (via `pkgctl repo clone` + makepkg with sysforge flag profiles) instead of installed as the binary via pacman. It also opts the package into `sysforge build` without a confirmation prompt (see §`build`). The boolean replaced the misleadingly-named `pkgbuild_patch` (which never patched anything — real PKGBUILD patching is gated by separate predicates); a legacy `pkgbuild_patch` key is still honored on read and migrated to the new name on the next write (`config.normalize_package_entry`, applied in `expand_package_groups`).
- `cache` *(bool)* — `false` disables ccache/sccache for this package (required for PGO stages).

```toml
[build]
pkgbuild_src_dir = "~/src"        # PKGBUILD source tree; auto-cloned if absent
repo_mode = "build_from_source"   # default for repo packages: "pacman" | "build_from_source"

[[package]]
name = "mesa-git"
enable_build_from_source = true   # override: build this repo package from source

[[package]]
name = "llvm"
cache = false                     # override: never cache instrumented PGO objects
```

An entry with only `name` and no override fields has no effect on the build. `sysforge packages add` rejects such calls.

### `[build]` global section

- `pkgbuild_src_dir` — directory holding pre-cloned PKGBUILDs (`<pkgbuild_src_dir>/<name>/PKGBUILD`). Missing AUR clones are auto-fetched here on demand.
- `repo_mode` — controls how the **bootstrap** (`run packages`) builds repo-source entries: `"pacman"` (install via `pacman -S --needed`) or `"build_from_source"` (build from PKGBUILD with sysforge flag profiles); per-package `enable_build_from_source = true` forces source-build regardless. At **steady-state** `repo_mode = "build_from_source"` is a *bulk drift-surfacing* switch: it pulls **every** installed repo package into `sysforge update`'s walk so repo-side version drift is reported alongside AUR drift. It is **not** how you get a repo package source-built going forward — that happens automatically once sysforge has built it (build_state authority; `sysforge build mesa` is the natural entry). Of the bulk set, only the overridden / already-source-built subset is rebuilt from source; the remainder takes a fast pacman path (`checkupdates` for upgrade detection, one terminal `sudo pacman -Syu` after the source-build loop). This avoids a per-package `pkgctl repo clone` for every installed repo package and is what makes the "track everything" mode tolerable on a maintained workstation. The legacy value `"profiled"` is accepted on read and maps to `"build_from_source"` (single resolver: `config.resolve_repo_mode`).

### Package groups

`[group.<name>]` tables declare named sets that expand into `[[package]]`-equivalent entries at load time, so a desktop stack (e.g. 20+ git packages) can be tracked without enumerating every member as its own block:

```toml
[group.cosmic]
packages = ["cosmic-session-git", "cosmic-comp-git", "cosmic-settings-git"]
# Optional defaults inherited by every member:
# enable_build_from_source = true
```

Expansion semantics (single expansion point: `primitives/config.expand_package_groups` — every manifest consumer routes through it; do not re-expand `[group.*]` elsewhere):

- Each member becomes a synthetic entry carrying its group defaults plus `group = "<name>"` marking its origin.
- An explicit `[[package]]` entry for the same name wins **outright** over the group entry — no field merge — so a member can be individually overridden.
- The first group to claim a name wins over later groups.
- Bootstrap (`run packages`): members are installed like any entry. Steady-state (`sysforge update`): members participate as overrides; a member with no group defaults is legitimately inert (its meaning is the bootstrap set) and is exempt from the inert-override warning that hand-written entries get.
- `packages list` shows groups as written in the file (name, member count, defaults, members), after the explicit-entry table. Groups are hand-edited TOML, **or** written by the guided desktop selection below; `packages add`/`remove` manage explicit `[[package]]` entries only.

#### Curated desktop catalog

`primitives/pkg_catalog.py` ships a curated catalog of desktop-environment groups (`gnome`, `kde`, `xfce`, `mate`, `cinnamon`, `lxqt`, `budgie`, `cosmic`) and is the single home for the catalog, the guided selection prompt (`select_desktop`), the per-entry display-manager pairing (`display_manager_for`), and the `[group.*]` writer (`write_desktop_group`). It lives in the primitives layer because three surfaces consume it:

- **`sysforge packages add-group <de>`** — writes the chosen catalog group into `packages.toml` (idempotent: re-running replaces the same-named group block; other `[[package]]` blocks and tables are preserved byte-for-byte). Creates the file with the standard header if absent.
- **Configure stage (bootstrap, stage 3)** — after copying config into the target, `select_desktop` resolves the choice: `bootstrap.toml [desktop] environment` wins non-interactively (unattended installs); otherwise a TTY run prompts ("Install a graphical desktop? → numbered menu"); a non-TTY run with no preselection skips. The group is written into the *target's* `packages.toml` so the later packages stage installs it.
- **Reconfigure step `desktop`** — offers the same guided selection on a live system, writing to the live manifest.

The writer only adds `[group.*]` text; expansion stays in `config.expand_package_groups`. Each entry is intentionally minimal — a core session plus its display manager (and, for lightdm-based desktops, a greeter); users extend their own group afterward. Every entry declares a `display_manager` package that is also a member of its `packages` tuple, so the package installs *and* its unit can be enabled.

**Display-manager enablement is the single fix that makes the selection actually boot into a GUI.** Installing the DM package on Arch does not enable its systemd unit, so the packages stage (the one home — both fresh-install and `sysforge run packages` flow through it) enables `<display_manager>.service` for every desktop group whose DM package built, via `_enable_display_managers` → `pkg_catalog.display_manager_for`. It runs once per distinct DM, outside the sentinel scope (cosmetic), and never `--now`-starts the unit (the install may be headless over SSH) — it takes effect on the next boot. This is **not** done in the configure stage: configure runs in the chroot *before* any packages are installed, so the unit doesn't yet exist there.

### Manifest lifecycle commands

`sysforge packages` is a small namespace for managing override entries:

- **`packages list`** (default when no subcommand) — tabulates entries: name and any override fields set. `--orphans` lists entries whose package is not currently installed (informational only; entries are still valid rules).
- **`packages add <pkg> [--source ...] [--enable-build-from-source] [--no-cache] [--reason TEXT]`** — adds or updates an override entry. Requires at least one of `--enable-build-from-source`, `--no-cache`, `--reason` (the *behavior-changing* override fields); calls with only `<pkg>` or `<pkg> --source` are rejected. `--source` is metadata that pins routing (`repo` vs `aur`) — it doesn't satisfy validation on its own, since classification arrives at the same value automatically. Entries with no behavior-changing override are auto-pruned on the next `packages.toml` write-back (`add` or `remove`); the auto-prune is legacy-aware (a pre-rename `pkgbuild_patch` entry counts as non-inert so it is never silently dropped).
- **`packages add-group <de>`** — writes a curated desktop-environment group (see *Curated desktop catalog* above) into `packages.toml`. Idempotent; the group installs (and its display manager is enabled) via `sysforge run packages`.
- **`packages remove <pkg>`** — removes the `[[package]]` block for the named entry using line-level manipulation; preserves all surrounding comments and section headers.

All subcommands accept `--packages FILE` to target a specific file (default: `/etc/sysforge/packages.toml`).

`build_state.toml` inspection and repair has its own namespace — see `sysforge state` (`state list`, `state repair`).

Valid per-entry fields: `name`, `source`, `enable_build_from_source`, `cache`, `reason`. The legacy `pkgbuild_patch` key is accepted and normalized to `enable_build_from_source`. Unknown fields are ignored.

### `-march=native` strategy

SysForge uses `-march=native` rather than hardcoding CPU-specific flags. Optimization becomes a compile-time concern — it works across CPU families without separate logic. If a package is incompatible with native tuning, a higher-priority rule pointing to the `bare` profile overrides `-march` for that package only.

---

