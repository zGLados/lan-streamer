#!/bin/sh
# Runs in the test container (tests/local_linux.py): a test clip with a web
# server for it, then lan-streamer as its systemd service would run it (user
# lanstreamer, data in /var/lib/lan-streamer) – containers have no systemd.
#   start.sh          the app on port 8080 with the stored configuration
#   start.sh smoke    a smoke test with switching (test patterns + the clip), then checks its logs
set -e
if [ ! -f /media/clip.mp4 ]; then
    mkdir -p /media
    gst-launch-1.0 -q videotestsrc num-buffers=300 pattern=ball \
        ! video/x-raw,width=1280,height=720,framerate=30/1 ! x264enc ! h264parse ! mp4mux name=m \
        ! filesink location=/media/clip.mp4 \
        audiotestsrc num-buffers=430 freq=330 ! avenc_aac ! aacparse ! m.
    cp /media/clip.mp4 /media/clip-http.mp4
fi
python3 -m http.server 8000 --directory /media >/dev/null 2>&1 &
install -d -o lanstreamer -g lanstreamer /var/lib/lan-streamer /var/cache/lan-streamer
as_service() {
    runuser -u lanstreamer -- env XDG_DATA_HOME=/var/lib XDG_CACHE_HOME=/var/cache/lan-streamer \
        HOME=/var/lib/lan-streamer /opt/lan-streamer/bin/lan-streamer "$@"
}

if [ "$1" != smoke ]; then
    as_service --port 8080
    exit
fi

logs=/var/lib/lan-streamer/logs
fail() { tail -20 "$logs/lan-streamer.log"; echo "FAILED: $*"; exit 1; }
as_service --encoder cpu --smoke-test 45 --switch-interval 6 \
    --source /media/clip.mp4 --source http://127.0.0.1:8000/clip-http.mp4 || fail "the app's smoke test"
sent=$(grep -m1 'MPEG-TS sent:' "$logs/lan-streamer.log" || true)
echo "First stream table sent to MediaMTX: $sent"
echo "$sent" | grep -q 'H.264' && echo "$sent" | grep -q 'Opus' || fail "stream table lacks H.264 or Opus"
for name in 'Test input A' 'Test input B' 'clip.mp4' 'clip-http.mp4'; do
    grep -q "On air: $name" "$logs/lan-streamer.log" || fail "input $name never went on air"
done
grep -q 'online, 2 tracks' "$logs/mediamtx.log" || fail "MediaMTX did not get 2 tracks"
if grep -q 'decode errors' "$logs/mediamtx.log"; then fail "MediaMTX reported decode errors"; fi
echo "OK: smoke test"
