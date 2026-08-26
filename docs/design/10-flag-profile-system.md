## Flag Profile System

### Profile structure

```toml
[defaults]
profile = "standard"
toolchain = "gcc"        # global package-compiler default (see "Toolchain field")

[profiles.bare]
# Fallback profile, no flags

[profiles.standard]
extends = "bare"
# Compiler comes from the `toolchain` field (defaults to "gcc" via [defaults]).
# Override individual CC/CXX/AR/… keys to win over the bundle, or set
# `toolchain = "llvm"` here / in a rule to switch the whole bundle.
CFLAGS = "-march=native -O2 -pipe"
CXXFLAGS = "$CFLAGS"
LDFLAGS = "-Wl,-O1,--sort-common,--as-needed,-z,relro,-z,now"
makepkg_flags = ["--noconfirm", "--syncdeps"]

[profiles.optimized]
extends = "standard"
CFLAGS = "-march=native -O3 -pipe -fno-plt"
LDFLAGS = "-Wl,-O1,--sort-common,--as-needed,-z,relro,-z,now,--icf=all"

[profiles.pgo_llvm_toolchain]
extends = "optimized"
build_mode = "pgo_llvm_toolchain"

[profiles.patched]
extends = "optimized"
build_mode = "source_built"   # legacy "patched_pkgbuild" read-accepted, warns, gone in 4.0.0

[profiles.kernel]
extends = "bare"
build_mode = "kernel"
batch = true
makepkg_flags = ["--noconfirm", "--syncdeps", "-f", "-c"]
```

### `build_mode` vocabulary

`build_mode` is set in two layers and `profile.py` carries the single documented
enumeration of every token (next to `_OPTIMIZED_BUILD_MODES`). The profile layer
(`profiles.toml`, user-set) uses `source_built` | `pgo_llvm_toolchain` | `kernel`
(omit for a standard build); the build_state layer (`build_state.toml`, stamped at
build time) uses `source_built` | `pacman` | `kernel` plus the optimization modes
(`pgo_mesa`/`pgo`/`autofdo_kernel`/`propeller_kernel`/`bolt_llvm`). The two layers
share one value space: `source_built` means "a plain from-source build" in both —
the profile layer extracts the PKGBUILD's embedded profile, build_state stamps it
as the rebuild-on-update marker.

The profile layer normalizes its own legacy token **on read only** (never written
into new data): `patched_pkgbuild → source_built` (`profile.normalize_build_mode`,
applied at the `get_build_mode` read chokepoint). That alias is a registered
`compat` deprecation as of 2.6.1-STD3 — reading it warns once per run and it is
**removed in 4.0.0** (standards row 24; a `compat` removal may only land on an
`X.0.0`, and 3.0.0 ships with the surface live). The build_state layer's
equivalent read alias, `profiled → source_built` (formerly `BuildState.__init__`),
was removed in 3.0.0 — a pre-rename `build_state.toml` entry now reads `profiled`
verbatim until `sysforge state repair` rewrites it. The "does this mode extract
the embedded PKGBUILD profile?" gate has one home —
`profile.build_mode_uses_extracted_profile` (`source_built`/`kernel`, legacy
profile-layer token accepted) — consumed by `flag_drift` and `makepkg_wrapper`;
don't re-spell the membership inline.

### Toolchain field

`toolchain = "gcc" | "llvm"` is a single knob that expands to the correct
compiler/binutils bundle, so a profile need not hand-set the six-plus correlated
keys (and risk a silently half-LLVM build). Valid in `[defaults]` (global
default) and any `[profiles.NAME]`.

| value  | expands to |
|--------|------------|
| `gcc`  | `CC=gcc`, `CXX=g++` (binutils from system base-devel) |
| `llvm` | `CC=clang`, `CXX=clang++`, `AR=llvm-ar`, `NM=llvm-nm`, `RANLIB=llvm-ranlib`, `STRIP=llvm-strip`, and `-fuse-ld=lld` injected into `LDFLAGS` when no `-fuse-ld=` is already declared |

