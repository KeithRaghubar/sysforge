# Coverage baseline (ratchet floor)

Re-seeded 2026-08-07 from `make coverage`.

The **soft ratchet floor** for the suite. `make coverage-ratchet` runs the
instrumented suite and compares the current TOTAL against the **TOTAL** row
below, reporting HOLD / IMPROVE / DROP; a DROP surfaces as a `[WARN]` in the
release preflight (`make preflight`, `RUN_COVERAGE=1`), never a hard gate — the
instrumented suite is slow and a drop is advisory. Re-stamp when cutting a
release with `make coverage-ratchet-update TESTS=<n>` so the floor tracks the
shipped suite; commit the result.

Only the **TOTAL** row is gated. The per-module rows are informational
(refreshed on re-stamp for awareness) — they flag decomposition targets and
historically thin modules, not additional gates.

Regenerate the report with `make coverage` (layers `pytest-cov` via a
`uv run --no-sync` overlay; branch coverage on; writes `coverage.json`).

Suite at baseline: **4569 tests passing**, total **86.8%**.

| Scope | Coverage |
|---|---|
| **TOTAL** | **86.8%** |
| `sysforge/cli.py` | 96.2% |
| `sysforge/update.py` | 86.6% |
| `sysforge/build_core.py` | 92.0% |
| `sysforge/doctor.py` | 88.5% |
| `sysforge/primitives/makepkg_wrapper.py` | 75.1% |
| `sysforge/primitives/aur.py` | 96.6% |
| `sysforge/primitives/profile.py` | 93.9% |
| `sysforge/primitives/resource_guard.py` | 100.0% |
| `sysforge/primitives/auto_repair.py` | 94.3% |
| `sysforge/pipeline/stages/kernel.py` | 88.1% |
| `sysforge/pipeline/stages/toolchain.py` | 85.1% |
| `sysforge/pipeline/stages/packages.py` | 71.2% |

Notes:

- `packages.py` is the lowest-covered tracked module and the furthest below the
  total, with `makepkg_wrapper.py` next; treat a further slip in either as worth
  a closer look even when the TOTAL holds. These notes carry no figures on
  purpose — `coverage-ratchet-update` refreshes the table above but not this
  prose, so a number written here goes stale at the next re-stamp (it had drifted
  to `61.4%` against a table reading `74.9%` before the 2026-08-07 stamp).
- `resource_guard.py` and `auto_repair.py` were the 2.2.0-F3 characterization
  targets; they are tracked here so a future regression in those cold,
  build-hot-path primitives is visible.
