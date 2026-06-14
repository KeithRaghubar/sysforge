---
name: use-uv-not-pip
enabled: true
event: bash
pattern: ^\s*(?:sudo\s+|[A-Z_][A-Z0-9_]*=\S*\s+)*pip\s+(install|uninstall|freeze|sync|list)\b
action: block
---

**`pip` package operations blocked.**

This workstation uses `uv` for Python package management.

- Install: `uv pip install <pkg>` or `uv add <pkg>`
- Uninstall: `uv pip uninstall <pkg>` or `uv remove <pkg>`
- Freeze: `uv pip freeze`
- Sync: `uv sync`
- List: `uv pip list`

The pattern is anchored to start-of-command and only matches `pip` as the leading binary (optionally prefixed by `sudo` or env-var assignments like `PIP_INDEX_URL=...`). It will not trip on `pip install` appearing inside quoted strings, heredocs, grep patterns, or chained after `&&` / `;` — that's intentional, to avoid false positives. Bare `pip` (e.g. `pip --version`) is not blocked.
