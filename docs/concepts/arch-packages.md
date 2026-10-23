# How an Arch Package Is Built

Every package pacman installs, from the official repos or the AUR, starts as a `PKGBUILD`: a
short bash script that says where the source comes from and how to build and install it.
`makepkg` runs that script and produces a package file that pacman can install. This page
walks through the pieces, since most of the other concept pages assume them.

The [glossary](glossary.md) defines the terms these pages share.

---

## The PKGBUILD

A PKGBUILD sets variables and arrays that describe the package, then defines functions that
build it. A minimal one:

```bash
pkgname=hello
pkgver=2.12.1
pkgrel=1
pkgdesc="GNU hello world"
arch=('x86_64')
url="https://www.gnu.org/software/hello/"
license=('GPL-3.0-or-later')
depends=('glibc')
source=("https://ftp.gnu.org/gnu/hello/hello-$pkgver.tar.gz")
sha256sums=('…')

build() {
  cd "hello-$pkgver"
  ./configure --prefix=/usr
  make
}

package() {
  cd "hello-$pkgver"
  make DESTDIR="$pkgdir" install
}
```

### Version fields

- **`pkgver`** is the upstream version.
- **`pkgrel`** counts builds of that version. It goes up when the PKGBUILD changes without a
  new upstream version (a fix, a rebuild against a new library) and resets to `1` on the next
  `pkgver`.
- **`epoch`** (optional) overrides everything else, for when upstream's version scheme changes
  in a way that would otherwise sort as a downgrade.

The full version is `epoch:pkgver-pkgrel`, and pacman compares versions with the rules
`vercmp` implements, not as plain strings: `1.10` is newer than `1.9`.

### Arrays

Some fields are plain strings and some are bash arrays, and makepkg checks which is which:
`depends=glibc` is refused with `depends should be an array`, even for a single element.

| Array | Meaning |
|---|---|
| `depends` | Needed to run the package; installed with it |
| `makedepends` | Needed only to build it |
| `checkdepends` | Needed only to run its test suite (`check()`) |
| `optdepends` | Enables extra features; shown to the user, never installed automatically |
| `provides`, `conflicts`, `replaces` | Virtual names it satisfies, packages it cannot coexist with, packages it supersedes |
| `source` | Files to download or copy in, one per element |
| `sha256sums`, `b2sums`, … | One checksum per `source` element, in the same order |
| `validpgpkeys` | Fingerprints allowed to sign signed sources |
| `options` | Per-package build switches that override `makepkg.conf`, such as `!lto` or `!strip` |

### Functions

makepkg calls them in this order, skipping the ones that are not defined:

1. **`prepare()`** applies patches and other changes to the extracted source.
2. **`pkgver()`** computes the version for a VCS package (below), from the prepared source.
3. **`build()`** compiles.
4. **`check()`** runs the test suite (skipped with `--nocheck`).
5. **`package()`** installs the result into `$pkgdir`, a fake root that becomes the package's
   contents. It runs under `fakeroot`, so files can be owned by root without real root access.

---

## makepkg and makepkg.conf

`makepkg` reads its settings from `/etc/makepkg.conf`, the drop-in files in
`/etc/makepkg.conf.d/`, and a per-user file (`~/.config/pacman/makepkg.conf` or
`~/.makepkg.conf`), later files overriding earlier ones. The settings that most change the
result:

- **`CFLAGS`, `CXXFLAGS`, `LDFLAGS`, `RUSTFLAGS`** are exported to every build. This is where
  the default optimization and hardening flags come from (see
  [Compiler optimization basics](compiler-optimization.md)).
- **`MAKEFLAGS`** passes options such as `-j$(nproc)` to `make`.
- **`BUILDENV`** turns build-time features on or off: `ccache`, `check`, `sign`, `color`.
- **`OPTIONS`** controls packaging steps: `strip`, `debug` (a separate `-debug` package with
  symbols), `lto`, `docs`, `emptydirs`.

Each PKGBUILD can override `OPTIONS` entries with its own `options` array, and a package that
breaks under LTO, for example, sets `options=('!lto')`.

### Where things end up

| Variable | Default | Holds |
|---|---|---|
| `BUILDDIR` | The PKGBUILD's directory | The `src/` (extracted, built source) and `pkg/` (the fake root) directories |
| `PKGDEST` | The PKGBUILD's directory | The finished `.pkg.tar.zst` files |
| `SRCDEST` | The PKGBUILD's directory | Downloaded sources, reused across builds |
| `LOGDEST` | The PKGBUILD's directory | Build logs, with `--log` |

