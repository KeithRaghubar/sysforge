# SysForge Test VM

QEMU/KVM test environment for validating the kernel stage and other pipeline
work that requires a real system (mkinitcpio, bootloader, actual kernel boot).

## Dependencies

```bash
make vm-deps
```

Installs: `qemu-desktop`, `edk2-ovmf`

## Directory layout

VM state lives in `~/.local/share/sysforge-vm/` (override with `SYSFORGE_VM_DIR`).
Nothing in this directory is committed — it holds large binary files.

```
~/.local/share/sysforge-vm/
    arch-sysforge.qcow2    # disk image (created by make vm-image)
    OVMF_VARS.4m.fd        # per-VM EFI vars (copied from system template on first boot)
    archlinux.iso          # Arch ISO (you provide this)
```

## First-time setup

### 1. Place an Arch ISO

Download the latest Arch ISO and put it at:
```
~/.local/share/sysforge-vm/archlinux.iso
```

### 2. Create the disk image

```bash
make vm-image
```

Creates a 40 GiB thin-provisioned qcow2 image.

### 3. Install Arch into the VM

```bash
./tools/vm/boot.sh --iso
```

Inside the VM:
```bash
# Set a root password (archinstall will use it)
# Then run unattended install:
archinstall --config /run/archiso/copytoram/boot/syslinux/ ...
# Or interactively — just make sure systemd-boot is the bootloader.
```

For a fully unattended install, mount or copy `archinstall-config.json` into
the live environment and run:
```bash
archinstall --config /path/to/archinstall-config.json --disk-layouts /path/to/archinstall-config.json
```

The config in this repo installs: systemd-boot, NetworkManager, sshd, python,
uv, git, base-devel. Root password is not set in the config — archinstall will
prompt (or pass `--root-password` on the CLI for full automation).

### 4. After install: save the clean snapshot

Reboot into the installed system, then from the QEMU monitor (Ctrl-Alt-2):
```
savevm clean
```

This is your reset point. Every test run can start from here.

### 5. Install SysForge into the VM

SSH in (or use the QEMU window):
```bash
ssh -p 10022 root@localhost
```

Then inside the VM:
```bash
git clone https://github.com/KeithRaghubar/sysforge.git
cd sysforge
makepkg -si
```

Save another snapshot after this if you want a pre-installed baseline:
```
savevm sysforge-installed
```

## Regular use

```bash
# Boot from clean snapshot (changes discarded on exit — safe for testing)
make vm-snapshot

# Boot normally (changes persist — use when you want to keep state)
make vm-boot

# SSH into a running VM
ssh -p 10022 root@localhost
```

## Resetting

From within QEMU monitor (Ctrl-Alt-2):
```
loadvm clean
```

Or just exit and `make vm-snapshot` again — `--snapshot` always starts from
the last saved `clean` state.

## Kernel stage testing workflow

1. `make vm-snapshot` — start from clean state
2. SSH in, clone the kernel PKGBUILD into `~/builds/`
3. Write `/etc/sysforge/kernel.toml`
4. `sysforge install --start-from kernel`
5. Verify boot, check mkinitcpio output, verify bootctl entries
6. Exit QEMU — changes discarded
7. Repeat from step 1 for the next iteration

## Notes

- SSH is forwarded: `host:10022 → VM:22`
- QEMU monitor: Ctrl-Alt-2 (switch back with Ctrl-Alt-1)
- OVMF vars (`OVMF_VARS.4m.fd`) are copied from the system template on first
  boot and then kept writable — this is how EFI variable persistence works
  across reboots. Do not use the system template directly.
- Secure Boot is intentionally disabled (using `OVMF_CODE.4m.fd` not
  `OVMF_CODE.secboot.4m.fd`). Enable it in `boot.sh` if you need SB testing.
