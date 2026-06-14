---
name: no-git-commits
enabled: true
event: bash
pattern: ^\s*git\s+(commit|push|merge|rebase|reset\s+--hard)\b
action: warn
---

**Heads up: user normally handles git operations themselves.**

The default rule is: don't commit, push, merge, rebase, or hard-reset — leave changes staged or unstaged for the user to review.

**Exception:** the release helpers (`make release-{major,minor,patch}`) legitimately drive `git` operations as part of their flow. If this command is part of a release helper invocation, proceed.

Otherwise, stop and confirm with the user before continuing.
