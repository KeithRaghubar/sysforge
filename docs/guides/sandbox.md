# Sandboxed Builds with SysForge

A PKGBUILD's `prepare()`/`build()`/`package()` are arbitrary code, and by default they run as
you, with read access to `~/.ssh`, your GPG material and your browser profiles. Turn on the
build sandbox to run them inside a clean container instead.

---

## Turning it on

```bash
sudo pacman -S devtools                        # one-time
mkarchroot ~/chroot/root base-devel            # one-time
```

```toml
# /etc/sysforge/sysforge.toml (or ~/.config/sysforge/sysforge.toml)
[security]
sandbox_builds = true
sandbox_chroot_dir = "~/chroot"
```

Builds then run through `makechrootpkg`; artifacts still land in your usual `PKGDEST`.
Freezing gates what code comes *in* ([freeze guide](freeze.md)), sandboxing gates what a build
can *reach*. They are independent and can be used together. A single profile can override the
switch either way with `sandbox_builds` in `profiles.toml`. Builds that cannot be isolated stop
with an error rather than quietly running unsandboxed, and `run toolchain` / `run kernel` are
never sandboxed, since both build against the host they are upgrading.

---

## How dependencies resolve

The container has its own `pacman.conf`, so on its own it would resolve build dependencies from
the stock repos rather than from what you have installed. sysforge closes that gap two ways:

- Packages built earlier *in the same run* are injected into the container directly.
- Everything else you built from source is published to a local pacman repo,
  `[sysforge-local]`, at the versions you actually have installed. The chroot lists it ahead of
  the stock repos and mounts it read-only, so a sandboxed build links against what your host
  runs, including your own toolchain. This is on by default whenever the sandbox is on; set
  `sandbox_local_repo = false` under `[security]` to resolve those dependencies from the stock
  repos instead. It adds a `[sysforge-local]` block to `<sandbox_chroot_dir>/root/etc/pacman.conf`.
  Other tools that use the same chroot see that repo too (read-only), so keep sysforge's state
  directory in place while the block is there; turning the setting off removes the block on the
  next sandboxed build.

---

## What differs from a host build

A sandboxed build still differs from a host build in one way that is a feature: only the
dependencies a PKGBUILD declares are installed, so a package can't silently pick up an optional
library that merely happens to be on your system. `build_state.toml` records whether each
package was built in the sandbox, and `sysforge update --explain-drift` lists packages built
the other way after you change the setting (`--rebuild-on-flag-drift` rebuilds them; see the
[drift guide](drift.md)).
