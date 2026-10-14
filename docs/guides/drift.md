# Drift and Rebuilds with SysForge

What `sysforge update` rebuilds on its own, what it only reports, and how to act on what it
reports. "Drift" means a package you built is no longer what a fresh build would produce: a
newer version upstream, a different compiler, or different flags.

For the internals see [DESIGN.md](../../DESIGN.md) (§Drift detection, §`update`). This guide
only describes how to use it.

---

## Three kinds of drift

| Kind | What changed | What `update` does |
| --- | --- | --- |
| **Version** | The PKGBUILD's version is newer than the installed one | Rebuilds it |
| **Toolchain** | The package was built with a different compiler than the active one | Reports it |
| **Flag** | The package's flags now resolve differently than when it was built | Reports it |

Version drift is the ordinary "there is an update" case and needs nothing from you. The other
two are reported on a single line at the end of the version check and left alone unless you
ask for a rebuild. A third kind, **ABI drift** (an installed program linked against a library
version the system no longer has), is the job of `sysforge doctor pkg --all`, not `update`.

---

## I changed a profile: what rebuilds?

Nothing, until you say so. An edit to `profiles.toml` can change the flags of every package
you build from source, so an unattended `update` that rebuilt on flag drift could start a
full-system rebuild from one line. Instead the next `update` prints:

```text
flag drift: 12 package(s) resolve to different flags than when built: mesa, llvm, ffmpeg (+9 more).
Pass --rebuild-on-flag-drift to rebuild, or --explain-drift to list.
```

See exactly what changed, without building or downloading anything:

```bash
sysforge update --explain-drift
```

`--explain-drift` lists both axes and exits. Each flag-drifted package gets a per-key diff
(`+` for a flag key that is new, `-` for one that is gone, `old → new` for one that changed):

```text
[SYSFORGE] No toolchain drift. Active variant: pgo_llvm.
[SYSFORGE] 2 package(s) resolve to different flags than when built:
  mesa
  CFLAGS: '-O2 -march=native ...' → '-O3 -march=native ...'
  ffmpeg
  CFLAGS: '-O2 -march=native ...' → '-O3 -march=native ...'
```

The report never touches the network: it skips the source sync and every version lookup, because
both axes compare your local PKGBUILD, config and toolchain against what `build_state.toml`
recorded.

When the diff is what you meant, rebuild the drifted packages:

```bash
sysforge update --rebuild-on-flag-drift
```

To rebuild a single package instead, use `sysforge build <pkg>`.

---

## What counts as flag drift

Flag drift compares the **effective** flags (what makepkg would actually be handed) against
the effective flags recorded at build time. So beyond a profile edit, all of these drift a
package:

- a `[[rules]]` change that matches the package to a different profile;
- a change to `[preserved_system_tokens]` or `[append_conflict_groups]` in `profiles.toml`;
- a change to the hardening baseline in `/etc/makepkg.conf`;
- turning the build sandbox (`[security] sandbox_builds`, or a profile's `sandbox_builds`) on or
  off, since a sandboxed build links against different dependency versions. This one shows up
  as its own diff line: `sandboxed: False → True`.
- a change to a build input that never passes through a profile. Each shows up as its own
  diff line, named by the input:
  - `python`: the Python minor version for a package that depends on Python at runtime, so
    `[build] python` in `sysforge.toml` or a distro Python upgrade (`3.13 → 3.14`). A patch
    release such as 3.13.1 → 3.13.2 does not count;
  - `mesa_gallium` / `mesa_vulkan`: mesa's driver lists, with `[mesa] filter_drivers` on, after
    you change `[mesa] gallium`/`vulkan` or swap the GPU they are detected from;
  - `llvm_targets`: LLVM's target list, after a `toolchain.toml [llvm] targets` change or a
    GPU swap. LLVM belongs to the toolchain stage, so this is reported with a
    `sysforge run toolchain` hint rather than rebuilt by `update`;
  - `pgo_profile`: the PGO profile an `update` rebuild would reuse from the package's store, so
    one that appeared (`+pgo_profile`) or was deleted (`-pgo_profile`) since the build. A package
    built with `--pgo=use` is checked for these inputs too, though not for its profile flags.

  A package built before sysforge recorded its build inputs shows no build-input drift until
  its next rebuild.

Two cases never show flag drift:

- A PKGBUILD with `options=('!buildflags')`: makepkg ignores the configured flags, so a flag
  change cannot affect it. The sandbox and build-input lines above still apply.
- A package built before sysforge recorded flags. Rebuild it once and it is tracked from then on.

---

## Toolchain drift

Every source build records which toolchain built it (`gcc`, `stock_llvm` or `pgo_llvm`) and a
fingerprint of the compiler. A package drifts when either one differs from the active
toolchain:

- **different variant:** you switched `toolchain.toml` from `compiler = "gcc"` to LLVM, and
  older packages still carry `gcc`;
- **same variant, rebuilt toolchain:** `sysforge run toolchain` rebuilt clang (fresh PGO
  profile, same version), so packages built with the old binary differ from what you would
  get now.

```text
toolchain drift: 40 package(s) built under a different toolchain than active (pgo_llvm): mesa (gcc), ...
Pass --rebuild-on-toolchain-drift to rebuild, or --explain-drift to list.
```

This is off by default for a different reason than flag drift: most C and C++ packages gain
nothing measurable from being rebuilt by a newer clang. Where it does pay off is libraries
that link libLLVM (mesa is the usual one), so rebuilding those by name is often better than a
blanket rebuild:

```bash
sysforge build mesa
```

How the fingerprint is taken is set by `[toolchain] drift_detect` in `toolchain.toml`.
`"fingerprint"` (the default) is fast. Reinstalling an identical compiler package can flag
everything once, and the next rebuild clears it. `"content_hash"` hashes libLLVM itself, which
is exact but reads a file of more than 100 MB on every check.

Packages installed straight from the repos have no recorded toolchain and never drift. Nothing
drifts until the toolchain stage has run at least once.

---

## Rebuilding on drift

| Flag | Rebuilds |
| --- | --- |
| `--rebuild-on-flag-drift` | flag-drifted packages |
| `--rebuild-on-toolchain-drift` | toolchain-drifted packages |
| `--rebuild-on-drift` | both |

A drift rebuild runs at an unchanged version, so sysforge forces makepkg to build even though
a package with that version already sits in `PKGDEST`.

To make one of these your standing default, set it in the `[update]` table of `sysforge.toml`.
A flag on the command line still wins.

```toml
[update]
rebuild_on_drift = false
rebuild_on_toolchain_drift = false
rebuild_on_flag_drift = true
```

---

## Packages `update` reports but cannot rebuild

`update` checks flag drift for every source-built package in `build_state.toml`, including
packages outside this run:

- **stage-owned packages:** the kernel built by `sysforge run kernel` and the LLVM packages
  built by `sysforge run toolchain`. `update` never rebuilds these;
- **packages excluded by name**, when you ran `sysforge update <pkg> ...`.

These show up in the drift line and in `--explain-drift` like any other package. (The kernel
takes its optimization from Kconfig, not from `CFLAGS`, so a profile's compiler flags never
drift it.) Under a
rebuild flag they are listed as `not queued`, each with the command that would rebuild it:

```text
flag drift: 3 package(s) resolve to different flags than when built.
rebuilding (--rebuild-on-flag-drift): mesa, ffmpeg; not queued: linux-sysforge (`sysforge run kernel`)
```

---

## Quick reference

| I want to... | Run |
| --- | --- |
| see all drift, touching nothing | `sysforge update --explain-drift` |
| rebuild what my profile change affected | `sysforge update --rebuild-on-flag-drift` |
| rebuild after switching compilers | `sysforge update --rebuild-on-toolchain-drift` |
| rebuild one package | `sysforge build <pkg>` |
| rebuild a drifted kernel or LLVM package | the `sysforge run` command the drift line names |
| check for broken library links | `sysforge doctor pkg --all` |
