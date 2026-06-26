# docs/design/ — source layout for DESIGN.md

`DESIGN.md` at the repo root is **generated**. Do not edit it directly. Edit the
focused source files in this directory and run `make design`; `make check-design`
(run in the release preflight) fails if the committed `DESIGN.md` has drifted
from these sources.

- **Order** is defined by [`_manifest`](_manifest) — the files are concatenated
  in the order listed there, under a generated banner.
- **This file (`index.md`) is not part of `DESIGN.md`** — it is a navigation aid
  for contributors editing the sources.

## Current sources

| File | Section |
|------|---------|
| `00-header.md` | Title + Table of Contents |
| `01-philosophy.md` | Philosophy |
| `02-distribution-model.md` | Distribution Model |
| `03-architecture-overview.md` | Architecture Overview |
| `04-directory-structure.md` | Directory Structure |
| `05-package-manifest.md` | Package Manifest |
| `06-config-layer.md` | Config Layer |
| `07-pipeline-layer.md` | Pipeline Layer |
| `08-cli-verb-framework.md` | CLI Verb Framework |
| `09-primitives-layer.md` | Primitives Layer |
| `10-flag-profile-system.md` | Flag Profile System |
| `11-makepkg-wrapper.md` | Makepkg Wrapper |
| `12-logging.md` | Logging |
| `13-man-pages.md` | Man Pages |
| `14-hardware-detection.md` | Hardware Detection |
| `15-cache-management.md` | Cache Management |
| `16-graphics-stack.md` | Graphics Stack Build Order |
| `17-release-plan.md` | Release Process |
| `18-reconverge.md` | Drift detection |
| `19-known-gaps.md` | Known Gaps |
| `21-standards.md` | Standards & Specifications |

## Planned reorganization

These files currently mirror the original layer-organized `DESIGN.md` one-to-one
(the lossless first step of the docs migration). The next steps split them into
a verb- and module-oriented layout — `verbs/<verb>.md`, `modules/<module>.md`,
and `cross-cutting/<concern>.md` — so verb behavior stops being filed under the
"Primitives Layer" section and duplicated concerns (sentinel, ABI) are stated
once with cross-links. `_manifest` is updated in lockstep as files move.
