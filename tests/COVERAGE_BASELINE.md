# Coverage baseline (ratchet floor)

Re-seeded 2026-10-20 from `make coverage`.

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

Suite at baseline: **7084 tests passing**, total **91.2%**.

| Scope | Coverage |
|---|---|
| **TOTAL** | **91.2%** |
| `sysforge/cli.py` | 92.6% |
| `sysforge/update.py` | 88.1% |
| `sysforge/build_core.py` | 91.3% |
| `sysforge/doctor.py` | 89.5% |
| `sysforge/build/makepkg_wrapper.py` | 84.0% |
| `sysforge/primitives/aur.py` | 96.8% |
| `sysforge/primitives/profile.py` | 95.1% |
| `sysforge/primitives/resource_guard.py` | 100.0% |
| `sysforge/primitives/auto_repair.py` | 94.8% |
| `sysforge/pipeline/stages/kernel/stage.py` | 96.4% |
| `sysforge/pipeline/stages/toolchain/stage.py` | 88.5% |
| `sysforge/pipeline/stages/packages.py` | 92.5% |
| `sysforge/primitives/snapshot.py` | 99.2% |
| `sysforge/packages_cmd.py` | 93.3% |
| `sysforge/pipeline/stages/reconfigure.py` | 81.9% |
| `sysforge/verbs/artifact.py` | 95.2% |
| `sysforge/primitives/dep_analysis.py` | 81.9% |
| `sysforge/pipeline/stages/configure.py` | 90.2% |
| `sysforge/primitives/makepkg_pgo.py` | 99.4% |
| `sysforge/pipeline/stages/toolchain/pkgbuilds.py` | 88.1% |

Notes:

- `reconfigure.py` and `dep_analysis.py` are the lowest-covered tracked modules
  and the furthest below the total, with `makepkg_wrapper.py` next; treat a
  further slip in any of them as worth a closer look even when the TOTAL holds. These notes carry no figures on
  purpose — `coverage-ratchet-update` refreshes the table above but not this
  prose, so a number written here goes stale at the next re-stamp (it had drifted
  to `61.4%` against a table reading `74.9%` before the 2026-08-07 stamp).
- The rows from `snapshot.py` down are the 3.5.0-DEV2 targets: the modules the
  2026-10-18 review found bugs in were the least covered, so they are tracked to
  keep their failure and resume paths from thinning out again.
- `resource_guard.py` and `auto_repair.py` were the 2.2.0-F3 characterization
  targets; they are tracked here so a future regression in those cold,
  build-hot-path primitives is visible.
