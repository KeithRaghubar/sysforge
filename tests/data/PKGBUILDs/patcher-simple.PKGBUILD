pkgname=mypkg
pkgver=1.0
pkgrel=1
groups=('mygroup')

build() {
  export CFLAGS+=" -fno-stack-protector"
  export LDFLAGS+=" -Wl,--gc-sections"
  make
}
