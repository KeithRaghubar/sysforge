# SysForge Test VM

QEMU/KVM test environment for validating the kernel stage and other pipeline
work that requires a real system (mkinitcpio, bootloader, actual kernel boot).

**Two test tiers.** This is the heavy one. The packaging and portability checks
in `tools/smoke.sh` need none of the above, so they also run against a throwaway
container in seconds, on Arch *and* on a derivative — see
[`tools/container/README.md`](../container/README.md). Use the VM for what only a
booted machine can answer: bootstrap/install, kernel staging, graphics/DKMS,
restart detection.

## Dependencies

```bash
make vm-deps
```

Installs: `qemu-desktop`, `edk2-ovmf`, `gtk-vnc` (provides `gvncviewer` for the
ISO install / GUI steps), and `socat` (used by `vm-monitor`, `vm-console`,
`vm-savevm`, `vm-stop`) — the `DEV_DEPS_VM` set in the Makefile.

`make vm-pkg-*` additionally needs `devtools` (`make dev-deps-pkg`) for the clean
chroot. `make dev-deps` installs every tier at once; `make dev-deps-list` shows
what each needs and what is already present.

## Directory layout

VM state lives in `~/.local/share/sysforge-vm/` (override with `SYSFORGE_VM_DIR`).
Nothing in this directory is committed — it holds large binary files.

```
~/.local/share/sysforge-vm/
    arch-sysforge.qcow2    # disk image (created by make vm-image)
    OVMF_VARS.4m.qcow2     # per-VM EFI vars (qcow2 for savevm support; created on first boot)
    *.iso                  # installer ISO (you provide this; any filename)
    known_hosts            # VM-local SSH known hosts (avoids ~/.ssh/known_hosts pollution)
    qemu-monitor.sock      # QEMU monitor socket (created at runtime)
```

## First-time setup

### 1. Place an installer ISO

Download the latest Arch ISO and drop it in the VM directory — the filename does
not matter:
```
~/.local/share/sysforge-vm/
```

`make vm-iso` uses the single `*.iso` it finds there. If the directory holds more
than one, name the one to boot:
```bash
SYSFORGE_VM_ISO=archlinux-2026.01.01-x86_64.iso make vm-iso
```
`SYSFORGE_VM_ISO` takes a bare filename (resolved under the VM directory) or an
absolute path.

Nothing in the VM tooling assumes a distro, so a parallel tree for a second one
works with no code change — give it its own directory so the disk images and
snapshots stay separate:
```bash
SYSFORGE_VM_DIR=~/.cache/sysforge-vm-other make vm-image vm-iso
```
Note that only the Arch install is automated: `tools/vm/archinstall-config.json`
drives it. Another distro ships its own installer, so its VM is a hand-built
one-time snapshot that no target can regenerate.

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

This checked-in `archinstall-config.json` is the manual VM path and also the
**golden fixture** for `primitives/archinstall_config.py`: the bootstrap
`install` stage generates an equivalent config from `bootstrap.toml` at runtime
and runs the same `archinstall --config … --silent` (see DESIGN.md §Pipeline
Layer → *Install stage*). Keep the fixture in step with the 3.0.15 schema the
builder targets.

After install completes, shut down the VM:

```bash
poweroff
```

### 4. Boot and save the clean snapshot

```bash
make vm-boot
```

Once the VM has fully booted (give it ~10 seconds), save the clean snapshot
via the `vm-savevm` wrapper:

```bash
make vm-savevm NAME=clean
```

This is your reset point. Every test run can start from here. To boot straight
from a saved snapshot (instead of `loadvm` in the monitor after boot), use
`vm-loadvm`, which restores it at startup via QEMU's `-loadvm`:

```bash
make vm-loadvm NAME=clean          # headless
make vm-loadvm NAME=clean GUI=1    # with a VNC display
make vm-loadvm clean               # name may also be given positionally
```

### 4a. Enable the serial console (existing VM only)

