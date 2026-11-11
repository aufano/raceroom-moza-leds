# Maintainer: Alberto Ufano <aufano@gmail.com>
#
# System-wide install. Run `makepkg -si` inside a checkout of this repository:
# the sources are taken from the tree itself ($startdir), so no tarball is
# fetched and the package always matches the commit you are looking at. Once the
# repository is tagged, `source=("$pkgname-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz")`
# with a real checksum is the better form for anything published.
pkgname=raceroom-moza-leds
pkgver=1.0.0
pkgrel=1
pkgdesc="Drive a Moza wheel's RPM LEDs from RaceRoom telemetry on Linux, without SimHub"
arch=('any')
url="https://github.com/aufano/raceroom-moza-leds"
license=('GPL-3.0-or-later')
depends=('python')
source=()
sha256sums=()

package() {
  local share="$pkgdir/usr/share/$pkgname"

  install -d "$share"
  install -m644 "$startdir"/*.py "$share"/
  install -m644 "$startdir/README.md" "$share"/

  # The launcher derives the share directory from its own location, so the
  # /usr/bin copy finds /usr/share/raceroom-moza-leds on its own.
  install -Dm755 "$startdir/packaging/$pkgname-daemon" \
    "$pkgdir/usr/bin/$pkgname-daemon"
  install -Dm644 "$startdir/packaging/$pkgname.service" \
    "$pkgdir/usr/lib/systemd/user/$pkgname.service"
  install -Dm644 "$startdir/LICENSE" \
    "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
