pkgname=lib32-rust-stub
pkgver=1.0.0
pkgrel=1
arch=('x86_64')
url="https://example.invalid/"
license=('MIT')

# Plain makedepends has lib32-meson; the rust toolchain only appears under
# the arch-specific array. The static parser must merge the two so consumes
# inference sees ``lib32-rust`` and emits the i686 cross-probe token.
makedepends=('meson')
makedepends_x86_64=('lib32-rust')

source=()
sha256sums=()

build() {
  # PKGBUILDs in the wild pin the rust toolchain inline. The preflight must
  # detect this regex and probe the named toolchain, not the workstation
  # default.
  export RUSTUP_TOOLCHAIN=stable
  cd "$srcdir"
  meson setup build
  meson compile -C build
}

package() {
  cd "$srcdir/build"
  DESTDIR="$pkgdir" meson install
}
