# Building a Kernel with SysForge

How `sysforge run kernel` builds, reviews and installs a custom kernel, how to run it
unattended, how to control the kernel config, and the checks that keep a bad build from leaving
the machine unbootable.

Related guides: [boot-entries.md](boot-entries.md) (the systemd-boot menu entries sysforge
writes) and [pgo.md](pgo.md) (AutoFDO and Propeller for the kernel). For the background
(Kconfig, modules, the initramfs, boot entries and fallback kernels) see
[Kernel builds and boot basics](../concepts/kernel-and-boot.md). For the internals see
[DESIGN.md](../../DESIGN.md) (§Kernel stage). This guide only describes how to use it.

---

## Turning it on

The kernel stage is off until you enable it in `kernel.toml`:

```toml
enabled = true
pkgname = "linux-sysforge"         # the name your kernel installs as
upstream_pkgname = "linux-zen"     # the kernel package to track and build from
```

With `upstream_pkgname` set, sysforge clones or updates that package's PKGBUILD and renames the
build to `pkgname`, so your kernel installs next to the stock one rather than replacing it.
Without it, sysforge builds the tree named `pkgname` under your `pkgbuild_src_dir` as it is.

Your kernel is then rebuilt by `sysforge run kernel`. `sysforge update` skips it: it tells you
when a new version is available and leaves the build to the kernel stage.

---

## What a run does

