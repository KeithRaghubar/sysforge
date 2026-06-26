## Philosophy

SysForge was motivated by source-based distros' compile-time control and performance tuning, without their fragility and maintenance overhead. The core insight is that source-based systems conflate several concerns that are better separated:

- **Hardware profiling** — what the machine has
- **Compiler flags** — how to build for it
- **Feature selection** — what to enable

SysForge separates these into distinct config layers and produces a standard mutable Arch system as output. It is not a distro. There is no ISO, no divergence from upstream Arch, no custom package ecosystem.

This document describes only **implemented** design. Planned features, candidate enhancements, and the rationale for purposely-excluded or abandoned ideas live in `/ROADMAP.md`. Roadmap items carry version-prefixed IDs (`<version>-<TYPE><n>`, reset each release) that appear only in the roadmap and release notes — never here.

---

