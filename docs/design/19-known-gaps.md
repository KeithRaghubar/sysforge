## Known Gaps

Implemented behaviour that is incomplete or has known limitations. These are not deferred features — they are holes in currently active code. Planned and abandoned work is tracked in `/ROADMAP.md` and `docs/ROADMAP-ABANDONED.md`, not here; how the working parts behave is described in their own sections.

**A repo package installed with plain `pacman -S` is not source-tracked.** `sysforge update` walks every installed AUR package, every package sysforge source-built (`build_state.toml`) and every repo package with a behaviour-changing override (§`sysforge update` → Phase 1). A repo package sysforge never built and has no override for is outside that set, so it gets no drift detection and no source rebuild, unless you build it once (`sysforge build <pkg>`), add an override in `packages.toml`, or set `[build] repo_mode = "build_from_source"`, which pulls every installed repo package into the walk.

---
