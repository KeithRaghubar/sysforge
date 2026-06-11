## Release Plan

- **GitHub:** public from day one; source of truth for all code
- **v0.1.0** (shipped) — profiled AUR helper. Userspace commands stable under real use: `build`, `fetch`, `update`, `resolve`, `doctor`, `converge`, `setup`, `packages` (list/add/remove/sync), `run pipeline`, `run reconfigure`, `run packages`. The `run toolchain` and `run kernel` stages shipped in this release; they were temporarily reclassified as experimental in v1.0 pending more testing and re-promoted in v1.x — see notes below. Marks the AUR publication milestone.
- **v0.2.0** (shipped) — follow-up release on the v0.1.0 surface: VM tooling (`tools/vm/`, `make vm-*` targets), install-path fixes for fresh Arch systems on Python 3.14, bulk-operation progress indicator, VCS detection and paging fixes, `doctor --graphics` scope refinement.
- **v1.0** (shipped) — system bootstrapper. Stages 1–4 fully implemented (partition, base_install, hardware, configure). Configure stage installs systemd-boot, enables NetworkManager/sshd, creates primary user with sudo, writes shell dotfiles, sets passwords, and sets the configured default login shell. Build-state persistence, `pacman -Q` superset coverage, and coloured CLI output all landed in 2026-04. v1.0 also reclassified `run toolchain`, `run kernel`, and the `sysforge update` PGO-toolchain profdata-reuse path (`build_mode = "pgo_llvm_toolchain"`) as **experimental, deferred post-1.0**: those code paths shipped but emitted a runtime `[WARN]` and were recommended-off for 1.0 users. The reclassification was lifted in v1.x once the implementations stabilised. Default `compiler` resolution is `gcc`; LLVM is opt-in. The shipped `[profiles.standard]` uses the system gcc/binutils; LLVM components (`clang`, `lld`, `llvm`, `compiler-rt`) are `optdepends` on the sysforge PKGBUILD and only required by the opt-in LLVM profile and `run toolchain --compiler=llvm`. Published to AUR via `tools/release.sh`.
- **v2.0** (pending release) — breaking cleanups plus the flagship trust feature. Removed the deprecated `converge` verb (its build-state-wide flag-drift coverage folded into `sysforge update` Phase 4.3 as the fold pass) and the legacy `update_repo_profiled` alias. Added the **PKGBUILD review gate** (full source-tree diff prompt before building any package whose source changed since the last accepted build; `--no-review` / `[build] review = false` escape hatches), **package groups** (`[group.*]` manifest tables expanding at load time), and the **scdoc-hybrid man page** (hand-written prose + generated COMMANDS sections; `argparse-manpage` dropped entirely). Build-failure auto-repair had already landed quietly in v1.x and is documented under Makepkg Wrapper.
- **v1.x:** `repo_mode = "profiled"` support in `sysforge update`; wrapping `pacman -Syu` inside `sysforge update` for a full AUR-helper experience; man page migration from `argparse-manpage` to a scdoc hybrid (hand-written narrative + auto-generated OPTIONS — see Man Pages section below); package groups (named DE sets for opt-in without enumerating every package); rule priority auto-calculation (CSS-specificity-style scoring from rule conditions); configure stage additions (btrfs snapshots, ccache/sccache init check, build time estimates); LLVM target filtering from hardware detection. The toolchain and kernel stages (and the `pgo_llvm_toolchain` update path) have been re-promoted from experimental — see the V1.x Roadmap *Landed* list below.

### AUR publishing process

Releases are driven by three Makefile targets — `make release-major`, `make release-minor`, `make release-patch` — each of which calls `tools/release.sh --bump=<level>`. The script handles the full flow end-to-end with a single up-front summary + approval prompt and one mid-run pause for the manual tag push. Phases:

1. **Bump, commit, tag.** Rewrites `pyproject.toml` (the single source of truth for version), `PKGBUILD` `pkgver=`, `PKGBUILD-git` `pkgver=` (leading `X.Y.Z` only — the `.r0.g0000000` suffix is preserved as the placeholder for the dynamic `pkgver()`), the `<!--version-->vX.Y.Z<!--/version-->` markers in `README.md` and `DESIGN.md`, regenerates `uv.lock` (via `uv lock`) and `man/sysforge.1` (via `make man`), then makes a single `release: vX.Y.Z` commit and tags it.
2. **Push pause.** Prints `git push origin main && git push origin vX.Y.Z` and waits on ENTER. The user pushes manually (releases are deliberate, not background events). The script verifies the tag is on `origin` before continuing.
3. **Post-tag artifacts.** Fetches the GitHub tarball sha256, updates `sha256sums=` in `PKGBUILD`, **validates both `PKGBUILD` and `PKGBUILD-git` in a clean chroot**, regenerates `.SRCINFO` and `.SRCINFO-git` (both gitignored — local artifacts only), and makes a second `release: vX.Y.Z sha256` commit (the `.SRCINFO` files do not get committed).
4. **Final instructions.** Prints `git push origin main` and the `git clone`/`cp`/`commit`/`push` sequence for the `sysforge` and `sysforge-git` AUR repos. The user runs those manually.

If interrupted between phases (Ctrl-C at the push pause, or a transient failure), re-running the same `make release-*` command resumes correctly: the script detects that the tag for the *current* `pyproject.toml` version already exists at HEAD and skips Phase 1.

