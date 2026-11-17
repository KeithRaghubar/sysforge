## Cache Management

### ccache and sccache

sysforge does not switch the compiler caches on. ccache runs when makepkg's own `BUILDENV` has `ccache` (in `makepkg.conf`) or a ccache directory is first on `PATH`; sccache runs when the environment sets `RUSTC_WRAPPER=sccache`. There is no sysforge setting for either, so a cache configured for plain makepkg works the same under sysforge.

What sysforge adds is the opt-out and the containment:

- **Per package:** `cache = false` in a `packages.toml` entry builds that package with `CCACHE_DISABLE=1` and an empty `RUSTC_WRAPPER` (`makepkg_wrapper._with_cache_opt_out`, `3.5.0-B139`). `CCACHE_DISABLE` reaches ccache however it was invoked, and the empty wrapper wins over one the shell or the profile exports.
- **PGO training:** the toolchain stage's Pass 3 sets `CCACHE_DISABLE=1` and `SCCACHE_DISABLE=1` itself, so a cache cannot answer for the instrumented compiler and leave the profile empty (§Toolchain stage).
- **Build sandbox:** the clean chroot carries no cache, so the sandbox disables the `ccache`/`distcc` `BUILDENV` options in its `makepkg.conf` and does not export `RUSTC_WRAPPER`, `CCACHE_DIR` or `SCCACHE_DIR` into it (`build_sandbox._HOST_ONLY_BUILDENV`, `_ENV_EXPORT_DENY`, `3.2.0-B2`). A missing accelerator is a hard makepkg error, and `makechrootpkg -c` resets the chroot each build, so a cache in there would never hit.

### Cache reporting

`[CACHE]` log tag emits passive monitoring lines at `[INFO]` level (visible at `-vv`):

- **Per-build:** ccache and sccache hit/miss deltas, bracketing each `makepkg` invocation. Hit rate % shown when compilations occur; "no compilations recorded" if delta is zero.
- **System probes (once per run):** ld.so cache mtime, pacman cache file count + size, ThinLTO cache dir size (extracted from `--thinlto-cache-dir=` in profile LDFLAGS).

`--cache-report` on both `build` and `pipeline` subcommands prints a structured per-package and totals summary to stderr at end of run, regardless of verbosity. A build run in the build sandbox reads `n/a (sandboxed, host cache not used)` and is left out of the totals (`3.5.0-B99`). This is the only user-visible output that ignores verbosity gating. The report is emitted through `log.ui` (not raw `print`), so it is captured by the unified run-log and its divider passes the Unicode gate like every other renderer.

Cache probe uses `ccache --print-stats --format=tab` (ccache ≥ 4.0, standard on Arch; the size key is `cache_size_kibibyte`, with `cache_size_bytes` read when present) and `sccache --show-stats` (an idle server prints no `Cache size`, so the size is then omitted). Both are skipped if the binary is absent.

### Toolchain fingerprint tracking

There is no cache-invalidation setting. Each source build records the toolchain that built it in `build_state.toml` (`toolchain_variant`, `toolchain_fingerprint`), and `sysforge update` reports a package built by a different or rebuilt toolchain as toolchain drift (§Drift detection), rebuilding it with `--rebuild-on-toolchain-drift`. The compile caches need nothing from sysforge here: ccache and sccache key their entries on the compiler itself, so a toolchain change misses rather than reusing objects from the old compiler.

---

