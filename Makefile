.PHONY: all help dev venv build install dev-install dev-uninstall clean distclean test test-x lint typecheck coverage coverage-ratchet coverage-ratchet-update man \
        dev-deps dev-deps-core dev-deps-pkg dev-deps-container dev-deps-release dev-deps-list \
        lint-py lint-sh \
        check-shipped check-personal check-standards check-bump check-standards-at next-id next-bump design check-design pre-release audit \
        roadmap-table check-roadmap-table roadmap-view \
        sync-config \
        release-major release-minor release-patch release-resume \
        vm-deps vm-image vm-boot vm-boot-gui vm-snapshot vm-loadvm vm-iso vm-monitor vm-console vm-savevm vm-ssh vm-ssh-root vm-ssh-builder vm-stop vm-clean \
        vm-pkg-stable vm-pkg-git vm-pkg-all vm-install-stable vm-install-git vm-smoke vm-test \
        container-build container-smoke container-smoke-cachyos container-shell container-clean

# ---------------------------------------------------------------------------
# Dev system dependencies (2.6.1-F3)
#
# The one record of what a development environment needs, split by the tier that
# needs it. Every `pacman -S` in this file resolves from a variable here — a
# target that grows a new tool adds it to the matching set, never its own
# install preamble.
#
# Python tooling is deliberately almost absent: apart from pytest and ruff
# (wanted on PATH for editor/hook integration), it is resolved per-invocation by
# `uv run --no-sync --with …` — pyright, reuse, pip-audit, pytest-cov, tomlkit
# never touch the system or the venv, so there is nothing to install or record.
# This is also why there is no `[dependency-groups] dev` in pyproject.toml: it
# would duplicate the overlays while claiming to be the source of truth.
# ---------------------------------------------------------------------------

# Suite, lint (Python + shell), manpage, editable install. `make test` / `lint`
# / `man` / `dev`.
DEV_DEPS_CORE      = python-pytest ruff shellcheck scdoc uv git base-devel
# makechrootpkg — clean-chroot package builds: vm-pkg-*, tools/release.sh.
DEV_DEPS_PKG       = devtools
# QEMU test VM: vm-boot, vm-snapshot, vm-console, vm-monitor (socat), …
DEV_DEPS_VM        = qemu-desktop edk2-ovmf gtk-vnc socat
# Container test tier: container-smoke, container-smoke-cachyos.
DEV_DEPS_CONTAINER = podman
# Release: GPG-signed commit/tag/tarball + the GitHub release upload.
DEV_DEPS_RELEASE   = gnupg github-cli

DEV_DEPS_ALL = $(DEV_DEPS_CORE) $(DEV_DEPS_PKG) $(DEV_DEPS_VM) \
               $(DEV_DEPS_CONTAINER) $(DEV_DEPS_RELEASE)

# $(call pacman_needed,<packages>) — install only if the set isn't already
# satisfied, so a complete environment never prompts for sudo. `pacman -Qq`
# exits non-zero if ANY package is missing; --needed then makes the install a
# no-op for the ones already present.
define pacman_needed
@pacman -Qq $(1) >/dev/null 2>&1 || sudo pacman -S --needed $(1)
endef

VM_DIR ?= $(HOME)/.local/share/sysforge-vm
VM_DISK = $(VM_DIR)/arch-sysforge.qcow2
VM_DISK_SIZE ?= 40G
VM_BUILD_DIR = $(VM_DIR)/build
VM_CHROOT ?= /var/lib/archbuild/extra-x86_64

##@ Build & test
all: test ## Default goal: run the full test suite

