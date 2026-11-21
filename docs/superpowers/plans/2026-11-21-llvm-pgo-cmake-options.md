# LLVM PGO via cmake options (4.0.0-B12) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.


**Goal:** `run toolchain` Pass 1 builds the unified `llvm` PKGBUILD without instrumenting its nested runtimes sub-build. sysforge delivers PGO for the pgo PKGBUILD through LLVM's own cmake cache options, not CFLAGS.

**Architecture:** A new `pkgbuild_patcher.patch_llvm_pgo` injects `-DLLVM_BUILD_INSTRUMENTED=IR -DLLVM_PROFILE_DATA_DIR=<store>` (generate) or `-DLLVM_PROFDATA_FILE=<profdata>` (use). It anchors at the first cmake configure, like `patch_llvm_dir`. A `BuildOptions.llvm_pgo` field carries the request from the toolchain passes to the wrapper, which patches the PKGBUILD and fails the build if it can't anchor. Pass 1, Pass 4a and the BOLT repackage switch to it. Passes 4b/4c keep `-fprofile-use` in CFLAGS.

**Tech Stack:** Python 3, pytest via `make test`, cmake-arg PKGBUILD patching.

**Spec / root cause:** Pass 1 on llvm 23.1.5 failed in `runtimes-configure`: `Could NOT find Threads`, caused by `ld.lld: cannot open …/build/lib/clang/23/lib/x86_64-pc-linux-gnu/libclang_rt.profile.a`. makepkg exports CFLAGS carrying `-fprofile-generate=/var/cache/sysforge/llvm-pgo/`. The runtimes sub-build (`LLVM_ENABLE_RUNTIMES='compiler-rt;openmp;offload;libcxx;libcxxabi'`) compiles with the just-built clang and picks those up, and that clang has no profile runtime yet. Verified in the 23.1.5 tree: `HandleLLVMOptions.cmake` appends the `LLVM_BUILD_INSTRUMENTED` / `LLVM_PROFDATA_FILE` flags to in-scope variables, and `llvm/runtimes/CMakeLists.txt` does not pass `LLVM_BUILD_INSTRUMENTED` through (`OFF` in `runtimes-bins/CMakeCache.txt`).

## Global Constraints

- Tests run only through `make test ARGS="…"` / `make test-x` / `make test-live`, never `pytest` directly.
- Branch `fix/toolchain-pgo-cmake` (B11 already committed as `6f0f939b`). Each task ends with a commit; messages carry no em-dashes and end with the Co-Authored-By line. Never push or merge unasked.
- Release-note entry: ``- **`4.0.0-B12` — <title>.** <body>``, under `## Fixed`, after `4.0.0-B11`, `---`-separated. Confirm the ID with `make next-id TYPE=B` (expect `4.0.0-B12`).
- Design edits go in `docs/design/*.md`, then `make design`. Never edit `DESIGN.md` by hand.
- Revise every passage the change falsifies (Pass 1/4 "+ `-fprofile-generate`" / "with `-fprofile-use`").
- The fix crossed what the live repos hold, so it ships a `make test-live` test as well as unit tests.
- `primitives/` must not import from `pipeline/`, `ui/` or `build/`.
- No other project names in code, docs or messages.

## Review Focus

1. **Several cmake configures in one PKGBUILD** (`cmake -S llvm`, then `cmake -B build-device-* -S runtimes`, wasi `cmake -S compiler-rt`): the args must land only in the first `-S llvm` statement. Tested in Task 1 with an excerpt shaped like the real PKGBUILD.
2. **Re-run on an already patched `PKGBUILD.sysforge`** (resume, retry after a fix): no duplicate `-D` args, and a changed store or profdata path replaces the old value. Task 1.
3. **A pgo PKGBUILD with no cmake configure** (user-configured pgo list, upstream build-system change): the build fails loudly; it never silently builds uninstrumented. Task 2.
4. **Generate and use requested on one PKGBUILD in turn** (Pass 1 then Pass 4a reuse the same `PKGBUILD.sysforge`, which makepkg_wrapper rewrites from the original each build): the use build carries no leftover `LLVM_BUILD_INSTRUMENTED`. Task 1 covers switching modes on one file.
5. **Reuse cache from a CFLAGS-era run** (`--reuse-built`): a stale artifact must not be reused, so the fingerprint changes. Task 4.

