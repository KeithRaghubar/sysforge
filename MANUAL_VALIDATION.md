# Manual Validation Checklist — Standalone Phase Commands

## `sysforge reconfigure`

**Step selection**
- [x] Enter → all 8 steps
- [x] Single number (`3`), multiple (`1 3 5`), range (`2-5`), named (`network gpg`), mixed (`1-3 gpg`)
- [x] Invalid input → falls back to all

**`--dry-run`** — all steps, no prompts, no writes

**editor** — show current + source; change without save; change + save to sysforge.toml

**config** — lists present files; edit flag_profiles (validates, re-open on fail); hardware_profile shows warning

**makepkg** — shows MAKEFLAGS/BUILDDIR/CFLAGS/etc; BUILDDIR missing → warn; offer sudo edit

**sudo** — root → warn; passwordless → OK; password + wheel → OK; not in wheel → warn

**disk** — free/total shown; <20GB → warn; estimated AUR space shown; estimated > free → warn

**network** — probes AUR / GitHub / archlinux.org / first mirrorlist entry; unreachable → warn

**gpg** — key count; imports `keys/pgp/*.asc`; `y` → gpg --refresh-keys

**preview** — lists packages with action/profile; toolchain + kernel summaries

**Final prompt** — `y` → clean exit; `n` → "Aborted by user." (standalone wording); non-interactive → no prompt

**Flags**
- [ ] `--packages` → disk + preview use that file
- [ ] `--state-dir` → pipeline progress display uses custom state

---

## `sysforge toolchain`

- [ ] No toolchain.toml → clean no-op
- [ ] GCC: builds, installs, state has `cc=/usr/bin/gcc cxx=/usr/bin/g++`
- [ ] LLVM pgo=false: single pass, state has `cc=…/clang cxx=…/clang++ ld=lld`
- [ ] LLVM pgo=true: pass 1 (system CC, install) → pass 2 (instrumented, extract to staging) → pass 3 (staged CC, install, staging removed); state result correct
- [ ] `--dry-run` → logs passes, no build, no state written
- [ ] `--no-update` → no git pull
- [ ] `--state-dir` → result written to custom dir

---

## `sysforge packages`

**Happy path**
- [ ] Repo packages → `pacman -S --needed`
- [ ] AUR/git packages → resolved, built, installed
- [ ] Summary: Total / Built / Failed / Skipped

**Checkpointing**
- [ ] Kill mid-run, re-run → already-built skipped
- [ ] All already built → all skipped, 0 built

**Failed package prompt**
- [ ] `r` retry all / `s` skip all / `c` per-package / `a` abort
- [ ] `--force-retry` → retries all without prompt

**Hardware gate**
- [ ] `requires_hardware`, no hardware_profile → skipped + warn
- [ ] `requires_hardware`, profile present + field set → built
- [ ] `requires_hardware`, profile present + field absent → skipped

**Profile override** — `profile = "patched"` → patched_pkgbuild build mode applied

**Toolchain injection**
- [ ] Run `sysforge toolchain` first (same state dir) → CC/CXX logged + injected per build
- [ ] No toolchain result → no injection, no log line

**`--dry-run`** — lists builds/installs; shows profile + CC overrides where applicable

**Flags**
- [ ] `--no-update` → no git pull per build
- [ ] `--packages` → loads custom packages.toml
- [ ] `--no-pkg-logs` → no per-package log files
- [ ] `--cache-report` → structured cache summary at end

---

## `sysforge kernel`

- [ ] No kernel.toml → clean no-op

**Happy path**
- [ ] lsmod snapshot written to state dir
- [ ] kconfig fragment written (`sysforge.config` in pkgbuild dir)
- [ ] Build runs
- [ ] `mkinitcpio -P` runs
- [ ] Bootloader updated (`systemd-boot` / `grub` / `none` → skipped)

**kconfig**
- [ ] Hardware kconfig from hardware_profile applied; manual entries win on conflict (+ warn)
- [ ] No entries from either source → no fragment, no error
- [ ] Invalid option / empty value / duplicate → RuntimeError before build

**Toolchain injection**
- [ ] Toolchain result in state → CC/CXX logged + injected
- [ ] No toolchain result → no injection

**Error cases** — missing pkgbuild_dir / pkgname / PKGBUILD → RuntimeError

**`--dry-run`** — logs all steps, no fragment written, nothing executed

**Flags**
- [ ] `--no-update` → no git pull
- [ ] `--state-dir` → lsmod + toolchain result from custom dir

---

## Cross-stage

- [ ] `sysforge toolchain && sysforge packages` → CC/CXX injected into package builds
- [ ] `sysforge toolchain && sysforge kernel` → CC/CXX injected into kernel build
