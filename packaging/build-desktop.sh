#!/usr/bin/env bash
# Build on Debian ARM64, including the uConsole. No changes to Cardputer output.
set -euo pipefail
umask 022
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
ARCH=$(dpkg --print-architecture)
if [ "$ARCH" != arm64 ]; then
    echo "Build this desktop package on ARM64 Debian (uConsole)." >&2
    exit 1
fi
make decoder
VERSION=$(python3 -c 'import sys; sys.path.insert(0,"app"); from version import VERSION; print(VERSION)')
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
export STAGE APP_INSTALL_DIR=/usr/lib/icomzero INSTALL_PREFIX=/usr
# Shared sources, helpers, datasets and notices; use the standard desktop prefix.
bash packaging/stage.sh
rm "$STAGE/usr/bin/icomzero"
install -m 755 packaging/desktop/icomzero "$STAGE/usr/bin/icomzero"
install -D -m 644 packaging/desktop/icomzero.desktop "$STAGE/usr/share/applications/icomzero.desktop"
install -D -m 644 packaging/icon.png "$STAGE/usr/share/icons/hicolor/48x48/apps/icomzero.png"
printf 'desktop\n' > "$STAGE/usr/lib/icomzero/desktop-platform"
mkdir -p "$STAGE/DEBIAN"
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: icomzero
Version: $VERSION
Architecture: arm64
Maintainer: unagisan69
Section: hamradio
Priority: optional
X-ICOMzero-Platform: desktop
Depends: libc6, python3, python3-tk, python3-pil, python3-pil.imagetk, python3-numpy, fonts-dejavu-core, alsa-utils, libasound2-plugins, pipewire-pulse
Homepage: https://github.com/unagisan69/ICOMzero
Description: Network radio, CW, FT8 and logbook for uConsole/Linux ARM64
 Keyboard-first scalable desktop interface using the native radio protocols.
CONTROL
chmod 755 "$STAGE" "$STAGE/DEBIAN"
umask 022
OUTPUT="$ROOT/build/icomzero-desktop_${VERSION}-1_arm64.deb"
dpkg-deb --root-owner-group --build "$STAGE" "$OUTPUT"
printf 'Built %s\n' "$OUTPUT"