---

### Task 1: `patch_llvm_pgo` patcher

**Files:**
- Modify: `sysforge/primitives/pkgbuild_patcher.py` (beside `patch_llvm_dir`, ~line 1782)
- Test: `tests/test_llvm_targets.py` (new section after the `patch_llvm_dir` tests, ~line 345)

**Interfaces:**
- Produces: `patch_llvm_pgo(patched_path, mode: str, path: str) -> bool` with `mode` in `{"generate", "use"}`. It returns True when the file changed, and raises `ValueError` on another mode.
  - generate → `-DLLVM_BUILD_INSTRUMENTED=IR -DLLVM_PROFILE_DATA_DIR="<path>"`
  - use → `-DLLVM_PROFDATA_FILE="<path>"`
  - It removes the other mode's keys if present (Review Focus 4).

- [ ] **Step 1: Write the failing tests.** Add a module constant `_UNIFIED_LLVM_PKGBUILD`, a trimmed excerpt of the real PKGBUILD. Its `build()` has `local cmake_args=(-G Ninja -DCMAKE_BUILD_TYPE=Release)`, `cmake -S llvm -B build "${cmake_args[@]}"`, a second `cmake -S llvm -B build "${cmake_args[@]}" -DLLVM_DISTRIBUTION_COMPONENTS="$c"`, then `cmake -B "build-device-$_target" -S runtimes -G Ninja \` with one continued `-DCMAKE_BUILD_TYPE=Release` line. Tests:
  - `test_patch_llvm_pgo_generate_lands_in_first_llvm_configure`: returns True. The text between the first `cmake -S llvm` and the next `\n  local` contains `-DLLVM_BUILD_INSTRUMENTED=IR` and `-DLLVM_PROFILE_DATA_DIR="/var/cache/sysforge/llvm-pgo"`; the `-S runtimes` statement contains neither; `text.count("LLVM_BUILD_INSTRUMENTED") == 1`.
  - `test_patch_llvm_pgo_use_injects_profdata_file`: `-DLLVM_PROFDATA_FILE="/var/cache/sysforge/llvm-pgo/clang.profdata"` appears once, with no `LLVM_BUILD_INSTRUMENTED`.
  - `test_patch_llvm_pgo_idempotent`: the second identical call returns False and the text is unchanged.
  - `test_patch_llvm_pgo_replaces_path`: generate with `/a`, then `/b` → contains `"/b"`, not `"/a"`, one occurrence.
  - `test_patch_llvm_pgo_switch_generate_to_use`: generate then use → `LLVM_BUILD_INSTRUMENTED` and `LLVM_PROFILE_DATA_DIR` are gone and `LLVM_PROFDATA_FILE` is present.
  - `test_patch_llvm_pgo_no_cmake_returns_false`: a PKGBUILD with `build() { make; }` → False, unchanged.
  - `test_patch_llvm_pgo_rejects_unknown_mode`: `pytest.raises(ValueError)`.
  - `test_patched_llvm_pgo_is_valid_bash`: after generate, `bash -n` on the file exits 0 (use `subprocess.run(["bash", "-n", str(p)])`).
- [ ] **Step 2: Run, expect FAIL** (ImportError): `make test ARGS="tests/test_llvm_targets.py -q -k llvm_pgo"`
- [ ] **Step 3: Implement `patch_llvm_pgo`.** For each managed key, use a regex of the `_LLVM_DIR_RE` shape: `-D<KEY>=` followed by a quoted or bare value. Strip the off-mode keys together with their ` \\\n<indent>` continuation prefix, so no empty continuation line is left behind. Replace a present key whose value differs. Insert missing ones at `_cmake_statement_end(text, _find_cmake_configure_anchor(text).start())` in the ` \\\n{indent}    {arg}` form `patch_llvm_dir` uses, one arg per continuation line. Log via `_log.info` like its siblings, and warn and return False when there's no anchor.
- [ ] **Step 4: Run, expect PASS**: same command; then `make test ARGS="tests/test_llvm_targets.py -q"` (no regressions).

### Task 2: `BuildOptions.llvm_pgo` → wrapper patch, fail loudly

**Files:**
- Modify: `sysforge/build/makepkg_wrapper.py`: the `BuildOptions` dataclass (~line 1322, beside `cmake_llvm_dir`), the `_run_build` signature (~line 829) and its cmake-patch block (~line 856–862), the `_run_build(... cmake_llvm_dir=options.cmake_llvm_dir)` call (~line 1925), and the import list (~line 119)
- Test: `tests/test_wrapper.py`

**Interfaces:**
- Consumes: `patch_llvm_pgo` (Task 1).
- Produces: `BuildOptions.llvm_pgo: tuple[str, str] | None = None` and `_run_build(..., llvm_pgo: tuple[str, str] | None = None)`. When it's set, `_run_build` calls `patch_llvm_pgo(pkgbuild_path, *llvm_pgo)` and ORs the result into `cmake_injected`. When the patch returns False and the PKGBUILD doesn't already carry the args (check the text for the mode's key), it raises `_build_failed_error(RuntimeError("[build_failed] LLVM PGO (<mode>) requested but no cmake configure found in <pkgbuild>: refusing to build uninstrumented"))`.

- [ ] **Step 1: Write the failing tests** in `tests/test_wrapper.py`. Find the existing test that drives `_run_build` with `cmake_llvm_dir` and copy its fixture and monkeypatch shape (`grep -n cmake_llvm_dir tests/test_wrapper.py`):
  - `test_run_build_llvm_pgo_patches_pkgbuild`: `llvm_pgo=("generate", "/store")` on a cmake PKGBUILD → the patched file contains `-DLLVM_BUILD_INSTRUMENTED=IR`, and makepkg (stubbed) is invoked.
  - `test_run_build_llvm_pgo_without_cmake_refuses`: same, on a no-cmake PKGBUILD → raises, and makepkg is **not** invoked.
  - `test_run_build_without_llvm_pgo_leaves_pkgbuild_alone`: default → no `LLVM_BUILD_INSTRUMENTED` in the patched file.
- [ ] **Step 2: Run, expect FAIL**: `make test ARGS="tests/test_wrapper.py -q -k llvm_pgo"`
- [ ] **Step 3: Implement** the field, the threading and the check, as specified in Interfaces.
- [ ] **Step 4: Run, expect PASS**; then `make test ARGS="tests/test_wrapper.py tests/test_build_core.py -q"`.

### Task 3: Toolchain passes use `llvm_pgo`

**Files:**
- Modify: `sysforge/pipeline/stages/toolchain/passes.py` (`build_pkg` ~line 37, its `make_build_options(...)` call ~line 94, `build_pass` ~line 142 and its `build_pkg` call ~line 269)
- Modify: `sysforge/pipeline/stages/toolchain/pgo.py`: Pass 1 `build_pass` (~line 506), `_repackage_llvm` (~line 160), `bolt_only`'s `profile_use` (~line 218), Pass 4a `build_pass` (~line 852) and its `_repackage_4a` closure (~line 874). Revise the Pass-1 purge comment (~line 528) to say the stage1-clang runtime/device compiles also write profraw that gets purged, and the module docstrings (~lines 12, 307, 337)
- Modify: `sysforge/pipeline/stages/toolchain/__init__.py` docstring (~lines 35, 81)
- Test: `tests/test_toolchain_pgo.py`

**Interfaces:**
- Consumes: `BuildOptions.llvm_pgo` (Task 2).
- Produces: `build_pkg(..., llvm_pgo: tuple[str, str] | None = None)` and `build_pass(..., llvm_pgo: tuple[str, str] | None = None)`, passed to `make_build_options(..., llvm_pgo=llvm_pgo)` and to `reuse.pkg_fingerprint` (Task 4 adds the parameter; in this task pass it as a keyword so Task 4 only changes `reuse.py`).
  - Pass 1: `llvm_pgo=("generate", str(pgo_store))`, `compiler_flags_extra` removed.
  - Pass 4a and `_repackage_llvm`: `llvm_pgo=("use", str(profdata_path))` and no `compiler_flags_extra`. `_repackage_llvm` takes `llvm_pgo` in place of `profile_use`. In `bolt_only`, `llvm_pgo` is None when the profdata file is missing (today's behaviour).
  - 4b and 4c are unchanged (`compiler_flags_extra=profile_use`).

- [ ] **Step 1: Write the failing tests.** Extend the `fake_build_pass` capture dicts in `tests/test_toolchain_pgo.py` with `"llvm_pgo": kw.get("llvm_pgo")`, then:
  - `test_pass1_instruments_via_cmake_not_cflags`: full profgen run (copy the setup of the test at ~line 160–236 that sees `cmake_llvm_dir == staging1`). The instrument call (`pkgs == {"llvm"}`, first call) has `llvm_pgo == ("generate", str(pgo_store))` and `cfe is None`.
  - `test_pass4a_uses_profdata_via_cmake_4b_keeps_cflags`: in the reuse-path test (~line 99), `pgo_call["llvm_pgo"] == ("use", str(profdata))`, `pgo_call["cfe"] is None`, `non_pgo_call["cfe"] == f"-fprofile-use={profdata}"`, `non_pgo_call["llvm_pgo"] is None`.
  - `test_repackage_llvm_threads_llvm_pgo`: patch `passes.build_pkg`, call `pgo._repackage_llvm(pgo_map, options, cc=…, cxx=…, llvm_pgo=("use", "/p"), linker_flags=None, pgo_env={})` → `build_pkg` kw `llvm_pgo == ("use", "/p")`, `compiler_flags_extra is None`.
  - `test_build_pass_threads_llvm_pgo_to_build_options`: mirror `test_build_pass_threads_pgo_reuse_to_build_options` (~line 419) → `BuildOptions.llvm_pgo == ("generate", "/s")`.
  - Update existing assertions that expect `cfe == "-fprofile-use=…"` / `"-fprofile-generate=…"` on the **pgo** call (`grep -n "fprofile" tests/test_toolchain_pgo.py tests/test_toolchain*.py`) to the new shape.
- [ ] **Step 2: Run, expect FAIL**: `make test ARGS="tests/test_toolchain_pgo.py -q"`
- [ ] **Step 3: Implement** as specified in Interfaces, and update the docstrings and the comment.
- [ ] **Step 4: Run, expect PASS**: `make test ARGS="tests/test_toolchain_pgo.py tests/test_toolchain_bolt.py -q"` (include every `tests/test_toolchain*.py` that references `_repackage_llvm` or `compiler_flags_extra`).

### Task 4: Reuse fingerprint covers `llvm_pgo`

**Files:**
- Modify: `sysforge/pipeline/stages/toolchain/reuse.py` (`pkg_fingerprint` ~line 115)
- Modify: `sysforge/primitives/build_fingerprint.py` (`_SCHEMA = 4` → `5`, line 59, plus its docstring list at ~line 236)
- Test: `tests/test_build_fingerprint.py` (or whichever file tests `pkg_fingerprint`; check with `grep -rn pkg_fingerprint tests`)

**Interfaces:**
- Consumes: the `build_pass` call site from Task 3.
- Produces: `pkg_fingerprint(ctx, name, pkgbuild_path, cc, compiler_flags_extra, linker_flags_extra, cmake_llvm_dir, extra_flags, *, llvm_pgo: tuple[str, str] | None = None)`, with the components dict key `"llvm_pgo": list(llvm_pgo) if llvm_pgo else None`.

- [ ] **Step 1: Write the failing test** `test_pkg_fingerprint_changes_with_llvm_pgo`: the same inputs with `llvm_pgo=None` vs `("use", "/p/clang.profdata")` vs `("use", "/q/clang.profdata")` give three distinct fingerprints.
- [ ] **Step 2: Run, expect FAIL** (unexpected kwarg).
- [ ] **Step 3: Implement**, and bump `_SCHEMA`.
- [ ] **Step 4: Run, expect PASS**; also `make test ARGS="tests/test_build_fingerprint.py tests/test_toolchain_pgo.py -q"`.

### Task 5: Live canary against the current official llvm PKGBUILD

**Files:**
- Create: `tests/test_live_llvm_pkgbuild.py`

**Interfaces:**
- Consumes: `patch_llvm_pgo` (Task 1).

- [ ] **Step 1: Write the test.** Module docstring in the style of `tests/test_live_stock_toolchain.py`: why it exists (unit tests stub the PKGBUILD shape, and the 2026-10-07 unified PKGBUILD broke the CFLAGS assumption while every unit test passed). `pytestmark = [pytest.mark.network]`. Fetch `https://gitlab.archlinux.org/archlinux/packaging/packages/llvm/-/raw/main/PKGBUILD` with `urllib.request` (timeout 30s, generic User-Agent). On a network error, call `pytest.fail` with a reason. Copy it to `tmp_path / "PKGBUILD.sysforge"`.
  - `test_live_llvm_pgo_generate_lands_in_main_configure`: `patch_llvm_pgo(p, "generate", "/store")` is True. The statement starting at the first `cmake -S llvm` (up to `_cmake_statement_end`) contains `-DLLVM_BUILD_INSTRUMENTED=IR`, and `bash -n` passes.
  - `test_live_llvm_pgo_use_lands_in_main_configure`: same for `"use"`.
