---
name: design-sync
description: Compare the working-tree diff against DESIGN.md and propose section-by-section updates so docs stay in lockstep with code. Invoke at the end of a non-trivial implementation change before touching README.md or CLAUDE.md (DESIGN.md is the source of truth — update it first).
disable-model-invocation: true
---

# design-sync

DESIGN.md is the source of truth for sysforge's module layout, public APIs, CLI structure, feature status, and known gaps. The doc-update order is fixed: **DESIGN.md → README.md → CLAUDE.md** (memory: `feedback_md_order.md`). This skill enforces that order by handling the hardest step — figuring out *which* DESIGN.md sections need to change for a given diff.

## When to invoke

Run `/design-sync` after finishing a non-trivial implementation change (new primitive, new CLI verb, behavior change in an existing primitive, schema change in a state file, status change for a feature). Skip for pure refactors, lint fixes, or test-only edits.

## What this skill does

1. **Inspect the diff.** Pull the working-tree changes since the last commit (or against `main` if the user asks):

   ```bash
   git -C "$CLAUDE_PROJECT_DIR" diff HEAD --stat
   git -C "$CLAUDE_PROJECT_DIR" diff HEAD
   ```

2. **Map files to DESIGN.md sections.** Walk the headings in DESIGN.md (`grep -n '^## ' DESIGN.md`) and decide which sections are touched by the diff. The mapping is roughly:

   | Diff touches | Likely DESIGN.md section |
   |---|---|
   | `sysforge/cli.py`, new verb/flag | `## Pipeline Layer`, `## Re-converge`, possibly a verb-specific subsection |
   | `sysforge/primitives/<X>.py` | `## Primitives Layer` → `<X>.py` subsection |
   | `sysforge/pipeline/stages/<X>.py` | `## Pipeline Layer` → stage subsection |
   | `etc/sysforge/*.toml` schema change | `## Config Layer`, `## Package Manifest`, or `## Flag Profile System` |
   | `sysforge/primitives/source_sync.py` | `## Primitives Layer → source_sync.py` (status semantics) |
   | New state file or `build_state.toml` schema | `## Directory Structure`, plus the relevant primitive section |
   | Release/version bump or PKGBUILD change | `## Release Plan` |
   | Feature reclassification (stable → experimental) | `## Release Plan`, `## V1.x Roadmap`, `## Known Gaps` |
   | Hardware/graphics detection change | `## Hardware Detection`, `## Graphics Stack Build Order` |

3. **Propose edits, section by section.** For each touched section, output:

   - **Section heading** (with line number from `grep -n`).
   - **What needs to change** in that section — new bullet, status update, schema delta, etc.
   - **Suggested wording** that matches DESIGN.md's existing tone (factual, present tense, no marketing language, no emoji).

4. **Flag drift, don't auto-edit.** Present the proposed edits to the user and wait for approval. The user owns DESIGN.md tone and terminology — never write to it without confirmation.

5. **Remind about downstream docs.** After DESIGN.md is updated, surface the next steps:

   - **README.md** — only if user-visible surface changed (CLI, install steps, supported features).
   - **CLAUDE.md** (project-level and the user-level `~/.claude/CLAUDE.md` if relevant) — only if a convention, gotcha, or workflow Claude needs to follow has changed.
   - **In-file comments** — memory `feedback_inline_comments.md` says check these too. Header docstrings in `sysforge/primitives/*.py` often mirror DESIGN.md's primitive subsections.

## Heuristics

- **CLI changes always touch DESIGN.md.** Even small flag additions get listed under the relevant verb.
- **Status changes are load-bearing.** Moving something between "shipped", "experimental", "deferred to v1.x", or "v2 roadmap" must be reflected in `## Release Plan` *and* the per-feature subsection.
- **Schema changes propagate.** A new field in `etc/sysforge/*.toml` shows up in `## Config Layer` (the schema), and possibly in `## Package Manifest` or `## Flag Profile System` (the semantics).
- **`source_sync` is special.** Its status semantics (`STATUS_DIVERGED`, `STATUS_FAILED`, `STATUS_RATE_LIMITED`, `STATUS_PURGE_REFUSED`) are documented in DESIGN.md and referenced in CLAUDE.md gotcha #3 — keep them in sync.

## Output format

```
DESIGN.md sections touched by this diff:

1. ## <Section> (line <N>)
   Change: <what's drifting>
   Proposed edit: <one or two sentences that fit DESIGN.md style>

2. ## <Section> (line <N>)
   ...

Downstream docs to update next:
- README.md: <yes/no, why>
- CLAUDE.md: <yes/no, why>
- In-file comments: <yes/no, which files>
```

If no sections are affected, say so plainly — don't manufacture work.
