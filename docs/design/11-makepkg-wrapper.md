## Makepkg Wrapper

### Environment isolation

SysForge treats the calling shell environment as untrusted for build tool vars. All keys in the `makepkg` and `toolchain` conf types (`CC`, `CXX`, `CFLAGS`, `CXXFLAGS`, `LDFLAGS`, `MAKEFLAGS`, etc.) are stripped from the inherited shell env before makepkg is invoked. The temp conf is the sole authority — shell vars set by `.zshrc`, `.bashrc`, or upstream tooling cannot bleed through and override profile settings. Each stripped key is logged individually under `[INFO][ENV]` with its old shell value, so the full before/after state is visible in the log. If `extra_env` (the profile's env-type keys) would override a shell var that was *not* in the strip set, a `[WARN][ENV]` is emitted.

SysForge bootstrap vars (`SYSFORGE_STATE_DIR`, `SYSFORGE_CONFIG_DIR`) are explicitly exempt from this rule — they are SysForge's own interface, not build tool vars.

Any build tool override needed at invocation time should use the corresponding SysForge flag (`--cc`, `--cxx`, `--ld`), not a shell export. This applies to both `sysforge build` and `sysforge pipeline`.

> **Cancelled design:** an `[env_precedence]` TOML table with a configurable priority stack (profile = 100, makepkg.conf = 80, shell = 20, PKGBUILD export = 10) was previously planned. It is superseded by this model — shell bleed-through is not a tunable priority, it is prevented entirely.

### Failure handling

Each scenario has a configurable behaviour in `[failure_handling]`:

```toml
[failure_handling]
pkgbuild_unparseable  = "warn_and_fallback"
no_rule_matched       = "fallback"
profile_missing       = "abort"
profile_cycle         = "abort"
tempfile_write_failed = "abort"
env_conflict          = "warn_and_fallback"
abi_mismatch          = "warn_and_fallback"
dep_unsatisfied       = "warn_and_fallback"
```

**Behaviours:** `abort`, `warn_and_fallback`, `fallback`, `error`

`profile_missing` and `tempfile_write_failed` always abort regardless of config.

### Interactive mode

`--interactive` on `sysforge build` does two things: it strips `--noconfirm` from the profile's `makepkg_flags`, and it makes `invoke_makepkg` inherit the parent's stdout/stderr instead of piping them through the line-classification loop. Stdio passthrough is what keeps unbuffered prompts visible — pacman's conflict prompt (`Remove sysforge? [y/N]`) and similar `\r`/no-newline output reaches the terminal immediately rather than sitting in a pipe buffer until the user blindly presses Enter. The tradeoff is that line-based output classification (`failed_stage`, `missing_deps`, `toolchain_mismatch` auto-retry, stdout-match fallback for `AlreadyBuilt`, `captured_output` for `auto_repair`) is bypassed in this branch; exit-code-based detection (`returncode == 13` → `AlreadyBuilt`, `returncode == 8` → install failure) still fires. Useful during development to review makepkg prompts without editing the profile; not appropriate for `update` batch flows, which depend on the classification path and therefore default `interactive=False`.

### Toolchain-mismatch auto-retry

When a package's build system (e.g. a Makefile shipped in `src/`) hardcodes `g++` but the active makepkg.conf carries clang-only flags (typically `-flto=thin`), GCC aborts with `cc1plus: error: unrecognized argument to '-flto=' option: 'thin'`. Static PKGBUILD scanning cannot detect this case because the hardcoded compiler lives in a file that only appears after `makepkg` extracts sources. To handle it automatically, `invoke_makepkg` scans stdout/stderr for a narrow list of toolchain-mismatch patterns:

```python
TOOLCHAIN_MISMATCH_PATTERNS = (
    "unrecognized argument to '-flto=' option",
    "unrecognized command-line option '-flto=thin'",
)
```

When any pattern matches and the process exits non-zero, `invoke_makepkg` raises `ToolchainMismatchError` (a `subprocess.CalledProcessError` subclass) instead of the plain exception. `_invoke_with_retry` re-raises this type unchanged — bypassing the interactive "correct manually" prompt — and `_run_build` catches it, sets `reactive_gcc_fallback=True`, and re-enters `emit_makepkg_conf` exactly once. The second attempt fires guards 3–4 (thin-LTO rewrite, LTO-disable for lld) regardless of the profile's CC and typically succeeds. If the retry also fails, the error bubbles out as a normal build failure.

`AlreadyBuilt` (carries the offending pkgbuild path) is raised when the makepkg run exits 13 (`E_ALREADY_BUILT`) or its stdout contains `"A package has already been built"` — covers chroot wrappers that may rewrite the exit code. Distinct from `CalledProcessError` so callers (currently `update.py`'s build loop) can locate the existing `.pkg.tar` in PKGDEST and install it instead of marking the build failed. `PGOBuildSkipped` is the third wrapper-specific exception: raised from `_run_build` when a `pgo_llvm_toolchain` build needs profdata that's absent/incompatible and the user (or non-interactive default) chose to skip.

To make the pattern scan work for every build mode, `invoke_makepkg` uses a `Popen`-with-tee capture path for non-interactive builds: each line is matched against the patterns, then forwarded to stdout (or to `[DEBUG][MAKEPKG]` when verbosity ≥ 3). stdin remains inherited so sudo prompts still work. The capture path is **skipped entirely when `interactive=True`** (see §Interactive mode) — in that branch the child inherits stdout/stderr directly, so the toolchain-mismatch auto-retry is unavailable. Batch flows (`update`, pipeline stages other than `kernel`) leave `interactive=False`, so they retain the retry.

### Build-failure auto-repair

> **Status: implemented (all 4 scenarios).** Lives in `sysforge/primitives/auto_repair.py`. `_run_build`'s outer loop catches `CalledProcessError`, walks `auto_repair.REGISTRY`, and on the first match runs the corresponding repair before retrying.

`invoke_makepkg`'s line-tee captures every stdout line into `captured_lines` and attaches the list to the raised `CalledProcessError` (and `ToolchainMismatchError`) as `captured_output`. `_run_build` wraps that buffer in a `BuildOutputAccumulator` (lines + optional `srcdir` for on-disk inspection) and feeds it to `auto_repair.apply_first_match`. Each scenario's `detect(accum)` returns a `MatchInfo` (or `None`); on match the wrapper consults `[failure_handling]` for the per-scenario behaviour, runs `repair(pkgbuild_dir, info)`, and re-enters the build loop. The set of already-fired scenarios is tracked per build (`_repaired_scenarios`) so a misdetected error cannot loop — once a scenario fires it is excluded from subsequent matches in the same build.

**Interactive-failure diagnosis (BUILDDIR/LOGDEST-aware).** In the interactive branch makepkg inherits the TTY, so `captured_output` is empty and the auto-repair scan above is unavailable. `makepkg_invoke` instead recovers a best-effort `build_diag.diagnose` signature from on-disk artifacts: `makepkg_env._effective_build_dir(pkgbuild_path, resolved_profile)` locates the meson/cmake side-car logs (`meson-log.txt`, `CMakeError.log`) under `$BUILDDIR/<pkgbase>/src`, and `makepkg_env._logdest_tail(pkgbuild_path)` reads the tail of the newest `$LOGDEST/<pkgbase>-*.log` (makepkg's captured stdout when `OPTIONS+=log`). Both `BUILDDIR` and `LOGDEST` are resolved through `pacman.get_builddir()` / `pacman.get_logdest()` — env first, then the layered system `makepkg.conf` — so a user who configures these only in `/etc/makepkg.conf` still gets a diagnosis. Don't read `os.environ["BUILDDIR"]` or assume `~/builds` directly here.

Configuration extends `[failure_handling]` with four scenario keys:

```toml
vendored_deps_missing = "auto_repair"
pgp_key_missing       = "auto_repair"
srcinfo_drift         = "auto_repair_with_warning"
checksum_mismatch     = "prompt_user"
```

Three new behaviour values join the existing `abort` / `warn_and_fallback` / `fallback` / `error` set: `auto_repair` (repair silently, retry, log at info), `auto_repair_with_warning` (repair, retry, log at `[WARN]`), `prompt_user` (surface the diff and require explicit consent before repairing). In **batch mode** (`batch=true` on the resolved profile) the `prompt_user` behaviour is short-circuited to `aborted` — auto-repair never runs unattended for security-sensitive scenarios. This is the load-bearing rule for `checksum_mismatch`.

`.SRCINFO` drift is the one scenario that is **not** in `REGISTRY` — it has a different lifecycle. `_run_build` calls `auto_repair.preflight_srcinfo(pkgbuild_dir, behaviour)` before the build starts, regenerating `.SRCINFO` (with a `[WARN]` by default) when `makepkg --printsrcinfo` differs from the committed file. The other three scenarios fire on retry from a captured failure.

Repair modes:

- **Vendored deps missing** — meson wraps and git submodules collapse to one primitive (both are "PKGBUILD failed to fetch declared subprojects before configure"). Detection: `Automatic wrap-based subproject downloading is disabled` (meson) or `.gitmodules` present in `${srcdir}/<top>` with empty submodule paths. Repair: `meson subprojects download` for wraps, `git submodule update --init --recursive` for submodules, run from the project root. Safe — fetches only what the project itself declares; no PKGBUILD mutation, no `source=()` change.
- **PGP key missing** — complements the proactive `import_pgp_keys` invoked before makepkg. Catches the case where signature validation fails mid-build because upstream rotated keys after the proactive import or `prepare()` pulled in newly signed sources. Detection: `gpg: Can't check signature: No public key` with an extractable key ID. Repair: rerun `import_pgp_keys` targeting the surfaced ID, retry once. Same trust model as the proactive path.
- **`.SRCINFO` drift** — sysforge parses PKGBUILD directly, so its own pipeline never reads `.SRCINFO`, but drift breaks AUR push and clean-chroot consumers. Detection: `makepkg --printsrcinfo` output diffs against the committed `.SRCINFO`. Repair: regenerate, with a `[WARN]` log line. Default is `auto_repair_with_warning` rather than silent because drift usually signals upstream PKGBUILD churn the user should see.
- **Checksum mismatch** — **not silent.** Default `prompt_user`: sysforge surfaces old vs. new sums and the source URL, then requires explicit consent before invoking `updpkgsums`. Silent auto-fix would mask supply-chain compromise — an attacker who replaces an upstream tarball would have sysforge "fix" the checksum and proceed. The user-prompt requirement is the load-bearing security distinction between this mode and the others; do not relax it without an equivalent compensating control.

### Patched PKGBUILD preservation

On build failure, patched PKGBUILD files are left in place for diagnosis rather than deleted:

- `patched_pkgbuild` mode: `PKGBUILD.sysforge` and `pkgbuild_extracted_profile.toml` are preserved. A `[WARN][PATCH]` line is emitted noting their location.
- Groups-only mode (non-patch builds): `PKGBUILD.sysforge` is also preserved on failure with a `[WARN][BUILD]` message.

On success, all patch artifacts are cleaned up in both modes.

### Interactive recovery menu

When `_invoke_with_retry` is not in batch mode and a build fails with no
built `.pkg.tar*` artifacts found (the "install-only failure" branch above
doesn't apply), it hands off to `makepkg_invoke._run_recovery_menu` — the
**one home** for interactive build-failure recovery. This replaces the old
bare "fix the PKGBUILD and press Enter" prompt with a small menu:

```
Recover:
  [e] edit PKGBUILD in $EDITOR — retries automatically on exit
  [c] retry with a different compiler / linker
  [r] retry as-is            (Enter)
  [a] abort
```

`[c]` is offered only when the caller supplied a `reemit_conf` closure (see
below) — a caller with no conf-emission seam (e.g. a test harness) degrades
to `[e]/[r]/[a]`.

- **`[e]`** snapshots the PKGBUILD to a sibling `<name>.orig` (once, on first
  edit — never overwritten by a later edit) before launching `$EDITOR`
  (`primitives/editor.py`'s `resolve_editor`/`editor_usable`, the same
  resolution chain as `config merge`), via the shared `/dev/tty` passthrough
  `run_tty_argv`. On editor exit it retries the build automatically; a
  still-failing retry re-shows the menu rather than raising.
- **`[c]`** prompts for `CC`/`CXX`/`LD`, then calls the caller-supplied
  `reemit_conf(cc, cxx, ld)` context manager to get a freshly emitted conf
  path and retries against it. `makepkg_invoke` never imports the conf
  emitter itself — `reemit_conf` is a closure the wrapper builds over its own
  `emit_makepkg_conf(...)` call (same `_conf_kwargs` as the main build), so
  the layering stays one-directional: `makepkg_wrapper` depends on
  `makepkg_invoke`, never the reverse. A successful swap is reported back as
  `RecoveryOutcome(action="retry", overrides={"cc", "cxx", "ld"})`.
- **`[r]`** retries unchanged; **`[a]`** aborts.

`_run_recovery_menu` returns a `RecoveryOutcome` (`action: "retry"|"abort"`,
optional `overrides`) only on a successful retry or an explicit abort — it
never returns mid-failure, it loops. `_invoke_with_retry` raises the
`[build_failed]` error on `action == "abort"`.

**Read-once channel back to the wrapper.** `_invoke_with_retry` runs one
import-cycle below `makepkg_wrapper`, which is the layer that owns the
profiles.toml writer — so the menu can't call the writer directly without
inverting the dependency. Instead a successful swap's `RecoveryOutcome` is
stashed in a `contextvars.ContextVar` (`_LAST_RECOVERY`) and the wrapper
drains it once via `take_last_recovery()` right after the build's `with
emit_makepkg_conf(...)` block exits successfully — `take_last_recovery`
resets the var on read, so a stale outcome from an earlier package can never
leak into the next one's persistence check.

**Persistence is best-effort.** `makepkg_wrapper._persist_recovery_overrides`
(called once per successful build, keyed on the package's `pkgbase`) drains
`take_last_recovery()`; if it carries `overrides` it calls
`profile_writer.write_package_compiler_override` — the sole profiles.toml
writer for this table (see §Flag/Profile System →
`[package_compiler_overrides]`) — wrapped so a read/write failure is logged
and swallowed rather than failing an otherwise-successful build. A swap with
any of `cc`/`cxx`/`ld` missing is skipped with a warning instead of writing a
partial row.

### Batch mode

`batch = true` on a profile switches to unattended mode — build failures abort immediately rather than prompting. Intended for pipeline use.

```toml
[profiles.batch]
extends = "standard"
batch = true
makepkg_flags = ["--noconfirm", "--syncdeps", "--rmdeps", "--install", "--noprogressbar", "--log", "--cleanbuild"]
clean_builddir = true
```

---

