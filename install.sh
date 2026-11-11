#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# raceroom-moza-leds -- user-level install (no root).
#
# Installs the daemon under $PREFIX/share, a launcher in $PREFIX/bin and the
# systemd user unit. It does NOT enable or start anything: the wheelbase serial
# port is a single resource shared with boxflat and lmu-rpm-leds, so starting
# the service is a decision, not a side effect of installing.
#
# For a system-wide install use the PKGBUILD in the repository root instead.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
NAME=raceroom-moza-leds
SHARE="$PREFIX/share/$NAME"
BIN="$PREFIX/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$SHARE" "$BIN" "$UNIT_DIR"
install -m644 "$SRC"/*.py "$SHARE"/
install -m644 "$SRC/README.md" "$SRC/LICENSE" "$SHARE"/

# The launcher works out the share directory from its own location, so the same
# file serves a user install and a packaged one.
install -m755 "$SRC/packaging/$NAME-daemon" "$BIN/$NAME-daemon"

install -m644 "$SRC/packaging/$NAME.service" "$UNIT_DIR/"
systemctl --user daemon-reload

echo "Instalado en:"
echo "  $SHARE"
echo "  $BIN/$NAME-daemon"
echo "  $UNIT_DIR/$NAME.service"
echo
echo "Para probar sin tocar el volante:"
echo "  $BIN/$NAME-daemon --dry-run --verbose"
echo
echo "Para activarlo:"
echo "  systemctl --user enable --now $NAME.service"
echo
echo "OJO: lmu-rpm-leds y $NAME escriben al mismo puerto serie."
echo "No los tengas los dos activos a la vez."
