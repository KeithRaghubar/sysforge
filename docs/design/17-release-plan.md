## Release Process

- **GitHub:** public from day one; source of truth for all code.
- **Per-release change history** lives in `docs/release-notes/vX.Y.Z.md` (see *Release notes* below). This section documents the *process* that cuts a release, not the history of past ones.

### Pre-release checklist

**The operational runbook is `docs/RELEASE-CHECKLIST.md`** — the paste-able, stage-by-stage command
sequence with tick boxes, kept standalone so it is readable at release time without wading through
design prose. It is the single home for the checklist; do not duplicate its steps here. This
section documents only *why* the gates are split the way they are.

Two gate runners exist and **neither is a superset of the other**. `make pre-release` holds the
slow, version-*independent* checks (lint, typecheck, full suite, and the five shared `check-*`
gates) so they can run on any branch at any time. `tools/release.sh` preflight holds the
version-*dependent* ones (`check-bump`, `check-standards-at`) — they need the target version, which
does not exist until a bump level is chosen — plus the repo and signing preconditions (on `main`,
clean tree, chroot present, GPG key usable). The five gates both runners share are re-run by the
script deliberately: it cannot assume `pre-release` was run recently, and a stale `DESIGN.md`
discovered *after* the tag is created costs a `make release-resume` cycle.

Three things sit in **neither** runner and are therefore the ones most easily skipped: coverage
(`make coverage-ratchet`), the CVE audit (`make audit`), and the runtime tiers — the container tier
(`make container-smoke`, `container-smoke-cachyos`) and the VM tier (`make vm-test`, plus the `git`
flavor and the stable↔git `conflicts=()` swap). Both runtime tiers install a genuinely built
package and so depend on `make vm-pkg-stable` / `vm-pkg-all` first. They are deliberately outside
the gates — they need a booted VM or a working podman and network, neither of which can be a hard
prerequisite of a lint run — but a minor or major release that skips them is untested against a
real install. See `tools/vm/README.md` and `tools/container/README.md`.

### AUR publishing process

Releases are driven by three Makefile targets — `make release-major`, `make release-minor`, `make release-patch` — each of which calls `tools/release.sh --bump=<level>`. The script handles the full flow end-to-end with a single up-front summary + approval prompt and one mid-run pause for the manual tag push. Phases:

1. **Bump, commit, tag.** Rewrites `pyproject.toml` (the single source of truth for version), `PKGBUILD` `pkgver=`, `PKGBUILD-git` `pkgver=` (leading `X.Y.Z` only — the `.r0.g0000000` suffix is preserved as the placeholder for the dynamic `pkgver()`), the `<!--version-->vX.Y.Z<!--/version-->` markers in `README.md`, `DESIGN.md`, **and the generated marker's source `docs/design/00-header.md`** (DESIGN.md is generated — rewriting only the output would be reverted by the next `make design`), regenerates `uv.lock` (via `uv lock`) and `man/sysforge.1` (via `make man`), renames the release-notes accumulator `docs/release-notes/unreleased.md` to `vX.Y.Z.md` (title stamped with version + date) and reseeds a fresh accumulator, then makes a single **GPG-signed** `release: vX.Y.Z` commit (which includes both the renamed `docs/release-notes/vX.Y.Z.md` and the reseeded `unreleased.md` — see *Release notes* below) and creates a **signed annotated tag** (`git tag -s`, immediately verified with `git tag -v`).
2. **Push pause.** Prints `git push origin main && git push origin vX.Y.Z` and waits on ENTER. The user pushes manually (releases are deliberate, not background events). The script verifies the tag is on `origin` before continuing.
3. **Post-tag artifacts + signed release.** Downloads the GitHub tarball, records its sha256 **and GPG-signs it** (detached armored `.asc`), updates `sha256sums=` in `PKGBUILD` to the two-element form (tarball hash + `SKIP` for the signature source), **publishes the GitHub release** (`gh release create`, or `gh release upload --clobber` on resume) with the `.asc` + `SHA256SUMS` + `SHA256SUMS.asc` assets, then **validates both `PKGBUILD` and `PKGBUILD-git` in a clean chroot** (the release must exist first — the stable PKGBUILD's signature source points at the release download URL, so `makepkg` fetches and verifies the `.asc` during validation), regenerates `.SRCINFO` and `.SRCINFO-git` (both gitignored — local artifacts only), and makes a second signed `release: vX.Y.Z sha256` commit (the `.SRCINFO` files do not get committed).
4. **Final instructions.** Confirms the signed GitHub release is published, then prints `git push origin main` and the `git clone`/`cp`/`commit`/`push` sequence for the `sysforge` and `sysforge-git` AUR repos. The user runs those manually.

**Release signing & downstream verification.** From this release onward every release is GPG-signed end to end with the maintainer key: the commit, the annotated tag, and the source tarball. `tools/release.sh` preflight hard-fails before touching any file if signing is not usable — `git user.signingkey` unset, `commit.gpgsign` not `true`, the secret key not in the keyring, or `gh` missing — so an *unsigned* or unpublishable release can never be produced. The stable `PKGBUILD` declares the maintainer fingerprint in `validpgpkeys` and adds the detached signature as a second `source` entry (`…releases/download/vX.Y.Z/sysforge-X.Y.Z.tar.gz.asc`, paired with `SKIP` in `sha256sums`), so `makepkg` verifies the maintainer signature at install time — closing the gap where the only integrity link was a hash the release script computed itself. The repo ships a placeholder fingerprint sentinel (`REPLACE_WITH_MAINTAINER_KEY_FINGERPRINT`); `check_shipped` tolerates it so dev gates pass before a key exists, but the release preflight refuses to publish while it is present. The `-git` (VCS) package tracks a git clone rather than a release tarball, so it carries no `validpgpkeys`/`.asc` (its provenance is the signed tag); `check_shipped pkgbuild_parity` allows `validpgpkeys` to be stable-only, and the `pkgbuild` group permits `SKIP` only when paired with a `.asc`/`.sig` source. Users verify releases per the *Verifying releases* section of `README.md`.

**Release notes.** Notes are authored **incrementally**, not reconstructed at release time. Each landing commit that completes a ROADMAP item appends its entry — under the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) category headings (`Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`) — to the running accumulator `docs/release-notes/unreleased.md` in that same commit. An entry carries ROADMAP.md's own entry shape: it leads with its roadmap ID (``- **`<ID>` — <title sentence>.** <body>``) and is separated from its neighbour by a `---` rule, so one item reads on its own rather than running into the next, and an item looks the same on the backlog as it does in the notes. `make check-standards` lints the accumulator under the same vocabulary — plus the ID-first lead and the ascending-ID ordering — so entries are validated as they land. The pre-flight in `tools/release.sh` **hard-fails** when the accumulator is missing or holds no authored `## ` sections, mirroring the check-shipped/check-personal/check-design gates. Phase 1 **renames** the accumulator to `docs/release-notes/vX.Y.Z.md`, stamps its `# ` title with the version + ISO date, reseeds a fresh accumulator, and commits both as part of the `release: vX.Y.Z` commit; Phase 4 prints the `gh release create` command that publishes the versioned file. Before releasing, the `/release-notes` repo skill reconciles/lints the accumulated entries against this section's framing (a hookify rule reminds before any `make release-*` invocation); it no longer authors the file from scratch.

If interrupted between phases (Ctrl-C at the push pause, or a transient failure), re-running the same `make release-*` command resumes correctly: the script detects that the tag for the *current* `pyproject.toml` version already exists at HEAD and skips Phase 1. That auto-detection cannot fire once a post-tag failure (e.g. chroot validation) needs fix commits *on top of* the release commit — the tag is no longer at HEAD, and re-running a `--bump` invocation would compute a fresh bump from the already-bumped version. For that case `make release-resume` (`tools/release.sh --resume`) explicitly finishes the release for the current version: it requires the `vX.Y.Z` tag to exist and be an *ancestor* of a clean HEAD (`git merge-base --is-ancestor`), performs no bump, and re-enters at Phase 3. The ancestor check is deliberately gated behind the explicit flag: after any *completed* release its tag is also an ancestor of HEAD, so ancestor-based auto-detection would misclassify every subsequent fresh release as a resume. To keep a transient post-tag failure from reading as an unrecoverable one, the script arms an `ERR` trap the moment the release becomes *in-flight* (a `--resume`/auto-detected-resume run, or the instant Phase 1 creates the signed tag): any later non-zero exit prints an advisory that the bump/commit/tag are already in place (and possibly pushed/published) and that `make release-resume` finishes it idempotently — so `make`'s bare `Error 255` is reframed rather than mistaken for a from-scratch failure. Failures *before* the tag exists (bad args, pre-flight, version bump) keep the trap silent and exit plainly, since there is nothing tagged to resume.

The version markers in `README.md` and `DESIGN.md` wrap the single live version token (`<!--version-->vX.Y.Z<!--/version-->`); only it rotates per release. Each document must carry exactly one such marker. The `versions` check group below enforces lockstep across all marker locations; release pre-flight refuses to run if the markers (or the `pkgver` lines, or any other version-bearing field) are out of sync.

