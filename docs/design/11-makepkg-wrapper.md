## Makepkg Wrapper

### Environment isolation

SysForge treats the calling shell environment as untrusted for build tool vars. All keys in the `makepkg` and `toolchain` conf types (`CC`, `CXX`, `CFLAGS`, `CXXFLAGS`, `LDFLAGS`, `MAKEFLAGS`, etc.) are stripped from the inherited shell env before makepkg is invoked. The temp conf is the sole authority — shell vars set by `.zshrc`, `.bashrc`, or upstream tooling cannot bleed through and override profile settings. Each stripped key is logged individually under `[INFO][ENV]` with its old shell value, so the full before/after state is visible in the log. If `extra_env` (the profile's env-type keys) would override a shell var that was *not* in the strip set, a `[WARN][ENV]` is emitted.

SysForge bootstrap vars (`SYSFORGE_STATE_DIR`, `SYSFORGE_CONFIG_DIR`) are explicitly exempt from this rule — they are SysForge's own interface, not build tool vars.

Any build tool override needed at invocation time should use the corresponding SysForge flag (`--cc`, `--cxx`, `--ld`), not a shell export. This applies to both `sysforge build` and `sysforge pipeline`.

### Build sandbox

Off by default (`[security] sandbox_builds`). When on, the makepkg invocation is **replaced** — not wrapped — by a `makechrootpkg` one, so `prepare()`/`build()`/`package()` execute inside a clean `systemd-nspawn` container as an unprivileged `builduser` with only the PKGBUILD directory bind-mounted. The environment isolation above keeps the *shell* from steering a build; this keeps the *build* from reaching the user's home. `[security] freeze_sources` gates code ingress, this gates blast radius, and the two compose.

`primitives/build_sandbox.py` is the one home: `resolve_sandbox(cfg)` at CLI entry, `for_profile(policy, resolved_profile)` for the per-profile override, `get_policy()` at the invocation seam, `suppressed(active)` for the stage exemption. The policy is a consulted module global for the same reason `net_policy`'s is — the seam sits six call sites deep behind the retry and recovery loops, and a threaded parameter defaults permissive at every site that forgets it.

Why the argv *shape* branches rather than composing another `build_throttle.wrapper_argv` prefix entry:

- `makechrootpkg` operates on the **current directory** and hardcodes `source PKGBUILD` for its pkgbase probe, so it cannot honour `-p <name>` — while SysForge never builds the upstream file at all: every route through `_run_build` reassigns the path to the patched `PKGBUILD.sysforge` sidecar. `as_canonical_pkgbuild` reconciles the two with a scoped rename around the build (upstream parked at `.sysforge-upstream-PKGBUILD`, sidecar installed as `PKGBUILD`), undone in the same `finally` that removes the scratch conf. A staged copy in a scratch directory was the alternative and is the larger surface to get wrong: the build also needs the package directory's local sources, `.install` scripts, patches and keys.
- It re-execs itself under `sudo` (devtools' `check_root`) with its own env-preserve list. SysForge must **not** prefix `sudo` — that would strip `PKGDEST`/`SRCDEST`/`LOGDEST`/`MAKEFLAGS` and silently relocate every artifact. This is the one documented exemption from the `privileged_argv` seam (§Privilege Seam): the escalation is the tool's own, and wrapping it breaks it. `dest_env_from_conf` exports those four from the *emitted* conf so the sandbox path leaves artifacts exactly where the host path does and `_find_built_packages` keeps working unchanged.
- `MAKEPKG_CONF` does not cross the container boundary, and neither do the env-delivered `CC`/`CXX` that `CONF_KEY_MAP["toolchain"]` deliberately keeps *out* of the conf file. `chroot_conf_text` therefore derives a container-side conf from the emitted one — dest keys repointed at `/build`, `/pkgdest`, `/srcdest`, `/logdest`, host-only build accelerators forced off (below), and the env-only keys re-added as `export` lines (the conf is sourced by bash, the one channel that survives `sudo -iu builduser`). Only the profile-resolved injections are re-exported; the inherited shell environment is not a build input and does not cross. The scratch conf lives in a dotted temp dir *inside* the PKGBUILD directory — the one thing already bind-mounted — and is removed in a `finally`, on the failure path too.
- The throttle prefix applies **outside** the container (nice/ionice propagate to children), but `[build] mem_limit`'s `RLIMIT_AS` clamp does not: the preexec would cap `makechrootpkg`, whose real work happens in a container in its own cgroup. `mem_cap_applies` returns `False` and the cap is dropped with a `[WARN]` rather than silently misapplied.
- Dependencies built earlier in the same run are installed on the **host**, which the container cannot see. `register_artifacts` / `install_args` (a session registry, written by the `build_core` build loop and the AUR dep loop) feed them back in as `makechrootpkg -I`, so an AUR dep chain still resolves.

**Host-only accelerators are stripped, not satisfied.** The chroot is `base-devel` only, so a `BUILDENV` naming `ccache` or `distcc` and a `RUSTC_WRAPPER` naming `sccache` all point at binaries that are not in there — and makepkg treats a missing accelerator as a *hard error raised before `prepare()`* (`Cannot find the ccache binary required for compiler cache usage`), so an inherited host `BUILDENV` failed every package the sandbox was pointed at (`3.2.0-B2`). `_container_buildenv` negates `_HOST_ONLY_BUILDENV` in the emitted line while leaving every other option's position and sense untouched (`check` and `sign` are build *policy*, not tooling), and `RUSTC_WRAPPER`/`CCACHE_DIR`/`SCCACHE_DIR` join `_ENV_EXPORT_DENY`. Disabled rather than installed into the chroot because the accelerators cannot work there regardless: `makechrootpkg -c` resets the working copy every build and no cache directory is bind-mounted, so the hit rate is structurally zero and there is nothing to trade away. A conf that sets no `BUILDENV` gets no line — makepkg's own default already disables both.

**The progress bar's row reservation has to cross the container's own pty.** `pty_runner.run_with_pty` shrinks the child's pty by `progress.reserved_rows()` so the bar's bottom row survives a build (§Primitives Layer), but a winsize belongs to the tty *device*, not the fd — and stdin used to stay the inherited real terminal. `makechrootpkg` → `sudo` → `arch-nspawn` → `systemd-nspawn --console=autopipe` allocates a **nested** pty and sizes it from stdin, so the container ran at the terminal's full height, scrolled over the reserved row, and the indicator disappeared for the entire sandboxed build (`3.2.0-B3`). Under a reservation the child's stdin is now the same pty as its stdout, so both descriptors report the reserved height and the nesting inherits it. `--console=autopipe` picks its interactive console *because* stdin is a tty, so the inherited terminal both triggered the extra pty layer and mis-sized it.

**Dependency scope — the standing limitation.** `arch-nspawn` gives the container its *own* `/etc/pacman.conf`, so `--syncdeps` resolves from the stock repos and never from the host's installed set. Two consequences, both currently accepted rather than solved:

- A locally-built dependency is invisible unless it is injected. The session registry covers packages built *in the same run* (so an `update` across a stack resolves), but not a package built in an earlier run and only present as an installed host package — `3.1.0-F9` closes that by seeding the injection set from `build_state.toml`.
- Versions can diverge from the host: a source-built package ahead of the repos (a self-built toolchain, a `-git` checkout) is replaced in the container by the repo version, so the build may link against something the host does not run. Only a local repo the container's `pacman.conf` names fixes this class — `3.1.0-F10`.

What is *not* lost is the optimization of the host's own packages: a dependent build consumes a dependency's headers and link stubs, while the optimization lives in that dependency's installed binary, which the built package still runs against on the host. The sandbox is therefore scoped, by design, at untrusted AUR leaf packages whose dependencies come from repos — the population the threat model is about — and is default-off for everything else.

Preflight refuses rather than downgrades — missing `devtools`, a missing `<chroot>/root`, or a `.sysforge-upstream-PKGBUILD` left behind by an interrupted run all raise `SandboxUnavailable` with the command that fixes it. A security opt-in that silently falls back to the host hands the user exactly the exposure they opted out of; the stash case refuses because completing the swap over it would replace the user's upstream PKGBUILD with a patched one for good. The filename itself is *not* a refusal (`3.2.0-B1`) — it never can be, since the canonical name is the one name the pipeline never produces.

`run toolchain` and `run kernel` are exempt via `build_sandbox.suppressed(...)` at the single `_run_build` call site in `makepkg_wrapper.run`, keyed on `owner_stage`: both build against, and install into, the host they are upgrading — a staged LLVM built in a container links against that container's libraries, and a kernel built there cannot see the host's DKMS modules or run its boot audit.

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

`AlreadyBuilt` (carries the offending pkgbuild path) is raised when the makepkg run exits 13 (`E_ALREADY_BUILT`) or its stdout contains `"A package has already been built"` — covers chroot wrappers that may rewrite the exit code. Distinct from `CalledProcessError` so callers can act on it instead of marking the build failed.

Interpretation is centralized (2.5.1-F2): every catch site routes its decision
through `primitives/already_built.resolve_already_built` — a decide-only policy
seam with two postures. `"reuse"` (build_core's batch loop, the toolchain
passes) treats the existing PKGDEST artifact as the product and proceeds — but
the batch loop reaches the seam only for *unforced* targets, since a target
built with `-f` (drift promotion, `build --rebuild`) cannot legitimately report
exit 13 and fails hard instead (3.0.0-B9);
`"review-gated"` (kernel stage) preserves the B5 semantics — the skipped build
also skipped the promised in-prepare() kconfig review, so interactive runs get
an install-as-built / rebuild-with-`-f` / abort prompt while unattended runs
proceed. The unattended arbitration (caller interactivity ∧ `--non-interactive`
∧ TTY) lives only in the seam. `makepkg_wrapper`'s own `except AlreadyBuilt`
(manifest capture for renamed builds) is a side-effect that fires regardless of
policy, then re-raises.

`PGOBuildSkipped` is the third wrapper-specific exception: raised from `_run_build` when a `pgo_llvm_toolchain` build needs profdata that's absent/incompatible and the user (or non-interactive default) chose to skip.

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

- `source_built` mode (legacy token `patched_pkgbuild`, deprecated — removed in 4.0.0, row 24): `PKGBUILD.sysforge` and `pkgbuild_extracted_profile.toml` are preserved. A `[WARN][PATCH]` line is emitted noting their location.
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

Above the menu, the failure summary reports the toolchain the build actually
used: `Toolchain used:  CC=…  CXX=…  LD=…`. The `LD` field comes from
`_summary_linker`, which reuses the single `makepkg_flags.resolve_effective_linker`
authority over the profile's LDFLAGS *and* the system makepkg.conf LDFLAGS — so a
conf-level `-fuse-ld=` swap (e.g. the clang config's lld) is surfaced, not just a
profile-level one. Since linker choice (`-fuse-ld=…`, mold/lld swaps) is a frequent
failure cause, showing it alongside `CC`/`CXX` makes the diagnostic complete; the
resolver's PATH guard means a declared-but-missing linker degrades to `ld`.

`[c]` is offered only when the caller supplied a `reemit_conf` closure (see
below) — a caller with no conf-emission seam (e.g. a test harness) degrades
to `[e]/[r]/[a]`.

- **`[e]`** snapshots the PKGBUILD to a sibling `<name>.orig` (once, on first
  edit — never overwritten by a later edit) before launching `$EDITOR`
  (`primitives/editor.py`'s `resolve_editor`/`editor_usable`, the same
  resolution chain as `config merge`), via the shared `/dev/tty` passthrough
  `run_tty_argv`. On editor exit it retries the build automatically; a
  still-failing retry re-shows the menu rather than raising.
- **`[c]`** presents the compiler choice as a **coherent toolchain unit**
  (`_prompt_toolchain_swap` → `_TOOLCHAIN_UNITS`): the user picks `gcc`
  (`CC=gcc CXX=g++`) or `clang` (`CC=clang CXX=clang++`), never two independent
  free-text compilers — a mixed `cc`/`cxx` pair (e.g. `gcc` + `clang++`) only
  produces an incoherent override that fails the retry confusingly. The menu
  enumerates **only installed** toolchains (`_available_toolchain_units` gates
  on `shutil.which` for *both* `cc` and `cxx`), marks the current one, and offers
  `[m]` (hand-enter a `cc`/`cxx` pair — the advanced escape hatch) and `[b]`
  (back to the top menu). `LD` is prompted separately (linker choice is
  orthogonal to the compiler). It then calls the caller-supplied
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

