## Directory Structure

### Development (local repo)

The `sysforge/` package, one line per module. **Generated** by `make design` from each module's
docstring (its first paragraph), so it cannot fall behind the code: a new, removed or re-described
module makes `make check-design` fail until `make design` runs, and a module without a docstring
fails the build (`4.0.0-DEV7`). Describe a module in its docstring, not here.

<!--MODULE-TREE-->

Everything else in the repo, by role (paths here are checked by the `citations` group):

- `tests/` — the pytest suite (`make test`); `tests/conftest.py` forces `SYSFORGE_CONFIG_DIR` to the
  git-tracked fixtures in `tests/data/etc/sysforge/`, kept in parity with `etc/sysforge/` by
  `make check-shipped`. `tests/data/` also holds the surface snapshot (`tests/data/surface.json`),
  the `--json` schemas (`tests/data/json-schema/`) and the PKGBUILD fixtures (`tests/data/PKGBUILDs/`).
  `tests/cli_args.py` builds real parsed command lines for tests (§Process: no `SimpleNamespace`).
- `tools/` — contributor tooling: the guards (`tools/check_standards.py`, `tools/check_shipped.py`,
  `tools/surface.py`), the generators (`tools/build_design.py`, `tools/gen_roadmap_table.py`), the
  release script (`tools/release.sh`), the end-to-end and VM/container tiers (`tools/e2e/`,
  `tools/vm/`, `tools/container/`), and the live-ISO bootstrap helper (`tools/iso-install.sh`).
- `etc/sysforge/` — the shipped config defaults; `etc/pacman.d/hooks/` — the shipped libalpm hooks.
- `completions/` — zsh (`completions/_sysforge`) and bash (`completions/sysforge.bash`), kept in
  lockstep with the CLI.
- `man/` — the man page source.
- `docs/` — `docs/design/` (this document's sources), `docs/guides/` and `docs/concepts/` (user
  docs), `docs/release-notes/`, `docs/ROADMAP-ABANDONED.md`, `docs/RELEASE-CHECKLIST.md`.
- `PKGBUILD` (stable, GPG-verified) and `PKGBUILD-git`; `sysforge.install` (scriptlets).
- `Makefile` — the dev interface; `make help` lists every target.
- `DESIGN.md` (generated), `ROADMAP.md`, `README.md`, `pyproject.toml`.

### Installed

What the package installs, and what sysforge creates at runtime. `check_standards` (group
`design_tree`) fails when the PKGBUILD installs a path, tmpfiles creates a directory, or the code
joins a file onto the state dir that this section does not name.

