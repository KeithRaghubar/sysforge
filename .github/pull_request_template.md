<!-- What changes, and why. Link the ROADMAP ID or issue. -->

## Checklist

CI checks most of this; the boxes are for what it cannot see.

- [ ] Release-note entry in `docs/release-notes/unreleased.md`, led by its roadmap ID
- [ ] If a user-facing surface changed: `make surface` committed, and its diff matches what I meant to change
- [ ] If a name was removed or renamed: the old one is in `tools/retired_names.toml` (config keys: `RETIRED_KEYS`)
- [ ] The guide for this feature (`docs/guides/`) still describes it correctly
- [ ] `make pre-release` (or `make ci-local`) passes locally