Pointing `BUILDDIR` at a separate disk, or at a tmpfs for small packages, keeps build
products out of your PKGBUILD checkouts. Pointing `PKGDEST` at one directory collects every
built package in one place.

---

## pkgbase and split packages

One PKGBUILD can produce several packages. Set `pkgname` to an array and define a
`package_<name>()` function for each:

```bash
pkgbase=foo
pkgname=('foo' 'foo-docs')

build() { …; }
package_foo()      { …; }
package_foo-docs() { …; }
```

This is a **split package**. The source is downloaded and built once, and each `package_*`
function picks its share of the result. **`pkgbase`** names the PKGBUILD as a whole (and the
AUR or repo entry it lives under); each `pkgname` is something pacman installs. Variables such
as `depends` and `pkgdesc` can be overridden inside each `package_*` function. A single
`makepkg` builds all of them, so updating one member means rebuilding all of them.

The official repos use this heavily: LLVM's PKGBUILD, for example, builds `llvm`,
`llvm-libs`, `clang`, `lld` and more from one source tree.

---

## .SRCINFO

The AUR needs to know a package's name, version and dependencies without running untrusted
bash. `.SRCINFO` is that metadata, written out in a plain key/value format by
`makepkg --printsrcinfo > .SRCINFO` and committed next to the PKGBUILD. Tools that only need
to read the metadata parse `.SRCINFO`. It can go stale if someone edits the PKGBUILD and forgets
to regenerate it.

---

## VCS packages and pkgver()

A package named `-git` (or `-svn`, `-hg`, …) builds whatever the upstream repository holds
right now instead of a release tarball:

```bash
pkgname=foo-git
pkgver=r1234.abc1234
source=("git+https://example.org/foo.git")
sha256sums=('SKIP')

pkgver() {
  cd foo
  printf "r%s.%s" "$(git rev-list --count HEAD)" "$(git rev-parse --short=7 HEAD)"
}
```

The `pkgver` written in the file is only a placeholder. After fetching and `prepare()`, makepkg runs `pkgver()`
to compute the real version from the checkout and writes it back into the PKGBUILD. The
checksum is `SKIP`, because the content changes with every commit. Knowing whether such a
package has an update means asking the remote repository for its latest commit, since the
PKGBUILD itself never changes.

---

## Checksums and signatures

- **Checksums** (`sha256sums`, `b2sums`) prove that what you downloaded is byte-for-byte what
  the PKGBUILD's author downloaded. They catch a corrupted or swapped download. They say
  nothing about whether that file was trustworthy to begin with.
- **Signatures** (a `.sig` or `.asc` source next to the file it signs, checked against
  `validpgpkeys`) prove that a specific upstream developer signed the release. makepkg checks
  them with your GnuPG keyring, so the signer's public key must be imported first.

Neither one reviews the PKGBUILD itself, which is ordinary bash that runs as your user. For an
AUR package, reading the PKGBUILD and its changes before building is the only real protection.

---

## Clean builds

A build on your own system can pick up any library that happens to be installed, even one the
PKGBUILD never declares, so it may work for you and fail for anyone else. A **clean chroot**
build (with `devtools`' `makechrootpkg`) starts from a minimal root and installs only the
declared dependencies, which proves the PKGBUILD's dependency lists are complete. The official
repos are built this way.

---

## Further reading

- Arch wiki: [PKGBUILD](https://wiki.archlinux.org/title/PKGBUILD),
  [makepkg](https://wiki.archlinux.org/title/Makepkg),
  [Creating packages](https://wiki.archlinux.org/title/Creating_packages),
  [VCS package guidelines](https://wiki.archlinux.org/title/VCS_package_guidelines),
  [.SRCINFO](https://wiki.archlinux.org/title/.SRCINFO),
  [DeveloperWiki: Building in a clean chroot](https://wiki.archlinux.org/title/DeveloperWiki:Building_in_a_clean_chroot)
- `PKGBUILD(5)`, `makepkg(8)`, `makepkg.conf(5)`, `vercmp(8)`

---

## In sysforge

sysforge builds every package with makepkg, giving each build a temporary `makepkg.conf`
with the flags of the profile that matches it. The [getting started guide](../guides/getting-started.md) covers
building, tracking and updating packages; the [freeze guide](../guides/freeze.md) covers reviewing
changed PKGBUILDs and fetching PGP keys.
