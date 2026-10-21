# Profile-Guided Optimization with SysForge

A practical guide for people who have never done PGO before. It covers *what to run* and,
more importantly, *what to do in between*, the part no command can do for you.

For the internals (how flags are injected, how renames work, why each gate exists) see
[DESIGN.md](../../DESIGN.md). This guide only describes how to use it.

---

## How it works

Every method here follows the same three steps: **record** (build or run something that
collects data), **exercise** (use it the way you normally do; *this step is yours, and it
decides the result*), and **use** (rebuild from what was collected). Packages and the LLVM
toolchain use instrumentation, counters compiled into a special build; the kernel uses
sampling with `perf`.

How the methods differ, what each changes in the program, why the workload matters so much,
and when none of them pay off are explained in
[Profile-guided and post-link optimization](../concepts/pgo-and-post-link.md). This guide
covers what to run.

---

## Which method do I want?

| Goal | Method | Command | Worth it? |
|---|---|---|---|
| Faster graphics driver (shader compile, draw-call overhead) | Package PGO | `sysforge build mesa --pgo=record` / `--pgo=use` | Yes, **if your GPU uses Mesa** (see below) |
| Faster compiles for *everything* you build | Toolchain PGO | `sysforge run toolchain` with `compiler = "llvm"`, `pgo = true` | Yes, if you build a lot from source; costs hours once |
| Faster kernel (syscalls, scheduling, I/O paths) | Kernel AutoFDO (+ Propeller) | `sysforge run kernel --autofdo=record\|capture\|use`, then optionally again with `--propeller` | Modest gains; two rounds, each spans a reboot |
| Some other hot, long-lived library | Package PGO | `sysforge build <pkg> --pgo=…` | Rarely (SysForge warns unless you allow-list it) |
| Post-link layout of clang | BOLT | `toolchain.toml [bolt]` | Experimental; adds time to the toolchain build |

All of these require the **LLVM toolchain** (clang + `llvm-profdata`). Under `toolchain = gcc`
the PGO commands refuse with a hint before doing any work.

---

## Before you start

- **LLVM toolchain.** Install `clang`, `lld`, `llvm`, `compiler-rt`, and set
  `toolchain = "llvm"` in a profile or `[defaults]` (`profiles.toml`).
- **Store permissions.** Profiles live under `/var/cache/sysforge/` (owned `root:sysforge`,
  group-writable). An instrumented library writes its data *as whatever user runs the
  program*, so your user must be in the `sysforge` group: `id -nG | grep -w sysforge`.
  If it isn't, data is silently dropped.
- **Disable compiler caches for PGO packages.** Add `cache = false` to the package's
  `[[package]]` entry in `packages.toml` so ccache/sccache never mixes profiled and
  unprofiled objects.
- **Disk and time.** Every record or use pass is a full clean build (SysForge forces this,
  since stale objects from a differently-instrumented build would corrupt the profile). Toolchain
  PGO in particular needs ~40 GiB free and several hours.

---

## Package PGO (mesa and others)

### Step 0: check that the package is actually on your hot path

For mesa this matters a lot. Games only benefit if they render through a **Mesa driver**:

```sh
vulkaninfo --summary | grep -i driverName   # radv / anv / nvk / … = Mesa
glxinfo -B | grep -i "OpenGL vendor"        # "AMD" / "Intel" / "Mesa" = Mesa
```

If you see `NVIDIA` (the proprietary driver), games go through NVIDIA's own driver, and Mesa
PGO will do essentially nothing for them. Skip it.

### Step 1: record

```sh
sysforge build mesa --pgo=record
```

This builds and installs an *instrumented* mesa under its normal name. From now on, every
program that loads mesa writes a `.profraw` file into the store
(`/var/cache/sysforge/pgo/<pkgbase>`, so `/var/cache/sysforge/pgo/mesa` for mesa). No
environment variables needed.

**Verify it's collecting** after the first session. Don't find out three evenings later:

```sh
ls -lt /var/cache/sysforge/pgo/mesa | head      # fresh .profraw files?
sysforge state profiles                          # store listed, size growing?
```

### Step 2: exercise it (the important part)

General rules that apply to any package:

