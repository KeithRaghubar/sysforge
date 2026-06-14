---
name: release-notes
enabled: true
event: bash
pattern: (make\s+release-(major|minor|patch)|tools/release\.sh)
action: warn
---

**Release notes required before releasing.**

`tools/release.sh` preflight hard-fails if `docs/release-notes/v<NEW>.md` is missing
for the target version. Before running the release command, check the file exists for
the bumped version; if not, run the `/release-notes` skill to draft it first.