New installs get `console=ttyS0` baked into the bootloader entries by step 3's
`archinstall-config.json`. A VM installed *before* that change needs it added
once, in-guest (it's systemd-boot loader-entry state, not settable from the host):

```bash
make vm-ssh-root
# Inside the VM:
sed -i '/^options /{/console=ttyS0/!s/$/ console=tty0 console=ttyS0,115200/}' /boot/loader/entries/*.conf
reboot
```

After this, `make vm-console` reaches a login prompt on the serial console.
`systemd` auto-starts `serial-getty@ttyS0` once the cmdline names the port — no
explicit `systemctl enable` is needed. Re-save your clean snapshot afterward.

> **Historical note on the savevm wrapper.** Older libslirp had a BOOTP
> VMState bug — plain `savevm` over SLIRP user-mode networking emitted
> `warning: Slirp: Save of field slirp_bootpclient/macaddr failed` and
> produced a snapshot whose networking was unusable. `make vm-savevm` used to
> work around it by detaching the netdev backend (`netdev_del net0`) before
> the save and reattaching it after. That workaround is now removed: on
> qemu 11.0.1 / libslirp 4.9.3 a plain `savevm` snapshot restores (via a
> fresh `-loadvm`) with the SSH host-forward fully working, and the
> `netdev_del`/`netdev_add` dance was actively harmful — `netdev_del` on a
> slirp backend still peered to its NIC frontend does **not** close the
> hostfwd listening socket on port 10022, so the reattach could never rebind
> the port and left the VM with no network backend at all. `make vm-savevm`
> now just runs `savevm` and SSH on port 10022 stays up throughout.

### 5. Install SysForge into the VM

The recommended path builds a `.pkg.tar.zst` from your **local working tree**
on the host (same clean chroot used by `tools/release.sh`) and installs it
into the VM over SSH. This way, PKGBUILD changes can be validated against the
VM before they hit AUR.

One-time chroot setup on the host (skip if already done):

```bash
sudo mkarchroot /var/lib/archbuild/extra-x86_64/root base-devel
```

`devtools` (which provides `makechrootpkg`) must also be installed.

Then, with the VM running (`make vm-snapshot` — backgrounds automatically):

```bash
# Build a .pkg.tar.zst from the working tree via the clean chroot.
# Output lands in ~/.local/share/sysforge-vm/build/.
make vm-pkg-stable        # PKGBUILD (release flavor — includes uncommitted edits)
make vm-pkg-git           # PKGBUILD-git (VCS flavor — committed state only)
make vm-pkg-all           # both

# scp + sudo pacman -U into the running VM.
make vm-install-stable
make vm-install-git

# Combined: vm-pkg-stable + vm-install-stable.
make vm-test
```

#### Flavor semantics

| Flavor | PKGBUILD used | Source basis | Notes |
|---|---|---|---|
| `stable` | `PKGBUILD` | Tarball of the live working tree | Mirrors AUR `sysforge`. Picks up uncommitted edits. |
| `git` | `PKGBUILD-git` | Local bare clone of the repo | Mirrors AUR `sysforge-git`. Only sees *committed* state — warns if working tree is dirty. |

`conflicts=('sysforge')` in PKGBUILD-git means installing the `git` flavor will
remove a previously-installed `stable` build, and vice versa via
`conflicts=('sysforge-git')`. That conflict-pair is itself part of what these
targets validate.

#### Sanity checks after install

Run `make vm-smoke` to assert all of the following automatically (exits non-zero
if any fail), or check them by hand. It also asserts the portability invariants
(distro identity, sync-repo discovery, `makepkg.conf` merge baseline, version
compare) — the same checks the container tier runs, from the one copy in
`tools/smoke.sh`:

```bash
make vm-ssh
# Inside the VM:
sysforge --version
pacman -Qi sysforge                                 # description, deps, optdeps
ls /usr/share/libalpm/hooks/sysforge-*.hook         # 3 pacman hooks installed
ls /usr/share/bash-completion/completions/sysforge  # bash completion installed
ls /usr/share/zsh/site-functions/_sysforge          # zsh completion installed
ls -ld /var/lib/sysforge/sentinels                  # tmpfiles.d created the dir
```

Save a baseline snapshot after the install if you want a pre-installed VM:
```bash
make vm-savevm NAME=sysforge-installed
```

#### Alternative (legacy): build inside the VM

```bash
make vm-ssh-builder   # makepkg refuses to run as root
git clone https://github.com/KeithRaghubar/sysforge.git
cd sysforge && makepkg -si
```

Only exercises the *pushed* GitHub state, not local edits. Useful as a smoke
test of what AUR users will see post-release.

#### Error paths

Both helper scripts (`tools/vm/build-pkg.sh`, `tools/vm/install-pkg.sh`) exit
non-zero with an actionable hint if a prerequisite is missing:

- Chroot absent → suggests `sudo mkarchroot …`
- `makechrootpkg` absent → suggests `pacman -S devtools`
- VM SSH port 10022 unreachable → suggests `make vm-snapshot`
- No built `.pkg.tar.zst` found → suggests `make vm-pkg-<flavor>`

Run them directly if you need flags the Make targets don't expose.

## Regular use

All three boot targets (`vm-snapshot`, `vm-boot`, `vm-iso`) run QEMU with
`-daemonize`: they return immediately and the VM keeps running in the
background. Use `make vm-stop` to shut it down.

```bash
# Boot ephemerally (changes discarded on exit — safe for testing)
make vm-snapshot

# Boot normally (changes persist — use when you want to keep state)
make vm-boot

# Boot the installed disk with a VNC display (to see a graphical desktop)
make vm-boot-gui   # then: gvncviewer localhost

# SSH into a running VM
make vm-ssh         # root (default — works in the ISO and for any username)
make vm-ssh-builder # builder user (makepkg / sysforge work, post-install only)
make vm-ssh-root    # root (explicit; same as vm-ssh)

# Open the QEMU monitor (loadvm, info snapshots, quit, etc.)
# For savevm use 'make vm-savevm NAME=<tag>' — see step 4 for why.
make vm-monitor

# Stop the VM (clean shutdown via monitor; falls back to kill)
make vm-stop
```

## Resetting

`--snapshot` boots in *ephemeral mode* — writes go to a throwaway overlay over
the current on-disk state. It does **not** auto-load a named snapshot inside
the qcow2. To start from a specific saved snapshot:

```bash
make vm-monitor
# loadvm clean        # (or any snapshot from `info snapshots`)
```

Then `make vm-stop` and re-run `make vm-snapshot` for the next ephemeral run.

## Kernel stage testing workflow

1. `make vm-snapshot` — start from clean state
2. `make vm-ssh-builder` — SSH in as builder (makepkg needs a non-root user)
3. Clone the kernel PKGBUILD into `~/builds/`
4. Write `/etc/sysforge/kernel.toml`
5. `sysforge pipeline --start-from kernel`
6. Verify boot, check mkinitcpio output, verify bootctl entries
7. `quit` in monitor — changes discarded
8. Repeat from step 1 for the next iteration

If a stage reboots the VM and resumes on the console (before SSH is back), read
its prompts with `make vm-console` rather than `gvncviewer` — the serial console
won't clip the bottom rows the way the VNC display can.

## Notes

- VM runs headless by default — no display window. Primary access is via SSH;
  `make vm-console` is the text-mode fallback when SSH isn't reachable.
- Every boot exposes a **serial console** on a host socket. Attach with
  `make vm-console` (detach with `Ctrl-]`) to read interactive text prompts —
  e.g. the configure stage — when SSH isn't available (post-reboot, mid-pipeline).
  Requires `console=ttyS0` on the guest cmdline; new installs get this
  automatically (step 3 bakes it in), existing VMs need the one-time step 4a.
- Two targets enable a VNC display on `localhost:5900` (reach it with
  `gvncviewer localhost`): `make vm-iso` for the interactive Arch install, and
  `make vm-boot-gui` to boot the already-installed disk with a display (e.g. to
  see a desktop environment render). All other boot targets stay headless. The
  VNC framebuffer is pinned to 1280x720 (via `virtio-vga,edid=on,xres=…,yres=…`)
  so the gvncviewer window (no scrollbar, no scaling) doesn't clip the bottom
  rows, and so the guest re-selects that resolution when gvncviewer disconnects
  and reconnects rather than dropping to a default; for reliable text prompts
  prefer `make vm-console`.
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