Resolution (in `profile._expand_toolchain`, the one home, run **after**
`merge_extends` so the directive inherits/overrides like any key): an explicit
`CC`/`CXX`/`AR`/… in the resolved profile wins (`setdefault`); otherwise the
resolved profile's own `toolchain`; otherwise `[defaults] toolchain`. The
expansion is pure (no fs probing) — a missing `lld` is reconciled by the
emit-time linker guard, exactly as for a hand-written `-fuse-ld=lld`.

This field is the **package** compiler knob. It is distinct from two other axes
that also take `gcc`/`llvm`:

- `toolchain.toml`'s `compiler` — whether the toolchain *stage* builds/registers
  a system compiler. On a successful register/build the stage writes
  `[defaults] toolchain` to match it (via `config.set_default_toolchain`), so the
  package default tracks the registered compiler. See pipeline-layer → toolchain
  stage.
- `toolchain_variant` — which toolchain the stage *built* (`stock_llvm`/`pgo_llvm`/
  `gcc`), recorded in `build_state.toml` for drift detection. Not derived from
  this field.

### `extends` semantics

Full inheritance with explicit override. The child starts as a complete copy of the parent's resolved values, then applies its own keys on top.

**Direct keys** fully replace the parent's value.

**`[profiles.x.append]` subsection** — keys merged into the parent's value using token-level list merge rather than string concatenation.

#### Append merge algorithm

1. Tokenize parent and child values by whitespace
2. For each child token, resolve in this order:
   - **Explicit conflict group** — if the token belongs to a defined conflict group, remove all other group members from the accumulated list, insert the child token
   - **Prefix match** — extract the token's prefix (everything up to `=`, or up to trailing digits for flags like `-O2`); if a matching prefix exists, replace in-place
   - **Append** — no match, add to end
3. Reconstruct as space-joined string

**Worked example:**
```
parent CFLAGS:   "-march=native -O2 -pipe -fstack-protector"
append CFLAGS:   "-O3 -fno-stack-protector --icf=all"

-O3                   prefix "-O" matches "-O2"              → replace in-place
-fno-stack-protector  conflict group "stack"                 → removes "-fstack-protector", inserts
--icf=all             no match                               → append

result: "-march=native -O3 -pipe -fno-stack-protector --icf=all"
```

#### Conflict groups

Defined in the `[append_conflict_groups]` table of `/etc/sysforge/profiles.toml`:

```toml
[conflict_groups]
pic   = ["-fPIC", "-fPIE", "-fpic", "-fpie", "-fno-pic", "-fno-pie"]
lto   = ["-flto", "-flto=thin", "-flto=full", "-fno-lto"]
stack = ["-fstack-protector", "-fstack-protector-strong", "-fno-stack-protector"]
```

User-defined groups in `~/.config/sysforge/profiles.toml` (under `[append_conflict_groups]`) follow the same `extends_system` merge model. Explicit conflict groups take precedence over prefix matching.

#### Preserved system tokens (hardening)

Profile keys overwrite their system counterparts **per key, not per token** — so a profile that
rewrites `CFLAGS` around `-march`/`-O` would otherwise drop the rest of `/etc/makepkg.conf`'s
`CFLAGS`, including the distro's compiler hardening baseline (`-Wp,-D_FORTIFY_SOURCE=3`,
`-fstack-protector-strong`, `-Werror=format-security`, `-fexceptions`, …). The `LDFLAGS` half of
that set (`-z,relro,-z,now`) was already carried by the shipped profiles; the compiler half was not.

`[preserved_system_tokens]` in `/etc/sysforge/profiles.toml` declares, per conf key, which tokens of
the *system* value survive an override:

```toml
[preserved_system_tokens]
CFLAGS   = ["-fexceptions", "-Wp,-D_FORTIFY_SOURCE=3", "-Wformat",
            "-Werror=format-security", "-fstack-protector-strong",
            "-fstack-clash-protection", "-fcf-protection"]
CXXFLAGS = [...]
```

`emit_makepkg_conf` applies the set right after the system conf is parsed — before the linker, LTO,
lib32 and musl-static guards, so their scrubs see the final token set — via
`profile.merge_preserved_tokens`, with **profile-wins** precedence:

