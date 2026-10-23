# Compiler Optimization Basics

What the common optimization flags actually change, what each one costs, and why the flags a
distribution ships are a compromise between speed, portability and safety. The examples use
GCC and Clang, which accept the same spelling for nearly all of these flags.

The [glossary](glossary.md) defines the terms these pages share.

---

## What a distribution builds with

Arch's stock `/etc/makepkg.conf` (pacman 7) builds every package with:

```
CFLAGS="-march=x86-64 -mtune=generic -O2 -pipe -fno-plt -fexceptions
        -Wp,-D_FORTIFY_SOURCE=3 -Wformat -Werror=format-security
        -fstack-clash-protection -fcf-protection
        -fno-omit-frame-pointer -mno-omit-leaf-frame-pointer"
CXXFLAGS="$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"
LDFLAGS="-Wl,-O1 -Wl,--sort-common -Wl,--as-needed -Wl,-z,relro -Wl,-z,now
         -Wl,-z,pack-relative-relocs"
LTOFLAGS="-flto=auto"
```

with `lto` in `OPTIONS`. Every choice here has to work on every x86-64 CPU Arch supports and
for every package in the repos. Building for one machine lets you make different choices, and
the rest of this page is about what those choices trade.

---

## Optimization levels

| Level | What it does |
|---|---|
| `-O0` | No optimization. Fast to compile, easy to debug, slow to run. |
| `-O1` | Cheap optimizations that do not cost much compile time. |
| `-O2` | Nearly every optimization that does not trade code size for speed. The standard choice for distributions. |
| `-O3` | `-O2` plus more aggressive inlining, loop transformations (unswitching, more unrolling, loop distribution) and more aggressive vectorization. |
| `-Os`, `-Oz` | Optimize for size (`-Oz` harder, Clang and recent GCC). |

`-O3` is not reliably faster than `-O2`. Its extra inlining and unrolling make code larger,
and larger code means more instruction-cache misses, which can cost more than the
transformations save. Programs dominated by numeric loops tend to gain. Large programs with
lots of branchy code often gain nothing, or lose a little. Both compilers already vectorize at
`-O2` (GCC since version 12, with a cautious cost model), so the gap is smaller than it used to
be. Measure the programs you care about rather than assuming.

---

## -march and portability

`-march` selects which CPU instructions the compiler may use. Arch's `-march=x86-64` is the
original 2003 instruction set, which runs on every 64-bit x86 CPU but cannot use anything newer
on its own: no AVX, no AVX2, no newer bit-manipulation instructions.

- **`-march=native`** asks the compiler to detect the CPU it runs on and use everything that
  CPU supports. It also tunes instruction scheduling for that CPU (`-mtune=native`).
- **The cost is portability.** A binary built with `-march=native` may use instructions that
  other CPUs lack, and crashes on them with `Illegal instruction` (SIGILL), usually the moment
  it reaches such an instruction, which may be long after it starts. Moving a disk to a
  different machine, or replacing the CPU with an older model, can leave such programs unable
  to run.
- **A middle ground** is the x86-64 microarchitecture levels: `-march=x86-64-v2`, `-v3` (AVX2,
  roughly Intel Haswell or AMD Excavator and newer) and `-v4` (AVX-512). They are portable
  across every CPU of that level.

How much `-march` gains depends on whether a program's hot code can use the newer
instructions. Code that vectorizes or uses bit manipulation heavily (compression, image and
audio processing, cryptography without hand-written assembly) can gain a lot. Much ordinary
code gains little, and libraries that matter most for speed often detect the CPU at run time
and choose the best code path themselves, whatever they were compiled with.

`-mtune` alone, without `-march`, only changes how instructions are scheduled, never which ones
are used, so it is always portable.

---

## Link-time optimization

Normally each source file is compiled to machine code on its own, and the compiler cannot
inline or optimize across file boundaries. With LTO (`-flto`), the compiler writes its
intermediate representation into the object files instead, and the real optimization happens
when the linker brings all of them together. Calls between files can then be inlined, unused
functions removed and constants propagated across the whole program.

- **Full LTO** (`-flto` in Clang, `-flto-partition=one` in GCC) merges the whole program into
  one module and optimizes it as one. It sees the most and usually produces the best code, but
  the link runs on one core and can use a great deal of memory for a large program.
- **Thin LTO** (`-flto=thin`, Clang) keeps the modules separate, shares a small summary of each
  between them, and optimizes them in parallel, importing only the functions each one needs.
  It gets most of full LTO's benefit at a fraction of the link time and memory, and supports
  incremental caching.
- **GCC's default LTO** (`-flto=auto`) partitions the program and optimizes the partitions in
  parallel, which plays the same role as Thin LTO.

LTO's gain is usually moderate, and it is largest for programs split into many small files that
call each other often. It also exposes bugs: code that relied on undefined behaviour across file
boundaries, or on symbols that LTO decides are unused, can break. That is why PKGBUILDs carry
`options=(!lto)` for packages known not to work.

---

## The linker

The linker joins object files and libraries into the final binary. Several flags in the
defaults are linker options passed through `-Wl,`:

- **`-O1`** lets the linker spend a little more time building smaller, faster symbol hash
  tables.
