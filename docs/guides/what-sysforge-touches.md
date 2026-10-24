# What SysForge Touches

SysForge installs packages and edits a few system files, so here is where it writes before you
run it. Every command it runs as root goes through `sudo` from one function in the code, and
`makepkg` itself never runs as root.

---

## The package install

- the config files in `/etc/sysforge/` (pacman keeps your edits across upgrades),
- a `sysforge` group, and the directories `/var/lib/sysforge` (build state, logs) and
  `/var/cache/sysforge` (optimization profiles), owned by that group,
- four pacman hooks (`/usr/share/libalpm/hooks/sysforge-*.hook`). After a pacman transaction they
  append a timestamp (and, for three of them, the changed package names) to a file under
  `/var/lib/sysforge/sentinels/`, which the next `sysforge update` reads. They change nothing
  else,
- the man page and shell completions.

## As your user, without sudo

- PKGBUILD checkouts in `~/src` (`pkgbuild_src_dir` under `[paths]` in `profiles.toml`) and
  build trees in `~/builds` (`BUILDDIR`),
- a temporary `makepkg.conf` per build, deleted when the build ends,
- caches in `~/.cache/sysforge`, and the scripts, units and hooks you adopt with
  `sysforge artifact` in `~/.local/share/sysforge`,
- your own config overrides, if you create them, in `~/.config/sysforge/`.

## With sudo, only for the command that needs it

- `usermod -aG sysforge <you>` and a per-user ACL on `/var/lib/sysforge` and `/var/cache/sysforge`,
  the first time you run it, so you can write them before the group applies at your next login.
- `pacman -U`, `-S` and `-R` to install what it built and the build dependencies, and to remove or
  revert packages. `pacman -Syu` runs only with `sysforge update --sysupgrade` or
  `system_upgrade = true`, and `pacman -Fy` refreshes the file database when it needs to find
  which package provides a file.
- `sysforge setup` adds `IgnoreGroup = sf-build` to `/etc/pacman.conf`, after asking, so a plain
  `pacman -Syu` doesn't replace your optimized builds with repo ones. `sysforge run reconfigure`
  can set `EDITOR`/`VISUAL` in `/etc/environment`, if you choose that.
- `sysforge run kernel` installs the kernel, runs `mkinitcpio -P` and updates the bootloader
  (`bootctl update` or `grub-mkconfig`). On systemd-boot it adds a loader entry per kernel it
  builds and never edits yours ([boot entries guide](boot-entries.md)). `sysforge run toolchain`
  installs the LLVM/Clang it built.
- The [build sandbox](sandbox.md), when you turn it on, adds a `[sysforge-local]` repo to the
  chroot's `pacman.conf` and applies your `cpu_quota`/`mem_limit` to the container's systemd
  slice for as long as the build runs.
- `sysforge artifact deploy` and `remove` write or delete the files you adopted, and
  `pre_build_snapshot = true` (off by default) takes a btrfs snapshot before builds.

The [fresh-install pipeline](fresh-install.md) is different: run from the Arch ISO, it
partitions disks and installs a whole system.

---

## Undoing it

For one package, `sysforge revert-to-stock <pkg>` reinstalls the repo build, and
`sysforge state forget <pkg>` stops maintaining it but keeps your build installed.

To remove SysForge entirely, run:

```bash
sysforge self-uninstall --dry-run   # see what it will do
sysforge self-uninstall
```

It puts back the repo build of every package SysForge built that a repo carries. A build no repo
carries, such as an AUR package or your own kernel, stays installed and keeps working, since
there is nothing to go back to. Then it takes `sf-build` out of `IgnoreGroup` in
`/etc/pacman.conf` and the build sandbox's local repo out of the chroot's `pacman.conf`, removes
the package, and deletes `/etc/sysforge`, `/var/lib/sysforge`, `/var/cache/sysforge` (with any
optimization profiles), the `sysforge` directories under `~/.cache`, `~/.local/share`,
`~/.config` and `~/.local/state`, and the `sysforge` group. If a revert fails, it stops there,
with SysForge still installed, so you can fix it and run the command again.

`--keep-data` removes only the package: config you edited stays as `/etc/sysforge/*.pacsave`,
and the state, profiles, group and your own directories stay too. Scripts you adopted with
`sysforge artifact` stay where you deployed them either way; only SysForge's copies go. A
`sysforge run reconfigure` setting in `/etc/environment` is yours to remove.
