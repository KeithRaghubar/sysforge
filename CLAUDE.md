# SysForge — Claude Code Context

Read DESIGN.md before proposing architecture or API changes. It is the source of truth for module layout, public APIs, CLI structure, feature status, and known gaps. Do not duplicate it here.

Repo: <https://github.com/KeithRaghubar/sysforge.git> — Language: Python, Config: TOML, Tests: ~1280 pytest tests (`make test`)

## Dev Environment

- Ryzen 7 5800X3D, RTX 5070, Arch Linux, COSMIC on Wayland, nvidia-open-dkms
- `pkgbuild_src_dir = ~/src` (sources); builds at `~/builds`
- `etc/sysforge/` = shipped defaults (installed by PKGBUILD); `tests/data/etc/sysforge/` = Keith's live personal config for dev/testing. Separate dirs, not symlinked — update both explicitly when a change affects both.

## Known Bugs & Gotchas

1. **`test_pipeline.py`** — imports from both `config.py` and `profile.py`. Watch for breakage if module boundaries shift.
2. **`match_rules` and `pkgbase`** — rules match against `pkgbase` too for split packages; don't regress this.
3. **Source sync goes through the scheduler, not `git pull --rebase`** — any new `fetch`/`update`/`build` code path that needs a fresh PKGBUILD must call `sysforge.primitives.source_sync.get_scheduler().request(SyncRequest(...))`. The scheduler handles RPC short-circuit, rate limiting, and dedup. See DESIGN.md §`source_sync.py` for status semantics (`STATUS_DIVERGED` is a warning; `STATUS_FAILED` / `STATUS_RATE_LIMITED` / `STATUS_PURGE_REFUSED` are blockers).

## Experimental (post-1.0)

`run toolchain` (stage 6), `run kernel` (stage 8), and the PGO-toolchain profdata-reuse path in `sysforge update` (`build_mode = "pgo_llvm_toolchain"`) are shipped but reclassified as experimental for 1.0 — they emit a runtime `[WARN]` at entry and default to disabled. Keep the implementation intact but do not treat them as part of the v1.0 stable surface. See DESIGN.md §Release Plan for full scope.

## Interaction Preferences

- Be direct. Own mistakes and fix them immediately.
- When a design decision is made, update DESIGN.md immediately in the same turn — don't wait to be reminded.