# Self-documenting target list, generated from this file at run time: a target
# is listed iff its own line carries a `## <description>` comment, grouped by
# the `##@ <Group>` banner above it, in file order. Deliberately uncoloured:
# standards row 5 makes `log.use_color()` the single colour authority, and a
# recipe emitting its own ANSI would be a second one that never sees NO_COLOR.
#
# Reading the Makefile itself is the point: there is no second list to keep in
# sync, so the help can never advertise a target that does not exist.
# `tests/test_makefile_help.py` closes the other direction — a .PHONY target
# that ships without a `##` description fails the suite.
help: ## List every target, grouped (this list)
	@awk 'BEGIN {FS = ":.*?## "} \
	     /^##@ / {printf "\n%s\n", substr($$0, 5); next} \
	     /^[a-zA-Z][a-zA-Z0-9_-]*:.*?## / {printf "  %-24s %s\n", $$1, $$2}' \
	     $(MAKEFILE_LIST)
	@echo
	@echo "Variables: ARGS= (test/audit)  SORT=/REVERSE= (roadmap-view)"
	@echo "           TYPE= (next-id)  BUMP= (check-bump)  VERSION= (check-standards-at)"

# ---------------------------------------------------------------------------
# Python dev
# ---------------------------------------------------------------------------

# Everything a full dev environment needs, every tier. Start here on a new
# machine: `make dev-deps && make dev`.
##@ Dev environment
dev-deps: ## Install every dev system dependency, all tiers
	$(call pacman_needed,$(DEV_DEPS_ALL))

# Per-tier subsets, for a machine that only needs one of them.
dev-deps-core: ## Install the core tier only (suite, lint, docs)
	$(call pacman_needed,$(DEV_DEPS_CORE))

dev-deps-pkg: ## Install the packaging tier (makepkg, devtools)
	$(call pacman_needed,$(DEV_DEPS_PKG))

dev-deps-container: ## Install the container test tier (podman)
	$(call pacman_needed,$(DEV_DEPS_CONTAINER))

dev-deps-release: ## Install the release tier (gnupg, github-cli)
	$(call pacman_needed,$(DEV_DEPS_RELEASE))

# Show what each tier needs and whether it is installed. Read-only, no sudo —
# run this before dev-deps to see what a full install would add.
dev-deps-list: ## Show each tier's deps and whether they are present
	@printf '%-12s %s\n' TIER PACKAGES
	@printf '%-12s %s\n' ---- --------
	@printf '%-12s %s\n' core '$(DEV_DEPS_CORE)'
	@printf '%-12s %s\n' pkg '$(DEV_DEPS_PKG)'
	@printf '%-12s %s\n' vm '$(DEV_DEPS_VM)'
	@printf '%-12s %s\n' container '$(DEV_DEPS_CONTAINER)'
	@printf '%-12s %s\n' release '$(DEV_DEPS_RELEASE)'
	@echo
	@for p in $(DEV_DEPS_ALL); do \
		if pacman -Qq "$$p" >/dev/null 2>&1; then s=installed; else s=MISSING; fi; \
		printf '  %-9s %s\n' "$$s" "$$p"; \
	done
	@echo
	@echo "Python tooling (pyright, reuse, pip-audit, pytest-cov, tomlkit) is"
	@echo "resolved per-invocation by 'uv run --with' and needs no install."

# The core tier plus the editable install of sysforge itself.
dev: dev-deps-core ## Core tier + editable install of sysforge
	uv pip install -e .

venv: ## Create the project virtualenv
	uv venv

build: ## Build the wheel/sdist into dist/
	uv build

install: ## Build and install the package (makepkg -si)
	makepkg -si

dev-install: ## Install from this checkout, bypassing the AUR
	tools/dev_install.sh install

dev-uninstall: ## Remove a dev-install
	tools/dev_install.sh uninstall

# Pass extra pytest args via ARGS, e.g.
#   make test ARGS="tests/test_standards_compliance.py -q"
##@ Test & lint
test: ## Run the full suite (ARGS= extra pytest args)
	pytest $(ARGS)

test-x: ## Run the suite, stop on first failure
	pytest -x $(ARGS)

# Both linters. Shell is not a second-class language here: 12 of the repo's
# scripts are the test/release tooling, where a quoting bug fails a release or
# silently tests the wrong tree.
lint: lint-py lint-sh ## Lint Python and shell (ruff + shellcheck)

lint-py: ## Lint Python only (ruff)
	ruff check sysforge/