```
/usr/bin/sysforge
/etc/sysforge/                       # system config, root-owned; backup=() keeps edits across upgrades
    sysforge.toml                    # runtime behaviour: failure handling, safety, sandbox, aur, ...
    profiles.toml                    # flag profiles, [[rules]], append_conflict_groups, consumes_inference
    packages.toml                    # the package manifest: build-rule overrides, [group.*]
    kernel.toml                      # kernel stage
    toolchain.toml                   # toolchain stage, profile stores, bench
    bootstrap.toml                   # written by iso-install.sh (or hand-copied from the example)
/usr/share/sysforge/
    bootstrap.toml.example           # starter template for manual hand-edit setups
/usr/share/licenses/sysforge/
    LICENSE                          # MIT text (not an Arch common license, so shipped per package)
/usr/share/man/man1/sysforge.1
/usr/share/bash-completion/completions/sysforge
/usr/share/zsh/site-functions/_sysforge
/usr/share/libalpm/hooks/
    sysforge-kernel.hook             # kernel/toolchain reminders
    sysforge-toolchain.hook
    sysforge-buildstate.hook         # build-state staleness signal for `sysforge update`
    sysforge-artifacts.hook          # transaction record for the artifact-drift check
/usr/lib/sysforge/pacman-hook-helper.sh
/usr/lib/sysusers.d/sysforge.conf    # the sysforge group
/usr/lib/tmpfiles.d/sysforge.conf    # the dirs below, root:sysforge 2775 (setgid)
~/.config/sysforge/                  # $XDG_CONFIG_HOME/sysforge
    profiles.toml                    # user overrides (optional; merges with system via extends_system)
~/.cache/sysforge/                   # $XDG_CACHE_HOME/sysforge
    aur-packages.txt                 # AUR name cache (regenerable, refreshed every 24h)
    completion-packages.txt          # merged local+repo+AUR names for Tab (regenerable, keyed on inputs)
~/.local/share/sysforge/             # $XDG_DATA_HOME/sysforge
    artifacts/                       # managed-artifact content (authoritative, irreplaceable)
~/.local/state/sysforge/             # $XDG_STATE_HOME/sysforge: the state dir when /var/lib/sysforge is not writable
/var/cache/sysforge/                 # regenerable profile stores ([profile_store] / SYSFORGE_PROFILE_STORE)
    llvm-pgo/                        # LLVM instrumentation-PGO profraw/profdata (pgo_store / SYSFORGE_PGO_STORE)
    pgo/ autofdo/ propeller/ bolt/   # per-method stores, <method>/<target>
/var/lib/sysforge/                   # the state dir (--state-dir / SYSFORGE_STATE_DIR override it)
    pipeline_state.toml              # pipeline checkpoint state
    build_state.toml                 # per-package build metadata (sysforge build/update)
    source_meta.toml                 # per-package AUR RPC + git HEAD snapshot (source_sync scheduler)
    hardware_profile.toml            # hardware stage output
    artifacts.toml                   # managed-artifact registry
    artifacts-ignored.toml           # artifacts the user declined to manage
    opted-out.toml                   # packages the user stopped sysforge tracking (revert / state forget)
    bench.toml                       # last bench result per target
    stage_timings.toml               # step-duration rings per stage
    stage_in_progress.toml           # install sentinel record (+ stage_in_progress.lock, its liveness lock)
    stage_in_progress.lock
    kernel-build.lock                # one kernel build at a time
    kernel_rust_minimums.json        # kernel stage Rust toolchain minimums
    kbuild_module_map.json           # module -> CONFIG_ map for driver coverage
    lsmod.snapshot                   # union-merged loaded-module list for localmodconfig
    kconfig-history/                 # past kernel .configs, <pkgname>-<release>.config.gz
    local-repo/                      # the build sandbox's local package repo
    sentinels/                       # libalpm-hook signals consumed by `sysforge update`
    .sysforge-init-notice            # first-install notice marker (PKGBUILD post_install)
    llvm_abi_hazard.log              # toolchain Gate-3 symbol-version evidence
    sysforge.log                     # unified pipeline log (cleared on success unless --persist-log)
    sysforge-<verb>.log              # per-verb unified run logs
```

#### makepkg-owned paths (not sysforge dirs)

The PKGBUILD source tree, build workspace, and package output directory are **makepkg's domain**, not sysforge XDG/FHS dirs, so they fall outside the `paths.py` discipline of §21-standards:

- **`pkgbuild_src_dir`** (`~/src` by default, `[paths] pkgbuild_src_dir` in `profiles.toml`) — production PKGBUILD checkouts. FHS-style user source code, deliberately *not* under `$XDG_*` (it is not regenerable cache).
- **`BUILDDIR`** (`$HOME/builds` by default, set in `[profiles.bare]`) — makepkg's build workspace. Kept as a top-level `~/builds` rather than `$XDG_CACHE_HOME/sysforge` on purpose: it is a **shared** workspace the user also builds in manually (sysforge does not own it), and `~/src`/`~/builds` form a matched pair of user working dirs. It is fully user-overridable via the `BUILDDIR` profile key or `/etc/makepkg.conf`; sysforge resolves the effective value through `pacman.get_builddir()` (env → system conf), never assuming the default.
- **`PKGDEST` / `SRCDEST` / `LOGDEST`** — left to makepkg/`/etc/makepkg.conf`; sysforge never writes a default and reads them via `pacman.get_pkgdest()` / `get_srcdest()` / `get_logdest()` when it needs to locate an artifact, source, or log.

The optional `[makepkg]` table in `bootstrap.toml` (`packager`, `makeflags`) lets an unattended install stamp `PACKAGER` / `MAKEFLAGS` into the target `/etc/makepkg.conf` during the configure stage; on a running system the `reconfigure` makepkg step offers the same interactively.

---

