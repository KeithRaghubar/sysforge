# Profile-Guided and Post-Link Optimization

A compiler has to guess which code runs often and which almost never runs. Profile-guided
optimization replaces that guess with measurements from real runs. This page explains the
main ways of collecting those measurements, what each one changes in the program, and when
none of them are worth the effort.

The [glossary](glossary.md) defines the terms these pages share.

---

## Why the guess matters

Most of a large program's run time is spent in a small part of its code. Without a profile,
the compiler optimizes every function as though it might be hot: it uses fixed rules to
decide what to inline, guesses which way each branch goes, and lays out functions in the
order they appear in the source. When those guesses are wrong, hot code ends up spread over
many cache lines and memory pages, with rarely used code in between. The CPU then spends
time fetching instructions instead of running them, which shows up as instruction-cache
misses, iTLB misses and mispredicted branches.

A profile tells the compiler three things it cannot work out from the source: which
functions are hot, which way each branch usually goes, and which call sites are called most.
With that it can inline the calls that matter, put the likely side of each branch on the
straight-line path, move code that never runs out of the way and keep hot code together.

---

## The two ways to collect a profile

### Instrumentation (classic PGO)

The compiler builds a special version of the program with counters added to every function
and branch (`-fprofile-generate` in GCC and Clang). Running that build writes the counts to
disk: `.gcda` files for GCC, `.profraw` files for Clang, which `llvm-profdata merge` combines
into one [profdata](glossary.md#profdata) file. The final build reads the profile back with `-fprofile-use`.

- **Precise.** Every branch is counted exactly, not estimated.
- **Expensive to collect.** The counters slow the program noticeably while it records, and
  the instrumented build is a separate build you install, use and then replace.
- **Tied to the source.** Clang stores a checksum of each function's control flow with its
  counts. A function whose code changed since the profile was recorded no longer matches,
  and the compiler ignores the profile for it (`-Wprofile-instr-out-of-date` reports this).

### Sampling (AutoFDO)

Instead of building a special version, you run the normal optimized program under `perf`,
which uses the CPU's branch-recording hardware (Intel's LBR, AMD's BRS or LBRv2) to sample
which branches were taken. A converter (`llvm-profgen` or `create_llvm_prof`) maps the
samples back to source lines using the binary's debug information and writes a sample
profile, which the compiler reads with `-fprofile-sample-use`.

- **Cheap to collect.** Sampling adds little overhead, so you can profile a production build
  doing real work, including something you cannot easily instrument, such as a running kernel.
- **Approximate.** Samples are statistical, and the mapping back through debug information
  loses some detail, so the profile is less precise than an instrumented one.
- **Degrades gradually.** Samples are keyed to line offsets within a function, so an older
  profile still applies, less and less accurately, as the code changes.
- **Needs hardware support.** Without branch recording the converter has too little
  information to work with.

---

## Layout after compiling: Propeller and BOLT

PGO and AutoFDO change decisions made while compiling each function. Two newer tools work
later, on the program's layout, using a profile sampled from an already optimized build.

- **Propeller** works with the compiler and linker. The profiled build is compiled with
  basic-block address maps (`-fbasic-block-address-map`). The sampled profile is converted into
  two lists: which basic blocks of each function should be split into their own sections, and
  in which order the linker should place the resulting pieces. The final build recompiles with
  `-fbasic-block-sections=list=…` and links with a symbol ordering file. Because it works at
  basic-block granularity across the whole binary, it can put the hot blocks of many functions
  next to each other and move the cold ones far away.
- **BOLT** works after linking. It reads the finished binary (linked with `--emit-relocs`, so
  it can move code safely), a profile collected either by sampling with `perf` or by an
  instrumented copy that BOLT builds itself, and writes a new binary with functions and basic
  blocks reordered, hot and cold parts of functions split, and some branches simplified. The
  source and build system are not involved.

Both do mostly the same job: code layout. They are usually applied on top of PGO or AutoFDO,
not instead of it, and a program rarely gains much from running both. Both profiles are tied
to one exact binary: a Propeller profile only matches the build it was sampled from, and a
BOLT profile is applied to the binary it describes.

| Technique | When it acts | What it changes | How the profile is collected |
|---|---|---|---|
| Instrumented PGO | Compile time | Inlining, branch layout, register allocation, hot/cold splitting inside functions | Counters in a special build |
| AutoFDO | Compile time | The same decisions, from sampled data | `perf` branch samples of the normal build |
| Propeller | Compile and link time | Basic-block and function order across the binary | `perf` branch samples of the normal optimized build |
| BOLT | After linking | Function and basic-block order, hot/cold splitting | `perf` samples or BOLT's own instrumentation |

---

## The training workload decides the result

Whatever the program does while the profile is collected becomes the definition of "hot".
Code the workload never reached is treated as cold: it is optimized for size and moved out
of the way. If the workload does not match real use, the paths you care about can end up
*slower* than in an unprofiled build.

A good [training workload](glossary.md#training-workload):

- **Is real use, or close to it.** A synthetic benchmark exercises a narrow slice of the code.
- **Covers breadth.** Each distinct way the program is used lights up different code.
- **Is balanced.** Counts add up over time, so one long session of a single task outweighs
  several short ones of others. Match each task's share of the profile to its share of real
  use.

Compilers are an easy case, because compiling a large, varied code base is both the training
run and the real use. Interactive programs, drivers and kernels are harder: someone has to
choose the workload.

---

## Profiles go stale

A profile describes the code it was collected from. As the program changes, parts of the
profile no longer apply:

- **Instrumented profiles** stop applying function by function. A changed function falls back
  to unprofiled optimization, and the compiler can report how many functions no longer match.
  After a large upstream release most of the profile may be unused.
- **Sample profiles** fade gradually, because they are keyed to line offsets rather than
  exact code. An old AutoFDO profile still helps a newer version, just less.
- **Propeller and BOLT profiles** describe one exact binary. Any rebuild, even with no source
  change, needs a new one.

A profile that no longer matches is wasted effort, not a hazard: the compiler only uses the
parts that still apply. Refresh it after big upstream changes, or when the compiler reports
that most of it is unused.

---

## When it does not pay off

These techniques help most where the CPU's front end is the bottleneck: large programs with
lots of code and branches, where instruction fetch and branch prediction limit speed.
Compilers, interpreters, databases, browsers and kernels are the classic cases. They help
little or not at all when:

- **The program is small.** If its hot code already fits in the instruction cache, there is
  little layout to improve.
- **The time is spent elsewhere.** Waiting on disk, the network, the GPU or memory bandwidth is
  not changed by better code layout.
- **The work is a few tight loops.** Numeric kernels that are already vectorized and
  branch-light leave the profile little to change.
- **The program runs rarely or briefly.** A faster build of a tool you run twice a month does
  not repay the hours of building and recording.
- **The workload cannot be represented.** If real use varies too much to capture, the profile
  can make some paths slower while others get faster.

Measure before and after rather than assuming a gain; see
[Measuring performance honestly](measuring-performance.md).

---

## Further reading

- Clang: [Profile Guided Optimization](https://clang.llvm.org/docs/UsersManual.html#profile-guided-optimization)
- LLVM: [How To Build Clang and LLVM with Profile-Guided Optimizations](https://llvm.org/docs/HowToBuildWithPGO.html)
- GCC: [Options That Control Optimization](https://gcc.gnu.org/onlinedocs/gcc/Optimize-Options.html) (`-fprofile-use`, `-fauto-profile`)
- AutoFDO tools: [github.com/google/autofdo](https://github.com/google/autofdo)
- Propeller: [github.com/google/llvm-propeller](https://github.com/google/llvm-propeller)
- BOLT: [llvm-project/bolt](https://github.com/llvm/llvm-project/tree/main/bolt)
- Linux kernel: [AutoFDO](https://docs.kernel.org/dev-tools/autofdo.html) and
  [Propeller](https://docs.kernel.org/dev-tools/propeller.html)

---

## In sysforge

sysforge uses instrumented PGO for packages such as mesa and for its LLVM toolchain build,
AutoFDO and Propeller for the kernel, and BOLT for the toolchain's libraries. The
[PGO guide](../guides/pgo.md) covers which one to use and every step of running it.
