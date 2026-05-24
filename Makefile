.PHONY: all dev venv build install clean distclean test test-x lint man \
        check-shipped pre-release \
        release-major release-minor release-patch \
        vm-deps vm-image vm-boot vm-snapshot vm-iso vm-monitor vm-ssh vm-ssh-root vm-stop vm-clean \
        vm-pkg-stable vm-pkg-git vm-pkg-all vm-install-stable vm-install-git vm-test

VM_DIR ?= $(HOME)/.local/share/sysforge-vm
VM_DISK = $(VM_DIR)/arch-sysforge.qcow2
VM_DISK_SIZE ?= 40G
VM_BUILD_DIR = $(VM_DIR)/build
VM_CHROOT ?= /var/lib/archbuild/extra-x86_64

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

# Pre-release shipped-file validator. Runs the seven check groups in
# tools/check_shipped.py (configs, pkgbuild, pkgbuild_parity, hooks,
# completions, versions, manpage). tools/release.sh invokes this from
# preflight; also runnable standalone.
check-shipped:
	uv run --no-sync python tools/check_shipped.py

# Composite gate: lint + tests + shipped-file consistency. Run before
# kicking off `make release-{major,minor,patch}`.
pre-release: lint test check-shipped

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

vm-stop:
	@if [ -f "$(VM_DIR)/qemu.pid" ] && kill -0 "$$(cat $(VM_DIR)/qemu.pid)" 2>/dev/null; then \
		echo "Stopping VM via monitor..."; \
		echo "quit" | socat - UNIX-CONNECT:$(VM_DIR)/qemu-monitor.sock 2>/dev/null \
		  || kill "$$(cat $(VM_DIR)/qemu.pid)"; \
		rm -f $(VM_DIR)/qemu.pid; \
	else \
		echo "No running VM"; \
		rm -f $(VM_DIR)/qemu.pid; \
	fi

vm-clean:
	@if [ -f "$(VM_DISK)" ]; then \
		echo "Removing VM disk image: $(VM_DISK)"; \
		rm -f $(VM_DISK) $(VM_DIR)/OVMF_VARS.4m.fd $(VM_DIR)/OVMF_VARS.4m.qcow2 $(VM_DIR)/known_hosts $(VM_DIR)/qemu.pid $(VM_DIR)/qemu-monitor.sock; \
	else \
		echo "No disk image found at $(VM_DISK)"; \
	fi
	rm -rf $(VM_BUILD_DIR)

# ---------------------------------------------------------------------------
# Local PKGBUILD validation in the VM
#
# Build a .pkg.tar.zst from the working tree via the same clean chroot
# tools/release.sh uses, then scp + pacman -U it into the running VM.
# Lets us exercise packaging changes (deps, optdeps, hooks, tmpfiles,
# completions) before publishing to AUR.
# ---------------------------------------------------------------------------

vm-pkg-stable:
	./tools/vm/build-pkg.sh stable --out=$(VM_BUILD_DIR) --chroot=$(VM_CHROOT)

vm-pkg-git:
	./tools/vm/build-pkg.sh git --out=$(VM_BUILD_DIR) --chroot=$(VM_CHROOT)

vm-pkg-all: vm-pkg-stable vm-pkg-git

vm-install-stable:
	./tools/vm/install-pkg.sh stable --pkg-dir=$(VM_BUILD_DIR)

vm-install-git:
	./tools/vm/install-pkg.sh git --pkg-dir=$(VM_BUILD_DIR)

# Full round-trip; assumes the VM is already booted (e.g. `make vm-snapshot`).
vm-test: vm-pkg-stable vm-install-stable
