"""Restream: sends an output to an RTMP server (YouTube, Twitch, another streaming PC).

A process of its own per output, so a refused or dropped connection never
touches the output itself. It reads the finished output back from MediaMTX
over SRT, keeps the H.264 picture as it is (no second encode), turns the Opus
sound into AAC (what RTMP servers expect) and sends FLV with rtmp2sink
(rtmp:// and rtmps://).

    python -m lanstreamer.media.restream --source "srt://127.0.0.1:8890?streamid=read:out1" \\
        --target rtmp://a.rtmp.youtube.com/live2/<key>

stdout (one JSON object per line): {"event": "live"} once data reaches the
server connection; errors: {"event": "error", "message": "..."} and exit 1.
"""

from __future__ import annotations

import argparse
import sys
import threading

from lanstreamer.media import gst
from lanstreamer.media.output import emit, log


def build(Gst, source: str, target: str):
    pipeline = Gst.parse_launch(
        f'srtsrc uri="{source}" ! tsdemux name=demux'
        " queue name=vq ! h264parse ! video/x-h264,stream-format=avc ! queue ! mux.video"
        " queue name=aq ! opusdec ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2"
        " ! avenc_aac bitrate=160000 ! aacparse ! queue ! mux.audio"
        " flvmux name=mux streamable=true ! rtmp2sink name=sink"
    )
    pipeline.get_by_name("sink").set_property("location", target)
    heads = {
        "video": pipeline.get_by_name("vq").get_static_pad("sink"),
        "audio": pipeline.get_by_name("aq").get_static_pad("sink"),
    }

    def pad_added(_demux, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
        kind = "video" if name == "video/x-h264" else "audio" if name == "audio/x-opus" else None
        if kind and not heads[kind].is_linked():
            pad.link(heads[kind])
        elif name:
            log(f"restream: ignoring a {name} track")

    pipeline.get_by_name("demux").connect("pad-added", pad_added)
    return pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="lan-streamer restream to RTMP")
    parser.add_argument("--source", required=True, help="SRT read URL of the output at MediaMTX")
    parser.add_argument("--target", required=True, help="rtmp:// or rtmps:// URL with the stream key")
    args = parser.parse_args(argv)

    Gst = gst.init()
    from gi.repository import GLib

    try:
        pipeline = build(Gst, args.source, args.target)
    except Exception as exc:  # noqa: BLE001 – report any setup problem to the parent
        emit("error", message=str(exc))
        return 1
    loop = GLib.MainLoop()
    result = {"code": 0}

    def first_buffer(_pad, _info):
        emit("live")
        return Gst.PadProbeReturn.REMOVE

    pipeline.get_by_name("sink").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, first_buffer)

    def on_message(_bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            log(f"restream error from {message.src.get_name()}: {error.message} ({debug})")
            emit("error", message=error.message)
            result["code"] = 1
            loop.quit()
        elif message.type == Gst.MessageType.EOS:
            emit("error", message="the output ended")
            result["code"] = 1
            loop.quit()

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)

    def read_stdin() -> None:  # the parent closes stdin to stop us
        for _line in sys.stdin:
            pass
        GLib.idle_add(loop.quit)

    threading.Thread(target=read_stdin, daemon=True).start()
    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        emit("error", message="the restream pipeline did not start")
        return 1
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    return result["code"]


if __name__ == "__main__":
    sys.exit(main())