**Shipped-file pre-release checks.** Phase 1 of `tools/release.sh` (and the standalone `make check-shipped` / `make pre-release` targets) gate on `tools/check_shipped.py`, which validates every artifact the PKGBUILD ships:

- **`configs`** — every `etc/sysforge/*.toml` is parsed through its real runtime loader (`load_config`, `load_sysforge_toml`, `load_bootstrap`, the stage `_load_*` helpers); unknown top-level sections/keys against a per-file allowlist (`_KNOWN_SECTIONS` / `_KNOWN_TOP_KEYS`) are an error; missing `tests/data/etc/sysforge/` counterpart for every shipped TOML (except per-host `bootstrap.toml`) is an error. **Fixture↔shipped key-inventory lockstep** (`_check_fixture_lockstep`): for the flat stage/global configs (`_LOCKSTEP_FILES` = `kernel.toml`, `toolchain.toml`, `sysforge.toml`), the *set* of documented keys (active assignments + commented `# key =` examples + section headers) must match between the shipped file and its fixture — values may differ, the key set may not. This is the only cross-check tying the tracked fixtures to shipped reality now that the personal live config is fully decoupled; the rich-body configs (`packages.toml`, `profiles.toml`) are excluded because their fixtures legitimately carry test-specific `[[package]]` / profile bodies. The complementary **allowlist↔stage-code parity** guard lives in `tests/test_check_shipped.py` (`TestAllowlistCodeParity`): the allowlist must equal the keys each stage actually reads via `kernel_cfg.get(...)` / `tcfg.get(...)` (helper-resolved keys like `pgo_store` accounted for), and every read key must be documented in the shipped file — this is what catches a new config key that the code reads but nobody allowlisted or documented (the `base_config` class of regression).
- **`pkgbuild`** — every `install -Dm…` source in `PKGBUILD` must exist in the working tree; every `$pkgdir/etc/…` install target must be declared in `backup=()`, and vice versa (no stale `backup=` entries); each `sha256sums` entry is paired with its `source` and may only be `SKIP` when that source is a detached signature (`.asc`/`.sig`) — an all-zero/`DRYRUN…` value, or a `SKIP` on a hashable source, is a placeholder error; `validpgpkeys` must be declared and each entry must be a 40-hex fingerprint (or the `REPLACE_WITH_MAINTAINER_KEY_FINGERPRINT` dev sentinel).
- **`pkgbuild_parity`** — `PKGBUILD` and `PKGBUILD-git` parse to the same dict (via `pkgbuild_meta.parse_pkgbuild`) except for a tightly-scoped allowlist of keys that are *supposed* to differ (`pkgname`, `pkgver`, `pkgrel`, `pkgdesc`, `source`, `sha256sums`, `conflicts`, `provides`, `validpgpkeys` — stable-only, since the VCS package has no release `.asc`). `depends` / `makedepends` / `optdepends` / `backup` arrays must be byte-identical.
- **`hooks`** — every `etc/pacman.d/hooks/sysforge-*.hook` `Exec` line must invoke `tools/pacman-hook-helper.sh` and pass a subcommand the helper documents (`kernel`, `toolchain`, `buildstate`).
- **`completions`** — every verb and every long-flag in the argparse parser tree (reached via `sysforge.cli._build_parser`) must appear in both `completions/_sysforge` and `completions/sysforge.bash`; stale top-level verb entries in the zsh case statement (function-suffix matches case-word but parser doesn't know the verb) are an error. Mirrors the `completions-cli-parity` subagent's audit; this is the mechanical layer that runs every release.
- **`versions`** — `pyproject.toml` `[project] version` must equal `PKGBUILD` `pkgver=`, the leading `X.Y.Z` of `PKGBUILD-git` `pkgver=`, and every `<!--version-->vX.Y.Z<!--/version-->` marker in `README.md` and `DESIGN.md` (literal `vX.Y.Z` placeholder strings in prose are filtered out by the `\d+\.\d+\.\d+` constraint).
- **`manpage`** — regenerates `man/sysforge.1` via the scdoc-hybrid pipeline `make man` uses (`tools/gen_options.py` splices the argparse-derived COMMANDS sections into `man/sysforge.1.scd.in`, then `scdoc` renders) into temp files and diffs against the committed page; any difference is an error, with the fix `make man && git add man/sysforge.1`. Both sides are passed through `_normalize_roff` before diffing, so scdoc-version-specific artifacts aren't findings: the `.TH … "DATE"` header (daily date change), the `.\" Generated by scdoc <version>` banner, and hyphen escaping (`\-` vs `-`, functionally inert in roff). This keeps the gate coupled to the CLI surface rather than to the local scdoc build. Skipped with a `warn` if `scdoc` isn't on PATH. See the Man Pages section.

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

