# SysForge Test VM

QEMU/KVM test environment for validating the kernel stage and other pipeline
work that requires a real system (mkinitcpio, bootloader, actual kernel boot).

## Dependencies

```bash
make vm-deps
```

Installs: `qemu-desktop`, `edk2-ovmf`

Also install `gvncviewer` for the ISO install step:

```bash
sudo pacman -S gtk-vnc
```

## Directory layout

VM state lives in `~/.local/share/sysforge-vm/` (override with `SYSFORGE_VM_DIR`).
Nothing in this directory is committed — it holds large binary files.

```
~/.local/share/sysforge-vm/
    arch-sysforge.qcow2    # disk image (created by make vm-image)
    OVMF_VARS.4m.qcow2     # per-VM EFI vars (qcow2 for savevm support; created on first boot)
    archlinux.iso          # Arch ISO (you provide this)
    known_hosts            # VM-local SSH known hosts (avoids ~/.ssh/known_hosts pollution)
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

ISO mode exposes a VNC display. Connect with `gvncviewer`:

```bash
gvncviewer localhost
```

In the VNC console, set the root password so you can SSH in for the rest:

```bash
passwd
systemctl start sshd
```

Then from the host, transfer the config and run the installer over SSH:

```bash
# On host — serve the config over HTTP
cd tools/vm && python3 -m http.server 8080

# SSH into the live environment
make vm-ssh-root

# In the live environment
curl http://10.0.2.2:8080/archinstall-config.json -o /root/archinstall-config.json
archinstall --config /root/archinstall-config.json --silent
```

After install completes, shut down the VM:

```bash
poweroff
```

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
make vm-ssh        # logs in as builder (makepkg cannot run as root)
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
make vm-ssh        # builder user (makepkg / sysforge work)
make vm-ssh-root   # root (admin tasks)

# Open the QEMU monitor (savevm, loadvm, quit, etc.)
make vm-monitor
```

## Resetting

```bash
make vm-monitor
# loadvm clean
```

Or just exit (`quit` in monitor or Ctrl-C in the terminal) and `make vm-snapshot`
again — `--snapshot` always starts from the saved `clean` state.

## Kernel stage testing workflow

1. `make vm-snapshot` — start from clean state
2. `make vm-ssh` — SSH in as builder
3. Clone the kernel PKGBUILD into `~/builds/`
4. Write `/etc/sysforge/kernel.toml`
5. `sysforge pipeline --start-from kernel`
6. Verify boot, check mkinitcpio output, verify bootctl entries
7. `quit` in monitor — changes discarded
8. Repeat from step 1 for the next iteration

## Notes

- VM runs headless — no display window. All access is via SSH.
- ISO install is the exception: `make vm-iso` enables VNC on `localhost:5900`.
  Use `gvncviewer localhost` to reach the console.
- SSH is forwarded: `host:10022 → VM:22`
- SSH host keys are stored in `~/.local/share/sysforge-vm/known_hosts` —
  isolated from `~/.ssh/known_hosts`. Delete it after a VM reinstall.
- QEMU monitor socket: `~/.local/share/sysforge-vm/qemu-monitor.sock`
  Connect with `make vm-monitor` (wraps `socat`)
- OVMF vars are stored as qcow2 so `savevm` works. Do not use the system
  template (`OVMF_VARS.4m.fd`) directly.
- Secure Boot is intentionally disabled (using `OVMF_CODE.4m.fd` not
  `OVMF_CODE.secboot.4m.fd`). Enable it in `boot.sh` if you need SB testing.
- `builder` user (password: `builder`) has sudo — use for all makepkg/sysforge work.
