# Maintainer: Keith Raghubar <keith.raghubar@proton.me>

pkgname=sysforge
pkgver=0.1.0
pkgrel=1
pkgdesc="All-in-one Arch Linux helper for system setup and package management with compiler-optimized builds"
arch=('any')
url="https://github.com/KeithRaghubar/sysforge"
license=('MIT')
depends=(
    'python>=3.11'
)
makedepends=(
    'git'
    'python-build'
    'python-installer'
    'python-wheel'
    'python-hatchling'
)
optdepends=(
    'uv: faster Python environment management'
    'ccache: compiler cache support'
    'sccache: Rust compiler cache support'
    'zsh: shell completions'
)
conflicts=('sysforge-git')
provides=('sysforge')
source=("$pkgname-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz")
sha256sums=('SKIP')  # TODO: fill in before AUR submission

build() {
    cd "$srcdir/$pkgname-$pkgver"
    python -m build --wheel --no-isolation
}

package() {
    cd "$srcdir/$pkgname-$pkgver"
    python -m installer --destdir="$pkgdir" dist/*.whl

    # Zsh completion
    install -Dm644 completions/_sysforge \
        "$pkgdir/usr/share/zsh/site-functions/_sysforge"

    # TODO: ship a default /etc/sysforge/flag_profiles.toml
    # Needs a canonical example config path in the repo (not tests/data/).
}
