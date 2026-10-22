#!/usr/bin/env bash
# Builds the Linux packages from this checkout:
#   <out>/lan-streamer_<version>_amd64.deb             (Debian/Ubuntu: apt install ./…deb)
#   <out>/lan-streamer-<version>-linux-x86_64.tar.gz   (install.sh: apt-get + /opt + systemd)
#
#   packaging/linux/build.sh <version> <folder with the gst-plugins-rs .so files> [out]
#
# GStreamer comes from the distribution; only fallbacksrc and audioloudnorm
# (gst-plugins-rs, not packaged by Ubuntu 24.04) are shipped, built in CI
# against the distribution's GStreamer. The Python packages are bundled as
# wheels for Python 3.12 and 3.13, so installing needs no internet.
set -euo pipefail

VERSION=$1
RSPLUGINS=$2
OUT=${3:-build/linux}
MEDIAMTX_VERSION=v1.21.2
MEDIAMTX_SHA256=121be6e00397e473a95d83c75f4fcd3819a6af3b0e1a50889cba43ab4fab6a27  # linux_amd64

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
STAGE="$OUT/stage"
ROOT="$STAGE/opt/lan-streamer"
DEBVERSION=${VERSION//-/\~}  # 0.0.1-test.42 → 0.0.1~test.42 (sorts before 0.0.1)

step() { echo "== $*"; }

rm -rf "$OUT"
mkdir -p "$ROOT"/{bin,lib,share,wheels,mediamtx,gstreamer} "$STAGE/DEBIAN" "$STAGE/usr/bin"

step "lan-streamer wheel ($VERSION) and its dependencies"
printf '%s' "$VERSION" > "$REPO/src/lanstreamer/BUILD"
trap 'rm -f "$REPO/src/lanstreamer/BUILD"' EXIT
python3 -m pip wheel --quiet --no-deps --wheel-dir "$ROOT/wheels" "$REPO"
rm -f "$REPO/src/lanstreamer/BUILD"
wheel=$(ls "$ROOT"/wheels/lanstreamer-*.whl)
for py in 3.12 3.13; do
    python3 -m pip download --quiet --dest "$ROOT/wheels" --only-binary=:all: \
        --implementation cp --python-version "$py" \
        --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 \
        "$wheel"
done
ls "$ROOT/wheels"

step "MediaMTX $MEDIAMTX_VERSION"
archive="$OUT/download/mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz"
mkdir -p "$OUT/download"
curl -fsSL -o "$archive" "https://github.com/bluenviron/mediamtx/releases/download/$MEDIAMTX_VERSION/$(basename "$archive")"
echo "$MEDIAMTX_SHA256  $archive" | sha256sum -c --quiet -
tar -xzf "$archive" -C "$ROOT/mediamtx" mediamtx mediamtx.yml LICENSE

step "GStreamer plugins from gst-plugins-rs"
cp "$RSPLUGINS"/libgstfallbackswitch.so "$RSPLUGINS"/libgstrsaudiofx.so "$ROOT/gstreamer/"

step "launcher, service, scripts"
install -m 755 "$HERE/lan-streamer" "$ROOT/bin/lan-streamer"
install -m 755 "$HERE/setup.sh" "$HERE/remove.sh" "$ROOT/lib/"
install -m 644 "$HERE/lan-streamer.service" "$HERE/depends.txt" "$ROOT/share/"
install -m 644 "$REPO/LICENSE" "$ROOT/LICENSE"
ln -s /opt/lan-streamer/bin/lan-streamer "$STAGE/usr/bin/lan-streamer"

step "Debian package"
depends=$(grep -v '^#' "$HERE/depends.txt" | paste -sd, - | sed 's/,/, /g')
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: lan-streamer
Version: $DEBVERSION
Architecture: amd64
Maintainer: zGLados <https://github.com/zGLados/lan-streamer>
Depends: $depends
Section: video
Priority: optional
Homepage: https://github.com/zGLados/lan-streamer
Description: self-hosted modular streaming platform for the LAN
 Input plugins (short-video submissions, YouTube, OBS ingest, playlists, ...)
 feed a scheduler that plays a continuous, re-encoded 1080p live stream in
 the browser over WebRTC. Runs as the systemd service "lan-streamer";
 player page and admin page on port 8080.
CONTROL
cat > "$STAGE/DEBIAN/postinst" <<'SCRIPT'
#!/bin/sh
set -e
if [ "$1" = configure ]; then /opt/lan-streamer/lib/setup.sh; fi
SCRIPT
cat > "$STAGE/DEBIAN/prerm" <<'SCRIPT'
#!/bin/sh
set -e
# the venv and the unit are not files of the package: remove them here
if [ "$1" = remove ] || [ "$1" = upgrade ]; then /opt/lan-streamer/lib/remove.sh; fi
SCRIPT
cat > "$STAGE/DEBIAN/postrm" <<'SCRIPT'
#!/bin/sh
set -e
if [ "$1" = purge ]; then
    rm -rf /var/lib/lan-streamer /var/cache/lan-streamer
    if id lanstreamer >/dev/null 2>&1; then userdel lanstreamer; fi
fi
SCRIPT
chmod 755 "$STAGE"/DEBIAN/{postinst,prerm,postrm}
dpkg-deb --root-owner-group --build "$STAGE" "$OUT/lan-streamer_${DEBVERSION}_amd64.deb"

step "tar.gz with install.sh"
name="lan-streamer-$VERSION-linux-x86_64"
mkdir -p "$OUT/$name/opt"
cp -a "$ROOT" "$OUT/$name/opt/"
install -m 755 "$HERE/install.sh" "$OUT/$name/"
install -m 644 "$HERE/depends.txt" "$REPO/LICENSE" "$OUT/$name/"
tar -czf "$OUT/$name.tar.gz" -C "$OUT" "$name"

ls -la "$OUT"/*.deb "$OUT"/*.tar.gz
