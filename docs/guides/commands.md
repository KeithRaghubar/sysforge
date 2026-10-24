# Everyday Commands

The commands you will use most, grouped by what you are trying to do. For the complete
reference (every verb, subcommand and flag) use `sysforge --help` (or `sysforge help <command>`)
and the [man page](../../man/sysforge.1); [DESIGN.md](../../DESIGN.md) documents the semantics
behind them.

---

## How SysForge fits with makepkg and pacman

- **`makepkg`:** `build` and `update` wrap it (with flag passthrough) to build the packages
  SysForge manages, injecting your compiler-flag profile via a temporary `makepkg.conf`.
  A profiled front-end for those packages, not a general `makepkg` replacement.
- **`pacman`:** SysForge wraps *part* of the surface: search/query, install (through
  `build`/`update`/`packages`), and uninstall (`sudo pacman -R` plus build-state demotion).
  It deliberately does not cover all of pacman; pacman remains the package-database authority.

---

## Build and maintain packages

```bash
sysforge build PKG [-si]        # build (and install) with the resolved profile
sysforge build PKG --rebuild    # rebuild even if the same version is already built
sysforge update                 # rebuild outdated managed packages
sysforge fetch PKG              # download a PKGBUILD without building
sysforge resolve PKG            # show which profile applies to PKG, and why
```

## Everyday package lifecycle

```bash
sysforge search cosmic          # search installed, repo, and AUR for a term
sysforge uninstall mesa         # remove a package (demotes it out of build state)
sysforge revert-to-stock mesa   # undo a source-built package back to the repo version
sysforge self-uninstall         # revert everything to stock, then remove SysForge itself
```

## Manifest and build state

```bash
sysforge packages list          # manage the package manifest
sysforge state list             # inspect build state
```

## Inspect and diagnose

```bash
sysforge doctor                 # fast full system health sweep
sysforge doctor pkg mesa-git    # health-check one package's deps + linkage
sysforge log [PKG]              # page the unified log, or one package's build log
sysforge log --verb update      # page the log a verb's last run left
sysforge env                    # show the inherited build environment and its sources
sysforge bench toolchain        # time your optimized clang against the stock repo clang
sysforge help [COMMAND]         # help for sysforge or one command (same as --help)
```

For scripts, status bars and timers, `state list`, `doctor` and `update --dry-run` take `--json`:
stdout then carries one JSON document with a top-level `"schema"` version and nothing else.
Fields may be added in a minor release; a renamed or removed field bumps `"schema"`.

## Config and your own artifacts

```bash
sysforge config check           # settings sysforge retired or does not know
sysforge config clean           # rewrite the retired ones (--unknown: drop unknown ones)
sysforge config reset FILE [KEY]  # back to the shipped default (whole file: keeps a backup)
sysforge config merge           # adopt shipped config-default drift (.sfnew)
sysforge setup                  # pacman integration (IgnoreGroup + hooks), a preset, update preferences
sysforge artifact list          # inventory your scripts, systemd units, pacman hooks
sysforge artifact review        # offer newly-discovered ones for adoption
sysforge artifact deploy NAME   # push a managed artifact to the live system
```

Adopted artifacts are drift-tracked: `deploy` and `remove` both refuse when the live copy
changed outside SysForge, until you pick a winner (`--force`, or `--adopt-live` to fold the
live copy back in).

To override system defaults without touching `/etc/sysforge/`, create user copies in
`~/.config/sysforge/`.

## Freezing source downloads

```bash
sysforge update --frozen              # refuse new AUR/VCS source egress for this run
sysforge update --thaw mesa-git       # ...except mesa-git
sysforge update --no-frozen           # lift an [security] freeze_sources = true default
```

`--frozen` (or `[security] freeze_sources = true` in `sysforge.toml`) blocks new AUR clones,
official-repo (`pkgctl`) checkouts, source-sync fetches, and VCS version probes; existing
checkouts still build. `--thaw` and `--no-frozen` are both run-scoped lifts only; nothing
persists. The [freeze guide](freeze.md) covers the details.

To keep sysforge away from the AUR altogether, set `[aur] enabled = false` in `sysforge.toml`:
no clones, lookups, search results or package-list downloads. AUR-tracked packages are
reported as `AUR disabled` and left alone; repo and local packages work as before.

---

## Everything else

For the rest (`--cleansrc`, `--install-only`, throttling, profiling/`--timings`, the `doctor`
axes, `run <stage>`, and profile/rule semantics), see `sysforge --help`, the
[man page](../../man/sysforge.1), and [DESIGN.md](../../DESIGN.md) (`glow -p DESIGN.md` to render
in-shell).
