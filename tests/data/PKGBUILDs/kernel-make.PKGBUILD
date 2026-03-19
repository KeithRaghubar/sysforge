pkgbase=linux-custom
pkgname=('linux-custom' 'linux-custom-headers')
pkgver=6.12

build() {
  make LOCALVERSION="$(date +%Y%m%d)" all
}

package_linux-custom() {
  make INSTALL_MOD_PATH="$pkgdir/usr" modules_install
}
