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
build_mode = "patched_pkgbuild"

[profiles.kernel]
extends = "bare"
build_mode = "kernel"
batch = true
makepkg_flags = ["--noconfirm", "--syncdeps", "-f", "-c"]
```

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

Build CPU/IO throttling has **one home**: `primitives/build_throttle.py`. Four knobs — `nice`, `ionice`, `cpu_quota`, `jobs` — keep packages from saturating the machine. Each is a global default in `sysforge.toml [build]` (see §Config Layer) and a per-profile override: all four are in `profile.SYSFORGE_KEYS`, so a profile may carry them but they are **never** written to the conf or env. `resolve_throttle(resolved_profile, config)` resolves them — a key present on the profile wins over the global default, an absent key falls back to it. The resolver is pure; every malformed value (bad niceness range, non-`N%` quota, etc.) is dropped with a warning so a typo never fails a build.

Two delivery channels, by mechanism:

- **Invocation wrapper** (`nice`/`ionice`/`cpu_quota`) — `wrapper_argv(throttle)` builds an argv prefix prepended to the `makepkg` command at the single subprocess chokepoint (`makepkg_invoke.invoke_makepkg`, where `cmd` is assembled). `cpu_quota` wraps the build in a transient `systemd-run --scope --user -p CPUQuota=N%` (folding `Nice=`/`IOSchedulingClass=` in) so the cgroup ceiling applies; the scope keeps the controlling TTY, so the interactive path still gets prompts. Each tool is guarded by `shutil.which` — a missing `systemd-run`/`nice`/`ionice` drops just that piece (a missing `systemd-run` downgrades a hard cap to soft nice/ionice). Throttling is best-effort and **must never fail a build**.
- **MAKEFLAGS** (`jobs`) — `apply_jobs_to_makeflags` rewrites the `-jN` token in the `MAKEFLAGS` value at conf emit (`emit_makepkg_conf(jobs=…)`, threaded from `_run_build`), normalising to short `-jN`; appends when absent (make honours the last `-j`, so a `-j$(nproc)` baseline is still capped).

Don't add a parallel `nice`/`systemd-run`/`-j` path elsewhere.

### Flag guards

`emit_makepkg_conf` runs a series of guards after profile overrides are applied but before the conf is written. Each guard detects and reconciles toolchain incompatibilities, logging at `[WARN][CONF]` (the conf module narrates its own flag adjustments; the underlying transforms stay pure in `makepkg_flags`). Guards run in this order:

1. **Linker guard** — detects the effective linker from `-fuse-ld=X` in LDFLAGS (default: `ld`/bfd). Strips lld-only flags (`--icf=*`) when the effective linker is not lld.

2. **RUSTFLAGS linker reconciliation** — if RUSTFLAGS declares `-C link-arg=-fuse-ld=X` with a different linker than LDFLAGS, overrides it to match. Handles both spaced (`-C link-arg=...`) and compact (`-Clink-arg=...`) forms. Prevents LTO link failures from mismatched linkers (e.g. mold cannot process LLVM bitcode produced with lld).

3. **GCC thin-LTO rewrite** — `-flto=thin` is clang-only. When GCC is in effect, rewrites `-flto=thin` → `-flto` in LTOFLAGS, CFLAGS, CXXFLAGS, and LDFLAGS. Falls back to system conf values when the profile doesn't override a key.

4. **GCC + lld LTO disabling** — GCC LTO produces `.gnu.lto_*` bitcode that only GNU ld/gold can process; lld cannot read it. When GCC is in effect and the effective linker is lld, LTO is disabled entirely: LTOFLAGS cleared, `-flto*` stripped from flag keys, and `lto` flipped to `!lto` in OPTIONS (prevents makepkg's `${LTOFLAGS:--flto}` fallback).

5. **Full LTO stripping** (PGO only) — strips `-flto`/`-flto=full` from CFLAGS/CXXFLAGS/LDFLAGS and clears LTOFLAGS during PGO passes.

6. **lib32 march scrub** — when `invoke_makepkg` detects a `lib32-*` build (`pkgbuild_path.parent.name.startswith("lib32-")`), `emit_makepkg_conf` strips host-CPU-specific or 64-bit-only `-march=` tokens from CFLAGS and CXXFLAGS in both profile overrides and system-conf passthrough. Stripped values: `-march=native` (resolves to the host's amd64 microarch — `znver3` on Zen 3), `-march=x86-64`, `-march=x86-64-v2`, `-march=x86-64-v3`, `-march=x86-64-v4` (microarch levels defined only for 64-bit code). Other `-march=` values (e.g. `-march=i686`) and all non-`-march` flags are preserved. Without this guard a `[profiles.bare]` lib32-* build inherits the system conf's `-march=native` unchanged, and multilib GCC then refuses the compile with a confusing "unrecognized target arch" error rather than a clear "host flag stripped for lib32" log line.

7. **lib32 PGO scrub** — for a `lib32-*` build, `emit_makepkg_conf` strips PGO profile flags (`-fprofile-use`/`-fprofile-instr-use`/`-fprofile-generate`/`-fprofile-instr-generate`, via `makepkg_flags._strip_pgo_flags`) from CFLAGS/CXXFLAGS/LDFLAGS. This runs *after* the `compiler_flags_extra` injection, so it catches the toolchain stage's injected `-fprofile-use=<store>/clang.profdata` too. The profile is trained on the x86_64 clang self-build and is discarded by an i686 (`-m32`) build (clang emits `-Wbackend-plugin "count discarded"`), so it adds nothing and must not reach the lib32 build. See pipeline-layer → *lib32 is not toolchain-managed* for why lib32 isn't built by the toolchain stage at all by default.

Guards 3–4 fire when any of the following is true:
- **Profile CC is GCC** — `cc_override` (CLI `--cc`) > `resolved_profile["CC"]` resolves to a non-`clang` compiler.
- **PKGBUILD hardcodes GCC (proactive)** — `pkgbuild_meta.has_hardcoded_gcc()` statically scans every PKGBUILD(5) build-time function — `prepare()`, `build()`, `check()`, `package()`, and any `package_<pkgname>()` split-package variant — for direct `gcc`/`g++` invocations, `ccache gcc`, or `CC=gcc`/`CXX=g++` assignments. Quoted forms (`export CC='gcc -m32'`, `CXX="g++"`) are handled. `verify()` is excluded — it authenticates sources, never compiles. Conservative: ignores `$CC`/`${CXX}` references, `-lgcc` library references, and comments. False is not authoritative — a Makefile checked out in `src/` may still hardcode `g++`.
- **`lib32-*` package (proactive)** — Arch's multilib has no `lib32-clang`; every `lib32-*` package compiles with 32-bit GCC by construction. `invoke_makepkg` triggers the guard whenever `pkgbuild_path.parent.name` starts with `lib32-`, even when `has_hardcoded_gcc()` returns False. The directory name (rather than parsed `pkgname`) is used because real-world `lib32-*` PKGBUILDs interpolate (`pkgname=lib32-$_basename`), which the static parser does not expand.
- **Reactive GCC fallback (post-failure retry)** — set when the previous invocation of makepkg failed with a clang-flag-rejected-by-GCC error and `_run_build` is re-entering the conf emit path. See [Toolchain-mismatch auto-retry](#toolchain-mismatch-auto-retry).

The `[WARN][FLAG]` rewrite log records which trigger fired so the cause is visible in the per-package log. The effective linker is determined by guard 1 and shared with subsequent guards.

---

