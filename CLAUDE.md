# SysForge — Claude Code Context

Read DESIGN.md before proposing architecture or API changes. It is the source of truth for module layout, public APIs, CLI structure, feature status, and known gaps. Do not duplicate it here.

Repo: <https://github.com/KeithRaghubar/sysforge.git> — Language: Python, Config: TOML, Tests: 1281 pytest tests (`make test`)

## Dev Environment

- Ryzen 7 5800X3D, RTX 5070, Arch Linux, COSMIC on Wayland, nvidia-open-dkms
- `pkgbuild_src_dir = ~/src` (sources); builds at `~/builds`
- `etc/sysforge/` = shipped defaults (installed by PKGBUILD); `tests/data/etc/sysforge/` = Keith's live personal config for dev/testing. Separate dirs, not symlinked — update both explicitly when a change affects both.

## Known Bugs & Gotchas

1. **`test_pipeline.py`** — imports from both `config.py` and `profile.py`. Watch for breakage if module boundaries shift.
2. **`match_rules` and `pkgbase`** — rules match against `pkgbase` too for split packages; don't regress this.

## Interaction Preferences

- Be direct. Own mistakes and fix them immediately.
- When a design decision is made, update DESIGN.md immediately in the same turn — don't wait to be reminded.