1. **Sync** the PKGBUILD tree (see [Local changes to the kernel tree](#local-changes-to-the-kernel-tree)).
2. **Preflight** (Gate 1): check for a fallback kernel and free space on `/boot`.
3. **Build**, with a pause to review the kernel config (below). Nothing is installed yet.
4. **Audit** the built config (Gate 2). A kernel that could not boot this machine stops here.
5. **Install**, then `mkinitcpio -P` and the bootloader update.
6. **Verify** the result (Gate 3): kernel image, initramfs and boot entry present, and DKMS
   modules rebuilt.

`sysforge run kernel --dry-run` shows the resolved plan without building anything.

---

## Reviewing the config

By default a kernel run opens a config review. After sysforge has merged its config changes into the
kernel's `.config`, the build pauses with a prompt saying the merged config is assembled and
asking you to press Enter to review it.

This is your one chance to read the assembled config before the menu opens. Press Enter and
`make nconfig` opens on the result. Change what you want, save and exit, and the build
continues. Ctrl-C at the pause aborts the build with nothing installed.

If the PKGBUILD runs its own menu (`make menuconfig`, for example), sysforge uses that one and
adds no second menu or pause.

The review needs a real terminal. A run with its output piped or captured skips it and warns
(an `--unattended` run skips it without the warning, since you asked for that).

An interactive build's output is still logged. The whole of it goes to the kernel's
per-package log, and if the build fails, its last lines also go to the run log
(`sysforge-run-kernel.log`), so you can see why a build failed after the terminal has
scrolled past it.

---

## Skipping the config review, or running unattended

The stage asks two separate things, and each has its own switch.

**The config review** is the `make nconfig` (or `menuconfig`) step where you look over the
merged config before the build. To skip it for one run:

```bash
sysforge run kernel --no-review-config
```

or for good, in `kernel.toml`:

```toml
review_config = false
```

The review step is dropped, and any interactive step the PKGBUILD has of its own (`make
nconfig`, `menuconfig`, `oldconfig` and the like) is rewritten to `make olddefconfig`, which
takes the kernel's default for every option the config doesn't set. The stage still asks its
safety questions below if something needs your answer.

**Unattended** means sysforge asks nothing at all: pass the global `--unattended` (or set
`[ui] unattended = true` in `sysforge.toml`), or run with no terminal, as a timer does. That
skips the config review too, and every safety question gets its safe answer, which for the
kernel stage is to stop rather than guess.

In `kernel.toml` this setting was called `interactive` before 3.5.0. An old `interactive =
false` still works and warns; `sysforge config clean` renames it.

Two other things stop a run rather than guess: a kernel tree with local changes, when
sysforge can't ask you about it (see the next section), and a prompting step named in
`kconfig_targets` on any run that skips the review (see [Choosing the config
steps](#choosing-the-config-steps)).

---

## Local changes to the kernel tree

If the kernel's upstream has new commits but your local tree can't simply move to them,
because you committed or edited something there, building would mean building from source that
is out of date or hand-edited. sysforge warns, with how many commits each side has, and asks
before building (the default is No). An unattended run stops instead.

To throw away your local changes and start from upstream:

```bash
sysforge run kernel --cleansrc
```

`--cleansrc` refuses if the tree has uncommitted changes or commits upstream doesn't have.
`--cleansrc-force` deletes and re-clones the tree regardless. Use it only when there is nothing
in the tree you want to keep.

---

## Where the config comes from

Your kernel's `.config` is built up in layers, each applied over the one before:

1. **The base.** By default, the PKGBUILD's own config. Set `base_config = "running"` to start
   from the kernel you are running, or give a path to a config file. With `base_config_merge =
   "overlay"`, that base is laid over the PKGBUILD's config instead of replacing it, so options
   new in this kernel keep the packager's choices.
2. **sysforge's fragment**, which merges, in increasing priority:
   - the hardening set, if you set `hardening = true` (see [Hardening](#hardening) below);
   - drivers for the devices in this machine, from the hardware stage (`device_kconfig`);
   - CPU and platform options, from the hardware stage;
   - your own `[[kconfig]]` entries in `kernel.toml`, which win over all of them:

     ```toml
     [[kconfig]]
     option = "CONFIG_HZ_1000"
     value = "y"
     ```

3. **The config steps**: the PKGBUILD's own, or your `kconfig_targets` (next section).
4. **Your review** in `nconfig`, unless the review is skipped.

After the build, sysforge warns about any option it merged that did not survive into the final
config. Usually you turned it off in the menu, or a step such as `olddefconfig` dropped it
because something it depends on is off. This is a warning only. To skip the fragment entirely,
set `kconfig_merge = false`.

### Hardening

`hardening = true` in `kernel.toml` adds a curated set of compile-time hardening options to the
fragment, taken from the kernel's own self-protection recommendations:

```toml
hardening = true
```

It covers read-only, non-executable kernel and module memory, address and stack randomisation,
the stack protector and `FORTIFY_SOURCE`, hardened copies to and from user space, allocator
hardening, zeroed stack and heap memory, checks that stop on a corrupted kernel list, and a
smaller `/dev/mem` and system-call surface. Arch's own config already turns most of these on.
Listing them keeps a different base config, or a step such as `localmodconfig`, from dropping
them. Three are not on in Arch's config: `BUG_ON_DATA_CORRUPTION`, and two that cost some speed,
`INIT_ON_FREE_DEFAULT_ON` (zeroes memory as it is freed, a few percent on allocation-heavy work)
and `ZERO_CALL_USED_REGS` (small). `sysforge run kernel --dry-run` shows `hardening: on` in the
build plan.

The set has the lowest priority in the fragment. Any other entry for the same option wins, with
a warning, so you can turn one back off:

```toml
[[kconfig]]
option = "CONFIG_INIT_ON_FREE_DEFAULT_ON"
value = "n"
```

**It never makes the kernel require signed modules.** Module signature enforcement
(`MODULE_SIG_FORCE`) and a forced lockdown mode would stop unsigned DKMS modules, such as
`nvidia-open-dkms`, from loading, and the next boot would have no graphics driver. If you add one
of those yourself as a `[[kconfig]]` entry while DKMS modules are registered, sysforge refuses
before the build and names the modules.

This covers only the options compiled into the kernel. Runtime hardening, such as `sysctl.d`
settings and kernel command-line options, is system policy and outside what sysforge manages;
see the Arch wiki's [Security](https://wiki.archlinux.org/title/Security) page. `hardening`
needs `kconfig_merge` on: with `kconfig_merge = false` the run refuses rather than build an
unhardened kernel.

---

## Choosing the config steps

`kconfig_targets` in `kernel.toml` replaces the PKGBUILD's own config steps with a list of
`make` targets that you choose. Unset (the default), the PKGBUILD's steps run unchanged.

```toml
kconfig_targets = ["olddefconfig", "nconfig"]
```

The targets fall into four groups:

| Group | Targets | Rule |
| --- | --- | --- |
| Silent | `olddefconfig`, `defconfig`, `allmodconfig`, `alldefconfig`, `savedefconfig`, `listnewconfig` | always allowed, run in your order |
| Prompting | `oldconfig`, `localmodconfig`, `localyesconfig`, `mod2yesconfig` | only when the review runs |
| Menu | `config`, `nconfig`, `menuconfig`, `xconfig`, `gconfig` | at most one, always run last |
| Refused | `randconfig` | never: a random config is a boot hazard |

A run that skips the review refuses a prompting target instead of hanging on its prompt. For
`oldconfig` the error names `olddefconfig`, its silent equivalent. The others have none, so
they need the review on. A menu target listed earlier is moved to the end, so it always shows the
finished config, with the same pause before it as the default review. When the review is
skipped it becomes `olddefconfig`.

### Trimming to this machine's hardware

`localmodconfig` and `localyesconfig` cut the config down to the modules loaded right now. That
makes a smaller, faster build, and it is risky: a driver for anything not loaded at build time
is removed, whether it is a USB stick, a dock, or a filesystem you mount once a month. sysforge
warns whenever one is in the list.

Two settings soften it:

- `capture_lsmod_snapshot` (default on) keeps a running record of every module loaded across
  your builds at `<state_dir>/lsmod.snapshot`, and the trim uses that record rather than only
  what is loaded at that moment. Plug in and use your devices before a build to add them. To
  start the record over, delete the file.
- `keep_hotplug_drivers = true` (or `--keep-hotplug-drivers`) adds back, as modules, the driver
  classes for devices you attach later: USB, Thunderbolt, SD cards, hot-plug PCI and HID, and
  the filesystems removable media use.

```toml
kconfig_targets = ["localmodconfig", "nconfig"]
keep_hotplug_drivers = true
```

---

## Boot safety

The kernel stage is built so that a bad kernel never becomes your only kernel. The checks, and
how to override each:

| Check | When | Fails if | Override |
| --- | --- | --- | --- |
| Fallback kernel | before the build | no stock `linux` or `linux-lts` with a boot image is installed | `require_fallback_kernel = false` or `--allow-no-fallback` |
| `/boot` space | before the build | `/boot` is missing or has less than `min_boot_free_mb` (200 MiB) free | `boot_audit = false` |
| Config audit (Gate 2) | after the build, before install | the built config drops your root filesystem, its storage controller, encryption/LVM/RAID support, or core boot options | `--allow-boot-audit-failure` or `boot_audit = false` |
| Boot readiness (Gate 3) | after install | the kernel image or initramfs is missing, no boot entry uses it, or a DKMS module (e.g. `nvidia-open-dkms`) was not rebuilt for it | none |

The config audit runs before anything is installed, so a kernel it rejects never touches your
system. It catches the hard case where one change quietly switches off others that depend on it.
Missing drivers for other devices in the machine, and console or framebuffer changes, are only
warnings.

Keep the fallback kernel and its boot entry. If a new kernel fails to boot, choose the fallback
from the boot menu. The overrides in the table are for machines where you already have another
way back. `--allow-boot-audit-failure` in particular can install a kernel that cannot mount your root
filesystem.

**DKMS modules need the headers.** `build_headers` is on by default because out-of-tree modules
such as `nvidia-open-dkms` compile against them. Turning it off (`--no-headers`) means those
modules cannot be built for the new kernel and will not load after a reboot.

**If the install is interrupted** (power loss, a closed terminal) between the install and the
final check, the next `sysforge` command stops and offers to regenerate the initramfs (`sudo
mkinitcpio -P`), the step whose absence would leave the kernel unbootable.

---

## Quick reference

| I want to... | Run or set |
| --- | --- |
| build and review the config | `sysforge run kernel` |
| see the plan without building | `sysforge run kernel --dry-run` |
| skip the config review | `sysforge run kernel --no-review-config` |
| build from a timer or script | `sysforge --unattended run kernel` (or no terminal) |
| start from my running kernel's config | `base_config = "running"` or `--base-config running` |
| force a config option | a `[[kconfig]]` entry in `kernel.toml` |
| add the compile-time hardening set | `hardening = true` |
| pick the config steps | `kconfig_targets = [...]` |
| discard local edits to the kernel tree | `sysforge run kernel --cleansrc` |
| build with clang | `compiler = "llvm"` or `--toolchain llvm` |
| list boot entries | `sysforge state boot-entries` |
