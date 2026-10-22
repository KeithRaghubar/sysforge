## Cache Management

### ccache and sccache

Configured via a `[cache]` table in `profiles.toml`:

```toml
[cache]
ccache  = "auto"   # auto | enabled | disabled
sccache = "auto"
```

`auto` — use if installed, skip silently if not.

Compiler invocation uses explicit absolute paths rather than ccache's symlink shim (incompatible with makepkg prepending `$srcdir` to `PATH`):

```
CC="ccache /usr/bin/clang"
CXX="ccache /usr/bin/clang++"
```

sccache wraps Rust via `RUSTC_WRAPPER=sccache` (env pass). `CARGO_INCREMENTAL=0` set globally for all Rust builds — incremental fingerprinting is unreliable with managed flags.

`cache = false` in `packages.toml` disables both caches for a specific package. Required for all PGO stages.

### Cache reporting

`[CACHE]` log tag emits passive monitoring lines at `[INFO]` level (visible at `-vv`):

- **Per-build:** ccache and sccache hit/miss deltas, bracketing each `makepkg` invocation. Hit rate % shown when compilations occur; "no compilations recorded" if delta is zero.
- **System probes (once per run):** ld.so cache mtime, pacman cache file count + size, ThinLTO cache dir size (extracted from `--thinlto-cache-dir=` in profile LDFLAGS).

`--cache-report` on both `build` and `pipeline` subcommands prints a structured per-package and totals summary to stderr at end of run, regardless of verbosity. A build run in the build sandbox reads `n/a (sandboxed, host cache not used)` and is left out of the totals (`3.5.0-B99`). This is the only user-visible output that ignores verbosity gating. The report is emitted through `log.ui` (not raw `print`), so it is captured by the unified run-log and its divider passes the Unicode gate like every other renderer.

Cache probe uses `ccache --print-stats --format=tab` (ccache ≥ 4.0, standard on Arch; the size key is `cache_size_kibibyte`, with `cache_size_bytes` read when present) and `sccache --show-stats` (an idle server prints no `Cache size`, so the size is then omitted). Both are skipped if the binary is absent.

### Toolchain fingerprint tracking

There is no cache-invalidation setting. Each source build records the toolchain that built it in `build_state.toml` (`toolchain_variant`, `toolchain_fingerprint`), and `sysforge update` reports a package built by a different or rebuilt toolchain as toolchain drift (§Drift detection), rebuilding it with `--rebuild-on-toolchain-drift`. The compile caches need nothing from sysforge here: ccache and sccache key their entries on the compiler itself, so a toolchain change misses rather than reusing objects from the old compiler.

---

