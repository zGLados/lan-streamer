"""Sends a live test stream (H.264 + Opus in MPEG-TS) over SRT, like OBS would.

Runs with lan-streamer's own Python and GStreamer (installed or a dev setup):

    python tests/e2e/publish_srt.py "srt://127.0.0.1:8890?streamid=publish:ingest/ci" 20
"""

from __future__ import annotations

import sys

from lanstreamer.media import gst


def main() -> int:
    uri, seconds = sys.argv[1], float(sys.argv[2])
    Gst = gst.init()
    from gi.repository import GLib

    pipeline = Gst.parse_launch(
        "videotestsrc is-live=true pattern=ball ! video/x-raw,width=1280,height=720,framerate=30/1"
        ' ! textoverlay text="SRT ingest" font-desc="Sans Bold 30"'
        " ! x264enc tune=zerolatency speed-preset=ultrafast key-int-max=30 bitrate=2000"
        " ! video/x-h264,profile=constrained-baseline ! h264parse config-interval=-1 ! queue ! mux."
        " audiotestsrc is-live=true freq=550 volume=0.1 ! audioconvert ! audioresample"
        " ! opusenc ! queue ! mux."
        f' mpegtsmux name=mux alignment=7 ! srtsink uri="{uri}" wait-for-connection=false'
    )
    loop = GLib.MainLoop()
    result = {"code": 0}

    def on_message(_bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            print(f"publish error: {error.message} ({debug})", file=sys.stderr)
            result["code"] = 1
            loop.quit()

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)
    pipeline.set_state(Gst.State.PLAYING)
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    print(f"publishing to {uri} for {seconds:.0f} s", flush=True)
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    return result["code"]


if __name__ == "__main__":
    sys.exit(main())