| Profile value declares… | Result |
|---|---|
| the token already | left alone (never duplicated) |
| a token in the same conflict group (`-fno-stack-protector`) | not re-added — an explicit per-token opt-out |
| a token in the same prefix family (`-Wp,-D_FORTIFY_SOURCE=2`) | the profile's value wins |
| nothing related | the system token is appended |

Only tokens the system conf actually sets are ever restored — the table never invents a flag, so a
distro whose conf omits a token stays as it is (§Distro portability). A pure shell reference
(`CXXFLAGS = "$CFLAGS"`) is left untouched and inherits by expansion. Kernel builds never reach the
pass: `KERNEL_CLEAN_KEYS` keeps flag keys out of `profile_overrides` entirely, so the system values
pass through verbatim. A profile opts out of the whole pass with `preserve_system_tokens = false`
(a `SYSFORGE_KEYS` member — never written to a conf).

User `[preserved_system_tokens]` in `~/.config/sysforge/profiles.toml` follows the same
`extends_system` merge model as conflict groups.

The pass has **one implementation**, `profile.apply_preserved_system_tokens`, applied at two seams
that must agree exactly:

| Seam | Caller | What it produces |
|---|---|---|
| conf emission | `makepkg_conf.emit_makepkg_conf` | the flags the build actually compiles with |
| flag recording | `makepkg_conf.serialize_effective_flags` | the `flags_string` in `build_state.toml`, and the string `flag_drift` re-resolves |

Before 3.1.0-B11 only the first seam ran the pass, so `flags_string` recorded the *pre-merge*
resolved profile on both sides of the drift diff — the restoration was structurally invisible to
flag drift, and a change to `[preserved_system_tokens]`, to `[append_conflict_groups]`, or to the
system conf's own hardening baseline could alter every future build without a single package being
reported as drifted. Recording the effective flags also means the recorded string now depends on
`/etc/makepkg.conf`, which is outside sysforge's config: a distro update that changes the hardening
baseline is correctly reported as drift. Both seams take the same `kernel_build` verdict
(`build_mode == "kernel" or owner_stage == "kernel"` — `owner_stage` is persisted in `build_state`
precisely so the record-time verdict is replayable), so a stage-owned kernel build never drifts
against itself.


### Rule match field semantics

All match fields optional. Omitting a field passes unconditionally.

| Field | Semantics |
|---|---|
| `pkgnames` | ANY match + glob |
| `not_pkgnames` | ALL absent + glob |
| `groups` | ALL match + glob |
| `not_groups` | ALL absent, exact |
| `depends_any` | ANY exact |
| `depends_all` | ALL exact |
| `not_depends` | ALL absent, exact |
| `makedepends_any` | ANY exact |
| `makedepends_all` | ALL exact |
| `not_makedepends` | ALL absent, exact |

`pkgnames`/`not_pkgnames` and `groups` support fnmatch glob patterns. `depends_*` and `makedepends_*` are always exact.

### Multi-rule merge and priority

Highest-priority matching rule wins outright — its profile is resolved in full via the `extends` chain. Equal priority: first occurrence wins. `priority` range: 0–99 (system), 100–199 (user, bumped on merge).

**`append_groups` is additive across all matched rules** regardless of priority. Every matched rule's `append_groups` is collected and appended to the package's final group list. This is asymmetric by design — flag resolution is winner-takes-all, groups are accumulative.

### `consumes` field

Declares which conf types a build requires (`makepkg`, `rust`, `cmake`, `meson`, `env`).

- **Default:** auto-inferred from `makedepends` via the `[consumes_inference]` table in `profiles.toml`
- **Override:** explicit `consumes` on a profile replaces the inferred value

```toml
# /etc/sysforge/profiles.toml
[consumes_inference]
cargo  = ["makepkg", "rust", "env"]
meson  = ["makepkg", "meson", "env"]
cmake  = ["makepkg", "cmake", "env"]
ninja  = ["makepkg", "env"]
```

### Conf key routing

Profile keys are routed to one of three delivery channels:

