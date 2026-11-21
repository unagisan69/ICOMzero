#!/bin/sh
set -eu
chmod 755 "$STAGE"
install -D -m 755 app/icomzero "$STAGE$APP_INSTALL_DIR/icomzero"
install -m 644 app/radio.py app/civ.py app/models.py app/radios.json app/kenwood.py app/ft8.py app/ft8log.py app/ft8qso.py app/ft8station.py app/ft8tx.py app/meters.py app/ft8auto.py app/notifications.py "$STAGE$APP_INSTALL_DIR/"
install -m 755 build/icomzero-ft8-decode build/icomzero-ft8-encode "$STAGE$APP_INSTALL_DIR/"
install -D -m 644 vendor/ft8_lib/LICENSE "$STAGE/usr/share/doc/icomzero/ft8_lib-LICENSE"
install -D -m 644 vendor/ft8_lib/fft/COPYING "$STAGE/usr/share/doc/icomzero/kissfft-COPYING"
install -D -m 644 vendor/ft8_lib/fft/LICENSES/BSD-3-Clause "$STAGE/usr/share/doc/icomzero/kissfft-LICENSE"
install -D -m 644 LICENSE "$STAGE/usr/share/doc/icomzero/copyright"
install -D -m 644 CHANGELOG.md "$STAGE/usr/share/doc/icomzero/CHANGELOG.md"
install -d "$STAGE/usr/bin"
ln -s "$APP_INSTALL_DIR/icomzero" "$STAGE$INSTALL_PREFIX/bin/icomzero"
ln -s "$APP_INSTALL_DIR/icomzero" "$STAGE/usr/bin/icomzero"
