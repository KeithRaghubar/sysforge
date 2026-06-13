## Package Manifest

`packages.toml` is the **declared system manifest**. It plays two roles depending on context:

1. **Bootstrap (pipeline `run packages` stage):** every entry is installed. The manifest *is* the install list, because the system has nothing installed yet beyond the pacstrap base.
2. **Steady-state (`sysforge update`, `sysforge build`):** entries act as **build-rule overrides** applied to the live install set. Pacman owns the install set; `build_state.toml` mirrors it. An entry whose package is not currently installed is an inert rule, not a "missing" item.

This dual role is intentional: the manifest captures your declared intent, but at steady-state we respect the live system rather than reconciling against the manifest.

The orthogonality of the two roles means:
- An entry for `mesa-git` can stay in the manifest even if you've rolled back to repo `mesa` — it's an inert rule at steady-state, but the next pipeline bootstrap of a fresh system would still install it.
- An installed AUR package without an entry uses default rules — `sysforge update` still walks it via `pacman -Qm`, just with no overrides applied.
- `profiles.toml` and the manifest stay orthogonal: sourcing/patching choices vs. compiler flag tuning.

Each entry overrides at most these fields (all optional except `name`):
- `source` — `repo` (pacman) vs `aur`. Optional metadata; classification falls through to pacman / AUR RPC if omitted. Set explicitly only when classification is ambiguous or you want to force routing. `source` alone is **inert** and does not trigger any sysforge command path (matches the `packages add` validator); pair it with a behavior-changing field (`pkgbuild_patch`, `cache`, `reason`) if you want the entry to take effect.
- `pkgbuild_patch` *(bool)* — if `true`, the PKGBUILD patching library runs on this package before build.
- `cache` *(bool)* — `false` disables ccache/sccache for this package (required for PGO stages).

```toml
[build]
pkgbuild_src_dir = "~/src"   # PKGBUILD source tree; auto-cloned if absent
repo_mode = "profiled"       # default for repo packages: "pacman" | "profiled"

[[package]]
name = "mesa-git"
pkgbuild_patch = true        # override: patch flags before building

[[package]]
name = "llvm"
cache = false                # override: never cache instrumented PGO objects
```

An entry with only `name` and no override fields has no effect on the build. `sysforge packages add` rejects such calls.

### `[build]` global section

- `pkgbuild_src_dir` — directory holding pre-cloned PKGBUILDs (`<pkgbuild_src_dir>/<name>/PKGBUILD`). Missing AUR clones are auto-fetched here on demand.
- `repo_mode` — default build mode for repo-source packages: `"pacman"` (install via `pacman -S --needed`) or `"profiled"` (build from PKGBUILD with sysforge flag profiles). Per-package `pkgbuild_patch = true` overrides to profiled regardless. `sysforge update` walks repo packages only when a per-package override sets a behavior-changing field (`pkgbuild_patch`, `cache`, `reason`), or when `repo_mode = "profiled"` is set globally — in which case every installed repo package is in scope, but only the overridden subset is source-built; the remainder takes a fast pacman path (`checkupdates` for upgrade detection, one terminal `sudo pacman -Syu` after the source-build loop). This avoids the per-package `pkgctl repo clone` that would otherwise fire for every installed repo package and is what makes the "track everything" mode tolerable on a maintained workstation.

### Package groups

`[group.<name>]` tables declare named sets that expand into `[[package]]`-equivalent entries at load time, so a desktop stack (e.g. 20+ git packages) can be tracked without enumerating every member as its own block:

```toml
[group.cosmic]
packages = ["cosmic-session-git", "cosmic-comp-git", "cosmic-settings-git"]
# Optional defaults inherited by every member:
# pkgbuild_patch = true
```

Expansion semantics (single expansion point: `primitives/config.expand_package_groups` — every manifest consumer routes through it; do not re-expand `[group.*]` elsewhere):

- Each member becomes a synthetic entry carrying its group defaults plus `group = "<name>"` marking its origin.
- An explicit `[[package]]` entry for the same name wins **outright** over the group entry — no field merge — so a member can be individually overridden.
- The first group to claim a name wins over later groups.
- Bootstrap (`run packages`): members are installed like any entry. Steady-state (`sysforge update`): members participate as overrides; a member with no group defaults is legitimately inert (its meaning is the bootstrap set) and is exempt from the inert-override warning that hand-written entries get.
- `packages list` shows groups as written in the file (name, member count, defaults, members), after the explicit-entry table. Groups are hand-edited TOML, **or** written by the guided desktop selection below; `packages add`/`remove` manage explicit `[[package]]` entries only.

#### Curated desktop catalog

`primitives/pkg_catalog.py` ships a small curated catalog of desktop-environment groups (currently `gnome` and `kde`) and is the single home for the catalog, the guided selection prompt (`select_desktop`), and the `[group.*]` writer (`write_desktop_group`). It lives in the primitives layer because three surfaces consume it:

- **`sysforge packages add-group <gnome|kde>`** — writes the chosen catalog group into `packages.toml` (idempotent: re-running replaces the same-named group block; other `[[package]]` blocks and tables are preserved byte-for-byte). Creates the file with the standard header if absent.
- **Configure stage (bootstrap, stage 4)** — after copying config into the target, `select_desktop` resolves the choice: `bootstrap.toml [desktop] environment` wins non-interactively (unattended installs); otherwise a TTY run prompts ("Install a graphical desktop? → numbered menu"); a non-TTY run with no preselection skips. The group is written into the *target's* `packages.toml` so the later packages stage installs it.
- **Reconfigure step `desktop`** — offers the same guided selection on a live system, writing to the live manifest.

The writer only adds `[group.*]` text; expansion stays in `config.expand_package_groups`. The catalog is intentionally minimal (a core session + display manager per entry) — users extend their own group afterward.

### Manifest lifecycle commands

`sysforge packages` is a small namespace for managing override entries:

- **`packages list`** (default when no subcommand) — tabulates entries: name and any override fields set. `--orphans` lists entries whose package is not currently installed (informational only; entries are still valid rules).
- **`packages add <pkg> [--source ...] [--pkgbuild-patch] [--no-cache] [--reason TEXT]`** — adds or updates an override entry. Requires at least one of `--pkgbuild-patch`, `--no-cache`, `--reason` (the *behavior-changing* override fields); calls with only `<pkg>` or `<pkg> --source` are rejected. `--source` is metadata that pins routing (`repo` vs `aur`) — it doesn't satisfy validation on its own, since classification arrives at the same value automatically. Entries with no behavior-changing override are auto-pruned on the next `packages.toml` write-back (`add` or `remove`).
- **`packages add-group <gnome|kde>`** — writes a curated desktop-environment group (see *Curated desktop catalog* above) into `packages.toml`. Idempotent; the group installs via `sysforge run packages`.
- **`packages remove <pkg>`** — removes the `[[package]]` block for the named entry using line-level manipulation; preserves all surrounding comments and section headers.

All subcommands accept `--packages FILE` to target a specific file (default: `/etc/sysforge/packages.toml`).

`build_state.toml` inspection and repair has its own namespace — see `sysforge state` (`state list`, `state repair`).

Valid per-entry fields: `name`, `source`, `pkgbuild_patch`, `cache`. Unknown fields are ignored.

### `-march=native` strategy

SysForge uses `-march=native` rather than hardcoding CPU-specific flags. Optimization becomes a compile-time concern — it works across CPU families without separate logic. If a package is incompatible with native tuning, a higher-priority rule pointing to the `bare` profile overrides `-march` for that package only.

---

