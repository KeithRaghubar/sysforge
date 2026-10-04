# Profile-Guided Optimization with SysForge

A practical guide for people who have never done PGO before. It covers *what to run* and,
more importantly, *what to do in between* — the part no command can do for you.

For the internals (how flags are injected, how renames work, why each gate exists) see
[DESIGN.md](../../DESIGN.md). This guide only describes how to use it.

---

## The idea in one minute

A compiler normally guesses which code is hot. Profile-guided optimization replaces the guess
with measurements from your own machine:

1. **Record** — build a special version of the program that collects data while it runs.
2. **Exercise** — use it the way you normally do. *This step is yours, and it decides the
   result.*
3. **Use** — rebuild the program, feeding the collected data to the compiler. It lays out
   hot code together, inlines what you actually call, and moves cold code out of the way.

The optimized build is only as good as step 2. A profile collected from a workload that
doesn't resemble real use can make things *slower* in the paths you care about, because the
compiler deprioritises code it never saw run.

SysForge supports two families of profile collection:

| Family | How data is collected | Overhead while recording | Used for |
|---|---|---|---|
| **Instrumentation** (PGO) | Counters compiled into the binary | Noticeable (often 10–50% slower) | Packages (mesa, …), the LLVM toolchain |
| **Sampling** (AutoFDO, Propeller) | CPU hardware samples branches via `perf` | Low | The kernel |

---

## Which method do I want?

| Goal | Method | Command | Worth it? |
|---|---|---|---|
| Faster graphics driver (shader compile, draw-call overhead) | Package PGO | `sysforge build mesa --pgo=record` / `--pgo=use` | Yes, **if your GPU uses Mesa** (see below) |
| Faster compiles for *everything* you build | Toolchain PGO | `sysforge run toolchain` with `compiler = "llvm"`, `pgo = true` | Yes, if you build a lot from source; costs hours once |
| Faster kernel (syscalls, scheduling, I/O paths) | Kernel AutoFDO (+ Propeller) | `sysforge run kernel --autofdo=record\|capture\|use` | Modest gains; spans reboots |
| Some other hot, long-lived library | Package PGO | `sysforge build <pkg> --pgo=…` | Rarely — SysForge warns unless you allow-list it |
| Post-link layout of clang | BOLT | `toolchain.toml [bolt]` | **Currently blocked** — leave disabled |

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
- **Disk and time.** Every record or use pass is a full clean build (SysForge forces this —
  stale objects from a differently-instrumented build would corrupt the profile). Toolchain
  PGO in particular needs ~40 GiB free and several hours.

---

## Package PGO (mesa and others)

### Step 0 — check that the package is actually on your hot path

For mesa this matters a lot. Games only benefit if they render through a **Mesa driver**:

```sh
vulkaninfo --summary | grep -i driverName   # radv / anv / nvk / … = Mesa
glxinfo -B | grep -i "OpenGL vendor"        # "AMD" / "Intel" / "Mesa" = Mesa
```

If you see `NVIDIA` (the proprietary driver), games go through NVIDIA's own driver, and Mesa
PGO will do essentially nothing for them. Skip it.

### Step 1 — record

```sh
sysforge build mesa --pgo=record
```

This builds and installs an *instrumented* mesa under its normal name. From now on, every
program that loads mesa writes a `.profraw` file into the store
(`/var/cache/sysforge/pgo-mesa` for mesa, `/var/cache/sysforge/pgo/<pkgbase>` for anything
else). No environment variables needed.

**Verify it's collecting** after the first session — don't find out three evenings later:

```sh
ls -lt /var/cache/sysforge/pgo-mesa | head      # fresh .profraw files?
sysforge state profiles                          # store listed, size growing?
```

### Step 2 — exercise it (the important part)

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
3. **Play normally.** Loading screens, traversal, combat, menus — that's the real hot set.
4. **Proton/Steam Runtime games run in a container** that may not see `/var/cache`. If no
   `.profraw` appears after playing a Proton game, add this to the game's launch options
   during the record phase:

   ```
   PRESSURE_VESSEL_FILESYSTEMS_RW=/var/cache/sysforge/pgo-mesa %command%
   ```

   (Flatpak Steam needs an equivalent `--filesystem=` override.) Remove it afterwards.
