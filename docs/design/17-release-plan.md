## Release Process

- **GitHub:** public from day one; source of truth for all code.
- **Per-release change history** lives in `docs/release-notes/vX.Y.Z.md` (see *Release notes* below). This section documents the *process* that cuts a release, not the history of past ones.

### AUR publishing process

Releases are driven by three Makefile targets — `make release-major`, `make release-minor`, `make release-patch` — each of which calls `tools/release.sh --bump=<level>`. The script handles the full flow end-to-end with a single up-front summary + approval prompt and one mid-run pause for the manual tag push. Phases:

1. **Bump, commit, tag.** Rewrites `pyproject.toml` (the single source of truth for version), `PKGBUILD` `pkgver=`, `PKGBUILD-git` `pkgver=` (leading `X.Y.Z` only — the `.r0.g0000000` suffix is preserved as the placeholder for the dynamic `pkgver()`), the `<!--version-->vX.Y.Z<!--/version-->` markers in `README.md` and `DESIGN.md`, regenerates `uv.lock` (via `uv lock`) and `man/sysforge.1` (via `make man`), then makes a single `release: vX.Y.Z` commit (which also includes `docs/release-notes/vX.Y.Z.md` — see *Release notes* below) and tags it.
2. **Push pause.** Prints `git push origin main && git push origin vX.Y.Z` and waits on ENTER. The user pushes manually (releases are deliberate, not background events). The script verifies the tag is on `origin` before continuing.
3. **Post-tag artifacts.** Fetches the GitHub tarball sha256, updates `sha256sums=` in `PKGBUILD`, **validates both `PKGBUILD` and `PKGBUILD-git` in a clean chroot**, regenerates `.SRCINFO` and `.SRCINFO-git` (both gitignored — local artifacts only), and makes a second `release: vX.Y.Z sha256` commit (the `.SRCINFO` files do not get committed).
4. **Final instructions.** Prints `git push origin main`, the `git clone`/`cp`/`commit`/`push` sequence for the `sysforge` and `sysforge-git` AUR repos, and the `gh release create vX.Y.Z --notes-file docs/release-notes/vX.Y.Z.md` command for the GitHub release. The user runs those manually.

**Release notes.** Every release ships curated notes at `docs/release-notes/vX.Y.Z.md` (sections: Highlights / Breaking changes / Fixes / Internal — see `docs/release-notes/v2.0.0.md` for the format). The pre-flight in `tools/release.sh` **hard-fails** when the notes file for the target version is missing, mirroring the check-shipped/check-personal/check-design gates; Phase 1 commits the file as part of the `release: vX.Y.Z` commit, and Phase 4 prints the `gh release create` command that publishes it. Notes are drafted from `git log <last-tag>..HEAD` plus this section's framing — inside a Claude Code session the `/release-notes` repo skill does this (a hookify rule reminds before any `make release-*` invocation); outside one, write the file by hand.

If interrupted between phases (Ctrl-C at the push pause, or a transient failure), re-running the same `make release-*` command resumes correctly: the script detects that the tag for the *current* `pyproject.toml` version already exists at HEAD and skips Phase 1.

The version markers in `README.md` and `DESIGN.md` wrap the single live version token (`<!--version-->vX.Y.Z<!--/version-->`); only it rotates per release. Each document must carry exactly one such marker. The `versions` check group below enforces lockstep across all marker locations; release pre-flight refuses to run if the markers (or the `pkgver` lines, or any other version-bearing field) are out of sync.

**Shipped-file pre-release checks.** Phase 1 of `tools/release.sh` (and the standalone `make check-shipped` / `make pre-release` targets) gate on `tools/check_shipped.py`, which validates every artifact the PKGBUILD ships:

- **`configs`** — every `etc/sysforge/*.toml` is parsed through its real runtime loader (`load_config`, `load_sysforge_toml`, `load_bootstrap`, the stage `_load_*` helpers); unknown top-level sections/keys against a per-file allowlist (`_KNOWN_SECTIONS` / `_KNOWN_TOP_KEYS`) are an error; missing `tests/data/etc/sysforge/` counterpart for every shipped TOML (except per-host `bootstrap.toml`) is an error. **Fixture↔shipped key-inventory lockstep** (`_check_fixture_lockstep`): for the flat stage/global configs (`_LOCKSTEP_FILES` = `kernel.toml`, `toolchain.toml`, `sysforge.toml`), the *set* of documented keys (active assignments + commented `# key =` examples + section headers) must match between the shipped file and its fixture — values may differ, the key set may not. This is the only cross-check tying the tracked fixtures to shipped reality now that the personal live config is fully decoupled; the rich-body configs (`packages.toml`, `profiles.toml`) are excluded because their fixtures legitimately carry test-specific `[[package]]` / profile bodies. The complementary **allowlist↔stage-code parity** guard lives in `tests/test_check_shipped.py` (`TestAllowlistCodeParity`): the allowlist must equal the keys each stage actually reads via `kernel_cfg.get(...)` / `tcfg.get(...)` (helper-resolved keys like `pgo_store` accounted for), and every read key must be documented in the shipped file — this is what catches a new config key that the code reads but nobody allowlisted or documented (the `base_config` class of regression).
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

