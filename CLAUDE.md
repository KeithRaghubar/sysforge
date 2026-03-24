# SysForge — Claude Code Context

Read DESIGN.md before proposing architecture or API changes. It is the source of truth for module layout, public APIs, CLI structure, feature status, and known gaps. Do not duplicate it here.

Repo: <https://github.com/KeithRaghubar/sysforge.git> — Language: Python, Config: TOML, Tests: 851 pytest tests (`make test`)

## Dev Environment

- Ryzen 7 5800X3D, RTX 5070, Arch Linux, COSMIC on Wayland, nvidia-open-dkms
- `pkgbuild_dir = ~/src` (sources); builds at `~/builds`
- `tests/data/etc/sysforge/` is the live `/etc/sysforge/` (symlink) — edit test fixtures there, not just `etc/sysforge/`

## Known Bugs & Gotchas

1. **`_pkgmeta_placeholder`** — wiring was fixed once; history may resurface. Verify if touching metadata paths.
2. **`test_pipeline.py`** — imports from both `config.py` and `profile.py`. Watch for breakage if module boundaries shift.
3. **`match_rules` and `pkgbase`** — rules match against `pkgbase` too for split packages; don't regress this.

## Interaction Preferences

- Be direct. Own mistakes and fix them immediately.
- When a design decision is made, update DESIGN.md immediately in the same turn — don't wait to be reminded.
