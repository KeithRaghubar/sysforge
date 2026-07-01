---
name: release-notes
enabled: true
event: bash
pattern: (make\s+release-(major|minor|patch)|tools/release\.sh)
action: warn
---

**Release notes required before releasing.**

`tools/release.sh` preflight hard-fails if the running accumulator
`docs/release-notes/unreleased.md` is missing or has no authored `## ` sections.
Notes are authored per-task into the accumulator as items ship; the release script
renames it to `v<NEW>.md` at Phase 1. Before running the release command, run the
`/release-notes` skill to reconcile/lint the accumulated entries.