- **Toolchain env** (`toolchain` type: `CC`, `CXX`) — injected directly via subprocess env, always, regardless of `active_consumes`. makepkg does not export `CC`/`CXX` from `makepkg.conf` to child processes, so they must be present in the env that makepkg inherits at invocation time. SysForge handles this automatically — set them in a profile like any other key.
- **Conf file** (`makepkg`, `rust`, `cmake`, `meson` types) — written into the temp `makepkg.conf`. Only types in `active_consumes` are written.
- **Subprocess env** (`env` type, or any unclassified key) — injected via `subprocess.run(env=...)`. Used for `RUSTC_WRAPPER`, `CCACHE_DIR`, `SCCACHE_DIR`, `CC_LD`, `CXX_LD` (meson linker override), etc. Only delivered when `"env"` is in `active_consumes`. Keys that are present in the profile but not in `active_consumes` are logged as skipped (`[INFO][ENV]`).

Unclassified keys (not in any `CONF_KEY_MAP` type and not in `SYSFORGE_KEYS`) travel via env pass and are logged as `[WARN][ENV]`.

Toolchain keys (`CC`, `CXX`) from the **system** makepkg.conf are excluded from the emitted temp conf — makepkg sources the conf as a shell script, so any `CC`/`CXX` present would overwrite the env-injected values from the profile. Only env injection delivers toolchain keys.

### Build throttling

Build CPU/IO/memory throttling has **one home**: `primitives/build_throttle.py`. Five knobs — `nice`, `ionice`, `cpu_quota`, `jobs`, `mem_limit` — keep packages from saturating the machine. Each is a global default in `sysforge.toml [build]` (see §Config Layer) and a per-profile override: all are in `profile.SYSFORGE_KEYS`, so a profile may carry them but they are **never** written to the conf or env. `resolve_throttle(resolved_profile, config, override=None)` resolves them — a key present on the profile wins over the global default, an absent key falls back to it. The resolver never raises; every malformed value (bad niceness range, non-`N%` quota, junk size, etc.) is dropped with a warning so a typo never fails a build.

`cpu_quota` accepts either an absolute `"N%"` (100% = one core) **or** a decimal fraction of the host's total cores (`0.5` on a 16-core box → `800%`), translated against `os.cpu_count()` at resolution — the same config stays portable across machines. Only a value carrying a decimal point is read as a fraction; a bare integer without `%` stays an error, being too ambiguous against a percent. Both forms converge on a single resolved percentage, which is checked against `cpu_count*100`: a quota above the host's core count (a typo, or a config copied from a bigger box) is **kept but warned** — systemd's effective cap does the harmless clamping, so the value is honoured while the user still gets a signal (2.3.0-F7).

Two **run-scoped overrides** short-circuit that resolution, set once at CLI startup from the global flags (`cli._resolve_throttle_override` → `set_run_override`, mirroring `log.set_color_mode`) and read by `resolve_throttle` when no explicit `override` is passed — so `--no-throttle`/`--turbo` stay routed through this one home rather than threading a parameter through every makepkg call site:

- `--no-throttle` → `"bypass"`: returns a no-op throttle, ignoring config and profile.
- `--turbo` → `"boost"`: constructs `BuildThrottle(nice=_BOOST_NICE, ionice="best-effort")` directly, bypassing the `0..19` niceness clamp because a boost is an explicit request for *higher* than default priority (no CPU ceiling or job cap). `--turbo` is the stronger request and wins over `--no-throttle`. Lowering niceness may need privilege; `wrapper_argv`'s best-effort `nice` front-end simply runs at the current priority if the kernel refuses.

Two delivery channels, by mechanism:

