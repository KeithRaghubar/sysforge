# Shared Libraries and ABI

Why a program that built and ran fine yesterday can fail to start today with
`error while loading shared libraries` or `undefined symbol`, and why upgrading a library
sometimes means rebuilding everything that uses it. This page covers how programs find their
shared libraries on Linux, what "ABI-compatible" means, and how a library can break its users
without changing its version.

The [glossary](glossary.md) defines the terms these pages share.

---

## How a program finds its libraries

When a program is linked against a shared library, the linker does not copy the library's
code into the program. It records two things: the library's name, and the symbols (functions
and variables) the program needs from it. When the program starts, the dynamic loader
(`ld-linux-x86-64.so.2`) finds each library, maps it into memory and resolves each needed
symbol to an address.

The name recorded is not the file name you linked with but the library's **soname**, a name
the library declares about itself (the `DT_SONAME` entry, set with `-Wl,-soname`). A typical
library ships three names:

| File | What it is | Used by |
|---|---|---|
| `libfoo.so.1.4.2` | The real file, with the full version | Nothing directly |
| `libfoo.so.1` | Symlink named after the soname | The dynamic loader, at run time |
| `libfoo.so` | Symlink for development | The linker, at build time (`-lfoo`) |

The program's `NEEDED` entry says `libfoo.so.1` (see it with `readelf -d program`). Any file
answering to that soname satisfies it, so the library can be updated from 1.4.2 to 1.5.0 in
place, and every program picks up the new version without a rebuild. `ldd program` shows which
file each `NEEDED` entry resolves to on this system.

---

## API and ABI

- **API** (application programming interface) is the source-level contract: the function
  names, argument types and header declarations you write code against. An API change breaks
  the *build*.
- **ABI** (application binary interface) is the compiled contract: the symbol names in the
  binary, the size and layout of every structure passed across the boundary, the values of
  enums and constants, the calling convention, and for C++ the layout of classes and virtual
  tables. An ABI change breaks *already built* programs, which the build system never sees.

Many changes are compatible at the source level and still break the ABI: adding a field to a
public structure changes its size, reordering enum values changes their numbers, changing an
inline function in a header changes code that was already compiled into every user. The user
still compiles, but the binary built before the change now disagrees with the library about
what the bytes mean.

A library is **ABI-compatible** with an older version when every program linked against the
older version still loads and behaves correctly against the newer one. Adding new functions
is compatible. Removing or changing existing ones usually is not.

---

## Why a soname bump forces a rebuild

When a library breaks its ABI, its maintainers change the soname, for example from
`libfoo.so.1` to `libfoo.so.2`. That is a deliberate signal: programs linked against the old
soname must not load the new library, because they would misread it. The dynamic loader looks
for `libfoo.so.1` by name, does not find it once only `libfoo.so.2` is installed, and refuses
to start the program:

    error while loading shared libraries: libfoo.so.1: cannot open shared object file

The fix is to rebuild every program that uses the library, so each records the new soname
and compiles against the new headers. This is why a distribution rebuilds a library's
dependents together when its soname changes, and why the old and new versions of a library
can be installed side by side during a transition (their sonames differ, so they do not
conflict).

---

## Symbol versioning

A soname is one version number for the whole library. GNU symbol versioning gives each
exported symbol its own version tag, assigned by a linker version script. glibc uses this to
keep old behaviour available: when `memcpy` changed semantics, the old one stayed exported as
`memcpy@GLIBC_2.2.5` and the new one became `memcpy@@GLIBC_2.14` (`@@` marks the default
version, the one new programs link against). A program records the exact version it linked
with, so old programs keep getting the old function from the same library file.

The versions a program needs are listed by `objdump -T program` or `readelf -V program`. The
familiar failure is a program built on a newer system and run on an older one:

    version `GLIBC_2.38' not found (required by ./program)

Symbol versioning also changes how a missing symbol fails. A versioned reference such as
`foo@LIBFOO_1` is only satisfied by a library that exports `foo` with that version. Another
library exporting a plain `foo`, or `foo` with another version, does not count.

---

## How C++ standard-library symbols leak through another library

C++ templates, such as `std::string`, are compiled into whatever code uses them. When a
library uses a template, the compiler emits a copy of the functions it needs as weak symbols,
so that duplicates across files merge at link time instead of clashing. If the library
exports all its symbols (the default for symbols with default visibility), those weak copies
become part of its exported interface too, even though they belong to the C++ standard
library.

A program linked against that library can then have its references to, say,
`std::string::_M_assign` satisfied by the library's copy rather than by `libstdc++.so`,
because the linker takes the first definition it sees. With a version script that tags every
export, the program records the reference as `…@LIBNAME_VERSION`, permanently bound to the
library's version tag rather than to libstdc++'s.

libLLVM is the well-known worked case. Its build tags every exported symbol with one version
node (`LLVM_<major>.<minor>`) through a `global: *` version script, which sweeps in the weak
`std::` copies it happens to contain. A program such as a graphics driver linked against it
imports some `std::string` functions as `…@LLVM_<version>`. Now build libLLVM again, same
version and soname, but with different optimization: a build that inlines those functions
everywhere it uses them no longer emits the weak copies, so it no longer exports them. The
soname did not change, nothing told the dependent to rebuild, and yet it fails to load:

    symbol lookup error: …: undefined symbol: _ZNSt7__cxx1112basic_string…, version LLVM_<version>

Rebuilding the dependent against the new library cures it, because the new link finds the
functions in libstdc++ and records `@GLIBCXX_*` instead. The lesson is general: a library's
real ABI is its export table, not its version number, and two builds of the same version can
export different tables.

---

## Checking a binary

| Question | Command |
|---|---|
| Which sonames does it need? | `readelf -d program \| grep NEEDED` |
| Which file does each one resolve to here? | `ldd program` |
| Are any of its symbols unresolvable here? | `ldd -r program` |
| Which versioned symbols does it import? | `objdump -T program \| grep UND` |
| What does a library export, with versions? | `nm -D --defined-only libfoo.so.1` |
| Which soname does a library declare? | `readelf -d libfoo.so.1 \| grep SONAME` |

---

## Further reading

- Ulrich Drepper, [How To Write Shared Libraries](https://akkadia.org/drepper/dsohowto.pdf)
- GNU ld: [VERSION command](https://sourceware.org/binutils/docs/ld/VERSION.html) (version scripts)
- libstdc++: [ABI Policy and Guidelines](https://gcc.gnu.org/onlinedocs/libstdc++/manual/abi.html)
- Arch wiki: [Arch package guidelines](https://wiki.archlinux.org/title/Arch_package_guidelines)
- `ld.so(8)`, `ldd(1)`, `readelf(1)`

---

## In sysforge

sysforge reports **ABI drift**, an installed program that needs a library or symbol the
system no longer provides, through `sysforge doctor pkg --all`; the
[drift guide](../guides/drift.md) explains it beside the other kinds of drift. The toolchain
stage audits a newly built libLLVM against the programs already installed before it installs
it, as described in the toolchain section of the [PGO guide](../guides/pgo.md#toolchain-pgo-optimized-clang).
