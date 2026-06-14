## Roadmap

Forward-looking enhancements that build on existing infrastructure. Each is a candidate, not a commitment, and none is required for current functionality. Shipped work is recorded in `docs/release-notes/`, not here.

- **Rule priority auto-calculation** — auto-calculate a baseline specificity score from rule conditions (mirrors CSS specificity: more AND'd conditions = higher weight), with manual `priority` override for ties. Deferred until enough real rules exist to validate whether auto-priority causes ordering problems in practice.
- **Configure stage additions** — btrfs snapshot before build runs, ccache/sccache initialisation check, estimated build time heuristic.
- **Graphics runtime debugging refinement** — tighten the graphics/doctor diagnostics surface (exact scope TBD). A candidate when revisiting graphics-related code; not blocking.
- **System maintenance scope expansion** — grow sysforge beyond build/package management into a unified system-maintenance helper: track and manage user-owned system artifacts that currently live ad-hoc across `~/scripts`, `/etc/systemd/system/`, `/etc/pacman.d/hooks/`, etc. Candidate primitives: inventory of tracked files, source-of-truth dir under repo control, install/sync command, drift detection vs filesystem, integration with the existing config/profile/manifest layers.
