# Maintainer: Laurent Carlier <lordheavym@gmail.com>
# Contributor: Evangelos Foutras <foutrelis@gmail.com>
# Contributor: Jan "heftig" Steffens <jan.steffens@gmail.com>

pkgname=('lib32-llvm' 'lib32-llvm-libs')
pkgver=22.1.0
pkgrel=1
epoch=1
groups=('modified')
arch=('x86_64')
url="https://llvm.org/"
license=('Apache-2.0 WITH LLVM-exception')
makedepends=('cmake' 'ninja' 'lib32-zlib' 'lib32-zstd' 'lib32-libffi'
             'lib32-libxml2' 'python')
options=('staticlibs' '!lto') # tools/llvm-shlib/typeids.test fails with LTO
_source_base=https://github.com/llvm/llvm-project/releases/download/llvmorg-$pkgver
source=($_source_base/llvm-project-$pkgver.src.tar.xz{,.sig})
sha256sums=('25d2e2adc4356d758405dd885fcfd6447bce82a90eb78b6b87ce0934bd077173'
            'SKIP')
validpgpkeys=('474E22316ABF4785A88C6E8EA2C794A986419D8A'  # Tom Stellard <tstellar@redhat.com>
              'D574BD5D1D0E98895E3BF90044F2485E45D59042'  # Tobias Hieta <tobias@hieta.se>
              'FFB3368980F3E6BB5737145A316C56D064CACBA5'  # Douglas Yung <douglas.yung@sony.com>
              '71046D1E9C6656BDD61171873E83BABF4A4F9E85'  # Cullen Rhodes <cullen.rhodes@arm.com>
)

# Utilizing LLVM_DISTRIBUTION_COMPONENTS to avoid
# installing static libraries; inspired by Gentoo
_get_distribution_components() {
  local target
  ninja -t targets | grep -Po 'install-\K.*(?=-stripped:)' | while read -r target; do
    case $target in
      llvm-libraries|distribution)
        continue
        ;;
      # shared libraries
      LLVM|LLVMgold)
        ;;
      # libraries needed for clang-tblgen
      LLVMDemangle|LLVMSupport|LLVMTableGen)
        ;;
      # used by lldb
      LLVMDebuginfod)
        ;;
      # testing libraries
      LLVMTestingAnnotations|LLVMTestingSupport)
        ;;
      # exclude static libraries
      LLVM*)
        continue
        ;;
      # exclude llvm-exegesis (doesn't seem useful without libpfm)
      llvm-exegesis)
        continue
        ;;
    esac
    echo $target
  done
}

prepare() {
  cd llvm-project-$pkgver.src/llvm
  mkdir build

  # Remove CMake find module for zstd; breaks if out of sync with upstream zstd
  rm cmake/modules/Findzstd.cmake
}

build() {
  export CFLAGS+=" -fno-stack-protector"
  export CXXFLAGS="$CFLAGS"
  export LDFLAGS+=" -Wl,--gc-sections"
  cd llvm-project-$pkgver.src/llvm/build

  # Build only minimal debug info to reduce size
  CFLAGS=${CFLAGS/-g /-g1 }
  CXXFLAGS=${CXXFLAGS/-g /-g1 }

  local cmake_args=(
    -G Ninja
    -DCMAKE_BUILD_TYPE=Release
    -DCMAKE_INSTALL_DOCDIR=share/doc
    -DCMAKE_INSTALL_PREFIX=/usr
    -DCMAKE_SKIP_RPATH=ON
    -DLLVM_BINUTILS_INCDIR=/usr/include
    -DLLVM_BUILD_DOCS=OFF
    -DLLVM_BUILD_LLVM_DYLIB=ON
    -DLLVM_BUILD_TESTS=OFF
    -DLLVM_DEFAULT_TARGET_TRIPLE="i686-pc-linux-gnu"
    -DLLVM_ENABLE_BINDINGS=OFF
    -DLLVM_ENABLE_CURL=OFF
    -DLLVM_ENABLE_FFI=ON
    -DLLVM_ENABLE_RTTI=ON
    -DLLVM_ENABLE_SPHINX=OFF
    -DLLVM_HOST_TRIPLE=$CHOST
    -DLLVM_INCLUDE_BENCHMARKS=OFF
    -DLLVM_INSTALL_GTEST=ON
    -DLLVM_LIBDIR_SUFFIX=32
    -DLLVM_LINK_LLVM_DYLIB=ON
    -DLLVM_TARGET_ARCH:STRING=i686
    -DLLVM_USE_PERF=ON
    -DPACKAGE_BUGREPORT=https://gitlab.archlinux.org/archlinux/packaging/packages/lib32-llvm/-/issues
  )

  export CFLAGS+=" -m32"
  export CXXFLAGS+=" -m32"
  export PKG_CONFIG="i686-pc-linux-gnu-pkg-config"

  cmake .. "${cmake_args[@]}"
  local distribution_components=$(_get_distribution_components | paste -sd\;)
  test -n "$distribution_components"
  cmake_args+=(-DLLVM_DISTRIBUTION_COMPONENTS="$distribution_components")

  cmake .. "${cmake_args[@]}"
  ninja
}

package_lib32-llvm() {
  pkgdesc="Compiler infrastructure (32-bit)"
  depends=('lib32-llvm-libs' 'llvm' 'lib32-gcc-libs' 'lib32-glibc')

  cd llvm-project-$pkgver.src/llvm/build

  DESTDIR="$pkgdir" ninja install-distribution

  # The runtime library goes into lib32-llvm-libs
  mv "$pkgdir"/usr/lib32/lib{LLVM,LTO,Remarks}*.so* "$srcdir"
  mv -f "$pkgdir"/usr/lib32/LLVMgold.so "$srcdir"


  rm -rf "$pkgdir"/usr/{include,share/{doc,man,llvm,opt-viewer}}
  find "$pkgdir/usr/bin" -mindepth 1 -not -name llvm-config -delete
  mv "$pkgdir"/usr/bin/llvm-config{,32}

  install -Dm644 "$srcdir/llvm-project-$pkgver.src/llvm/LICENSE.TXT" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}

package_lib32-llvm-libs() {
  pkgdesc="LLVM runtime libraries (32-bit) "
  depends=('lib32-gcc-libs' 'lib32-glibc' 'lib32-zlib' 'lib32-zstd' 'lib32-libffi'
           'lib32-ncurses' 'lib32-libxml2')

  install -d "$pkgdir/usr/lib32"
  cp -P \
    "$srcdir"/lib{LLVM,LTO,Remarks}*.so* \
    "$srcdir"/LLVMgold.so \
    "$pkgdir/usr/lib32/"

  # Symlink LLVMgold.so from /usr/lib/bfd-plugins
  # https://bugs.archlinux.org/task/28479
  install -d "$pkgdir/usr/lib32/bfd-plugins"
  ln -s ../LLVMgold.so "$pkgdir/usr/lib32/bfd-plugins/LLVMgold.so"

  install -Dm644 "$srcdir/llvm-project-$pkgver.src/llvm/LICENSE.TXT" \
    "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}

# vim:set ts=2 sw=2 et:
