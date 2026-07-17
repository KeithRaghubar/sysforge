.PHONY: all dev venv build install dev-install dev-uninstall clean distclean test test-x lint coverage coverage-ratchet coverage-ratchet-update man \
        check-shipped check-personal check-standards design check-design pre-release \
        sync-config \
        release-major release-minor release-patch release-resume \
        vm-deps vm-image vm-boot vm-boot-gui vm-snapshot vm-loadvm vm-iso vm-monitor vm-savevm vm-ssh vm-ssh-root vm-ssh-builder vm-stop vm-clean \
        vm-pkg-stable vm-pkg-git vm-pkg-all vm-install-stable vm-install-git vm-smoke vm-test

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
	@pacman -Qq python-pytest ruff scdoc >/dev/null 2>&1 \
	    || sudo pacman -S --needed python-pytest ruff scdoc
	uv pip install -e .

venv:
	uv venv

build:
	uv build

install:
	makepkg -si

dev-install:
	tools/dev_install.sh install

dev-uninstall:
	tools/dev_install.sh uninstall

# Pass extra pytest args via ARGS, e.g.
#   make test ARGS="tests/test_standards_compliance.py -q"
test:
	pytest $(ARGS)

test-x:
	pytest -x $(ARGS)

lint:
	ruff check sysforge/

# Coverage report. Layers pytest-cov into an ephemeral uv overlay (same
# `uv run --no-sync` pattern as check-shipped/man) so nothing is added to
# the system or the venv. Prints a term summary and writes coverage.json;
# the ratchet baseline lives in tests/COVERAGE_BASELINE.md.
coverage:
	uv run --no-sync --with pytest-cov pytest \
	  --cov=sysforge \
	  --cov-report=term-missing:skip-covered \
	  --cov-report=json:coverage.json \
	  -o addopts="" -q

# Soft coverage ratchet (F5). Runs the instrumented suite, then compares the
# TOTAL against the floor in tests/COVERAGE_BASELINE.md. Advisory: reports
# HOLD / IMPROVE / DROP and exits non-zero only on a DROP so the release-prep
# preflight can surface it as a [WARN], never a hard gate.
coverage-ratchet: coverage
	uv run --no-sync python tools/coverage_ratchet.py --check

# Re-stamp the baseline floor from the current suite. Pass TESTS=<n> to record
# the suite size (otherwise the recorded count is left as-is). Run when cutting
# a release so the floor tracks the shipped suite. Commit the updated baseline.
coverage-ratchet-update: coverage
	uv run --no-sync python tools/coverage_ratchet.py --update $(if $(TESTS),--tests $(TESTS),)

# Type-check gate. Layers pyright into an ephemeral uv overlay (same
# `uv run --no-sync` pattern as coverage/check-shipped) so nothing is added to
# the system or the venv. Pyright config lives in pyproject [tool.pyright].
typecheck:
	uv run --no-sync --with pyright pyright sysforge/

# Pre-release shipped-file validator. Runs the seven check groups in
# tools/check_shipped.py (configs, pkgbuild, pkgbuild_parity, hooks,
# completions, versions, manpage). tools/release.sh invokes this from
# preflight; also runnable standalone.
check-shipped:
	uv run --no-sync python tools/check_shipped.py

# De-personalization gate. Fails if personal identity/path tokens leak into the
# published surface (docs, source comments, shipped configs); legitimate
# attribution (copyright/maintainer/--author) and the repo URL are allowed.
check-personal:
	uv run --no-sync python tools/check_personal.py

# Regenerate DESIGN.md from the modular sources under docs/design/ (concatenated
# per docs/design/_manifest under a generated banner). DESIGN.md is generated --
# edit the sources, then run this.
design:
	uv run --no-sync python tools/build_design.py

# DESIGN.md drift gate (mirrors the manpage check). Fails if the committed
# DESIGN.md is out of date with its docs/design/ sources. Wired into preflight.
check-design:
	uv run --no-sync python tools/build_design.py --check

# Standards-compliance gate. Runs the mechanically-checkable groups in
# tools/check_standards.py (paths/XDG-FHS, SPDX/REUSE, Keep a Changelog
# headings, UTF-8 encoding). The behavioural subset (NO_COLOR, --version,
# stdout/stderr, RFC 3339, reproducibility) is covered by `make test`
# (tests/test_standards_compliance.py). Source of truth: docs/design/21-standards.md.
check-standards:
	uv run --no-sync --with reuse python tools/check_standards.py

# Merge new shipped defaults (etc/sysforge/) into the live config dir
# ($SYSFORGE_CONFIG_DIR itself, else /etc/sysforge). Add-only: injects new
# keys/sections/comments, never overwrites existing values. Pure comment /
# commented-example drift the key-merge can't carry is dropped beside the
# target as <name>.sfnew (pacnew-style) to diff & adopt. tomlkit is dev-only
# (ephemeral uv overlay). Preview with ARGS="--dry-run"; retarget with
# ARGS="--target DIR".
sync-config:
	uv run --no-sync --with tomlkit python tools/sync_config.py $(ARGS)