- [ ] **Step 2: Run**: `make test-live ARGS="tests/test_live_llvm_pkgbuild.py"`. Expected: PASS against today's PKGBUILD. A failure here means the patcher doesn't fit the live shape, so stop and report it.
- [ ] **Step 3: Confirm it's skipped by `make test`**: `make test ARGS="tests/test_live_llvm_pkgbuild.py -q"` → skipped/deselected.

### Task 6: Docs, release note, guards, follow-up entry

**Files:**
- Modify: `docs/design/07-pipeline-layer.md` (Pass 1 ~line 609, Pass 4 ~line 617 and its 4a bullet; add one sentence that `build_sandbox.refuse_profile_generate` reads CFLAGS and so doesn't see the cmake form, which is fine only because `4.0.0-B10` exempts the whole toolchain stage)
- Modify: `sysforge/CLAUDE.md`, toolchain deep invariants: one bullet saying the pgo PKGBUILD gets PGO only through `pkgbuild_patcher.patch_llvm_pgo` (`BuildOptions.llvm_pgo`), never `compiler_flags_extra`, because makepkg's exported CFLAGS reach the runtimes sub-build; 4b/4c keep CFLAGS
- Modify: `docs/release-notes/unreleased.md` (B12 entry after B11)
- Modify: `ROADMAP.md`: new Planned `B` entry (ID from `make next-id TYPE=B` **after** B12 is in the release notes) for "the build-failure tail (last 80 lines) can be filled by repeated `LLVM Profile Warning: Running out of static counters` lines, hiding the real error". Tag it `*Priority: med · Effort: small · Bump: patch*`, then run `make roadmap-table`
- Check: `docs/guides/*.md` for Pass 1/Pass 4 flag text (`grep -rn "fprofile\|Pass 1\|instrument" docs/guides`), and update any hit

- [ ] **Step 1:** `make next-id TYPE=B` → expect `4.0.0-B12`.
- [ ] **Step 2:** Write the release note. Title: "`run toolchain` instruments LLVM without breaking the runtimes build". Body: the symptom (`Could NOT find Threads` at Pass 1 since the official llvm PKGBUILD merged the suite and builds compiler-rt/openmp/libc++ with the just-built clang), the cause in user terms, the fix (LLVM's own instrumentation options, applied to the LLVM projects only), that a `--reuse-built` artifact from before rebuilds once, and the tier line: "Unit tests plus a `make test-live` canary that patches the current official llvm PKGBUILD."
- [ ] **Step 3:** Design and CLAUDE.md edits; `make design`.
- [ ] **Step 4:** ROADMAP entry; `make roadmap-table`.
- [ ] **Step 5: Verify**: `make lint`, `make check-design`, `make check-standards`, `make check-roadmap-table`, `make check-surface` (expected: no surface change), `make test-x` (expected: all pass; baseline 7795 passed + new).
- [ ] **Step 6:** Commit the docs/notes/ROADMAP changes. Delete `docs/superpowers/` before any PR.

## Manual verification (yours, ~2–3h)

`sysforge run toolchain`. Pass 1 should get past `runtimes-configure`. While it runs or afterwards, check that `~/builds/llvm/src/llvm-project-*/build/CMakeCache.txt` has `LLVM_BUILD_INSTRUMENTED:STRING=IR`, and that `build/runtimes/runtimes-bins/CMakeCache.txt` has `CMAKE_C_FLAGS` without `-fprofile-generate`.