# Every tracked shell script plus the bash completion (which has no shebang, so
# it declares `shellcheck shell=bash` inline). Run at shellcheck's default
# severity — including info/style — because the tree is clean at that level and
# the cost of holding it there is one justified `disable=` comment per genuine
# exception, which is cheaper than the drift a laxer gate accumulates.
# `wildcard` filters the git list down to paths that exist on disk: a tracked
# script deleted but not yet committed would otherwise make shellcheck abort on
# a missing file instead of linting the rest.
LINT_SH_FILES = $(wildcard $(shell git ls-files '*.sh') completions/sysforge.bash)

lint-sh: ## Lint shell only (shellcheck)
	@command -v shellcheck >/dev/null 2>&1 \
	    || { echo "shellcheck not found — install with: make dev-deps-core"; exit 1; }
	shellcheck $(LINT_SH_FILES)

# Supply-chain audit (2.3.0-F3). The runtime dep surface is near-empty by
# design (tomli backport + optional pyalpm), but the dev/build toolchain
# (hatchling, pytest, ruff, pyright, coverage overlay, …) is still a
# supply-chain surface. Scans the active dev environment for packages with
# known CVEs via pip-audit in an ephemeral uv overlay (same --no-sync --with
# idiom as coverage/check-shipped) — nothing enters the shipped wheel or
# PKGBUILD. --skip-editable drops the local editable sysforge install (not on
# PyPI) so only the third-party build/test chain is reported. Run manually,
# and optionally before a release; kept out of the pre-release hard gate
# because it needs network and a fresh advisory shouldn't block a release.
# Pass e.g. ARGS="--fix" or a --requirement to scope it.
audit: ## Audit the dependency chain for known CVEs
	uv run --no-sync --with pip-audit pip-audit --skip-editable $(ARGS)

# Coverage report. Layers pytest-cov into an ephemeral uv overlay (same
# `uv run --no-sync` pattern as check-shipped/man) so nothing is added to
# the system or the venv. Prints a term summary and writes coverage.json;
# the ratchet baseline lives in tests/COVERAGE_BASELINE.md.
coverage: ## Run the suite with a coverage report
	uv run --no-sync --with pytest-cov pytest \
	  --cov=sysforge \
	  --cov-report=term-missing:skip-covered \
	  --cov-report=json:coverage.json \
	  -o addopts="" -q

# Soft coverage ratchet (F5). Runs the instrumented suite, then compares the
# TOTAL against the floor in tests/COVERAGE_BASELINE.md. Advisory: reports
# HOLD / IMPROVE / DROP and exits non-zero only on a DROP so the release-prep
# preflight can surface it as a [WARN], never a hard gate.
coverage-ratchet: coverage ## Compare coverage against the recorded floor
	uv run --no-sync python tools/coverage_ratchet.py --check

# Re-stamp the baseline floor from the current suite. Pass TESTS=<n> to record
# the suite size (otherwise the recorded count is left as-is). Run when cutting
# a release so the floor tracks the shipped suite. Commit the updated baseline.
coverage-ratchet-update: coverage ## Re-stamp the coverage floor (TESTS=<n>)
	uv run --no-sync python tools/coverage_ratchet.py --update $(if $(TESTS),--tests $(TESTS),)

# Type-check gate. Layers pyright into an ephemeral uv overlay (same
# `uv run --no-sync` pattern as coverage/check-shipped) so nothing is added to
# the system or the venv. Pyright config lives in pyproject [tool.pyright].
typecheck: ## Type-check the package with pyright
	uv run --no-sync --with pyright pyright sysforge/

# Pre-release shipped-file validator. Runs the seven check groups in
# tools/check_shipped.py (configs, pkgbuild, pkgbuild_parity, hooks,
# completions, versions, manpage). tools/release.sh invokes this from
# preflight; also runnable standalone.
##@ Docs, roadmap & gates
check-shipped: ## Gate: shipped-file parity (etc/, PKGBUILD, hooks…)
	uv run --no-sync python tools/check_shipped.py

# De-personalization gate. Fails if personal identity/path tokens leak into the
# published surface (docs, source comments, shipped configs); legitimate
# attribution (copyright/maintainer/--author) and the repo URL are allowed.
check-personal: ## Gate: no personal identity/path tokens in docs
	uv run --no-sync python tools/check_personal.py

# Regenerate DESIGN.md from the modular sources under docs/design/ (concatenated
# per docs/design/_manifest under a generated banner). DESIGN.md is generated --
# edit the sources, then run this.
design: ## Regenerate DESIGN.md from docs/design/
	uv run --no-sync python tools/build_design.py

# DESIGN.md drift gate (mirrors the manpage check). Fails if the committed
# DESIGN.md is out of date with its docs/design/ sources. Wired into preflight.
check-design: ## Gate: DESIGN.md in sync with docs/design/
	uv run --no-sync python tools/build_design.py --check

# Regenerate the Planned summary table in ROADMAP.md from each entry's
# `*Priority: … · Effort: …*` tag. Run after any add/remove/retag.
roadmap-table: ## Regenerate the Planned summary table in ROADMAP.md
	uv run --no-sync python tools/gen_roadmap_table.py

# Print the Planned summary table sorted by a column, without touching
# ROADMAP.md (the committed table always keeps its canonical triage order).
#   make roadmap-view                      # triage order (priority, effort, ID)
#   make roadmap-view SORT=effort          # cheapest first
#   make roadmap-view SORT=bump REVERSE=1  # major first
# SORT ∈ triage|id|item|priority|effort|bump. Ordinal columns rank by
# vocabulary (high < med < low), not alphabetically.
SORT ?= triage
roadmap-view: ## Print the Planned table sorted by SORT=<col> [REVERSE=1]
	@uv run --no-sync python tools/gen_roadmap_table.py --print --sort $(SORT) \
	    $(if $(REVERSE),--reverse,)

# ROADMAP.md summary-table drift gate (mirrors check-design). Also validates that
# every Planned entry carries a well-formed Priority/Effort tag. Wired into preflight.
check-roadmap-table: ## Gate: Planned table fresh and every entry tagged
	uv run --no-sync python tools/gen_roadmap_table.py --check

# Standards-compliance gate. Runs the mechanically-checkable groups in
# tools/check_standards.py (paths/XDG-FHS, SPDX/REUSE, Keep a Changelog
# headings, UTF-8 encoding). The behavioural subset (NO_COLOR, --version,
# stdout/stderr, RFC 3339, reproducibility) is covered by `make test`
# (tests/test_standards_compliance.py). Source of truth: docs/design/21-standards.md.
check-standards: ## Gate: standards compliance (docs/design/21-standards.md)
	uv run --no-sync --with reuse python tools/check_standards.py

# Allocate the next free ROADMAP ID for the CURRENT release cycle. TYPE is one
# of F/B/Q/STD; the version prefix is derived from pyproject.toml (counter
# resets on a minor/major bump), so a new item can't be misattributed to a
# stale cycle. Always run this before adding a ROADMAP entry.
#   make next-id TYPE=F   ->  e.g. 2.4.0-F1
next-id: ## Print the next free ROADMAP ID (TYPE=F|B|Q|STD)
	@uv run --no-sync python tools/check_standards.py --next-id $(TYPE)

# Print the bump the accumulated release notes require (standards row 3).
# Derived from docs/release-notes/unreleased.md: a `## Removed` section or a
# `**Breaking:**` bullet forces major, `## Added` minor, everything else patch.
# tools/release.sh enforces it; this target is for looking before you leap.
next-bump: ## Print the bump the release notes require
	@uv run --no-sync python tools/check_standards.py --derive-bump

# SemVer impact gate (standards row 3): fail if BUMP is weaker than what the
# accumulated release notes require. Caller: tools/release.sh preflight,
# routed through Make (not invoked directly) so test scaffolds that stub
# check-standards et al. as no-ops also cover this gate.
check-bump: ## Gate: BUMP=<kind> is not weaker than required
	@uv run --no-sync python tools/check_standards.py --require-bump=$(BUMP)

# Version-relative standards groups (deprecations still present / undocumented
# removals — rows 3 and 24), evaluated against VERSION, the release actually
# being cut. Caller: tools/release.sh preflight, after `make check-standards`
# (which cannot know the target version). Routed through Make for the same
# test-scaffold-stubbing reason as check-bump above.
check-standards-at: ## Gate: version-relative standards (VERSION=<x.y.z>)
	@uv run --no-sync python tools/check_standards.py --target-version=$(VERSION) \
	    --check=deprecations --check=semver_bump