# Composite gate: lint + tests + shipped-file consistency + impersonal docs +
# DESIGN.md freshness + standards compliance. Run before kicking off
# `make release-{major,minor,patch}`.
pre-release: lint typecheck test check-shipped check-personal check-design check-standards

release-major:
	bash tools/release.sh --bump=major

release-minor:
	bash tools/release.sh --bump=minor

release-patch:
	bash tools/release.sh --bump=patch

# Finish an in-flight release after a post-tag failure required fix commits on
# top of the release commit (tag no longer at HEAD): no bump, re-enters Phase 3.
release-resume:
	bash tools/release.sh --resume

# scdoc hybrid: hand-written prose in man/sysforge.1.scd.in, COMMANDS
# sections generated from the argparse tree by tools/gen_options.py.
# man/sysforge.1.scd is an intermediate (gitignored); man/sysforge.1 is
# committed. COLUMNS pinned for reproducible argparse help wrapping.
man:
	mkdir -p man
	COLUMNS=80 PYTHONPATH=. uv run --no-sync python tools/gen_options.py \
	  --template man/sysforge.1.scd.in \
	  --out man/sysforge.1.scd
	scdoc < man/sysforge.1.scd > man/sysforge.1

clean:
	rm -rf dist/ __pycache__/ *.egg-info/ .pytest_cache/ .coverage coverage.json
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

distclean: clean
	rm -rf .venv/

# ---------------------------------------------------------------------------
# Test VM
# ---------------------------------------------------------------------------

vm-deps:
	@pacman -Qq qemu-desktop edk2-ovmf gtk-vnc socat >/dev/null 2>&1 \
	    || sudo pacman -S --needed qemu-desktop edk2-ovmf gtk-vnc socat

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

# Like vm-boot, but with a VNC display so a graphical desktop is visible.
# Connect with: gvncviewer localhost   (gtk-vnc — installed by vm-deps)
vm-boot-gui:
	./tools/vm/boot.sh --gui

# Boot the installed disk restored from a named savevm snapshot.
#   make vm-loadvm NAME=clean          # headless
#   make vm-loadvm NAME=clean GUI=1    # with a VNC display
# Also accepts the name positionally: make vm-loadvm clean
vm-loadvm:
	@if [ -z "$(NAME)" ]; then echo "Usage: make vm-loadvm NAME=<snapshot-name> [GUI=1]  (or: make vm-loadvm <snapshot-name>)"; exit 2; fi
	./tools/vm/boot.sh --loadvm "$(NAME)" $(if $(GUI),--gui,)

vm-snapshot:
	./tools/vm/boot.sh --snapshot

vm-iso:
	rm -f $(VM_DIR)/known_hosts
	./tools/vm/boot.sh --iso

VM_SSH = ssh -p 10022 -o UserKnownHostsFile=$(VM_DIR)/known_hosts -o StrictHostKeyChecking=accept-new

# Default to root: it is the only account guaranteed to exist in every state the
# VM tooling targets — the live ISO, a half-installed system mid-pipeline, and an
# installed system regardless of the username the user chose at bootstrap. The
# builder account only exists post-install and only under the default username,
# so connecting as builder breaks for non-default usernames / ISO runs. Override
# with `make vm-ssh VM_USER=<name>` or use `vm-ssh-builder`.
VM_USER ?= root

vm-ssh:
	ssh-keygen -R '[localhost]:10022' -f $(VM_DIR)/known_hosts 2>/dev/null; $(VM_SSH) $(VM_USER)@localhost

vm-ssh-root:
	ssh-keygen -R '[localhost]:10022' -f $(VM_DIR)/known_hosts 2>/dev/null; $(VM_SSH) root@localhost

vm-ssh-builder:
	ssh-keygen -R '[localhost]:10022' -f $(VM_DIR)/known_hosts 2>/dev/null; $(VM_SSH) builder@localhost

vm-monitor:
	socat - UNIX-CONNECT:$(VM_DIR)/qemu-monitor.sock

# Attach to the VM's serial console over the host socket. Use for reading
# interactive prompts (e.g. the configure stage) when SSH isn't available
# (post-reboot, mid-pipeline). Detach with Ctrl-] (the socat escape).
# Requires console=ttyS0 on the guest cmdline — see tools/vm/README.md.
vm-console:
	@test -S "$(VM_DIR)/serial.sock" || { echo "VM not running (no serial socket at $(VM_DIR)/serial.sock). Start it with 'make vm-boot'."; exit 1; }
	socat -,raw,echo=0,escape=0x1d UNIX-CONNECT:$(VM_DIR)/serial.sock

# Accept the snapshot name as a bare positional goal: `make vm-savevm <name>`
# (and `make vm-loadvm <name>`) in addition to the `NAME=<name>` form. When one
# of these is the first goal, treat the next goal as the name and stub it out
# with a no-op recipe so Make does not try to build it as a target (it would
# otherwise look for a rule named e.g. `clean` and run the real `clean` target).
ifneq ($(filter vm-savevm vm-loadvm,$(firstword $(MAKECMDGOALS))),)
  VM_SNAP_POS := $(word 2,$(MAKECMDGOALS))
  ifneq ($(VM_SNAP_POS),)
    NAME ?= $(VM_SNAP_POS)
    $(eval $(VM_SNAP_POS):;@:)
    .PHONY: $(VM_SNAP_POS)
  endif
