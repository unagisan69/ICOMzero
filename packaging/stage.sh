#!/bin/sh
set -eu
install -D -m 755 app/icomzero "$STAGE$APP_INSTALL_DIR/icomzero"
install -D -m 644 app/radio.py "$STAGE$APP_INSTALL_DIR/radio.py"
install -D -m 644 LICENSE "$STAGE/usr/share/doc/icomzero/copyright"
install -D -m 644 CHANGELOG.md "$STAGE/usr/share/doc/icomzero/CHANGELOG.md"
install -d "$STAGE/usr/bin"
ln -s "$APP_INSTALL_DIR/icomzero" "$STAGE$INSTALL_PREFIX/bin/icomzero"
ln -s "$APP_INSTALL_DIR/icomzero" "$STAGE/usr/bin/icomzero"
