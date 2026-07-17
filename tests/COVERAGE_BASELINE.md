# Coverage baseline (ratchet floor)

Re-seeded 2026-07-16 from `make coverage`.

The **soft ratchet floor** for the suite. `make coverage-ratchet` runs the
instrumented suite and compares the current TOTAL against the **TOTAL** row
below, reporting HOLD / IMPROVE / DROP; a DROP surfaces as a `[WARN]` in the
`release-prep` preflight (`RUN_COVERAGE=1`), never a hard gate — the
instrumented suite is slow and a drop is advisory. Re-stamp when cutting a
release with `make coverage-ratchet-update TESTS=<n>` so the floor tracks the
shipped suite; commit the result.

Only the **TOTAL** row is gated. The per-module rows are informational
(refreshed on re-stamp for awareness) — they flag decomposition targets and
historically thin modules, not additional gates.

Regenerate the report with `make coverage` (layers `pytest-cov` via a
`uv run --no-sync` overlay; branch coverage on; writes `coverage.json`).

Suite at baseline: **3829 tests passing**, total **85.7%**.

| Scope | Coverage |
|---|---|
| **TOTAL** | **85.7%** |
| `sysforge/cli.py` | 96.3% |
| `sysforge/update.py` | 84.8% |
| `sysforge/build_core.py` | 91.9% |
| `sysforge/doctor.py` | 88.5% |
| `sysforge/primitives/makepkg_wrapper.py` | 62.6% |
| `sysforge/primitives/aur.py` | 96.4% |
| `sysforge/primitives/profile.py` | 93.8% |
| `sysforge/primitives/resource_guard.py` | 100.0% |
| `sysforge/primitives/auto_repair.py` | 94.3% |
| `sysforge/pipeline/stages/kernel.py` | 86.8% |
| `sysforge/pipeline/stages/toolchain.py` | 83.6% |
| `sysforge/pipeline/stages/packages.py` | 71.1% |

Notes:

- `makepkg_wrapper.py` (61.4%) is the lowest-covered tracked module and the
  furthest below the total; treat a further slip there as worth a closer look
  even when the TOTAL holds. `packages.py` (71.0%) is the next-thinnest.
- `resource_guard.py` and `auto_repair.py` were the 2.2.0-F3 characterization
  targets; they are tracked here so a future regression in those cold,
  build-hot-path primitives is visible.
