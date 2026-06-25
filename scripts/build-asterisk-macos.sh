#!/usr/bin/env bash
# Build Asterisk 22 from source on macOS Apple Silicon — no sudo (prefix=$HOME/asterisk).
#
# macOS dropped from Homebrew core, and Docker-on-Mac mangles SIP RTP, so on a Mac
# we compile. Adapted from https://github.com/nedimzecic/asterisk-macos:
#   - prefix /opt/sangoma -> $HOME/asterisk (removes the only sudo step)
#   - non-interactive menuselect (CLI, not the ncurses TUI) so it runs headless
#   - openssl@3 keg-only pkgconfig added to PKG_CONFIG_PATH
#   - AudioSocket modules explicitly enabled (the bridge depends on them)
#
# Logs to ~/asterisk-build.log; on exit writes ~/asterisk-build.status (exit code)
# and touches ~/asterisk-build.done so a watcher can detect completion.
set -euo pipefail
trap 'rc=$?; echo "BUILD_EXIT=$rc @ $(date)"; echo "$rc" > "$HOME/asterisk-build.status"; touch "$HOME/asterisk-build.done"' EXIT

eval "$(/opt/homebrew/bin/brew shellenv)"
PREFIX="$HOME/asterisk"
SRC="$HOME/asterisk-src"
PJ=2.15.1
AST=22.2.0
PB="https://raw.githubusercontent.com/nedimzecic/asterisk-macos/master"
OSSL="/opt/homebrew/opt/openssl@3/lib/pkgconfig"

echo "=== [1/4] brew deps ==="
brew install jansson libpq lua openssl@3 pkgconf portaudio postgresql@17 sqlite srtp unixodbc wget

mkdir -p "$PREFIX" "$SRC"; cd "$SRC"

echo "=== [2/4] pjproject $PJ ==="
[ -d "pjproject-$PJ" ] || { wget -q "https://github.com/pjsip/pjproject/archive/refs/tags/$PJ.tar.gz"; tar xzf "$PJ.tar.gz"; rm -f "$PJ.tar.gz"; }
curl -fsSL "$PB/pjproject.patch" -o pjproject.patch
patch -p2 --forward --directory="pjproject-$PJ" < pjproject.patch || true
cd "pjproject-$PJ"
export PKG_CONFIG_PATH="/opt/homebrew/lib/pkgconfig:$OSSL"
export CFLAGS="-I/opt/homebrew/include -O2 -DNDEBUG"
export LDFLAGS="-L/opt/homebrew/lib"
./configure --prefix="$PREFIX" --enable-shared --with-ssl \
  --disable-resample --disable-video --disable-opencore-amr --disable-speex-codec \
  --disable-speex-aec --disable-bcg729 --disable-gsm-codec --disable-ilbc-codec \
  --disable-l16-codec --disable-g711-codec --disable-g722-codec --disable-g7221-codec \
  --disable-silk --disable-opus --disable-v4l2 --disable-sound --disable-ext-sound \
  --disable-sdl --disable-libyuv --disable-ffmpeg --disable-openh264 --disable-ipp \
  --disable-libwebrtc --with-external-pa --with-external-srtp
make dep && make && make install
cd "$SRC"

echo "=== [3/4] asterisk $AST ==="
[ -d "asterisk-$AST" ] || { wget -q "https://github.com/asterisk/asterisk/releases/download/$AST/asterisk-$AST.tar.gz"; tar xzf "asterisk-$AST.tar.gz"; rm -f "asterisk-$AST.tar.gz"; }
curl -fsSL "$PB/asterisk.patch" -o asterisk.patch
patch -p2 --forward --directory="asterisk-$AST" < asterisk.patch || true
cd "asterisk-$AST"
export PKG_CONFIG_PATH="/opt/homebrew/lib/pkgconfig:$PREFIX/lib/pkgconfig:$OSSL"
export CFLAGS="-I/opt/homebrew/include -I$PREFIX/include"
export LDFLAGS="-L/opt/homebrew/lib -L$PREFIX/lib"
./configure --prefix="$PREFIX" --without-pjproject-bundled --with-pjproject \
  --without-iodbc --with-unixodbc=/opt/homebrew/opt/unixodbc/lib \
  --with-sqlite3=/opt/homebrew/opt/sqlite/lib
make menuselect.makeopts
menuselect/menuselect --disable res_geolocation --disable res_prometheus \
  --enable app_audiosocket --enable res_audiosocket menuselect.makeopts
make -j"$(sysctl -n hw.ncpu)"
make install
make samples

echo "=== [4/4] verify ==="
"$PREFIX/sbin/asterisk" -V
if ls "$PREFIX"/lib/asterisk/modules/ | grep -qi audiosocket; then
  echo "AudioSocket: BUILT"
else
  echo "AudioSocket: MISSING (problem!)"
fi
echo "=== build complete ==="