- **`--as-needed`** records a `NEEDED` entry for a shared library only if the program actually
  uses a symbol from it, so a stray `-lfoo` does not make the program load `libfoo` at start-up.
- **`-z relro -z now`** (full RELRO) resolves every dynamic symbol at start-up and then makes
  the table of resolved addresses read-only, so an attacker who can write memory cannot
  redirect calls through it. The cost is a slower start-up for programs with many symbols.
- **`--icf=all`** (lld and gold) merges functions whose machine code is identical. Binaries
  shrink, but two different functions can end up with the same address, which breaks the rare
  program that compares function pointers. `--icf=safe` only merges where this cannot be
  observed.

**`-fno-plt`** is a compiler flag that belongs with these. Calls to functions in shared
libraries normally go through a small stub in the procedure linkage table (PLT), which looks
the target up lazily on first use. With `-fno-plt` the call reads the target's address straight
from the global offset table instead, saving an indirect jump per call. It relies on the
addresses being resolved at start-up, which `-z now` already guarantees.

**Which linker** also matters for build time more than for the result. GNU `ld` (bfd) is the
traditional default. LLVM's `lld` (`-fuse-ld=lld`) links large programs many times faster and is
the usual partner for Clang's Thin LTO and for Propeller. `mold` is faster still.
For the same inputs and options they produce programs of very similar speed.

---

## Hardening flags

Hardening flags make classes of memory bugs harder to exploit, usually by turning them into a
clean crash. They cost a little speed and sometimes some size.

| Flag | What it does |
|---|---|
| `-D_FORTIFY_SOURCE=3` | Replaces calls such as `memcpy` and `strcpy` with checked versions when the compiler can tell the buffer size; level 3 also uses sizes only known at run time. Needs optimization (`-O1` or higher). |
| `-fstack-protector-strong` | Puts a canary value on the stack of functions with local arrays or address-taken variables and checks it on return. Arch's GCC enables it by default. |
| `-fstack-clash-protection` | Probes each page when a function allocates a large stack frame, so the stack cannot jump past the guard page into other memory. |
| `-fcf-protection` | Adds Intel CET markers (`endbr64` landing pads and shadow-stack support), so that where the CPU, kernel and C library enforce CET, indirect jumps and returns can only reach valid targets. |
| PIE (`-fPIE -pie`) | Builds programs position-independent so address-space layout randomization can move them. The default in Arch's GCC and Clang. |
| `-D_GLIBCXX_ASSERTIONS` | Enables bounds and precondition checks in libstdc++, such as `operator[]` on a vector. |
| `-ftrivial-auto-var-init=zero` | Fills uninitialized local variables with zeroes, so a missed initialization reads a known value instead of old stack contents. |
| `-fstrict-flex-arrays=3` | Treats only a true `[]` trailing array as variable-sized. With fortification, old-style `[0]` and `[1]` trailing arrays get their declared size checked, and legacy code that overruns them aborts. |

For most programs the combined cost of these is small, and much smaller than the gain from
the optimization flags above, which is why distributions keep them on. The ones with real
costs are those that add work to every function call or every allocation; the
[OpenSSF hardening guide](https://best.openssf.org/Compiler-Hardening-Guides/Compiler-Options-Hardening-Guide-for-C-and-C++.html)
discusses each one's trade-off. Turning hardening off to win speed is rarely worth it.

**Frame pointers** (`-fno-omit-frame-pointer`) are not hardening, but they are in Arch's defaults
for a related reason: they let profilers such as `perf` record reliable call stacks cheaply.
They cost one register and a couple of instructions per call.

---

## Further reading

- GCC: [Options That Control Optimization](https://gcc.gnu.org/onlinedocs/gcc/Optimize-Options.html),
  [x86 Options](https://gcc.gnu.org/onlinedocs/gcc/x86-Options.html)
- Clang: [Command Line Reference](https://clang.llvm.org/docs/ClangCommandLineReference.html),
  [ThinLTO](https://clang.llvm.org/docs/ThinLTO.html)
- LLVM: [LLVM Link Time Optimization](https://llvm.org/docs/LinkTimeOptimization.html),
  [lld](https://lld.llvm.org/)
- [x86-64 psABI](https://gitlab.com/x86-psABIs/x86-64-ABI) (microarchitecture levels)
- OpenSSF: [Compiler Options Hardening Guide for C and C++](https://best.openssf.org/Compiler-Hardening-Guides/Compiler-Options-Hardening-Guide-for-C-and-C++.html)
- Arch wiki: [makepkg](https://wiki.archlinux.org/title/Makepkg#Building_optimized_binaries)

---

## In sysforge

sysforge applies these flags through profiles in `profiles.toml`: `standard` (`-march=native
-O2`), `optimized` (`-O3`, `-fno-plt`, lld with `--icf=all`), `hardened` (`standard` plus
extra hardening) and your own, chosen per package by rules. The comments at the top of the shipped
`profiles.toml` explain how a profile is chosen, and `sysforge resolve <pkg>` shows which one applies to a package and
why. The hardening flags `/etc/makepkg.conf` sets are kept even when a profile replaces `CFLAGS`,
unless the profile sets `preserve_system_tokens = false`.