# Merge new shipped defaults (etc/sysforge/) into the live config dir
# ($SYSFORGE_CONFIG_DIR itself, else /etc/sysforge). Add-only: injects new
# keys/sections/comments, never overwrites existing values. Pure comment /
# commented-example drift the key-merge can't carry is dropped beside the
# target as <name>.sfnew (pacnew-style) to diff & adopt. tomlkit is dev-only
# (ephemeral uv overlay). Preview with ARGS="--dry-run"; retarget with
# ARGS="--target DIR".
sync-config: ## Merge new shipped defaults into the live config dir
	uv run --no-sync --with tomlkit python tools/sync_config.py $(ARGS)

# Composite gate: lint + tests + shipped-file consistency + impersonal docs +
# DESIGN.md freshness + standards compliance. Run before kicking off
# `make release-{major,minor,patch}`.
##@ Release
pre-release: lint typecheck test check-shipped check-personal check-design check-roadmap-table check-standards ## Composite gate: lint, types, tests, all checks

release-major: ## Cut a signed major release
	bash tools/release.sh --bump=major

release-minor: ## Cut a signed minor release
	bash tools/release.sh --bump=minor

release-patch: ## Cut a signed patch release
	bash tools/release.sh --bump=patch

# Finish an in-flight release after a post-tag failure required fix commits on
# top of the release commit (tag no longer at HEAD): no bump, re-enters Phase 3.
release-resume: ## Finish an in-flight release after a post-tag fix
	bash tools/release.sh --resume

# scdoc hybrid: hand-written prose in man/sysforge.1.scd.in, COMMANDS
# sections generated from the argparse tree by tools/gen_options.py.
# man/sysforge.1.scd is an intermediate (gitignored); man/sysforge.1 is
# committed. COLUMNS pinned for reproducible argparse help wrapping.
##@ Housekeeping
man: ## Render the man page from the scdoc source
	mkdir -p man
	COLUMNS=80 PYTHONPATH=. uv run --no-sync python tools/gen_options.py \
	  --template man/sysforge.1.scd.in \
	  --out man/sysforge.1.scd
	scdoc < man/sysforge.1.scd > man/sysforge.1

clean: ## Remove build artefacts
	rm -rf dist/ __pycache__/ *.egg-info/ .pytest_cache/ .coverage coverage.json
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

distclean: clean ## clean, plus the venv and caches
	rm -rf .venv/

# ---------------------------------------------------------------------------
# Test VM
# ---------------------------------------------------------------------------

##@ Test VM
vm-deps: ## Install the QEMU/VM host dependencies
	$(call pacman_needed,$(DEV_DEPS_VM))

vm-image: ## Create the VM disk image
	mkdir -p $(VM_DIR)
	@if [ -f "$(VM_DISK)" ]; then \
		echo "Disk image already exists: $(VM_DISK)"; \
		echo "Delete it first if you want to recreate: make vm-clean"; \
	else \
		qemu-img create -f qcow2 $(VM_DISK) $(VM_DISK_SIZE); \
		echo "Created: $(VM_DISK) ($(VM_DISK_SIZE))"; \
	fi

vm-boot: ## Boot the VM headless
	./tools/vm/boot.sh

# Like vm-boot, but with a VNC display so a graphical desktop is visible.
# Connect with: gvncviewer localhost   (gtk-vnc — installed by vm-deps)
vm-boot-gui: ## Boot the VM with a VNC display
	./tools/vm/boot.sh --gui

# Boot the installed disk restored from a named savevm snapshot.
#   make vm-loadvm NAME=clean          # headless
#   make vm-loadvm NAME=clean GUI=1    # with a VNC display
# Also accepts the name positionally: make vm-loadvm clean
vm-loadvm: ## Boot from a named savevm snapshot
	@if [ -z "$(NAME)" ]; then echo "Usage: make vm-loadvm NAME=<snapshot-name> [GUI=1]  (or: make vm-loadvm <snapshot-name>)"; exit 2; fi
	./tools/vm/boot.sh --loadvm "$(NAME)" $(if $(GUI),--gui,)

