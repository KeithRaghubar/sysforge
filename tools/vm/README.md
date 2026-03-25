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
    OVMF_VARS.4m.qcow2     # per-VM EFI vars (converted from system template on first boot; qcow2 for savevm support)
    archlinux.iso          # Arch ISO (you provide this)
    qemu-monitor.sock      # QEMU monitor socket (created at runtime)
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
make vm-iso
```

ISO mode opens a VNC display for interactive access. Connect with any VNC client:

```bash
vncviewer localhost:5900
```

Inside the live environment, transfer the config and run the installer:

```bash
# On host — serve the config over HTTP
cd tools/vm && python3 -m http.server 8080

# In VM console (host is reachable at 10.0.2.2 via QEMU user networking)
curl http://10.0.2.2:8080/archinstall-config.json -o /root/archinstall-config.json
archinstall --config /root/archinstall-config.json --silent
```

After install completes, shut down the VM (`poweroff` inside the VM or `quit`
in the QEMU monitor).

### 4. Boot and save the clean snapshot

```bash
make vm-boot
```

Once the VM has fully booted (give it ~10 seconds), save the clean snapshot:

```bash
make vm-monitor
# Inside the monitor:
savevm clean
quit
```

This is your reset point. Every test run can start from here.

### 5. Install SysForge into the VM

```bash
make vm-snapshot   # boot from clean snapshot
ssh -p 10022 root@localhost
```

Inside the VM:
```bash
git clone https://github.com/KeithRaghubar/sysforge.git
cd sysforge
makepkg -si
```

Save another snapshot after this if you want a pre-installed baseline:
```bash
make vm-monitor
# savevm sysforge-installed
```

## Regular use

```bash
# Boot from clean snapshot (changes discarded on exit — safe for testing)
make vm-snapshot

# Boot normally (changes persist — use when you want to keep state)
make vm-boot

# SSH into a running VM
ssh -p 10022 root@localhost

# Open the QEMU monitor (savevm, loadvm, quit, etc.)
make vm-monitor
```

## Resetting

```bash
make vm-monitor
# loadvm clean
```

Or just exit (`quit` in monitor or Ctrl-C in the boot terminal) and
`make vm-snapshot` again — `--snapshot` always starts from the saved `clean` state.

## Kernel stage testing workflow

1. `make vm-snapshot` — start from clean state
2. SSH in, clone the kernel PKGBUILD into `~/builds/`
3. Write `/etc/sysforge/kernel.toml`
4. `sysforge pipeline --start-from kernel`
5. Verify boot, check mkinitcpio output, verify bootctl entries
6. `quit` in monitor — changes discarded
7. Repeat from step 1 for the next iteration

## Notes

- VM runs headless — no display window. All access is via SSH.
- SSH is forwarded: `host:10022 → VM:22`
- QEMU monitor socket: `~/.local/share/sysforge-vm/qemu-monitor.sock`
  Connect with `make vm-monitor` (wraps `socat`)
- OVMF vars (`OVMF_VARS.4m.fd`) are copied from the system template on first
  boot and then kept writable — this is how EFI variable persistence works
  across reboots. Do not use the system template directly.
- Secure Boot is intentionally disabled (using `OVMF_CODE.4m.fd` not
  `OVMF_CODE.secboot.4m.fd`). Enable it in `boot.sh` if you need SB testing.
