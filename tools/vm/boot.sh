#!/usr/bin/env bash
# boot.sh — launch the SysForge test VM (headless)
#
# Usage:
#   ./tools/vm/boot.sh              # boot normally (changes persist)
#   ./tools/vm/boot.sh --snapshot   # boot in ephemeral mode, discard changes on exit
#   ./tools/vm/boot.sh --iso        # boot from an installer ISO for initial install
#   ./tools/vm/boot.sh --gui        # boot installed disk with a VNC display
#   ./tools/vm/boot.sh --loadvm NAME  # restore named savevm snapshot at startup
#                                     # (combine with --gui for a graphical restore)
#
# All modes run QEMU with -daemonize: the script exits as soon as the VM has
# initialized, and the VM continues running in the background. Use
# 'make vm-stop' (or 'quit' in 'make vm-monitor') to shut it down.
#
# --snapshot uses QEMU's -snapshot flag: writes go to a throwaway overlay over
# the current on-disk state. It does NOT auto-load a named snapshot. To start
# from a specific savevm snapshot, 'loadvm NAME' via the monitor before
# stopping, or restore after the next boot.
#
# Normal boots run headless (no window). Access the installed VM via SSH:
#   ssh -p 10022 root@localhost
#
# ISO mode (--iso) and GUI mode (--gui) open a VNC display:
#   gvncviewer localhost
# --iso is for the interactive install; --gui boots the already-installed
# disk with a display so a desktop environment is visible.
#
# --iso picks the ISO out of $VM_DIR: the single *.iso there, or whatever
# SYSFORGE_VM_ISO names (bare filename relative to $VM_DIR, or an absolute
# path) when the directory holds more than one. Nothing here assumes a distro,
# so a parallel VM tree for a second one needs no code change:
#   SYSFORGE_VM_DIR=~/.cache/sysforge-vm-other make vm-image vm-iso
#
# QEMU monitor (for loadvm, info snapshots, etc.):
#   make vm-monitor               (wraps socat to ~/.local/share/sysforge-vm/qemu-monitor.sock)
#   make vm-savevm NAME=clean     — save current state as 'clean' snapshot
#                                   (wraps the netdev detach/reattach needed to
#                                    avoid the libslirp BOOTP VMState bug; see
#                                    Makefile + tools/vm/README.md)
#   loadvm clean                  — restore to 'clean' snapshot
#   info snapshots                — list saved snapshots
#   quit                          — stop the VM

set -euo pipefail

VM_DIR="${SYSFORGE_VM_DIR:-$HOME/.local/share/sysforge-vm}"

DISK_IMAGE="$VM_DIR/arch-sysforge.qcow2"
OVMF_VARS_TEMPLATE="/usr/share/edk2/x64/OVMF_VARS.4m.fd"
OVMF_CODE="/usr/share/edk2/x64/OVMF_CODE.4m.fd"
OVMF_VARS="$VM_DIR/OVMF_VARS.4m.qcow2"  # per-VM writable copy (qcow2 for snapshot support)
ISO_PATH=""  # resolved lazily by resolve_iso(); only --iso needs it

# Resolve the installer ISO for --iso mode into ISO_PATH, or exit 1 with
# remediation. Deliberately lazy: every other boot mode ignores the ISO
# entirely, so a VM dir with zero (or several) ISOs must not block a normal
# boot. An explicit SYSFORGE_VM_ISO always wins; otherwise the directory must
# hold exactly one *.iso — ambiguity is an error rather than a silent pick,
# since booting the wrong installer yields a VM that only looks correct.
resolve_iso() {
    if [[ -n "${SYSFORGE_VM_ISO:-}" ]]; then
        case "$SYSFORGE_VM_ISO" in
            */*) ISO_PATH="$SYSFORGE_VM_ISO" ;;          # path, used as given
            *)   ISO_PATH="$VM_DIR/$SYSFORGE_VM_ISO" ;;  # bare name, under $VM_DIR
        esac
        if [[ ! -f "$ISO_PATH" ]]; then
            echo "Installer ISO not found: $ISO_PATH" >&2
            echo "SYSFORGE_VM_ISO is set to '$SYSFORGE_VM_ISO' but no such file exists." >&2
            exit 1
        fi
        return
    fi

    local isos=()
    shopt -s nullglob
    isos=("$VM_DIR"/*.iso)
    shopt -u nullglob

    case ${#isos[@]} in
        1) ISO_PATH="${isos[0]}" ;;
        0)  echo "No installer ISO found in $VM_DIR" >&2
            echo "Download an ISO for the distro you want to install and place it there," >&2
            echo "or set SYSFORGE_VM_DIR to a directory containing one." >&2
            exit 1 ;;
        *)  echo "Multiple ISOs found in $VM_DIR:" >&2
            printf '  %s\n' "${isos[@]##*/}" >&2
            echo "Pick one with SYSFORGE_VM_ISO=<filename> (or use a separate" >&2
            echo "SYSFORGE_VM_DIR per distro, which also keeps their disk images apart)." >&2
            exit 1 ;;
    esac
}