vm-snapshot: ## Boot from the default snapshot
	./tools/vm/boot.sh --snapshot

vm-iso: ## Build the install ISO
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

vm-ssh: ## SSH into the VM (VM_USER=, defaults to root)
	ssh-keygen -R '[localhost]:10022' -f $(VM_DIR)/known_hosts 2>/dev/null; $(VM_SSH) $(VM_USER)@localhost

vm-ssh-root: ## SSH into the VM as root
	ssh-keygen -R '[localhost]:10022' -f $(VM_DIR)/known_hosts 2>/dev/null; $(VM_SSH) root@localhost

vm-ssh-builder: ## SSH into the VM as the builder user
	ssh-keygen -R '[localhost]:10022' -f $(VM_DIR)/known_hosts 2>/dev/null; $(VM_SSH) builder@localhost

vm-monitor: ## Attach to the QEMU monitor socket
	socat - UNIX-CONNECT:$(VM_DIR)/qemu-monitor.sock

# Attach to the VM's serial console over the host socket. Use for reading
# interactive prompts (e.g. the configure stage) when SSH isn't available
# (post-reboot, mid-pipeline). Detach with Ctrl-] (the socat escape).
# Requires console=ttyS0 on the guest cmdline — see tools/vm/README.md.
vm-console: ## Attach to the VM serial console
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
vm-savevm: ## Save a named VM snapshot
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
vm-stop: ## Stop the VM and clear its runtime state
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

vm-clean: ## Remove the VM disk, snapshots and sockets
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

##@ Packaging in the VM
vm-pkg-stable: ## Build the stable package in the VM chroot
	./tools/vm/build-pkg.sh stable --out=$(VM_BUILD_DIR) --chroot=$(VM_CHROOT)

vm-pkg-git: ## Build the -git package in the VM chroot
	./tools/vm/build-pkg.sh git --out=$(VM_BUILD_DIR) --chroot=$(VM_CHROOT)

vm-pkg-all: vm-pkg-stable vm-pkg-git ## Build both packages in the VM chroot

vm-install-stable: ## Install the built stable package in the VM
	./tools/vm/install-pkg.sh stable --pkg-dir=$(VM_BUILD_DIR)

vm-install-git: ## Install the built -git package in the VM
	./tools/vm/install-pkg.sh git --pkg-dir=$(VM_BUILD_DIR)

# Full round-trip; assumes the VM is already booted (e.g. `make vm-snapshot`).
vm-smoke: ## Run the post-install smoke checks in the VM
	./tools/smoke.sh

vm-test: vm-pkg-stable vm-install-stable vm-smoke ## Build, install and smoke-test in the VM

# ---------------------------------------------------------------------------
# Container tier (2.6.1-F2)
#
# The same checks as vm-smoke, over `podman exec` instead of SSH, against a
# throwaway container — seconds instead of a boot + snapshot. Parameterized by
# distro: the `cachyos` arm is the one that exercises repo/AUR shadowing, a
# different makepkg.conf baseline, and bumped pkgrels on core packages.
#
# Needs a package built first (make vm-pkg-stable). Bootstrap, kernel staging,
# graphics/DKMS and restart detection stay with the VM tier.
# See tools/container/README.md.
# ---------------------------------------------------------------------------

##@ Container tier
container-build: ## Build the test container image
	./tools/container/harness.sh build --distro=$(or $(DISTRO),arch)

container-smoke: ## Run the smoke checks in an Arch container
	./tools/container/harness.sh smoke --distro=$(or $(DISTRO),arch) --pkg-dir=$(VM_BUILD_DIR)

container-smoke-cachyos: ## Run the smoke checks in a CachyOS container
	./tools/container/harness.sh smoke --distro=cachyos --pkg-dir=$(VM_BUILD_DIR)

container-shell: ## Open a shell in the test container
	./tools/container/harness.sh shell --distro=$(or $(DISTRO),arch) --pkg-dir=$(VM_BUILD_DIR)

container-clean: ## Remove the test container and image
	./tools/container/harness.sh clean --distro=$(or $(DISTRO),arch)
