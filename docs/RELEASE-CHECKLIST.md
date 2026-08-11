# Release Checklist

Operational runbook for cutting a SysForge release. Work top to bottom; every step is a command you
can paste. Design rationale for the gates lives in `docs/design/17-release-plan.md` (§Release
Process) — this file is the checklist, not the explanation.

**Rule of thumb:** stages 1–4 can be repeated freely and cost nothing but time. Stage 6 creates a
signed tag, after which the only forward path is `make release-resume`. Do not enter stage 6 with
anything from stages 1–5 unchecked.

---

## The two gate runners

Neither is a superset of the other, which is the usual source of confusion.

| Gate | `make pre-release` | `tools/release.sh` preflight |
| --- | :---: | :---: |
| `lint` (ruff + shellcheck) | yes | — |
| `typecheck` (pyright) | yes | — |
| `test` (full suite) | yes | — |
| `check-shipped` | yes | yes |
| `check-personal` | yes | yes |
| `check-design` | yes | yes |
| `check-roadmap-table` | yes | yes |
| `check-standards` | yes | yes |
| `check-bump BUMP=<kind>` | — | yes |
| `check-standards-at VERSION=<x.y.z>` | — | yes |
| on `main`, clean tree, chroot present, GPG key usable | — | yes |

`make pre-release` holds the slow, version-*independent* checks so they run on any branch at any
time. The release script holds the version-*dependent* ones — they need the target version, which
does not exist until a bump level is chosen — plus the repo and signing preconditions. The five
shared gates are re-run by the script deliberately: it cannot assume `pre-release` ran recently, and
stale generated files discovered *after* the tag cost a `release-resume` cycle.

Neither runner covers coverage, CVE audit, or the VM/container tiers. Those are stages 3 and 4
below, and they are the ones that get skipped precisely because no gate enforces them.

## The fast pre-check

```bash
make preflight          # read-only; ~stages 1-4 in one pass, PASS/WARN/FAIL per section
```

`tools/preflight.sh` mutates nothing and can be run at any point. It re-checks a subset of what the
two runners cover (branch/tree, version markers, lint, completions) and adds the three things no
gate touches: the coverage ratchet, the VM smoke, and the derivative container arm. Skip the slow
sections with `RUN_COVERAGE=0`, `RUN_VM_SMOKE=0`, `RUN_DISTRO_SMOKE=0`.

It is a convenience, not an authority. Its policy is deliberately uniform — missing optional
infrastructure always warns, never fails — so the two **version-sensitive** rules in stage 4 below
are the checklist's to enforce, not the script's. A green `make preflight` does not on its own mean
stage 4 is satisfied.

---

## Stage 1 — Repo hygiene

- [ ] On `main`, working tree clean (`git status --short` empty) — the script hard-fails otherwise.
- [ ] Every shipped ROADMAP item removed from `ROADMAP.md` in its own landing commit (git history is
      the record; no "done" markers).
- [ ] `docs/release-notes/unreleased.md` has authored `## ` sections — the preflight hard-fails on an
      empty accumulator.
- [ ] Entries in ascending roadmap-ID order.

