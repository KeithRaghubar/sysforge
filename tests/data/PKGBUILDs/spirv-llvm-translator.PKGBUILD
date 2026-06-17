# Test fixture — the multi-line makedepends=(... cmake ...) + `cmake -S … -B build`
# shape that bricked `sysforge run toolchain` (makepkg exit 12): the loose
# `^([ \t]*)cmake\b` anchor matched the bare `cmake` *dependency* on line below,
# so the -DLLVM_DIR injection spliced into the makedepends array. Trimmed from the
# real spirv-llvm-translator PKGBUILD; do not "tidy" the array onto one line — the
# multi-line form is the whole point of this fixture.
pkgname=spirv-llvm-translator
pkgver=22.1.2
pkgrel=1
pkgdesc="LLVM <-> SPIR-V converter for compilers targeting SPIR-V"
url="https://www.khronos.org/spirv/"
arch=(x86_64)
license=(NCSA)
depends=(
  glibc
  libstdc++
  llvm-libs
  spirv-tools
)
makedepends=(
  cmake
  git
  llvm
  ninja
  spirv-headers
)
checkdepends=(
  clang
  python
)
source=(
  git+https://github.com/KhronosGroup/SPIRV-LLVM-Translator#tag=v$pkgver
)
b2sums=('SKIP')

build() {
  local cmake_options=(
    -D BUILD_SHARED_LIBS=ON
    -D CMAKE_BUILD_TYPE=Release
    -D CMAKE_INSTALL_PREFIX=/usr
    -D LLVM_CONFIG=llvm-config
    -W no-dev
  )

  cmake -S SPIRV-LLVM-Translator -B build -G Ninja "${cmake_options[@]}"
  cmake --build build
}

package() {
  DESTDIR="$pkgdir" cmake --install build
}
