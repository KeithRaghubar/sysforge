## Architecture Overview

Three layers:

```
┌─────────────────────────────────────────┐
│  Config                                 │
│  TOML profiles + hardware overlays      │
├─────────────────────────────────────────┤
│  Pipeline                               │
│  Python DAG orchestrator                │
│  checkpoint/resume across stages        │
├─────────────────────────────────────────┤
│  Primitives                             │
│  PKGBUILD parser, makepkg wrapper,      │
│  dep analysis, flag extraction          │
└─────────────────────────────────────────┘
```

**Import direction:** `cli.py` → `verbs/runner.py` → command modules (`update.py`, `packages_cmd.py`, `resolve.py`, …) → `primitives/*`. Each command module defines a `*Verb(Verb)` subclass alongside its existing helpers; the runner dispatches uniformly across them. No command module imports from another command module. See [CLI Verb Framework](#cli-verb-framework).

---