endif

# Plain `savevm` over the qemu monitor.
#
# Older libslirp had a BOOTP VMState serialization bug
# (warning: Slirp: Save of field slirp_bootpclient/macaddr failed) that left the
# snapshot's networking unusable, which we used to work around by detaching the
# netdev backend (set_link off / netdev_del) before savevm and reattaching after.
# That workaround is obsolete AND harmful on qemu 11.x / libslirp 4.9.x: a
# `netdev_del` on a slirp backend still peered to its NIC frontend does NOT close
# the hostfwd listening socket on port 10022, so the reattaching `netdev_add`
# can never rebind the port and the VM is left with no network backend at all.
# Verified on qemu 11.0.1 / libslirp 4.9.3 that a plain savevm snapshot restores
# (via a fresh `-loadvm`) with the SSH host-forward fully working, so the netdev
# surgery is no longer needed.
vm-savevm:
	@if [ -z "$(NAME)" ]; then echo "Usage: make vm-savevm NAME=<snapshot-name>  (or: make vm-savevm <snapshot-name>)"; exit 2; fi
	@test -S "$(VM_DIR)/qemu-monitor.sock" || { echo "VM not running (no monitor socket at $(VM_DIR)/qemu-monitor.sock). Start it with 'make vm-boot'."; exit 1; }
	@printf 'savevm $(NAME)\n' | socat - UNIX-CONNECT:$(VM_DIR)/qemu-monitor.sock
	@echo "Saved snapshot '$(NAME)'. Verify with: make vm-monitor → info snapshots"

# Stop the VM and only clear state once the process is *confirmed* gone.
# Resolution mirrors boot.sh: pidfile first, then an `ss` port probe so an
# orphan whose pidfile vanished is still found. Escalates monitor quit ->
# SIGTERM -> SIGKILL, waiting between each, and removes the pidfile/sockets
# only after the PID is dead — never unconditionally, which is what created the
# orphan (live qemu, no pidfile) that boot.sh then had to recover from.
vm-stop:
	@VM_DIR="$(VM_DIR)"; pid=""; \
	if [ -f "$$VM_DIR/qemu.pid" ]; then \
		p="$$(cat "$$VM_DIR/qemu.pid" 2>/dev/null)"; \
		if [ -n "$$p" ] && kill -0 "$$p" 2>/dev/null; then pid="$$p"; fi; \
	fi; \
	if [ -z "$$pid" ] && command -v ss >/dev/null 2>&1; then \
		p="$$(ss -ltnpH 'sport = :10022' 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -n1)"; \
		if [ -n "$$p" ] && kill -0 "$$p" 2>/dev/null; then pid="$$p"; fi; \
	fi; \
	if [ -z "$$pid" ]; then \
		echo "No running VM"; \
		rm -f "$$VM_DIR/qemu.pid" "$$VM_DIR/qemu-monitor.sock" "$$VM_DIR/serial.sock"; \
		exit 0; \
	fi; \
	echo "Stopping VM (PID $$pid) via monitor..."; \
	echo "quit" | socat - UNIX-CONNECT:"$$VM_DIR/qemu-monitor.sock" 2>/dev/null || true; \
	for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$$pid" 2>/dev/null || break; sleep 0.5; done; \
	if kill -0 "$$pid" 2>/dev/null; then \
		echo "Monitor quit didn't stop it; sending SIGTERM..."; \
		kill "$$pid" 2>/dev/null || true; \
		for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$$pid" 2>/dev/null || break; sleep 0.5; done; \
	fi; \
	if kill -0 "$$pid" 2>/dev/null; then \
		echo "Still alive; sending SIGKILL..."; \
		kill -9 "$$pid" 2>/dev/null || true; \
		sleep 0.5; \
	fi; \
	if kill -0 "$$pid" 2>/dev/null; then \
		echo "WARNING: PID $$pid still alive after SIGKILL; leaving pidfile/sockets in place." >&2; \
		exit 1; \
	fi; \
	rm -f "$$VM_DIR/qemu.pid" "$$VM_DIR/qemu-monitor.sock" "$$VM_DIR/serial.sock"; \
	echo "VM stopped."

vm-clean:
	@if [ -f "$(VM_DISK)" ]; then \
		echo "Removing VM disk image: $(VM_DISK)"; \
		rm -f $(VM_DISK) $(VM_DIR)/OVMF_VARS.4m.fd $(VM_DIR)/OVMF_VARS.4m.qcow2 $(VM_DIR)/known_hosts $(VM_DIR)/qemu.pid $(VM_DIR)/qemu-monitor.sock $(VM_DIR)/serial.sock; \
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
vm-smoke:
	./tools/vm/smoke.sh

vm-test: vm-pkg-stable vm-install-stable vm-smoke