5. **32-bit games contribute nothing.** lib32-mesa is never instrumented.

### Step 3 — use

```sh
sysforge build mesa --pgo=use
```

This merges every collected `.profraw` (plus any earlier profile) into
`mesa.profdata`, deletes the consumed raw files, and builds an optimized
`mesa-sysforge` that replaces stock mesa. Your GPU apps should now run on the optimized
build — check with `pacman -Q mesa-sysforge`.

### Keeping it, refreshing it, removing it

- **It survives updates automatically.** Every later rebuild (`sysforge update`, plain
  `sysforge build mesa`) reapplies the existing profile, and prints a notice like
  *"collected against X, building Y"* as it ages.
- **Profiles go stale gradually.** Code that changed upstream loses its profile data. A
  rough rule: refresh after a major Mesa release, or when the notice shows a big version gap.
  Refreshing is just Step 1 → 3 again; the new data is **merged** with the old.
- **Start over** (e.g. after changing GPU or driver): `sysforge state profiles --purge
  pgo-mesa`, then record again.
- **Stop using PGO**: purge the store, then `sysforge revert mesa` (or rebuild normally).

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

A PGO-optimized clang compiles everything faster — typically 10–20%. That benefits every
package you build afterwards, including kernels and mesa.

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
toolchain from that profile (pass 4). You don't have to do anything in between.

Best practices:

- **Broaden the training.** LLVM's own C++ build under-represents C and graphics code. Add
  mesa to the training corpus so clang is also tuned for it:
  ```toml
  [packages]
  training_corpus = ["llvm", "mesa"]
  ```
- **Run it interactively the first time.** PGO asks for confirmation at several points
  (reusing an existing profile, purging staging, suspicious profile sizes). For unattended
  runs, pass `--auto-pgo`.
- **The profile is reused** across same-major LLVM rebuilds. After an LLVM *major* version
  bump it is incompatible and gets regenerated. Force a fresh one any time with
  `--rebuild-profdata`.
- **If a late package fails**, rerun with `--reuse-built` to skip re-optimizing the packages
  that already succeeded.

---

## Kernel AutoFDO / Propeller

The kernel uses **sampling**, not instrumentation. It needs a reboot between steps, so it's
three separate commands:

```sh
# 1. Build + install a profiling kernel (installed as <pkgname>-sysforge-fdo, alongside
#    your current kernel — it never replaces it)
sysforge run kernel --autofdo=record [--propeller]

# 2. Reboot into the profiling kernel, then print the exact perf commands for your CPU
sysforge run kernel --autofdo=capture [--propeller]
#    → run the printed `perf record … && create_llvm_prof …` WHILE doing your normal work

# 3. Build the optimized kernel from the profile (installed as <pkgname>-sysforge,
#    alongside the stock kernel so you can always fall back at the bootloader)
sysforge run kernel --autofdo=use [--propeller]
```

Best practices:

- **CPU support.** Intel uses LBR (well supported). AMD needs **Zen 3 or newer** (BRS), and
  AMD support is **experimental**. SysForge warns about this up front at `record`.
- **Capture under your real load** for several minutes: gaming, compiling, browsing —
  whatever this machine actually does. An idle desktop profile is nearly useless.
- **Use `--propeller` on all three steps or none.** It layers basic-block layout on top of
  AutoFDO and is the recommended kernel post-link optimization (instead of BOLT).
- **Check your kernel name.** If `kernel.toml` `pkgname` already ends in `-sysforge` (the
  shipped default), the optimized build *replaces* it rather than coexisting. Rename it
  (e.g. `linux-mine`) first if you want a stock fallback entry in the bootloader.

---

## BOLT

BOLT (post-link binary layout for clang) is wired in but **currently blocked** by how the
LLVM packages are built. Keep `toolchain.toml [bolt] enabled = false`. SysForge skips it with
a warning if enabled.

---

## Inspecting and cleaning up profiles

```sh
sysforge state profiles                    # every store: method, size, age, collected version
sysforge state profiles --purge pgo-mesa   # delete one store (asks first)
sysforge state profiles --purge pgo/htop   # per-package stores are METHOD/TARGET
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
| Kernel `record` warns BRS is experimental | AMD Zen 3+ branch sampling | Expected; proceed or skip kernel FDO |
