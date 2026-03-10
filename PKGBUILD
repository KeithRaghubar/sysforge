# Maintainer: Keith Raghubar <keith.raghubar@proton.me>

pkgname=sysforge
pkgver=0.1.0
pkgrel=1
pkgdesc="Reproducible, performance-tuned Arch Linux installer"
arch=('any')
url="https://github.com/youruser/sysforge"
license=('MIT')
depends=(
    'python>=3.11'
)
makedepends=(
    'git'
    'python-build'
    'python-installer'
    'python-wheel'
)
optdepends=(
    'uv: faster python dep management'
)
source=("$pkgname-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz")
sha256sums=('SKIP')

build() {
    cd "$srcdir/$pkgname-$pkgver"
    python -m build --wheel --no-isolation
}

package() {
    cd "$srcdir/$pkgname-$pkgver"
    python -m installer --destdir="$pkgdir" dist/*.whl
    install -Dm644 configs/flag_profiles.toml "$pkgdir/etc/sysforge/flag_profiles.toml"
}
