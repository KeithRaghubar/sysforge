# Boot Entries with SysForge

How sysforge adds your custom kernels to the systemd-boot menu, how that menu is ordered,
and how to control which entry boots. This guide applies to `bootloader = "systemd-boot"`
(the default). Under `grub`, `grub-mkconfig` discovers kernels by itself and none of this
applies.

For the internals see [DESIGN.md](../../DESIGN.md). This guide only describes how to use it.

---

## What sysforge manages

Each kernel built by `sysforge run kernel` gets one loader entry,
`/boot/loader/entries/sysforge-<kernel>.conf`. The entry is cloned from the entry you booted
(its `options` line, microcode `initrd` lines and so on), with the kernel image and
initramfs names swapped and a generated title such as `Arch Linux (sysforge)`. The first
line is a `# sysforge-managed` header recording which of your entries it was cloned from.

Your own entries are **never modified, renamed or deleted**. sysforge only reads them: as a
template, and to notice that one of them already boots a kernel (in which case it writes
nothing for that kernel). A file named `sysforge-*.conf` without the header is treated as
yours and is never overwritten; sysforge tells you and skips it.

The new entry keeps your entry's directory layout, so sysforge checks that every `linux`
and `initrd` path it would write exists under `/boot`. If one does not (a template that
loads from a subdirectory the new kernel is not installed to, or an initramfs generator whose
image sysforge does not produce), it refuses to write that entry, says which paths are
missing, and the kernel run reports that its boot entry could not be written. Set
`boot_entries = "off"` in `kernel.toml` and write that entry by hand. A template that loads
the `-fallback` initramfs is rewritten to the new kernel's regular initramfs.

Every kernel build re-renders all managed entries from their template, so an edit to your
own entry's `options` reaches the sysforge entries on the next build. Entries whose kernel
has been removed are pruned. `sysforge update --dry-run` (and its read-only reports) never
prunes anything.

See what is in effect with:

```bash
sysforge state boot-entries
```

It lists each kernel in `/boot` with the entry that boots it, who owns that entry (`you`,
`sysforge`, or `none` if no entry boots it) and the template a managed entry was cloned from.

---

## How the menu is ordered

systemd-boot sorts entries by these rules:

1. Entries **with** a `sort-key` come first: by `sort-key` A to Z, then `machine-id`, then
   `version` descending.
2. Entries **without** one follow, ordered by filename (without `.conf`) under version
   comparison, **descending**. A higher number lists higher: `99-x` is above `97-x`, and any
   digit-led name is above a letter-led one.
3. Automatic entries (Windows, firmware setup) come last.

So with the default name `sysforge-<kernel>.conf` and numbered entries of your own, the
sysforge entries list below all of yours.

## Checking and predicting

`bootctl list` shows the real menu order. To predict where a filename lands before you
choose it:

```bash
systemd-analyze compare-versions 97-sysforge-linux-sysforge 97-arch-custom
```

It prints which of the two sorts higher.

## Ordering by numbered filenames

If you order your menu with numeric prefixes, set `boot_entries_prefix` in `kernel.toml`:

```toml
boot_entries_prefix = "97"
```

The prefix is letters, digits and underscores only. sysforge entries are then named
`97-sysforge-<kernel>.conf`. If you have `97-arch-custom.conf`, a sysforge entry sorts just
above it, and anything numbered `98` or higher sorts above both. Changing the prefix later is
safe: sysforge writes the new names and removes its own old files.

## Systems that use sort-key

If your entries carry a `sort-key` (typical of `kernel-install` setups), sysforge copies the
`sort-key` and `machine-id` and sets `version` to the kernel's release, so its entries group
with your OS and list newest first, as the system's own do. `boot_entries_prefix` is ignored
there (a filename prefix cannot reorder entries that have a `sort-key`); sysforge logs one
line saying so.

## The default entry is separate

sysforge never changes which entry boots by default. That is set elsewhere:

- `default <glob>` in `/boot/loader/loader.conf` (for example `default 97-arch-custom*`).
- `default @saved` makes the last entry you booted the default.
- `bootctl set-default <entry>` stores a default in an EFI variable.
- Pressing `d` in the boot menu does the same, and an entry chosen that way overrides
  `loader.conf`.

## Booting a kernel once

To try a new kernel without making it the default, name it for the next boot only:

```bash
sudo bootctl set-oneshot <entry-id>
```

The reboot after that returns to the default. `<entry-id>` is the entry's filename, for example `sysforge-linux-sysforge.conf`
(see `bootctl list`). After an AutoFDO profiling build (`--autofdo=record`) with managed
entries, the advisory shown at `-v` includes the exact command for the file written; for any
other kernel, run it yourself. Under `default @saved`, confirm the
default with `bootctl list` afterwards: whether a one-shot boot becomes the saved default has
not been verified.

## Turning it off

If you boot with unified kernel images or manage entries with `kernel-install`, set:

```toml
boot_entries = "off"
```

sysforge then writes no entries and Gate 3 expects you to provide one. The same setting is
the way out of a preflight refusal (for example, when the boot partition is not mounted at
`/boot`, since classic entries can only load kernels from their own partition).