The version markers in `README.md` and `DESIGN.md` are HTML comments (`<!--version-->vX.Y.Z<!--/version-->`) so they render invisibly. Only the marked token rotates per release — historical version mentions in prose (`v0.1.0`, `v0.2.0`, `v1.0`, `v1.x`) are deliberately not wrapped and stay frozen. The `versions` check group below enforces lockstep across all marker locations; release pre-flight refuses to run if the markers (or the `pkgver` lines, or any other version-bearing field) are out of sync.

**Shipped-file pre-release checks.** Phase 1 of `tools/release.sh` (and the standalone `make check-shipped` / `make pre-release` targets) gate on `tools/check_shipped.py`, which validates every artifact the PKGBUILD ships:

- **`configs`** — every `etc/sysforge/*.toml` is parsed through its real runtime loader (`load_config`, `load_sysforge_toml`, `load_bootstrap`, the stage `_load_*` helpers); unknown top-level sections/keys against a per-file allowlist are an error; missing `tests/data/etc/sysforge/` counterpart for every shipped TOML (except per-host `bootstrap.toml`) is an error.
- **`pkgbuild`** — every `install -Dm…` source in `PKGBUILD` must exist in the working tree; every `$pkgdir/etc/…` install target must be declared in `backup=()`, and vice versa (no stale `backup=` entries); `sha256sums` is not a placeholder (`SKIP`, all-zero, `DRYRUN…`).
- **`pkgbuild_parity`** — `PKGBUILD` and `PKGBUILD-git` parse to the same dict (via `pkgbuild_meta.parse_pkgbuild`) except for a tightly-scoped allowlist of keys that are *supposed* to differ (`pkgname`, `pkgver`, `pkgrel`, `pkgdesc`, `source`, `sha256sums`, `conflicts`, `provides`). `depends` / `makedepends` / `optdepends` / `backup` arrays must be byte-identical.
- **`hooks`** — every `etc/pacman.d/hooks/sysforge-*.hook` `Exec` line must invoke `tools/pacman-hook-helper.sh` and pass a subcommand the helper documents (`kernel`, `toolchain`, `buildstate`).
- **`completions`** — every verb and every long-flag in the argparse parser tree (reached via `sysforge.cli._build_parser`) must appear in both `completions/_sysforge` and `completions/sysforge.bash`; stale top-level verb entries in the zsh case statement (function-suffix matches case-word but parser doesn't know the verb) are an error. Mirrors the `completions-cli-parity` subagent's audit; this is the mechanical layer that runs every release.
- **`versions`** — `pyproject.toml` `[project] version` must equal `PKGBUILD` `pkgver=`, the leading `X.Y.Z` of `PKGBUILD-git` `pkgver=`, and every `<!--version-->vX.Y.Z<!--/version-->` marker in `README.md` and `DESIGN.md` (literal `vX.Y.Z` placeholder strings in prose are filtered out by the `\d+\.\d+\.\d+` constraint).
- **`manpage`** — regenerates `man/sysforge.1` via the scdoc-hybrid pipeline `make man` uses (`tools/gen_options.py` splices the argparse-derived COMMANDS sections into `man/sysforge.1.scd.in`, then `scdoc` renders) into temp files and diffs against the committed page; any difference is an error, with the fix `make man && git add man/sysforge.1`. The `.TH … "DATE"` header is normalised before diffing so the daily date change isn't a finding. Skipped with a `warn` if `scdoc` isn't on PATH. See the Man Pages section.

Findings default to hard fail (non-zero exit); pass `--warn` for a report-only mode. The `--check=<group>` flag scopes the run (repeatable). The script accepts `--repo=<path>` so the tests in `tests/test_check_shipped.py` can point it at synthetic trees in `tmp_path` to verify each drift case still fires.

**Clean chroot validation.** The release gate catches underspecified `depends`/`makedepends` that a host build would silently accept because the dep is already installed. It uses `makechrootpkg` from `devtools` and is a hard prerequisite for the release flow — not the everyday `sysforge build` path, which remains a direct host-side `makepkg` invocation for speed.

One-time setup on the release machine:

```bash
sudo pacman -S --needed devtools
sudo mkarchroot /var/lib/archbuild/extra-x86_64/root base-devel
```

Per-release, the script runs (for each of `PKGBUILD` and `PKGBUILD-git`, in a tmpdir so no build artifacts land in the working tree):

```bash
makechrootpkg -c -u -r "$SYSFORGE_CHROOT"
```

- `-c` snapshots a clean copy of the root chroot for every build, so state from a prior release cannot leak in.
- `-u` updates the root chroot against current `core`/`extra` before the snapshot, so validation runs against what an AUR user will actually hit.
- The build never installs to the host; the release machine stays clean.
- A missing or empty `*.pkg.tar.zst` in the build tmpdir is a hard failure.

Escape hatches:

- `--skip-chroot` — bypass the chroot gate when iterating on `release.sh` itself. Never use for a real publish.
- `SYSFORGE_CHROOT` — override the chroot root (default `/var/lib/archbuild/extra-x86_64`) for CI or VM release runs.
- `--dry-run` — walk through every step without writing files, committing, hitting the network, or running the chroot build. Implies `--skip-chroot`.

`makechrootpkg` bind-mounts require root; the script assumes passwordless sudo is configured for it and fails fast with a clear message otherwise.

---

