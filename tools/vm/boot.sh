#!/usr/bin/env bash
# boot.sh — launch the SysForge test VM (headless)
#
# Usage:
#   ./tools/vm/boot.sh              # boot normally (changes persist)
#   ./tools/vm/boot.sh --snapshot   # boot from clean snapshot, discard changes on exit
#   ./tools/vm/boot.sh --iso        # boot from Arch ISO for initial install
#
# The VM runs headless (no window). Access it via SSH:
#   ssh -p 10022 root@localhost
#
# QEMU monitor (for savevm / loadvm):
#   socat - UNIX-CONNECT:~/.local/share/sysforge-vm/qemu-monitor.sock
#   savevm clean     — save current state as 'clean' snapshot
#   loadvm clean     — restore to 'clean' snapshot
#   info snapshots   — list saved snapshots
#   quit             — stop the VM

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_DIR="${SYSFORGE_VM_DIR:-$HOME/.local/share/sysforge-vm}"

DISK_IMAGE="$VM_DIR/arch-sysforge.qcow2"
OVMF_VARS_TEMPLATE="/usr/share/edk2/x64/OVMF_VARS.4m.fd"
OVMF_CODE="/usr/share/edk2/x64/OVMF_CODE.4m.fd"
OVMF_VARS="$VM_DIR/OVMF_VARS.4m.fd"  # per-VM writable copy
ISO_PATH="$VM_DIR/archlinux.iso"

CPU_CORES=4
RAM_MB=4096
SSH_PORT=10022   # host port forwarded to VM :22

SNAPSHOT=0
USE_ISO=0

for arg in "$@"; do
    case "$arg" in
        --snapshot) SNAPSHOT=1 ;;
        --iso)      USE_ISO=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

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
    echo "Copying OVMF vars template to $OVMF_VARS"
    cp "$OVMF_VARS_TEMPLATE" "$OVMF_VARS"
fi

# Build QEMU command
QEMU_ARGS=(
    qemu-system-x86_64

    # CPU and memory
    -enable-kvm
    -cpu host
    -smp "$CPU_CORES"
    -m "$RAM_MB"

    # UEFI firmware (no Secure Boot)
    -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE"
    -drive "if=pflash,format=raw,file=$OVMF_VARS"

    # Disk
    -drive "file=$DISK_IMAGE,if=virtio,format=qcow2,discard=unmap"

    # Network: user-mode NAT, SSH port forwarded to host
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22"
    -device "virtio-net-pci,netdev=net0"

    # Headless — no display window; use SSH to access the VM
    -display none

    # QEMU monitor via Unix socket (for savevm / loadvm)
    # Connect with: socat - UNIX-CONNECT:"$VM_DIR/qemu-monitor.sock"
    -monitor "unix:$VM_DIR/qemu-monitor.sock,server,nowait"

    # Misc
    -rtc base=localtime
)

if [[ $SNAPSHOT -eq 1 ]]; then
    QEMU_ARGS+=(-snapshot)
    echo "Booting from 'clean' snapshot (changes will be discarded)"
fi

if [[ $USE_ISO -eq 1 ]]; then
    if [[ ! -f "$ISO_PATH" ]]; then
        echo "Arch ISO not found: $ISO_PATH"
        echo "Download an Arch ISO and place it at $ISO_PATH"
        echo "Or set SYSFORGE_VM_DIR to the directory containing archlinux.iso"
        exit 1
    fi
    QEMU_ARGS+=(-cdrom "$ISO_PATH" -boot order=dc)
    echo "Booting from Arch ISO: $ISO_PATH"
fi

echo "VM running headless."
echo "  SSH:     ssh -p $SSH_PORT root@localhost"
echo "  Monitor: socat - UNIX-CONNECT:\"$VM_DIR/qemu-monitor.sock\""
echo "  Stop:    Ctrl-C (or 'quit' in monitor)"
exec "${QEMU_ARGS[@]}"