CPU_CORES=4
RAM_MB=4096
SSH_PORT=10022   # host port forwarded to VM :22

SNAPSHOT=0
USE_ISO=0
USE_GUI=0
LOADVM=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --snapshot)   SNAPSHOT=1 ;;
        --iso)        USE_ISO=1 ;;
        --gui)        USE_GUI=1 ;;
        --loadvm)     shift; LOADVM="${1:-}";
                      [[ -n "$LOADVM" ]] || { echo "--loadvm requires a snapshot name" >&2; exit 1; } ;;
        --loadvm=*)   LOADVM="${1#*=}";
                      [[ -n "$LOADVM" ]] || { echo "--loadvm requires a snapshot name" >&2; exit 1; } ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

# -loadvm restores point-in-time VM state; -snapshot opens a throwaway overlay.
# Restoring saved guest RAM into an ephemeral overlay is incoherent, and --iso
# boots external media (nothing to restore into) — reject both up front.
if [[ -n "$LOADVM" ]]; then
    if [[ $SNAPSHOT -eq 1 ]]; then
        echo "--loadvm cannot be combined with --snapshot (ephemeral overlay mode)." >&2
        exit 1
    fi
    if [[ $USE_ISO -eq 1 ]]; then
        echo "--loadvm cannot be combined with --iso." >&2
        exit 1
    fi
fi

# Sanity checks
if [[ ! -f "$DISK_IMAGE" ]]; then
    echo "Disk image not found: $DISK_IMAGE"
    echo "Run: make vm-image"
    exit 1
fi

if [[ ! -f "$OVMF_CODE" ]]; then
    echo "OVMF firmware not found: $OVMF_CODE"
    echo "Run: sudo pacman -S edk2-ovmf"
    exit 1
fi

# Copy OVMF vars template on first run — QEMU writes EFI vars back to this
# file; it must be a writable per-VM copy, not the system template.
if [[ ! -f "$OVMF_VARS" ]]; then
    echo "Converting OVMF vars template to qcow2 (required for savevm support)"
    qemu-img convert -f raw -O qcow2 "$OVMF_VARS_TEMPLATE" "$OVMF_VARS"
fi

# Build QEMU command
QEMU_ARGS=(
    qemu-system-x86_64

    # CPU and memory
    -enable-kvm
    -cpu host
    -smp "$CPU_CORES"
    -m "$RAM_MB"

    # Disk — declared before the pflash drives so QEMU's savevm picks this as
    # the vmstate target. bdrv_all_find_vmstate_bs() walks drives in
    # declaration order; if the writable OVMF_VARS pflash came first, savevm
    # would write guest RAM into the 528 KiB UEFI vars qcow2 instead of here.
    -drive "file=$DISK_IMAGE,if=virtio,format=qcow2,discard=unmap"

    # UEFI firmware (no Secure Boot)
    -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE"
    -drive "if=pflash,format=qcow2,file=$OVMF_VARS"

    # Network: user-mode NAT, SSH port forwarded to host
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22"
    -device "virtio-net-pci,netdev=net0"

    # Headless — no display window; use SSH to access the VM
    -display none

    # QEMU monitor via Unix socket (for savevm / loadvm)
    # Connect with: socat - UNIX-CONNECT:"$VM_DIR/qemu-monitor.sock"
    -monitor "unix:$VM_DIR/qemu-monitor.sock,server,nowait"

    # Serial console exposed over a host socket — read text-mode prompts (e.g.
    # the configure stage) without a display, when SSH isn't available
    # (post-reboot, mid-pipeline). Connect with `make vm-console`. Mirrors the
    # monitor-socket pattern above. A socket chardev (not mon:stdio) is required
    # because the VM runs -daemonize. Carries output once the guest cmdline has
    # console=ttyS0 (see tools/vm/README.md).
    -chardev "socket,id=ser0,path=$VM_DIR/serial.sock,server=on,wait=off"
    -serial chardev:ser0

    # Background after init; pidfile lets `make vm-stop` find the process.
    -daemonize
    -pidfile "$VM_DIR/qemu.pid"

    # Misc
    -rtc base=localtime
)

