pkgname=mypkg
pkgver=1.0
pkgrel=1

build() {
  if [[ $CARCH == x86_64 ]]; then
    CFLAGS+=" -m32"
  fi
  make
}
