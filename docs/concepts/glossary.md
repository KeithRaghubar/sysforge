# Glossary

Short definitions of the terms the guides and concept pages use, each with a link to the page
that explains it properly. Link a term here instead of explaining it again.

---

### ABI

The binary contract between a library and the programs built against it: symbol names,
structure layouts, calling conventions. A change that breaks it breaks programs that are
already built. See [Shared libraries and ABI](shared-libraries-and-abi.md#api-and-abi).

### AutoFDO

Profile-guided optimization from a sampled profile: the normal build is run under `perf`, and
its branch samples are converted into a profile for the next build. See
[Profile-guided and post-link optimization](pgo-and-post-link.md#sampling-autofdo).

### BOLT

A post-link optimizer that rewrites a finished binary's code layout from a profile. See
[Profile-guided and post-link optimization](pgo-and-post-link.md#layout-after-compiling-propeller-and-bolt).

### BUILDDIR

The makepkg setting for where a package's source is extracted and built. See
[How an Arch package is built](arch-packages.md#where-things-end-up).

### DKMS

Dynamic Kernel Module Support: rebuilds out-of-tree kernel modules, such as NVIDIA's, for
each installed kernel. See [Kernel builds and boot basics](kernel-and-boot.md#built-in-or-module).

### Drift

A package you built is no longer what a fresh build would produce, because its version, its
compiler or its flags have changed since. In sysforge, see the [drift guide](../guides/drift.md).

### Fallback kernel

A known-good kernel kept installed, with its own boot entry, to boot when a new kernel does
not. See [Kernel builds and boot basics](kernel-and-boot.md#why-a-fallback-kernel-matters).

### Initramfs

A small archive loaded with the kernel that holds what is needed to mount the real root
filesystem. See [Kernel builds and boot basics](kernel-and-boot.md#the-initramfs).

### Kconfig

The kernel's configuration system: the `Kconfig` files that declare options, and the `.config`
that records a build's choices. See [Kernel builds and boot basics](kernel-and-boot.md#the-kernel-configuration).

### LTO

Link-time optimization: the compiler optimizes the whole program at link time, across source
files. Full LTO merges everything into one unit; Thin LTO optimizes the pieces in parallel. See
[Compiler optimization basics](compiler-optimization.md#link-time-optimization).

### Pass

One complete build within a larger multi-build sequence, such as the instrumented, training and
optimized builds of a compiler bootstrap. See
[Bootstrapping an optimized compiler](compiler-bootstrap.md#why-build-in-stages).

### PGO

Profile-guided optimization: building a program using measurements of how it actually runs.
Often refers specifically to the instrumented kind. See
[Profile-guided and post-link optimization](pgo-and-post-link.md).

### pkgbase

The name of a PKGBUILD as a whole, which may build several packages. See
[How an Arch package is built](arch-packages.md#pkgbase-and-split-packages).

### PKGDEST

The makepkg setting for where finished package files are written. See
[How an Arch package is built](arch-packages.md#where-things-end-up).

### pkgrel

The release number of a package at a given upstream version, raised for each rebuild. See
[How an Arch package is built](arch-packages.md#version-fields).

### Profdata

The merged, indexed profile file that Clang reads with `-fprofile-use`, made from one or more
`.profraw` files by `llvm-profdata merge`. See
[Profile-guided and post-link optimization](pgo-and-post-link.md#instrumentation-classic-pgo).

### Profraw

A raw profile written by a Clang-instrumented program when it exits. Many of them are merged
into one [profdata](#profdata) file.

### Propeller

A link-time layout optimizer that reorders basic blocks and functions across a binary from a
sampled profile. See
[Profile-guided and post-link optimization](pgo-and-post-link.md#layout-after-compiling-propeller-and-bolt).

### Sentinel

A marker file sysforge writes before it changes the installed system and removes once the
change is verified. If one is still there on the next run, that run stopped partway, and
sysforge offers to recover before doing anything else. sysforge-specific; see the man page.

### Soname

The name a shared library declares for itself and that programs record as their dependency,
such as `libfoo.so.1`. It changes when the library breaks its ABI. See
[Shared libraries and ABI](shared-libraries-and-abi.md#how-a-program-finds-its-libraries).

### Split package

One PKGBUILD that builds several installable packages from one source build. See
[How an Arch package is built](arch-packages.md#pkgbase-and-split-packages).

### Training workload

What a program does while its profile is collected. It decides what the optimized build treats
as hot. See [Profile-guided and post-link optimization](pgo-and-post-link.md#the-training-workload-decides-the-result).

### VCS package

A package (usually named `-git`) that builds the latest upstream commit rather than a release,
with its version computed by `pkgver()`. See
[How an Arch package is built](arch-packages.md#vcs-packages-and-pkgver).