- **Invocation wrapper** (`nice`/`ionice`/`cpu_quota`/`mem_limit`) — `wrapper_argv(throttle)` builds an argv prefix prepended to the `makepkg` command at the single subprocess chokepoint (`makepkg_invoke.invoke_makepkg`, where `cmd` is assembled). A transient `systemd-run --scope --user` is opened whenever **either** a `cpu_quota` or a `mem_limit` is set (2.3.0-F9), carrying `-p CPUQuota=N%` and/or `-p MemoryMax=<bytes>` as configured, with `nice`/`ionice` folded in front. The scope is the primary tier for both ceilings because a cgroup `CPUQuota`/`MemoryMax` is kernel-enforced hierarchically over makepkg's whole fork tree, whereas an `RLIMIT_AS` preexec leaks across the fork tree and is escapable. The scope keeps the controlling TTY, so the interactive path still gets prompts. Each tool is guarded by `shutil.which` — a missing `systemd-run` drops the hard cap (a `cpu_quota` downgrades to soft nice/ionice with a warning; a `mem_limit` falls to the `RLIMIT_AS` preexec below). Throttling is best-effort and **must never fail a build**.
- **MAKEFLAGS** (`jobs`) — `apply_jobs_to_makeflags` rewrites the `-jN` token in the `MAKEFLAGS` value at conf emit (`emit_makepkg_conf(jobs=…)`, threaded from `_run_build`), normalising to short `-jN`; appends when absent (make honours the last `-j`, so a `-j$(nproc)` baseline is still capped).
- **Child preexec / cgroup** (`mem_limit`) — a per-build memory ceiling with a **dual mechanism** so the cap is enforced by exactly one path. `_coerce_mem_limit` parses a byte count or binary-suffixed size (`"24G"`) to bytes. `_scope_owns_mem_cap(throttle)` decides which mechanism applies — true iff `mem_limit` is set **and** `systemd-run` is available, the exact condition under which `wrapper_argv` emits a `MemoryMax` scope. When the scope owns the cap, `resolve_child_mem_cap` returns `None` (an `RLIMIT_AS` set in the systemd-run *client*'s preexec would never reach the scoped payload — a child of PID 1 — so applying it would be silently ineffective *and* risk double-counting). Otherwise — the non-systemd host — `resolve_child_mem_cap(throttle)` returns those bytes and `resource_guard.make_child_preexec(cap)` (the shared `preexec_fn`, replacing the three raw `lift_for_child` sites) clamps `RLIMIT_AS` in the makepkg child. Keying on scope emission rather than on `cpu_quota` (2.3.0-F9) also closes a prior gap: a `cpu_quota` set on a host **without** `systemd-run` no longer silently drops the memory cap. The clamp is best-effort (never above the current hard limit, never raising into the child).

Don't add a parallel `nice`/`systemd-run`/`-j`/`RLIMIT_AS` path elsewhere.

### `[package_compiler_overrides]`

An auto-managed table, keyed by `pkgbase`, that records a compiler/linker swap
recovered from an interactive build-failure (see pipeline-layer → Makepkg
Wrapper → Interactive recovery menu). Each row is an inline table:

```toml
[package_compiler_overrides]
some-pkgbase = { cc = "clang", cxx = "clang++", ld = "lld" }
```

**Applied last** in `resolve_profile` — after `merge_extends` and
`_expand_toolchain` — so it wins over whatever the matched profile (and its
`toolchain` expansion) resolved. The single read home is `resolve_profile`
(via `_apply_package_compiler_override`, keyed on the package's `pkgbase`,
falling back to `pkgname` only when no `pkgbase` field is present). `cc`/`cxx`
set `CC`/`CXX` directly; `ld` is **folded into `LDFLAGS`** as a `-fuse-ld=<ld>`
token (replacing any existing `-fuse-ld=` token) rather than carried as a
standalone key — linker selection is always conf-delivered through LDFLAGS, so
this keeps the override on the same delivery channel as a hand-written
profile.

The single write home is `profile_writer.write_package_compiler_override`
(line-level, comment-preserving — it never round-trips the whole document
through a TOML emitter, mirroring `packages_cmd._rewrite_packages_toml`). The
sole caller is the makepkg wrapper, persisting a successful recovery-menu
compiler swap; don't add a second writer for this table or write it from
anywhere else.

### Flag guards

`emit_makepkg_conf` runs a series of guards after profile overrides are applied but before the conf is written. Each guard detects and reconciles toolchain incompatibilities, logging at `[WARN][CONF]` (the conf module narrates its own flag adjustments; the underlying transforms stay pure in `makepkg_flags`). Guards run in this order:

1. **Linker guard** — detects the effective linker from `-fuse-ld=X` in LDFLAGS (default: `ld`/bfd). Strips lld-only flags (`--icf=*`) when the effective linker is not lld.

2. **RUSTFLAGS linker reconciliation** — if RUSTFLAGS declares `-C link-arg=-fuse-ld=X` with a different linker than LDFLAGS, overrides it to match. Handles both spaced (`-C link-arg=...`) and compact (`-Clink-arg=...`) forms. Prevents LTO link failures from mismatched linkers (e.g. mold cannot process LLVM bitcode produced with lld).

3. **GCC thin-LTO rewrite** — `-flto=thin` is clang-only. When GCC is in effect, rewrites `-flto=thin` → `-flto` in LTOFLAGS, CFLAGS, CXXFLAGS, and LDFLAGS. Falls back to system conf values when the profile doesn't override a key.

4. **GCC + lld LTO disabling** — GCC LTO produces `.gnu.lto_*` bitcode that only GNU ld/gold can process; lld cannot read it. When GCC is in effect and the effective linker is lld, LTO is disabled entirely: LTOFLAGS cleared, `-flto*` stripped from flag keys, and `lto` flipped to `!lto` in OPTIONS (prevents makepkg's `${LTOFLAGS:--flto}` fallback).

5. **Full LTO stripping** (PGO only) — strips `-flto`/`-flto=full` from CFLAGS/CXXFLAGS/LDFLAGS and clears LTOFLAGS during PGO passes.

6. **lib32 march scrub** — when `invoke_makepkg` detects a `lib32-*` build (`pkgbuild_path.parent.name.startswith("lib32-")`), `emit_makepkg_conf` strips host-CPU-specific or 64-bit-only `-march=` tokens from CFLAGS and CXXFLAGS in both profile overrides and system-conf passthrough. Stripped values: `-march=native` (resolves to the host's amd64 microarch — `znver3` on Zen 3), `-march=x86-64`, `-march=x86-64-v2`, `-march=x86-64-v3`, `-march=x86-64-v4` (microarch levels defined only for 64-bit code). Other `-march=` values (e.g. `-march=i686`) and all non-`-march` flags are preserved. Without this guard a `[profiles.bare]` lib32-* build inherits the system conf's `-march=native` unchanged, and multilib GCC then refuses the compile with a confusing "unrecognized target arch" error rather than a clear "host flag stripped for lib32" log line.

7. **lib32 PGO scrub** — for a `lib32-*` build, `emit_makepkg_conf` strips PGO profile flags (`-fprofile-use`/`-fprofile-instr-use`/`-fprofile-generate`/`-fprofile-instr-generate`, via `makepkg_flags._strip_pgo_flags`) from CFLAGS/CXXFLAGS/LDFLAGS. This runs *after* the `compiler_flags_extra` injection, so it catches the toolchain stage's injected `-fprofile-use=<store>/clang.profdata` too. The profile is trained on the x86_64 clang self-build and is discarded by an i686 (`-m32`) build (clang emits `-Wbackend-plugin "count discarded"`), so it adds nothing and must not reach the lib32 build. See pipeline-layer → *lib32 is not toolchain-managed* for why lib32 isn't built by the toolchain stage at all by default.

8. **PKGBUILD `options=()` opt-outs** — `emit_makepkg_conf` honors the parsed `globals["options"]` array (via `pkgbuild_meta.options_list_disabled`, which applies makepkg's later-wins override semantics so `options=('!lto' 'lto')` leaves `lto` enabled). Two toggles matter to the flag layer (F9):
   - **`!lto`** — the author declared LTO breaks this package (the cosmic-edit/onig mold-failure class). makepkg's own `!lto` only suppresses *its* LTOFLAGS injection; profile-baked `-flto` in CFLAGS/CXXFLAGS/LDFLAGS still reaches the compiler. So `emit_makepkg_conf` strips **every** `-flto` variant — including clang-PGO-friendly `-flto=thin` — via `makepkg_flags._strip_all_lto` (distinct from `_strip_full_lto`, which preserves thin for the PGO passes) and clears LTOFLAGS.
   - **`!buildflags`** — makepkg discards CFLAGS/CXXFLAGS/CPPFLAGS/LDFLAGS from the conf entirely, so the resolved profile flags never reach the build. `flag_drift.resolve_flag_drift` short-circuits to `STATUS_BUILDFLAGS_IGNORED` (not `STATUS_DRIFTED`), so `update`'s Phase 4.3 never false-triggers a rebuild when those flags change — a change that cannot affect the built package.

Guards 3–4 fire when any of the following is true:
- **Profile CC is GCC** — `cc_override` (CLI `--cc`) > `resolved_profile["CC"]` resolves to a non-`clang` compiler.
- **PKGBUILD hardcodes GCC (proactive)** — `pkgbuild_meta.has_hardcoded_gcc()` statically scans every PKGBUILD(5) build-time function — `prepare()`, `build()`, `check()`, `package()`, and any `package_<pkgname>()` split-package variant — for direct `gcc`/`g++` invocations, `ccache gcc`, or `CC=gcc`/`CXX=g++` assignments. Quoted forms (`export CC='gcc -m32'`, `CXX="g++"`) are handled. `verify()` is excluded — it authenticates sources, never compiles. Conservative: ignores `$CC`/`${CXX}` references, `-lgcc` library references, and comments. False is not authoritative — a Makefile checked out in `src/` may still hardcode `g++`.
- **`lib32-*` package (proactive)** — Arch's multilib has no `lib32-clang`; every `lib32-*` package compiles with 32-bit GCC by construction. `invoke_makepkg` triggers the guard whenever `pkgbuild_path.parent.name` starts with `lib32-`, even when `has_hardcoded_gcc()` returns False. The directory name (rather than parsed `pkgname`) is used because real-world `lib32-*` PKGBUILDs interpolate (`pkgname=lib32-$_basename`), which the static parser does not expand.
- **Reactive GCC fallback (post-failure retry)** — set when the previous invocation of makepkg failed with a clang-flag-rejected-by-GCC error and `_run_build` is re-entering the conf emit path. See [Toolchain-mismatch auto-retry](#toolchain-mismatch-auto-retry).

The `[WARN][FLAG]` rewrite log records which trigger fired so the cause is visible in the per-package log. The effective linker is determined by guard 1 and shared with subsequent guards.

### build()-hardcoded linker reconciliation

The conf layer writes linker flags into the generated `makepkg.conf` (guard 1 above), but it cannot reach linker flags the PKGBUILD **re-appends inside `build()`** — e.g. `RUSTFLAGS+=" -C link-arg=-fuse-ld=mold"`. These `build()`-level assignments win over what the conf layer set, silently overriding the effective linker. The patch layer closes this gap.

- **Detection** — `pkgbuild_meta.hardcoded_build_linker(parsed)` scans the parsed `build()` function body for `-fuse-ld=<name>` tokens and returns the hardcoded linker name, or `None` if none is present. Linker only — compiler assignments stay with `has_hardcoded_gcc`.
- **Effective-linker resolution** — `makepkg_flags.resolve_effective_linker(*, ld_override, profile_ldflags, system_ldflags)` is the shared definition of the effective linker consulted by **both** the conf layer (guard 1) and the patch layer. `ld_override` (CLI `--ld`) wins; then the first `-fuse-ld=` token in `profile_ldflags`, then `system_ldflags`; falls back to `"ld"` (bfd).
- **Rewrite** — `pkgbuild_patcher.patch_build_linker(path, target_linker)` rewrites every `-fuse-ld=<old>` token in the PKGBUILD's `build()` body to `-fuse-ld=<target_linker>`. Returns `{"old": …, "new": …, "count": …}` on success, `None` on no-op. Validated by the existing `validate_patched_pkgbuild` (G1 identity/deps unchanged, G2 managed `-D` — no new validator needed).
- **Wiring** — `makepkg_wrapper._maybe_patch_build_linker(path, pkgmeta, resolved_profile, ld_override)` is the sole call-site. It calls `hardcoded_build_linker`, then `resolve_effective_linker`, and only rewrites when `hardcoded != effective`. Returns the patch dict or `None`.

**Conf-vs-patch layer boundary:** the conf layer owns env flags it writes into `makepkg.conf`; the patch layer owns linker flags the PKGBUILD re-appends in `build()` — the case the conf layer structurally cannot reach (shell `+=` assignments inside a function body run after makepkg sources the conf).

Real-world trigger: `xdg-desktop-portal-cosmic-git` ships `RUSTFLAGS+=" -C link-arg=-fuse-ld=mold"` inside `build()` alongside a `# use mold ...` comment. The comment line carries no `-fuse-ld=` token so it is left intact; only the active flag line is rewritten.

---

