# Release Checklist

Operational runbook for cutting a SysForge release. Work top to bottom; every step is a command you
can paste. Design rationale for the gates lives in `docs/design/17-release-plan.md` (§Release
Process); this file is the checklist, not the explanation.

**Rule of thumb:** stages 1 to 5 can be repeated freely and cost nothing but time. Stage 6 creates a
signed tag, after which the only forward path is `make release-resume`. Do not enter stage 6 with
anything from stages 1 to 5 unchecked.

## At a glance

```bash
make next-bump                  # 1. what level the release notes require
make design roadmap-table       # 2. regenerate, then commit anything that moved
make ci-local                   # 3. every static gate + full suite, clean container
make coverage-ratchet audit     # 3. the two checks no gate runs
make e2e                                                                     # 4a
make vm-pkg-stable && make container-smoke && make container-smoke-cachyos   # 4a
make vm-snapshot && make vm-test && make vm-stop                             # 4b (+ git flavor)
# 5. /release-notes skill
make release-minor              # 6. or release-major / release-patch
# 7. push the sha256 commit + both AUR repos (commands below)
```

`make preflight` is not in that list on purpose. It is an optional status report you can run at any
point to see where stages 1 to 4 stand (next section).

---

## Which command does what

Four commands check release readiness. Their names overlap, so here is what each one is for.

| Command | Question it answers | Runs where | Can it stop a release? |
| --- | --- | --- | --- |
| `make pre-release` | Is the code correct? | your workstation | no (advisory) |
| `make ci-local` | Is the code correct away from my workstation? | throwaway Arch container | no (advisory) |
| `make preflight` | Is the repo *and its infrastructure* ready to release? | your workstation | no (report only) |
| `make release-*` gates | (inside `tools/release.sh`) Is it safe to tag *this version* now? | your workstation | **yes**: refuses to tag |

In more detail:

- **`make pre-release`** runs the static gates (`lint`, `typecheck`, `test`, and the five `check-*`
  gates) against your working tree, in your shell. It works on any branch at any time and changes
  nothing. Because it runs in your environment, a test that only passes because of your machine
  (clang-forced shell, live config, installed extras, your hardware) passes here too.