- **Run what you actually run.** Synthetic benchmarks exercise a narrow slice of code.
- **Cover breadth, not just depth.** Each distinct way you use the program lights up
  different code. A missing path gets treated as cold.
- **Keep sessions roughly balanced.** Counts scale with time; one 4-hour session of a
  single workload drowns out five 20-minute ones.
- **Don't leave the instrumented build installed forever.** It's slower and keeps writing.
  A few sessions over a day or two is plenty.

**Mesa for gaming, specifically:**

1. **Make the shader compiler work.** Compile stutter is where Mesa PGO is most noticeable,
   and a warm shader cache means the compiler barely runs. Before recording, clear:
   - `~/.cache/mesa_shader_cache*`
   - Steam's per-game caches under `<library>/steamapps/shadercache/<appid>/`

   Steam's *Shader Pre-Caching → background processing of Vulkan shaders* is then a
   useful workload by itself: it replays each game's real pipelines through the compiler.
2. **Cover every graphics API you use.** One title each for D3D9–11 (via DXVK), D3D12 (via
   vkd3d-proton), native Vulkan, and OpenGL if you play any. Each takes different paths
   through the driver.
3. **Play normally.** Loading screens, traversal, combat, menus: that's the real hot set.
4. **Proton/Steam Runtime games run in a container** that may not see `/var/cache`. If no
   `.profraw` appears after playing a Proton game, add this to the game's launch options
   during the record phase:

   ```
   PRESSURE_VESSEL_FILESYSTEMS_RW=/var/cache/sysforge/pgo/mesa %command%
   ```

   (Flatpak Steam needs an equivalent `--filesystem=` override.) Remove it afterwards.
5. **32-bit games contribute nothing.** lib32-mesa is never instrumented.

### Step 3: use

```sh
sysforge build mesa --pgo=use
```

This merges every collected `.profraw` (plus any earlier profile) into
`mesa.profdata`, deletes the consumed raw files, and builds an optimized
`mesa-sysforge` that replaces stock mesa. Your GPU apps should now run on the optimized
build. Check with `pacman -Q mesa-sysforge`.

### Keeping it, refreshing it, removing it

- **It survives updates automatically.** Every later rebuild (`sysforge update`, plain
  `sysforge build mesa`) reapplies the existing profile, and prints a notice like
  *"collected against X, building Y"* as it ages.
- **Profiles go stale gradually.** Code that changed upstream loses its profile data. A
  rough rule: refresh after a major Mesa release, or when the notice shows a big version gap.
  Refreshing is just Step 1 → 3 again; the new data is **merged** with the old.
- **Start over** (e.g. after changing GPU or driver): `sysforge state profiles --delete
  pgo/mesa`, then record again.
- **Stop using PGO**: delete the store, then `sysforge revert-to-stock mesa` (or rebuild normally).

**Is the profile stale?** Every rebuild that reuses a profile prints a line like

    PGO profile mesa: 412 of 18,210 profiled functions no longer match the source (2.3%)

A function "no longer matches" when its code changed since you recorded the profile; the compiler
ignores the profile for that function and optimizes it as if unprofiled. A few percent after an
upstream release is normal. Once the share reaches `[pgo] skew_warn_ratio` in `sysforge.toml`
(default `0.5`, i.e. most of the profile no longer applies), the line says the profile is stale,
the `update` summary repeats it, and it is time to re-record:
`sysforge build mesa --pgo=record`, exercise it, then `--pgo=use`. Builds run with
`--interactive` can't measure staleness and say so at `-vv`.

### Other packages

`--pgo` works on any package, but the doubled build plus manual workload rarely pays off.
SysForge prints a *"not recommended"* warning for anything other than mesa. If you've
decided a package is worth it (long-running, CPU-bound, used constantly), allow-list it
in `sysforge.toml`:

```toml
[pgo]
allow = ["mesa", "your-package"]
```

---

## Toolchain PGO (optimized clang)