```bash
git status --short
make next-bump          # what the accumulated notes require (a `## Removed` forces major)
```

## Stage 2 — Regenerate, then commit

The gates only *detect* drift here; they never fix it.

```bash
make design roadmap-table
git status --short      # commit anything that moved
```

## Stage 3 — Static gates

```bash
make pre-release        # lint + typecheck + test + the five shared checks
make coverage-ratchet   # coverage floor — not in any gate
make audit              # dependency CVEs — not in any gate
```

A coverage drop below the floor in `tests/COVERAGE_BASELINE.md` is advisory, never a hard gate. If
the drop is intended — or coverage improved — re-stamp the floor and commit it before releasing:

```bash
make coverage-ratchet-update TESTS=$(make test 2>/dev/null | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+')
```

## Stage 4 — Runtime tiers (VM + container)

Neither runner touches these. Both tiers install a *real built package*, so both start from
`make vm-pkg-stable` — the same clean-chroot path a release uses.

### 4a. Container tier — fast, run first

Seconds rather than a boot. The CachyOS arm is the one that actually exercises portability (repo/AUR
shadowing, a different `makepkg.conf` baseline, bumped pkgrels on core packages).

```bash
make vm-pkg-stable              # build the package (prerequisite for both smokes)
make container-smoke            # Arch base
make container-smoke-cachyos    # derivative — the portability arm
```

- [ ] `container-smoke` exits 0
- [ ] `container-smoke-cachyos` exits 0

Exit code `3` means the harness is unavailable (no podman, no network, base image unpullable) — that
is an infrastructure gap, not a pass. Resolve it or record the tier as unrun; do not read `3` as
green. Only `0` is a pass and `1` is a real break.

**Cadence rule — bump-sensitive, and the reason a script cannot own this.** The support tiers
promise CachyOS is "validated each minor," but `preflight.sh` section 9 only *warns* when the
harness is absent, and a warn can never enforce a cadence. So:

> For a **minor or major** bump, the derivative arm must be **green** — an unavailable harness is
> not good enough. A yellow/unrun derivative arm is acceptable for a **patch** release only.

If the bump is minor or major and `container-smoke-cachyos` did not exit `0`, the fix is to install
podman (`make dev-deps-container`) or get online and re-run — not to proceed. `RUN_DISTRO_SMOKE=0`
is likewise appropriate for a patch release only.

Not covered by the container tier: bootstrap, kernel staging, graphics/DKMS, restart detection.
Those need stage 4b.

### 4b. VM tier — the full round trip

Boot ephemerally so the run cannot contaminate the snapshot. All boot targets background
themselves (`-daemonize`) and return immediately.

```bash
make vm-snapshot                # boot ephemeral (writes discarded on exit)
make vm-test                    # = vm-pkg-stable + vm-install-stable + vm-smoke
make vm-stop
```

For a release, also validate the VCS flavor — it uses a different PKGBUILD, a different source basis
(committed state only), and the two carry a `conflicts=()` pair that the install itself tests:

```bash
make vm-snapshot
make vm-pkg-all                 # both flavors
make vm-install-stable && make vm-smoke
make vm-install-git  && make vm-smoke    # replaces stable via conflicts=()
make vm-stop
```

- [ ] `vm-smoke` passes on the `stable` flavor
- [ ] `vm-smoke` passes on the `git` flavor
- [ ] the stable↔git conflict pair swapped cleanly (the install step would have failed otherwise)

**Staleness rule.** `tools/smoke.sh`'s version gate is liveness-only — it accepts *any* version, so a
VM still holding a build of an older release passes every assertion. A pass therefore only counts
when the installed version matches `pyproject.toml`; `preflight.sh` section 8 reports this as a
`[WARN] … not vX.Y.Z`, which for a release is a **fail**. `make vm-test` is the full round trip that
refreshes it (`vm-pkg-stable` → `vm-install-stable` → `vm-smoke`); `make vm-smoke` alone only checks
whatever is already installed.

- [ ] the smoked VM reports the current `pyproject.toml` version, not an older build

Note the flavor semantics: `stable` tarballs the **live working tree** (uncommitted edits included),
`git` builds from a local bare clone and sees **committed state only** — it warns on a dirty tree. A
release must be validated from committed state, so run 4b with a clean tree.

If the VM does not exist yet, first-time setup (ISO, disk image, install, clean snapshot) is in
`tools/vm/README.md`; the container tier is documented in `tools/container/README.md`.

## Stage 5 — Release notes

```bash
# /release-notes skill — reconciles and lints the accumulator
```

- [ ] Entries reconciled against `docs/design/17-release-plan.md` §Release notes framing
- [ ] Keep a Changelog categories only (`Added`/`Changed`/`Deprecated`/`Removed`/`Fixed`/`Security`)
- [ ] Roadmap IDs inline, entries in ascending order

## Stage 6 — Cut the release

Point of no return: phase 1 creates a signed tag.

```bash
make next-bump                  # confirm the required level
make check-bump BUMP=minor      # confirm the intended level is not weaker
make release-minor              # or release-major / release-patch
```

The script runs its own preflight (table above), then bumps versions, regenerates `uv.lock` and the
man page, renames the accumulator to `vX.Y.Z.md`, signs the commit and tag, pauses for the manual
push, publishes the signed GitHub release, and validates both PKGBUILDs in a clean chroot.

- [ ] Push when prompted: `git push origin main && git push origin vX.Y.Z`
- [ ] Signed GitHub release published with `.asc` + `SHA256SUMS` assets
- [ ] Both PKGBUILDs validated in the clean chroot
- [ ] Stage 7 below completed

**If it fails after the tag exists**, do not re-run a `release-*` bump target — it would compute a
fresh bump from the already-bumped version. Fix forward, commit, then:

```bash
make release-resume
```

## Stage 7 — Post-release manual steps

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

Push to AUR (`sysforge-git`, VCS — note the `-git` suffixed sources land as plain names):

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
make dev-deps                   # all tiers
make dev-deps-list              # per-tier state
sudo mkarchroot /var/lib/archbuild/extra-x86_64/root base-devel   # release chroot
git config --global user.signingkey <KEYID>
git config --global commit.gpgsign true
```

The signing preflight hard-fails before touching any file if the key is unusable, so an unsigned
release cannot be produced.