- **`make ci-local`** runs the same gate list as `pre-release` (both read the Makefile's `GATES`),
  but in a fresh `archlinux:base-devel` container, as an unprivileged user, with only the core dev
  tools installed. It is the exact job GitHub CI runs (`tools/ci.sh`), reading your working tree,
  uncommitted edits included. **For a release, prefer it over `pre-release`:** it checks strictly
  more. Needs podman and network; fall back to `make pre-release` without them.
- **`make preflight`** (`tools/preflight.sh`) is a read-only readiness report: one pass that prints
  `PASS`/`WARN`/`FAIL` per section. It checks the things the gates do not: that the release
  infrastructure is present (clean `main`, chroot, a running VM, podman) and the coverage ratchet,
  VM smoke and derivative container smoke when that infrastructure is available. It does **not**
  run `typecheck` or the `check-*` gates, so a green preflight says nothing about them. Missing
  infrastructure is always a `WARN`, never a `FAIL`, so it cannot enforce the version-sensitive
  rules in stage 4. Use it as a dashboard, not a gate. Skip its slow sections with
  `RUN_COVERAGE=0`, `RUN_VM_SMOKE=0`, `RUN_DISTRO_SMOKE=0`.
- **The release script's gates** run automatically at the start of `make release-*`, before any file
  is touched. They are the only checks a release cannot skip. They re-run the shared static gates
  (they cannot assume you ran `pre-release` recently) and add the version-dependent ones, which need
  the target version and so cannot run any earlier. They do not run the full suite; that is the
  minutes-long tier, covered by stage 3.

### What each one checks

| Check | `pre-release` | `ci-local` | `preflight` | release gates |
| --- | :---: | :---: | :---: | :---: |
| `lint` (ruff + shellcheck) | yes | yes | yes | yes |
| `typecheck` (pyright) | yes | yes | no | yes |
| full test suite | yes (`test`) | yes (`test`) | yes (`coverage`) | no |
| end-to-end tier (`e2e`) | yes | no (its own CI job) | no | no |
| `check-shipped` (includes version markers, completions) | yes | yes | subset | yes |
| `check-personal`, `check-design`, `check-roadmap-table`, `check-standards` | yes | yes | no | yes |
| clean environment (not your workstation) | no | yes | no | no |
| coverage ratchet | no | no | yes (warn) | no |
| on `main`, clean tree | no | no | yes | yes |
| chroot present | no | no | yes (warn) | yes |
| `check-bump BUMP=<kind>`, `check-standards-at VERSION=<x.y.z>` | no | no | no | yes |
| GPG signing key usable, release notes non-empty | no | no | no | yes |
| VM smoke (installed package) | no | no | yes, if VM is up | no |
| derivative container smoke | no | no | yes, if podman | no |
| Arch base container smoke, CVE `audit`, VM `git` flavor | no | no | no | no |

The last row is why stages 3 and 4 exist: nothing runs those checks unless you do.
`check_standards` (`release_gates` group, standards row 27) keeps the `pre-release` and
release-gate columns in agreement, so a gate added to one cannot silently be missing from the other.

---

## Stage 1: Repo hygiene

- [ ] On `main`, working tree clean (`git status --short` empty). The release script refuses
      otherwise.
- [ ] Every shipped ROADMAP item removed from `ROADMAP.md` in its own landing commit (git history is
      the record; no "done" markers).
- [ ] `docs/release-notes/unreleased.md` has authored `## ` sections. The release script refuses an
      empty accumulator.
- [ ] Entries in ascending roadmap-ID order.

```bash
git status --short
make next-bump          # the level the accumulated notes require (a `## Removed` can force major)
```

## Stage 2: Regenerate, then commit

The gates only *detect* stale generated files; they never fix them.

```bash
make design roadmap-table
git status --short      # commit anything that moved
```

## Stage 3: Static gates

```bash
make ci-local           # every pre-release gate + the full suite, in a clean container
make coverage-ratchet   # coverage floor (no gate runs it)
make audit              # dependency CVEs (no gate runs it)
```

- [ ] `make ci-local` exits 0 (no podman or network: run `make pre-release` instead, and note that
      the clean-environment check was skipped)
- [ ] coverage ratchet reviewed
- [ ] `make audit` clean, or each finding reviewed

A coverage drop below the floor in `tests/COVERAGE_BASELINE.md` is advisory, never a hard gate. If
the drop is intended, or coverage improved, re-stamp the floor and commit it before releasing:

```bash
make coverage-ratchet-update TESTS=$(make test 2>/dev/null | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+')
```

## Stage 4: Runtime tiers (container + VM)

No gate touches these. Both tiers install a *real built package*, so both start from
`make vm-pkg-stable`, the same clean-chroot build a release uses.

**Exit codes** (both container smokes): `0` is a pass, `1` is a real break, `3` means the harness
is unavailable (no podman, no network, base image unpullable). `3` is an infrastructure gap, not a
pass: fix it or record the tier as unrun.

### 4a. Container tier: fast, run first

The end-to-end tier first: the documented paths (the Quick start, a repo package's opt-in, flag
drift, a failed build's cleanup) run as an unprivileged user on a fresh system, one container per
scenario. It builds sysforge from the checkout itself, so it needs no `vm-pkg-stable`. CI runs the
same scenarios on every pull request; run it here for the release commit. An `XFAIL` is a known,
filed bug and does not block; an `XPASS` fails the run (remove the scenario's `xfail` marker).

```bash
make e2e                        # tools/e2e/README.md
```

- [ ] `make e2e` exits 0

The smokes then check the packaging itself.

Seconds rather than a boot. The CachyOS arm is the one that actually exercises portability (repo/AUR
shadowing, a different `makepkg.conf` baseline, bumped pkgrels on core packages).

```bash
make vm-pkg-stable              # build the package (prerequisite for both smokes)
make container-smoke            # Arch base
make container-smoke-cachyos    # derivative: the portability arm
```

- [ ] `container-smoke` exits 0
- [ ] `container-smoke-cachyos` exits 0, **required for a minor or major release**

> **Cadence rule.** The support tiers promise CachyOS is "validated each minor". For a **minor or
> major** bump the derivative arm must exit `0`; an unavailable harness is not good enough. An
> unrun derivative arm (or `RUN_DISTRO_SMOKE=0` in preflight) is acceptable for a **patch** release
> only. If it did not exit `0`, install podman (`make dev-deps-container`) or get online and re-run;
> do not proceed. `make preflight` only *warns* here, which is why the rule lives in this file.

Not covered by the container tier: bootstrap, kernel staging, graphics/DKMS, restart detection.
Those need stage 4b.

### 4b. VM tier: the full round trip

Boot ephemerally so the run cannot contaminate the snapshot. All boot targets background themselves
(`-daemonize`) and return immediately. Run this with a clean tree: the `git` flavor builds from
**committed state only**, while `stable` tarballs the **live working tree**, so uncommitted edits
would make the two flavors test different code.

```bash
make vm-snapshot                # boot ephemeral (writes discarded on exit)
make vm-test                    # = vm-pkg-stable + vm-install-stable + vm-smoke
make vm-stop
```

Also validate the VCS flavor. It uses a different PKGBUILD and source basis, and the two carry a
`conflicts=()` pair that this install exercises:

```bash
make vm-snapshot
make vm-pkg-all                 # both flavors
make vm-install-stable && make vm-smoke
make vm-install-git  && make vm-smoke    # replaces stable via conflicts=()
make vm-stop
```

- [ ] `vm-smoke` passes on the `stable` flavor
- [ ] `vm-smoke` passes on the `git` flavor
- [ ] the stable/git conflict pair swapped cleanly (the install step fails otherwise)
- [ ] the smoked VM reports the current `pyproject.toml` version, not an older build

> **Staleness rule.** `tools/smoke.sh`'s version check is liveness-only: it accepts *any* version,
> so a VM still holding an older build passes every assertion. A pass only counts when the installed
> version matches `pyproject.toml`. `make preflight` reports a mismatch as `[WARN] … not vX.Y.Z`;
> for a release, treat that as a fail. `make vm-test` refreshes the install; `make vm-smoke` alone
> only checks whatever is already there.

If the VM does not exist yet, first-time setup (ISO, disk image, install, clean snapshot) is in
`tools/vm/README.md`; the container tier is documented in `tools/container/README.md`.

## Stage 5: Release notes

```bash
# /release-notes skill: reconciles and lints the accumulator
```

- [ ] Entries reconciled against `docs/design/17-release-plan.md` §Release notes framing
- [ ] Keep a Changelog categories only (`Added`/`Changed`/`Deprecated`/`Removed`/`Fixed`/`Security`)
- [ ] Roadmap IDs inline, entries in ascending order

## Stage 6: Cut the release

Point of no return: phase 1 creates a signed tag.

```bash
make next-bump                  # confirm the required level
make check-bump BUMP=minor      # confirm the intended level is not weaker
make release-minor              # or release-major / release-patch
```

The script first runs its release gates (see "Which command does what"), then bumps versions,
regenerates `uv.lock` and the man page, renames the accumulator to `vX.Y.Z.md`, signs the commit and
tag, pauses for the manual push, publishes the signed GitHub release, and validates both PKGBUILDs
in a clean chroot.

- [ ] Push when prompted: `git push origin main && git push origin vX.Y.Z`
- [ ] Signed GitHub release published with `.asc` + `SHA256SUMS` assets
- [ ] Both PKGBUILDs validated in the clean chroot
- [ ] Stage 7 below completed

**If it fails after the tag exists**, do not re-run a `release-*` bump target: it would compute a
fresh bump from the already-bumped version. Fix forward, commit, then:

```bash
make release-resume
```

## Stage 7: Post-release manual steps

The script stops here: the signed GitHub release exists, but the sha256 commit is unpushed and
neither AUR repo knows about the new version. Substitute the released version for `X.Y.Z`.

Push the sha256 commit the chroot validation produced:

```bash
git push origin main
```

Push to AUR (`sysforge`, stable):

```bash
git clone ssh://aur@aur.archlinux.org/sysforge.git /tmp/aur-sysforge
cp PKGBUILD .SRCINFO sysforge.install /tmp/aur-sysforge/
cd /tmp/aur-sysforge && git add -A && git commit -m "Update to X.Y.Z" && git push
```

Push to AUR (`sysforge-git`, VCS; note the `-git` suffixed sources land as plain names):

```bash
git clone ssh://aur@aur.archlinux.org/sysforge-git.git /tmp/aur-sysforge-git
cp PKGBUILD-git /tmp/aur-sysforge-git/PKGBUILD
cp .SRCINFO-git /tmp/aur-sysforge-git/.SRCINFO
cp sysforge.install /tmp/aur-sysforge-git/
cd /tmp/aur-sysforge-git && git add -A && git commit -m "Update to X.Y.Z" && git push
```

- [ ] sha256 commit pushed to `origin/main`
- [ ] `sysforge` AUR repo updated
- [ ] `sysforge-git` AUR repo updated

---

## Prerequisites (one-time, per machine)

```bash
make dev-deps                   # all tiers (podman for ci-local and the container tier included)
make dev-deps-list              # per-tier state
sudo mkarchroot /var/lib/archbuild/extra-x86_64/root base-devel   # release chroot
git config --global user.signingkey <KEYID>
git config --global commit.gpgsign true
```

The release script's signing check refuses before touching any file if the key is unusable, so an
unsigned release cannot be produced.
