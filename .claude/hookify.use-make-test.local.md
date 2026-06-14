---
name: use-make-test
enabled: true
event: bash
pattern: ^\s*pytest(\s|$)
action: block
---

**Direct `pytest` invocation blocked.**

This project uses the Makefile as the canonical test entry point. Use one of:

- `make test` — run the full suite
- `make test-v` — verbose output
- `make test-x` — stop on first failure

See sysforge/CLAUDE.md.
