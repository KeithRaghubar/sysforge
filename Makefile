.PHONY: all dev venv build install clean distclean test test-x lint man \
        release-major release-minor release-patch \
        vm-deps vm-image vm-boot vm-snapshot vm-iso vm-monitor vm-ssh vm-ssh-root vm-clean

VM_DIR ?= $(HOME)/.local/share/sysforge-vm
VM_DISK = $(VM_DIR)/arch-sysforge.qcow2
VM_DISK_SIZE ?= 40G

all: test

# ---------------------------------------------------------------------------
# Python dev
# ---------------------------------------------------------------------------

dev:
	@pacman -Qq python-pytest ruff >/dev/null 2>&1 \
	    || sudo pacman -S --needed python-pytest ruff
	@uv pip install --quiet argparse-manpage
	uv pip install -e .

venv:
	uv venv

build:
	uv build

install:
	makepkg -si

test:
	pytest

test-x:
	pytest -x

lint:
	ruff check sysforge/

release-major:
	bash tools/release.sh --bump=major

release-minor:
	bash tools/release.sh --bump=minor

release-patch:
	bash tools/release.sh --bump=patch

man:
	mkdir -p man
	PYTHONPATH=. uv run --no-sync argparse-manpage \
	  --module sysforge.cli \
	  --function _build_parser \
	  --author "Keith Raghubar" \
	  --author-email "aur.archlinux.org.buckskin000@passmail.net" \
	  --project-name sysforge \
	  --url "https://github.com/KeithRaghubar/sysforge" \
	  --output man/sysforge.1

clean:
	rm -rf dist/ __pycache__/ *.egg-info/ .pytest_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

distclean: clean
	rm -rf .venv/

# ---------------------------------------------------------------------------
# Test VM
# ---------------------------------------------------------------------------

vm-deps:
	@pacman -Qq qemu-desktop edk2-ovmf gtk-vnc >/dev/null 2>&1 \
	    || sudo pacman -S --needed qemu-desktop edk2-ovmf gtk-vnc

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
	rm -f $(VM_DIR)/known_hosts
	./tools/vm/boot.sh --iso

VM_SSH = ssh -p 10022 -o UserKnownHostsFile=$(VM_DIR)/known_hosts -o StrictHostKeyChecking=accept-new

vm-ssh:
	ssh-keygen -R '[localhost]:10022' -f $(VM_DIR)/known_hosts 2>/dev/null; $(VM_SSH) builder@localhost

vm-ssh-root:
	ssh-keygen -R '[localhost]:10022' -f $(VM_DIR)/known_hosts 2>/dev/null; $(VM_SSH) root@localhost

vm-monitor:
	socat - UNIX-CONNECT:$(VM_DIR)/qemu-monitor.sock

vm-clean:
	@if [ -f "$(VM_DISK)" ]; then \
		echo "Removing VM disk image: $(VM_DISK)"; \
		rm -f $(VM_DISK) $(VM_DIR)/OVMF_VARS.4m.fd $(VM_DIR)/OVMF_VARS.4m.qcow2 $(VM_DIR)/known_hosts; \
	else \
		echo "No disk image found at $(VM_DISK)"; \
	fi