A PGO-optimized clang compiles everything faster. LLVM's own
[PGO build guide](https://llvm.org/docs/HowToBuildWithPGO.html) says users report up to 20%
less compile time; what you get depends on your machine and what you compile, which
`sysforge bench toolchain` (below) measures. That benefits every package you build
afterwards, including kernels and mesa.

Enable it in `toolchain.toml`:

```toml
enabled  = true
compiler = "llvm"
pgo      = true
```

Then run:

```sh
sysforge run toolchain
```

Unlike package PGO, **the training workload is automatic**: SysForge builds an
instrumented clang and uses it to compile LLVM itself (pass 3), then rebuilds the final
toolchain from that profile (pass 4). You don't have to do anything in between. Why a
compiler is built in passes, and why the installed one is only ever replaced as a whole, is
explained in [Bootstrapping an optimized compiler](../concepts/compiler-bootstrap.md).

The progress line at the bottom of the terminal says where the run is, for example
`toolchain 5/8 · optimize · llvm · ninja 74% · 1h34m · ~40m left`: the step out of the
steps this run takes (preflight, instrument, bootstrap, train, optimize, then the pre-install
audit, install and verification), the package being built, how far its build has got, the
time since the stage started and an estimate of what is left. The estimate comes from how
long each step took on your earlier runs, so it appears from the second run on. Every line of
the run log (`sysforge log --verb run-toolchain`) also starts with the time it was written, so a finished run
shows how long each step took.

The pre-install audit checks the new libLLVM against the programs already using it, such as
mesa's drivers. A symbol one of them needs and the new build no longer exports either stops
the run before anything is installed or queues that program for a rebuild after the install.
[Shared libraries and ABI](../concepts/shared-libraries-and-abi.md) explains how a
rebuilt library can break its users without changing its version.

Best practices:

- **Broaden the training.** LLVM's own C++ build under-represents C and graphics code. Add
  mesa to the training corpus so clang is also tuned for it:
  ```toml
  [packages]
  training_corpus = ["llvm", "mesa"]
  ```
- **Run it interactively the first time.** PGO asks for confirmation at several points
  (reusing an existing profile, purging staging, suspicious profile sizes). For unattended
  runs, pass `--yes`.
- **The profile is reused** across same-major LLVM rebuilds. After an LLVM *major* version
  bump it is incompatible and gets regenerated. Force a fresh one any time with
  `--rebuild-profdata`.
- **If a late package fails**, rerun with `--reuse-built` to skip re-optimizing the packages
  that already succeeded.
- **Other LLVM packages are replaced too.** The repos build clang, lld, lldb, libclc and the
  rest from one `llvm` package, so the toolchain build produces all of them. SysForge
  installs the ones you configured plus any of the others you already have (lldb, for
  example), so they all match the new libLLVM. Ones you never installed are left out.

### Did it help?

`sysforge bench toolchain` compiles the same fixed workload with the stock repo clang and
with your installed clang, alternating between them, and reports each one's median time,
its spread and the stock/sysforge ratio. A ratio above 1.00x means your build compiled
the workload faster.

```sh
sysforge bench toolchain            # 5 interleaved runs per compiler
sysforge bench toolchain --runs 10  # tighter median, longer run
sysforge bench toolchain --offline  # use the stock package only from the pacman cache
```

The bench gives no pass or fail verdict, because what counts as a worthwhile gain depends
on your machine and on what you compile. To judge a result, or to time something the bench
doesn't cover, see [Measuring performance honestly](../concepts/measuring-performance.md). It installs nothing and needs no sudo: the stock
clang is unpacked into a temporary directory. If the stock package isn't in the pacman
cache, or the cached copy fails its checksum, it is downloaded as your user from your
configured mirrors (trying each in turn, as pacman does) and checked against the sync
database's checksum before it is used. If no mirror has that package any more, because
your sync database is older than the repo and the package has since been rebuilt, the
bench downloads a fresh copy of the sync database into its temporary directory (as your
user, with `fakeroot`; your system's database is left alone) and compares against the
build the repo carries now. If that fails too, it tells you to run `sudo pacman -Syu`.
`--offline` never does this. Only the compiler and the libraries it loads are
unpacked, about 260 MB in `$TMPDIR` for the length of the run, and a downloaded package
(about 105 MB for both) is deleted once it is unpacked. On Arch `$TMPDIR` is usually `/tmp`,
a RAM-backed tmpfs; set `TMPDIR` to a disk directory if that is too small. While it runs, the progress line names the step: fetching each stock
package, unpacking it, the warm-up, then each timed round (`round 3/5`).

By default the workload is a pair of built-in source files that no profile is trained on.
To measure on your own code, list files or directories in `toolchain.toml`:

```toml
[bench]
corpus = ["~/bench-sources"]
```

Each file is compiled on its own with only `-O2 -c` (and `-std=c++17` for C++), with no
include paths or defines. Point the corpus at sources that build that way, such as
single-file programs or preprocessed sources (`clang -E` output saved as `.c`/`.cpp`),
rather than at a project's `src/` directory, whose files usually need its headers.

After `sysforge run toolchain` builds and installs a new clang, it prints a reminder to run
the bench. To have it run the bench by itself at that point instead, opt in:

```toml
[bench]
after_build = true
```

It is off by default because right after a long build the machine is at its busiest, which
makes for a noisier measurement, and the bench may need to download the stock packages. If
the bench can't run then, the toolchain install is not affected: you get a line saying why,
and you can run `sysforge bench toolchain` later.

The result is recorded, and `sysforge state profiles` shows it in the GAIN column next to
the toolchain profile. It is marked `(stale)` once clang is upgraded or rebuilt (even at
the same version, for example with `--rebuild-profdata`) or the workload changes; run the
bench again to refresh it. If you ran the bench with `--state-dir`, pass the same
`--state-dir` to `state profiles` to see it.

If the stock and installed clang versions differ, the report says so, because the ratio
then includes upstream changes as well as the optimization. Across a major-version gap it
shows no ratio at all.

---

## Kernel AutoFDO / Propeller

The kernel uses **sampling**, not instrumentation: you boot a profiling kernel, sample it
with `perf` while you work, convert the samples into a profile, and rebuild from it. The
sampling happens on the *running* profiling kernel, so every round spans a reboot.

There are two rounds:

- **Round 1, AutoFDO.** This one is required. It produces `kernel.afdo` and an optimized
  kernel, `<name>-fdo`.
- **Round 2, Propeller.** This one is optional. It adds basic-block layout on top of round 1
  and produces `<name>-propeller`. Its profile has to be sampled from a kernel that already
  applies round 1's profile, which is why it is a separate round. Round 2 freezes ("pins") a
  copy of round 1's profile, and uses that same copy for its profiling kernel and its final
  build.

Every kernel installs **beside** your plain kernel and never replaces it, so the plain one
is always there to fall back to in the boot menu. The names come from `kernel.toml`
`pkgname`: sysforge adds `-sysforge` if the name doesn't already end in it, then one role
suffix. With the shipped `pkgname = "linux-sysforge"`:

| Kernel | Installed as |
|---|---|
| Plain (`sysforge run kernel`) | `linux-sysforge` |
| Profiling (`record`, both rounds) | `linux-sysforge-profiling` |
| AutoFDO-optimized (round 1 `use`) | `linux-sysforge-fdo` |
| AutoFDO + Propeller (round 2 `use`) | `linux-sysforge-propeller` |

For example, `pkgname = "linux-mine"` gives `linux-mine-sysforge-fdo`.

### Prerequisites

- **The LLVM toolchain for the kernel** (`kernel.toml` `compiler = "llvm"`, or a clang
  toolchain). On gcc, kernel FDO refuses before it builds anything.
- **`perf`**: `sudo pacman -S perf`.
- **`llvm-profgen`**, the round 1 converter. It ships with the `llvm` package, so it matches
  the clang that uses the profile.
- **`generate_propeller_profiles`**, the round 2 converter (round 2 only). Build it from the
  AutoFDO tools (github.com/google/autofdo).
- **Keep the build tree.** Converting needs the profiling kernel's `vmlinux` from its build
  directory (usually under `~/builds`). Don't wipe it between `record` and the conversion.
- **CPU support.** Intel uses LBR, which is well supported. AMD needs branch sampling, which
  Zen 4 and newer have (`amd_lbr_v2` in `/proc/cpuinfo`) and only some Zen 3 parts have (`brs`).
  Client Zen 3 CPUs such as the Ryzen 5000 series have neither, so kernel FDO can't run on them.
  AMD support is **experimental**. The warning `record` prints at `-v` says which applies to you.

`capture` checks that the tools are on your `PATH` and refuses if one is missing.

### Round 1: AutoFDO

```sh
sysforge run kernel --autofdo=record     # build + install linux-sysforge-profiling
# reboot into linux-sysforge-profiling
sysforge run kernel --autofdo=capture    # prints the commands below; run them yourself
#   ... perf record, chown, llvm-profgen (as printed)
sysforge run kernel --autofdo=use        # build + install linux-sysforge-fdo
# reboot into linux-sysforge-fdo
```

| Step | Booted kernel | Reboot after? |
|---|---|---|
| `record` | any | yes, into `-profiling` |
| `capture` + `perf record` | must be `-profiling` (`capture` refuses otherwise) | no |
| convert (`llvm-profgen`) | any (the build tree must still exist) | no |
| `use` | any | yes, into `-fdo` |

`capture` builds nothing. It finds the `vmlinux` that matches the running kernel exactly and
prints three things to run, with every path filled in: a `sudo perf record … -- sleep 300`,
a `sudo chown` so your user can read the sample file, and the `llvm-profgen` command that
writes `kernel.afdo`. Copy them as printed. To boot the profiling kernel once without
changing your default, see
[Booting a kernel once](boot-entries.md#booting-a-kernel-once).

### Round 2: Propeller (optional)

Round 2 needs round 1's `kernel.afdo`, so start it once round 1's conversion has run:

```sh
sysforge run kernel --autofdo=record --propeller    # pins kernel.afdo, rebuilds linux-sysforge-profiling with it
# reboot into linux-sysforge-profiling
sysforge run kernel --autofdo=capture --propeller   # prints perf + generate_propeller_profiles
#   ... run the printed commands
sysforge run kernel --autofdo=use --propeller       # build + install linux-sysforge-propeller
# reboot into linux-sysforge-propeller
```

| Step | Booted kernel | Reboot after? |
|---|---|---|
| `record --propeller` | any | yes, into `-profiling` |
| `capture --propeller` + `perf record` | must be `-profiling` (`capture` refuses otherwise) | no |
| convert (`generate_propeller_profiles`) | any (the build tree must still exist) | no |
| `use --propeller` | any | yes, into `-propeller` |

Pass `--propeller` to all three round 2 steps. Round 2 reuses the `-profiling` name, so its
profiling kernel replaces round 1's.

A Propeller profile only matches the exact kernel version it was sampled on. If the kernel
`pkgver` has changed since round 2's `record`, `--autofdo=use --propeller` refuses: redo
round 2, or build AutoFDO only with `--autofdo=use`. Collecting a new round 1 after round 2
doesn't break round 2, because round 2 uses its pinned copy. Redo round 2 when you want it to
build on the new profile.

### Capturing a good profile

The printed `perf record` samples the **whole system** for 5 minutes. What it records is
whatever the machine is doing during that window, so:

- **Start your workload first, then run `perf record`.** Games, compiles, browsing,
  whatever this machine actually does.
- **Match your real mix.** Each workload's share of the window becomes its share of the
  profile. If you play games two hours a day and compile ten minutes a day, spend most of
  the window gaming.
- **Longer is harmless.** Idle time adds almost nothing to the profile. Ctrl-C ends the
  capture early and keeps what was collected. To sample for longer, change the `300` in the
  `sleep 300` that `capture` printed.
- **Re-running replaces, it doesn't add.** A second `perf record` overwrites `perf.data`
  (the old file is kept as `perf.data.old`), and the runs are not merged. Capture everything
  you want in one run.

**Check you collected enough.** After round 1's conversion, look at the hottest functions
(the store is `/var/cache/sysforge/autofdo/<pkgname>/` by default). `llvm-profdata` prints
every function first and the top-20 table last, so keep only the end:

```sh
llvm-profdata show --sample --topn=20 /var/cache/sysforge/autofdo/linux-sysforge/kernel.afdo | tail -n 25
```

The list should show kernel code your workload exercises: filesystem, networking, the
scheduler, your GPU driver's ioctls. If it is mostly idle loops and timer code, the machine
wasn't busy enough during the capture. Capture again under real load, then re-run the
conversion.

### Keeping it applied

`--autofdo=use` builds the optimized kernel once. To have **every** plain
`sysforge run kernel` build it, including the rebuild after a kernel update, set the mode in
`kernel.toml`:

```toml
[fdo]
mode = "autofdo"      # "off" (default) | "autofdo" | "propeller"
```

If your `kernel.toml` predates this setting, add the block yourself just above the
`[[kconfig]]` section: a table header captures every setting written below it, so it must
come after all the top-level keys.
An explicit `--autofdo=…` on the command line always wins over `mode`. `record` and
`capture` are never implied, because they need a reboot and you at the keyboard.

You don't need to bump `pkgrel` or force anything after a new capture. sysforge records which
profile each optimized kernel was built with, and when you run `--autofdo=use` (or a plain run
with `mode` set) after capturing a new profile, it rebuilds the kernel even though the version
hasn't changed. If neither the profile nor the kernel changed, there is nothing new to build:
an unattended run reinstalls the package it already built, and an interactive run asks first,
offering to install it as built, rebuild it, or abort. Install as built is the usual answer;
choose rebuild only if you want to review the kernel config again (a changed profile already
forces a rebuild, so you never need it for that). Every `--autofdo=record` always builds fresh,
and first deletes the profile the previous round captured (round 1's `kernel.afdo`, or round
2's Propeller pair), because it came from a different kernel binary: until you run `capture`
and its commands again, `use` refuses rather than apply it.

While `mode` is set, every plain `sysforge run kernel` is a `use` build, so it never rebuilds
the plain `<name>` kernel you keep as a fallback. To rebuild the plain kernel, set
`mode = "off"` (or comment the line out) for that run, then set it back.

What happens when the kernel moves on:

- **`mode = "autofdo"`** keeps applying the profile after updates. AutoFDO degrades
  gracefully, so if the profile was collected on a different major.minor kernel (for example
  7.2 against 7.3), the build still goes ahead and warns that a new round 1 would help. A
  profile collected before sysforge recorded versions builds with a "collected version
  unknown" warning.
- **`mode = "propeller"`** applies round 2 only while the kernel `pkgver` is the one round 2
  sampled. After an update, the build falls back to AutoFDO only and installs
  `<name>-fdo`, with a warning. The older `<name>-propeller` stays installed with its old
  profile, and sysforge never removes it. Remove it yourself when you no longer want it, or
  redo round 2 on the new kernel. For a kernel whose PKGBUILD computes its version while it
  builds (a `-git` kernel), sysforge can't check the version in advance, so this mode always
  falls back to AutoFDO for it.
  If round 2 itself can't be used (it was never finished, its pinned copy of the round 1
  profile has changed, or its profile pair is incomplete), the build refuses instead. The
  message says how to fix round 2, or to set `mode = "autofdo"` in the meantime.
- **No profile at all** (for example, after purging the store) refuses before the build. To
  go on without one, set `mode = "off"`.

### Cleanup

After you finish profiling, remove the profiling kernel:

```sh
sudo pacman -R linux-sysforge-profiling
```

If you built headers (the default, `build_headers = true`), remove them too:

```sh
sudo pacman -R linux-sysforge-profiling-headers
```

The next `sysforge run kernel` or `sysforge update` removes its sysforge-managed boot entry.
Up to four sysforge kernels can be installed at once (plain, profiling, `-fdo`,
`-propeller`). Every kernel build checks `/boot` for free space (`min_boot_free_mb`) before
it starts, and removing kernels you no longer boot keeps that check passing.

**Upgrading from an earlier sysforge.** Earlier releases named these kernels differently:

- **The old profiling kernel** was `<pkgname>-sysforge-fdo`, with `-sysforge-fdo` appended to
  `pkgname` as is. With the shipped `pkgname = "linux-sysforge"` that is
  `linux-sysforge-sysforge-fdo`. Nothing uses it any more, so remove it (and its `-headers`
  package, if built): `sudo pacman -R linux-sysforge-sysforge-fdo`.
- **If your `pkgname` doesn't end in `-sysforge`** (for example `linux-mine`), that old
  profiling kernel is `linux-mine-sysforge-fdo`, which is now the AutoFDO kernel's name. Check
  which one you have before removing it: `pacman -Qi linux-mine-sysforge-fdo` shows its build
  date, and one built before you upgraded is the old profiling kernel. The next
  `--autofdo=use` replaces it either way.
- **The old optimized kernel** was `<pkgname>-sysforge`. With the shipped `pkgname` that is
  `linux-sysforge` itself, so it now simply counts as the plain kernel, and the next `use`
  installs `linux-sysforge-fdo` beside it. With another `pkgname` it is, for example,
  `linux-mine-sysforge`, which nothing builds any more: remove it by hand once the new
  `linux-mine-sysforge-fdo` boots.
- **A Propeller profile from before this release** has no round 2 record, so
  `use --propeller` asks you to redo round 2.

---

## BOLT

BOLT rearranges clang's compiled code so the parts used together sit together, on top of
PGO. LLVM's [BOLT tutorial for clang](https://github.com/llvm/llvm-project/blob/main/bolt/docs/OptimizingClang.md)
reports up to 15% faster compiles on top of PGO and LTO, for a clang binary with LLVM linked
in. sysforge rewrites the shared libraries instead, so measure your own result with
`sysforge bench toolchain`. It is **experimental**: it has not yet been through a full
toolchain build. Turn it on
in `toolchain.toml`:

```toml
[bolt]
enabled = true
```

It runs during `sysforge run toolchain`, right after the optimized LLVM is built: SysForge
compiles a sample of LLVM's own source with an instrumented copy of clang's libraries,
rewrites them from that profile, checks the result still compiles, and packages it. If any
step fails, you get a warning and the toolchain installs without BOLT. It rewrites
`libclang-cpp`; add `libllvm = true` to rewrite `libLLVM` too, which every program that
uses LLVM loads, so it is off by default. It needs the LLVM packages as the repos build
them now (one `llvm` package), and it does not need `perf`.

To try BOLT again without rebuilding LLVM (after changing `training_workload`, say, or
when a run skipped it), run:

```sh
sysforge run toolchain --bolt-only
```

It rewrites the build that the last `run toolchain` left in your build directory,
repackages llvm, and installs through the same checks as a full run, in minutes rather
than an hour. It works even if `[bolt] enabled` is off. It refuses when that build isn't
the finished optimized build of the version you have checked out (for example after an
interrupted run); run `sysforge run toolchain` first. SysForge keeps the original
libraries next to the build, so running `--bolt-only` again always starts from them. If
BOLT can't run, nothing is installed.

To see which installed packages are BOLTed, look for `bolt_llvm` in the build-mode
column of `sysforge state list` (it marks the packages that carry a rewritten library,
`clang` by default). To check a library directly, look for the section BOLT adds:

```sh
readelf -SW /usr/lib/libclang-cpp.so.23.1 | grep bolt_info
```

### Installing a finished build

If a run finishes building but doesn't install (the install step failed, or nobody
answered its sudo prompt), install what it built without building it again:

```sh
sysforge run toolchain --install-only
```

It installs the packages the last run left, BOLTed ones included, through the same
checks, snapshot and verification as a full run. It refuses when the last build isn't
the optimized build of the version you have checked out (including a run that stopped
before its final pass, which leaves instrumented packages behind), or a package is
missing; run `sysforge run toolchain` then. A full or `--bolt-only` run asks for your sudo password
before its long work starts, and stops without changing anything if sudo isn't granted at
install time, saying to run `--install-only`.

---

## Inspecting and cleaning up profiles

```sh
sysforge state profiles                     # every store: method, size, age, collected version, measured gain
sysforge state profiles --delete pgo/mesa   # delete one store (asks first)
sysforge state profiles --delete pgo/htop   # per-package stores are METHOD/TARGET
```

After a purge, the next rebuild of that package is unprofiled until you collect again.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `--pgo=use` aborts: nothing collected | No `.profraw` was written | Check `sysforge` group membership; for Proton games see the container note above; confirm the instrumented build is what's installed |
| `.profraw` appears for native apps but not Proton games | Steam Runtime container can't see the store | `PRESSURE_VESSEL_FILESYSTEMS_RW=…` launch option |
| No measurable gain after `use` | Program isn't on your hot path (e.g. Mesa with an NVIDIA GPU), or the workload didn't match real use | Re-check Step 0; re-record with a broader workload |
| Rebuild prints *"age unknown"* | Profile predates version tracking | Harmless; refresh the profile when convenient |
| PGO command refuses immediately | Toolchain is gcc | Switch the profile to `toolchain = "llvm"` |
| Kernel `record` warns BRS is experimental | AMD branch sampling (`brs`/`amd_lbr_v2`) | Expected; proceed or skip kernel FDO |
| Kernel `record` warns branch sampling is unsupported, or `perf record` fails with *doesn't support branch stack sampling* | CPU has neither `brs` nor `amd_lbr_v2` (e.g. Ryzen 5000) | Kernel FDO isn't possible on this CPU; skip it |
| `capture`: *needs tools that are not on PATH* | `perf`, `llvm-profgen` (round 1) or `generate_propeller_profiles` (round 2) is missing | Install what each hint line names, then re-run `capture` |
| `capture`: *not booted into `<name>-profiling` (running: …)* | You're on another kernel, or you re-ran `record` and haven't rebooted since | Reboot into the profiling kernel, then re-run `capture` |
| `capture`: *the profiling kernel's build tree is gone* | The build directory (usually under `~/builds`) was wiped after `record` | Re-run `--autofdo=record` (add `--propeller` in round 2), reboot, `capture` again |
| `capture`: *the running kernel (…) matches only …/vmlinux, which has no debug info* | Only the stripped `-headers` copy of the running kernel's `vmlinux` was found: the profiling build tree is gone, or you aren't booted into the profiling kernel | On the profiling kernel, re-run `--autofdo=record` (add `--propeller` in round 2), reboot, `capture` again; otherwise reboot into the profiling kernel and re-run `capture` |
| `capture`: *cannot read /proc/version* | The running kernel can't be identified | Check that `/proc` is mounted and readable, then re-run `capture` |
| `record --propeller`: *needs round 1's AutoFDO profile* | Round 1 has not produced `kernel.afdo` | Finish round 1 (`record`, reboot, `capture`, convert) first |
| `use --propeller`: *no round-2 record* | A Propeller profile from before two rounds existed, or round 2's `record` never finished | Redo round 2: `record --propeller`, reboot, `capture --propeller`, convert, `use --propeller` |
| `use --propeller`: *the pinned AutoFDO profile … changed since round 2* | The pinned copy in the Propeller store was replaced or edited | Redo round 2 from `--autofdo=record --propeller` |
| `use --propeller`: *round 2 profiled X but this build is Y* | The kernel was updated after round 2 | Redo round 2 on the new kernel, or build AutoFDO only with `--autofdo=use` |
| `use --propeller`: *Propeller profile incomplete* | The `generate_propeller_profiles` step wasn't run, or failed | Re-run `capture --propeller` and the commands it prints |
| Plain `run kernel` refuses: *kernel.toml [fdo] mode applies the AutoFDO profile …, but there is none* | `mode` is set but round 1 was never finished, or its store was purged | Collect round 1, or set `mode = "off"` |
| WARN: *Propeller round 2 profiled X, this build is Y: building AutoFDO only* | `mode = "propeller"` after a kernel update | Expected; the build installs `-fdo`. Redo round 2, or remove the old `-propeller` kernel when you no longer want it |
| WARN: *the building kernel version is unknown until makepkg runs pkgver(), so the round-2 Propeller profile cannot be verified against it: building AutoFDO only* | `mode = "propeller"` with a kernel whose PKGBUILD computes its version during the build (a `-git` kernel) | Expected; the build installs `-fdo`. Use `--autofdo=use --propeller` explicitly to apply round 2 anyway (it warns that it cannot verify the match) |
| WARN: *AutoFDO profile was collected on X, building Y* | `mode = "autofdo"` after a kernel major.minor update | The profile still applies; collect a new round 1 when convenient |
