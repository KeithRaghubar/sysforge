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

To remove SysForge entirely, revert the packages you want back to stock, then
`sudo pacman -Rns sysforge`. Pacman removes the hooks and package files and keeps any config you
edited as `/etc/sysforge/*.pacsave`. Delete `/var/lib/sysforge`, `/var/cache/sysforge` and the
`sysforge` directories under `~/.cache`, `~/.local/share` and `~/.config` yourself, remove
`sf-build` from `IgnoreGroup` in `/etc/pacman.conf` if `setup` added it, and
`sudo groupdel sysforge`. Packages it built stay installed and keep working.
