# Bootstrapping an Optimized Compiler

A compiler is a program like any other, so it can be built with itself, and it can be
profile-guided like any other. But the compiler is also the tool that builds everything else,
including its own replacement, and that changes how it has to be built and installed. This
page explains the stages of a compiler bootstrap, what a profile-guided build adds, and why
replacing an installed compiler has to be all or nothing.

The [glossary](glossary.md) defines the terms these pages share. The background on profiles
is in [Profile-guided and post-link optimization](pgo-and-post-link.md).

---

## Why build in stages

Say you have version A of a compiler installed and want to install version B from source. The
only thing that can compile B is A, so the first result is "B compiled by A". That binary has
B's features, but its machine code is whatever A was able to produce. It may also have been
built by a different compiler entirely (GCC building Clang, for example).

A **bootstrap** fixes this in stages, each one a separate build, or **pass**:

1. **Stage 1:** build B with the installed compiler. The result is a working B, built with
   A's code generation.
2. **Stage 2:** build B again, this time with the stage 1 compiler. The result is B compiled
   by B: it gets B's own optimizations and no longer depends on what A could do.
3. **Stage 3** (optional): build B once more with the stage 2 compiler and compare the output
   with stage 2. A correct compiler that compiles the same source with the same options must
   produce the same result, so any difference points to a miscompilation. GCC's default
   bootstrap does this comparison.

A plain stage 2 compiler is self-hosted and consistent, but it is not faster than the
distribution's own build of the same version with the same flags. Making it faster takes a
profile.

---

## A profile-guided bootstrap

A compiler is a good candidate for profile-guided optimization: it is a large program whose
speed is limited by instruction fetch and branch prediction, and the training workload is
obvious, because compiling code is what it does. LLVM documents the process for Clang:

1. **Instrumented build.** Build the compiler with profiling counters (`-fprofile-generate`).
   This build is slower than normal, and is only used to collect data.
2. **Training.** Use the instrumented compiler to compile a representative body of code.
   Every function and branch it runs is counted. LLVM's own sources are the usual workload,
   since they are large, varied C++. Adding C code, or code from the projects you build most,
   broadens the profile.
3. **Merge.** Combine the raw profiles from every compiler process into one profdata file
   with `llvm-profdata merge`.
4. **Optimized build.** Build the compiler once more with `-fprofile-use`, and usually with
   LTO, from exactly the same source as the instrumented build, so the profile matches the
   code it describes.

Each step produces something different:

| Pass | Produces | Kept? |
|---|---|---|
| Stage 1 (when needed) | A compiler of the new version to build the rest with | Only until the run ends |
| Instrumented | A slow compiler that writes profiles | Only until training ends |
| Training | `.profraw` files, merged into a profdata | The profdata, for reuse |
| Optimized | The final compiler | Installed |

The profile is specific to the compiler version: a new major version changes too much for
the old profile to apply, while a minor update can usually reuse it with some loss. The
instrumented and optimized builds should come from the same source, which is why the whole
sequence runs again when the version changes.

### Adding BOLT

[BOLT](pgo-and-post-link.md#layout-after-compiling-propeller-and-bolt) can rewrite the finished
compiler's layout on top of PGO. LLVM's build system has a recipe for it: link the optimized
compiler with relocations kept (`--emit-relocs`), run it on a training workload under BOLT's
instrumentation or under `perf`, then let BOLT rewrite the binary. It adds a training run and
a rewrite after the optimized build, but does not need another compile of the compiler itself.

---

## Why the installed toolchain must never be half-replaced

A modern compiler is not one file. Clang, for example, is a small `clang` executable plus
`libclang-cpp`, `libLLVM`, the compiler runtime (`compiler-rt`), the linker `lld`, and tools
such as `llvm-ar` and `llvm-profdata`, all from the same version and expected to match exactly.
The shared libraries have version-specific sonames and symbol versions (see
[Shared libraries and ABI](shared-libraries-and-abi.md)), so a new `clang` started against
an old `libLLVM`, or the reverse, fails with missing symbols before it compiles anything.

That makes replacing a toolchain unusually risky:

- **Replace all of it at once.** Install every package of the new version in one transaction,
  so there is no moment when the system holds a mixture.
- **Never install an intermediate pass.** The instrumented compiler and the stage 1 compiler
  are build artifacts. Each pass should be built and run from its own staging directory, with
  the system compiler untouched until the final install.
- **Check before and after.** The new libraries also serve programs that already use them
  (graphics drivers, debuggers, language runtimes), so check that the new build still exports
  what those programs need before installing it, and that the result works after.
- **Keep a way back.** If the installed compiler breaks, you cannot use it to build a fix.
  Keep the previous packages (the pacman cache does this) so a broken install can be reverted
  with `pacman -U`, which needs no compiler at all.

---

## Further reading

- LLVM: [How To Build Clang and LLVM with Profile-Guided Optimizations](https://llvm.org/docs/HowToBuildWithPGO.html),
  [Advanced Build Configurations](https://llvm.org/docs/AdvancedBuilds.html) (multi-stage builds,
  PGO and BOLT caches)
- GCC: [Building](https://gcc.gnu.org/install/build.html) (the three-stage bootstrap and its
  comparison)
- BOLT: [Optimizing Clang with BOLT](https://github.com/llvm/llvm-project/blob/main/bolt/docs/OptimizingClang.md)

---

## In sysforge

`sysforge run toolchain` runs this sequence for LLVM: an instrumented build, training on the
packages you choose, an optimized build, optionally BOLT, then an audit, one install of the
whole suite with a snapshot to restore, and a verification. The toolchain section of the
[PGO guide](../guides/pgo.md#toolchain-pgo-optimized-clang) covers running it.
