# Maintainer: Evangelos Foutras <foutrelis@archlinux.org>
# Contributor: Jan "heftig" Steffens <jan.steffens@gmail.com>

PGO_BUILD=${PGO_BUILD:-0}
pkgname=('llvm' 'llvm-libs')
pkgver=22.1.0
pkgrel=2
groups=('modified')
arch=('x86_64')
url="https://llvm.org/"
license=('Apache-2.0 WITH LLVM-exception')
makedepends=(
  'cmake' 'ninja' 'zlib' 'zstd' 'curl' 'libffi' 'libedit' 'libxml2'
  'python-setuptools' 'python-psutil' 'python-sphinx'
  'python-myst-parser' 'python-build' 'python-installer' 'python-wheel'
)
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
# installing static libraries; inspired by source-based distros
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
  export CFLAGS=${CFLAGS/-g /-g1 }
  export CXXFLAGS=${CXXFLAGS/-g /-g1 }

  # PGO requires Thin LTO; Full LTO conflicts with per-TU instrumentation
  local lto_type="FULL"
  if [[ $PGO_BUILD -eq 1 ]]; then
    lto_type="THIN"
  fi
  if [[ $CC == "gcc" ]]; then
    lto_type="OFF"
  fi

  local cmake_args=(
    -G Ninja
    -DCMAKE_BUILD_TYPE=Release
    -DCMAKE_C_COMPILER=clang
    -DCMAKE_CXX_COMPILER=clang++
    -DCMAKE_AR=/usr/bin/llvm-ar
    -DCMAKE_RANLIB=/usr/bin/llvm-ranlib
    -DLLVM_USE_LINKER=lld
    -DCMAKE_INSTALL_DOCDIR=share/doc
    -DCMAKE_INSTALL_PREFIX=/usr
    -DCMAKE_SKIP_RPATH=ON
    -DLLVM_BINUTILS_INCDIR=/usr/include
    -DLLVM_BUILD_DOCS=OFF
    -DLLVM_BUILD_LLVM_DYLIB=ON
    -DLLVM_BUILD_TESTS=OFF
    -DLLVM_ENABLE_BINDINGS=OFF
    -DLLVM_ENABLE_CURL=ON
    -DLLVM_ENABLE_FFI=ON
    -DLLVM_ENABLE_LTO="$lto_type"
    -DLLVM_ENABLE_PIC=ON
    -DLLVM_ENABLE_RTTI=ON
    -DLLVM_ENABLE_SPHINX=ON
    -DLLVM_HOST_TRIPLE=$CHOST
    -DLLVM_INCLUDE_BENCHMARKS=OFF
    -DLLVM_INSTALL_GTEST=ON
    -DLLVM_INSTALL_UTILS=ON
    -DLLVM_LINK_LLVM_DYLIB=ON
    -DLLVM_USE_PERF=ON
    -DSPHINX_WARNINGS_AS_ERRORS=OFF
    -DPACKAGE_BUGREPORT=https://gitlab.archlinux.org/archlinux/packaging/packages/llvm/-/issues
  )

  if [[ $PGO_BUILD -eq 1 ]]; then
    # Stage 1: instrumented build (no -flto, instrumentation handles its own flags)
    cmake -B "$srcdir/build-instrumented" -S "$srcdir/llvm-project-$pkgver.src/llvm" \
      "${cmake_args[@]}" \
      -DLLVM_ENABLE_LTO=OFF \
      -DLLVM_BUILD_INSTRUMENTED=IR || return 1
    ninja -C "$srcdir/build-instrumented" || return 1

    # Start background merger
    ~/scripts/bg-profmerge.sh &
    BGMERGE_PID=$!

    # Stage 2: profiling run
    cmake -B "$srcdir/build-profiling" -S "$srcdir/llvm-project-$pkgver.src/llvm" \
      "${cmake_args[@]}" \
      -DLLVM_TABLEGEN="$srcdir/build-instrumented/bin/llvm-tblgen" || return 1
    ninja -C "$srcdir/build-profiling" || return 1


    # Stop background merger cleanly
    touch "$HOME/kernel-prof/.stop"
    wait $BGMERGE_PID 2>/dev/null

    # Final merge
    llvm-profdata merge \
      -output="/var/tmp/llvm-self.profdata" \
      "$HOME/kernel-prof/merged-temp.profdata" \
      "$HOME/pgo"/*.profraw 2>/dev/null || \
      llvm-profdata merge \
      -output="/var/tmp/llvm-self.profdata" \
      "$HOME/kernel-prof/merged-temp.profdata" || return 1
  fi

  # Stage 3 / normal build — always runs
  cmake -B "$srcdir/build" -S "$srcdir/llvm-project-$pkgver.src/llvm" \
    "${cmake_args[@]}" \
    $([[ $PGO_BUILD -eq 1 ]] && echo -DLLVM_PROFDATA_FILE="/var/tmp/llvm-self.profdata") || return 1
  cd "$srcdir/build"
  local distribution_components=$(_get_distribution_components | paste -sd\;)
  test -n "$distribution_components" || return 1
  cmake -B "$srcdir/build" -S "$srcdir/llvm-project-$pkgver.src/llvm" \
    "${cmake_args[@]}" \
    $([[ $PGO_BUILD -eq 1 ]] && echo -DLLVM_PROFDATA_FILE="/var/tmp/llvm-self.profdata") \
    -DLLVM_DISTRIBUTION_COMPONENTS="$distribution_components" || return 1
  ninja -C "$srcdir/build" || return 1

  # Include lit for running lit-based tests in other projects
  pushd "$srcdir/llvm-project-$pkgver.src/llvm/utils/lit"
  python -m build --wheel --no-isolation
  popd
}

package_llvm() {
  pkgdesc="Compiler infrastructure"
  depends=('llvm-libs' 'curl' 'perl' 'libstdc++' 'glibc' 'libgcc')

  cd "$srcdir/build"
  DESTDIR="$pkgdir" ninja install-distribution

  pushd "$srcdir/llvm-project-$pkgver.src/llvm/utils/lit"
  python -m installer --destdir="$pkgdir" dist/*.whl
  popd

  mv -f "$pkgdir"/usr/lib/lib{LLVM,LTO,Remarks}*.so* "$srcdir"
  mv -f "$pkgdir"/usr/lib/LLVMgold.so "$srcdir"
  install -Dm644 "$srcdir/llvm-project-$pkgver.src/LICENSE.TXT" \
    "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}

package_llvm-libs() {
  pkgdesc="LLVM runtime libraries"
  depends=('libgcc' 'glibc' 'libstdc++' 'zlib' 'zstd' 'libffi' 'libedit' 'libxml2')
  provides=('libLLVM.so' 'libLTO.so' 'libRemarks.so')

  install -d "$pkgdir/usr/lib"
  cp -P \
    "$srcdir"/lib{LLVM,LTO,Remarks}*.so* \
    "$srcdir"/LLVMgold.so \
    "$pkgdir/usr/lib/"

    # Symlink LLVMgold.so from /usr/lib/bfd-plugins
    # https://bugs.archlinux.org/task/28479
    install -d "$pkgdir/usr/lib/bfd-plugins"
    ln -s ../LLVMgold.so "$pkgdir/usr/lib/bfd-plugins/LLVMgold.so"

    install -Dm644 "$srcdir/llvm-project-$pkgver.src/LICENSE.TXT" \
      "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}

# vim:set ts=2 sw=2 et:
