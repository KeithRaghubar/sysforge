# Coverage baseline (ratchet floor)

Established 2026-06-02 on branch `refactor/cohesive-modules`, **before** the
Phase 0 behavior-first test rewrite. No sub-step of the cohesive-modules
refactor may lower total or per-target-module coverage below these values.

Regenerate with `make coverage` (layers `pytest-cov` via a `uv run --no-sync`
overlay; branch coverage on; writes `coverage.json`).

Suite at baseline: **2294 tests passing**, total **80.3%**.

| Scope | Coverage |
|---|---|
| **TOTAL** | **80.3%** |
| `sysforge/cli.py` | 25.9% |
| `sysforge/update.py` | 76.2% |
| `sysforge/build_core.py` | 87.6% |
| `sysforge/doctor.py` | 88.1% |
| `sysforge/primitives/makepkg_wrapper.py` | 71.9% |
| `sysforge/primitives/aur.py` | 90.8% |
| `sysforge/primitives/profile.py` | 92.2% |
| `sysforge/pipeline/stages/kernel.py` | 90.6% |
| `sysforge/pipeline/stages/toolchain.py` | 83.5% |
| `sysforge/pipeline/stages/packages.py` | 67.4% |

Notes:

- `cli.py` (25.9%) is the lowest-covered decomposition target — its inline
  `Verb` classes are exercised only indirectly. The Phase 0 verb behavior
  harness (P0.3) is expected to raise it well before `cli.py` is thinned in
  Phase 2c.
- `makepkg_wrapper.py` (71.9%) and `update.py` (76.2%) sit below the total;
  the behavior-first rewrite must raise them, not just preserve them.
- Lowest non-target modules at baseline (for awareness, not gated):
  `pager.py` 24.4%, `resource_guard.py` 26.3%, `paths.py` 48.9%,
  `reconfigure.py` 57.3%.