# Refuse to launch if a VM is already running; otherwise QEMU fails on socket /
# port bind with an opaque error. Detection has two arms: the pidfile (fast
# path), and a port probe that catches an orphaned qemu whose pidfile went
# missing — e.g. `make vm-stop` removes the pidfile unconditionally after its
# monitor `quit`, so a `quit` that didn't actually stop qemu leaves a live
# process with no pidfile. The SSH forward port is in the base QEMU_ARGS for
# every mode, so probing it catches headless/gui/iso/snapshot alike.
RUNNING_PID=""
if [[ -f "$VM_DIR/qemu.pid" ]]; then
    EXISTING_PID="$(cat "$VM_DIR/qemu.pid" 2>/dev/null || true)"
    if [[ -n "$EXISTING_PID" ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
        RUNNING_PID="$EXISTING_PID"
    fi
fi
if [[ -z "$RUNNING_PID" ]] && command -v ss >/dev/null 2>&1; then
    PORT_PID="$(ss -ltnpH "sport = :$SSH_PORT" 2>/dev/null \
                  | grep -oP 'pid=\K[0-9]+' | head -n1 || true)"
    if [[ -n "$PORT_PID" ]] && kill -0 "$PORT_PID" 2>/dev/null; then
        RUNNING_PID="$PORT_PID"
    fi
fi
if [[ -n "$RUNNING_PID" ]]; then
    echo "VM already running (PID $RUNNING_PID, host port $SSH_PORT in use). Use 'make vm-stop' first." >&2
    exit 1
fi

# No VM running — clear any stale pidfile/sockets from a previous run (an
# orphaned process that was killed, or a `make vm-stop` that removed only the
# pidfile). Unconditional: gating these behind pidfile existence left stale
# sockets behind when the pidfile was already gone, which QEMU then can't bind.
rm -f "$VM_DIR/qemu.pid" "$VM_DIR/qemu-monitor.sock" "$VM_DIR/serial.sock"

# Restore a named savevm snapshot at startup. Placed after the drives in
# QEMU_ARGS (declaration order matters — see the -drive comment above) so QEMU
# resolves the vmstate against the same block device savevm wrote it to.
if [[ -n "$LOADVM" ]]; then
    QEMU_ARGS+=(-loadvm "$LOADVM")
fi

if [[ $SNAPSHOT -eq 1 ]]; then
    QEMU_ARGS+=(-snapshot)
    echo "Booting in ephemeral mode (changes will be discarded on exit)."
    echo "To revert to a named snapshot before this boot:"
    echo "  1) make vm-monitor   # connect to the running VM's monitor"
    echo "  2) loadvm NAME       # restore the desired snapshot"
    echo "  3) quit"
    echo "Then re-run 'make vm-snapshot'. List saved snapshots with 'info snapshots' in the monitor."
fi

VNC_PORT=5900

if [[ $USE_ISO -eq 1 ]]; then
    resolve_iso
    QEMU_ARGS+=(
        -cdrom "$ISO_PATH"
        -boot order=dc
        # VNC display for interactive ISO install (normal boots are headless).
        # Geometry is pinned (virtio-vga = device form of -vga virtio, with
        # xres/yres) so the gvncviewer window fits the host screen without
        # clipping the bottom rows — gvncviewer has no scrollbar or scaling.
        # edid=on advertises the pinned mode as the preferred EDID resolution,
        # so the guest re-selects 1280x720 when gvncviewer disconnects and
        # reconnects (without it the virtio-gpu connector renegotiates to a
        # default on reconnect).
        -device "virtio-vga,edid=on,xres=1280,yres=720"
        -display "vnc=127.0.0.1:0"
    )
    echo "Booting from installer ISO: $ISO_PATH"
    echo "  Console: gvncviewer localhost"
    echo "  Serial:  make vm-console"
    echo "  Stop:    make vm-stop (or 'quit' in make vm-monitor)"
elif [[ $USE_GUI -eq 1 ]]; then
    # Boot the installed disk with a VNC display so a graphical desktop is
    # visible. Reuses the ISO path's display mechanism: the base args carry
    # -display none, and QEMU honors the last -display, so this VNC wins.
    QEMU_ARGS+=(
        # Geometry pinned (+ edid=on for resolution persistence across
        # gvncviewer reconnects) — see the --iso branch above for the rationale.
        -device "virtio-vga,edid=on,xres=1280,yres=720"
        -display "vnc=127.0.0.1:0"
    )
    echo "VM running with GUI (VNC display on 127.0.0.1:$VNC_PORT)."
    echo "  Console: gvncviewer localhost"
    echo "  Serial:  make vm-console"
    echo "  SSH:     ssh -p $SSH_PORT root@localhost"
    echo "  Monitor: make vm-monitor"
    echo "  Stop:    make vm-stop (or 'quit' in make vm-monitor)"
else
    echo "VM running headless."
    echo "  SSH:     ssh -p $SSH_PORT root@localhost"
    echo "  Serial:  make vm-console"
    echo "  Monitor: make vm-monitor"
    echo "  Stop:    make vm-stop (or 'quit' in make vm-monitor)"
fi
if [[ -n "$LOADVM" ]]; then
    echo "  Restoring from snapshot '$LOADVM'."
fi
"${QEMU_ARGS[@]}"
