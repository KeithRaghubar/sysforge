.PHONY: dev build install clean test test-v test-x lint release man \
        vm-deps vm-image vm-boot vm-snapshot vm-iso vm-monitor vm-clean

VM_DIR ?= $(HOME)/.local/share/sysforge-vm
VM_DISK = $(VM_DIR)/arch-sysforge.qcow2
VM_DISK_SIZE ?= 40G

all: test

# ---------------------------------------------------------------------------
# Python dev
# ---------------------------------------------------------------------------

dev-deps:
	sudo pacman -S --needed python-pytest ruff
	uv pip install argparse-manpage

dev:
	uv pip install -e .

venv:
	uv venv

build:
	uv build

install:
	makepkg -si

test:
	pytest

test-v:
	pytest -v

test-x:
	pytest -x

lint:
	ruff check sysforge/

release:
	bash tools/release.sh

man:
	mkdir -p man
	argparse-manpage \
	  --module sysforge.cli \
	  --function _build_parser \
	  --author "Keith Raghubar" \
	  --author-email "aur.archlinux.org.buckskin000@passmail.net" \
	  --project-name sysforge \
	  --url "https://github.com/KeithRaghubar/sysforge" \
	  --output man/sysforge.1

clean:
	rm -rf dist/ .venv/ __pycache__/ *.egg-info/ .pytest_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# Test VM
# ---------------------------------------------------------------------------

vm-deps:
	sudo pacman -S --needed qemu-desktop edk2-ovmf

vm-image:
	mkdir -p $(VM_DIR)
	@if [ -f "$(VM_DISK)" ]; then \
		echo "Disk image already exists: $(VM_DISK)"; \
		echo "Delete it first if you want to recreate: make vm-clean"; \
	else \
		qemu-img create -f qcow2 $(VM_DISK) $(VM_DISK_SIZE); \
		echo "Created: $(VM_DISK) ($(VM_DISK_SIZE))"; \
	fi

vm-boot:
	./tools/vm/boot.sh

vm-snapshot:
	./tools/vm/boot.sh --snapshot

vm-iso:
	./tools/vm/boot.sh --iso

vm-monitor:
	socat - UNIX-CONNECT:$(VM_DIR)/qemu-monitor.sock

vm-clean:
	@if [ -f "$(VM_DISK)" ]; then \
		echo "Removing VM disk image: $(VM_DISK)"; \
		rm -f $(VM_DISK) $(VM_DIR)/OVMF_VARS.4m.fd; \
	else \
		echo "No disk image found at $(VM_DISK)"; \
	fi
