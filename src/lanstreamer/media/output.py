"""Output worker: one GStreamer pipeline per output, controlled over stdin/stdout.

The pipeline never stops while the worker runs (F-SCH-03):

    background (black + silence, always live) ─┐
    input slot (current)                       ├─► compositor / audiomixer ─► H.264 + Opus ─► SRT ─► MediaMTX
    input slot (next, prerolling)              ─┘

Switching is "make before break": the next input starts in a new slot at zero
opacity and volume. Once its first frame has arrived it fades in, and only then
is the old slot removed. An input that never delivers a frame is dropped and
the current one keeps playing.

Protocol, one JSON object per line:
    stdin:  {"cmd": "play", "label": "...", "source": {"kind": "test", "pattern": "smpte"}}
            {"cmd": "play", "label": "...", "source": {"kind": "uri", "uri": "https://..."}}
            {"cmd": "stop"}
    stdout: {"event": "ready"} {"event": "playing", "label": "..."}
            {"event": "switch_failed", "label": "...", "reason": "..."}
            {"event": "stats", "fps": 59.9} {"event": "error", "message": "..."}
            {"event": "stream", "caps": "video/x-h264, profile=..., level=..."}
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass, field

from lanstreamer.media import encoders, gst

FADE_S = 1.0
START_TIMEOUT_S = 15.0
AUDIO_CAPS = "audio/x-raw,rate=48000,channels=2"


def emit(event: str, **data) -> None:
    print(json.dumps({"event": event, **data}), flush=True)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@dataclass
class Slot:
    label: str
    bin: object
    pads: list = field(default_factory=list)  # (ghost pad, mixer, mixer pad)
    video_pad: object = None
    audio_pad: object = None
    ready: bool = False
    started: float = field(default_factory=time.monotonic)


class Output:
    def __init__(self, Gst, GLib, args: argparse.Namespace):
        self.Gst = Gst
        self.GLib = GLib
        self.args = args
        # I420 end to end: what x264 takes, so no extra conversion per frame.
        self.raw_caps = (
            f"video/x-raw,format=I420,width={args.width},height={args.height},"
            f"framerate={args.fps}/1,pixel-aspect-ratio=1/1"
        )
        self.loop = GLib.MainLoop()
        self.current: Slot | None = None
        self.pending: Slot | None = None
        self.next_zorder = 1
        self.frames = 0
        self.muxed: set[str] = set()  # mux inputs that have delivered data
        self.pipeline = self._build()

    # --- pipeline ---------------------------------------------------------------

    def _build(self):
        Gst, a = self.Gst, self.args
        pipeline = Gst.parse_launch(
            f"compositor name=vmix background=black ignore-inactive-pads=true"
            f" ! {self.raw_caps}"
            f" ! textoverlay name=info halignment=right valignment=top"
            f'   font-desc="Sans 16" shaded-background=true'
            f" ! videoconvert name=vconv n-threads=4"
            f" ! queue name=encq leaky=downstream max-size-buffers=3 max-size-bytes=0 max-size-time=0"
            f" h264parse name=hparse config-interval=-1 ! queue ! mux."
            f" audiomixer name=amix ignore-inactive-pads=true ! {AUDIO_CAPS}"
            f" ! audioconvert ! opusenc bitrate=128000 frame-size=10 ! queue ! mux."
            # SRT instead of RTSP: rtspclientsink's TCP writes fail on Windows
            # after some seconds (GLib writev bug), SRT brings its own sockets.
            f" mpegtsmux name=mux alignment=7 latency=0"
            f' ! srtsink uri="{a.publish}" wait-for-connection=false sync=false'
            f" videotestsrc is-live=true pattern=black ! {self.raw_caps} ! vmix."
            f" audiotestsrc is-live=true wave=silence ! {AUDIO_CAPS} ! amix."
        )
        spec = encoders.find(a.encoder)
        encoder = encoders.make_encoder(Gst, spec, a.fps, a.bitrate) if spec else None
        if encoder is None:
            raise RuntimeError(f"encoder {a.encoder} is not available")
        h264 = Gst.ElementFactory.make("capsfilter", None)
        h264.set_property("caps", Gst.Caps.from_string(encoders.H264_CAPS))
        pipeline.add(encoder)
        pipeline.add(h264)
        queue, parse = pipeline.get_by_name("encq"), pipeline.get_by_name("hparse")
        if not (queue.link(encoder) and encoder.link(h264) and h264.link(parse)):
            raise RuntimeError(f"cannot link encoder {a.encoder}")

        parse.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._count_frame)
        parse.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._report_caps)

        # MediaMTX only reads the first stream table (PMT) of the MPEG-TS. Hold the
        # output back until video and audio both reached the muxer, so the first
        # PMT it sees lists both tracks.
        mux = pipeline.get_by_name("mux")
        for pad in mux.sinkpads:
            pad.add_probe(Gst.PadProbeType.BUFFER, self._on_mux_input)
        mux.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, self._hold_mux_output
        )
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        return pipeline

    def _on_mux_input(self, pad, _info):
        self.muxed.add(pad.get_name())
        return self.Gst.PadProbeReturn.REMOVE

    def _hold_mux_output(self, _pad, _info):
        if len(self.muxed) < 2:
            return self.Gst.PadProbeReturn.DROP
        return self.Gst.PadProbeReturn.REMOVE

    def _report_caps(self, pad, _info):
        # Runs in the streaming thread: it must never raise, or PyGObject drops
        # the buffer. caps.to_string() avoids reading GstFraction values, which
        # our PyGObject build (no gst-python overrides) cannot convert.
        try:
            caps = pad.get_current_caps()
            if caps:
                emit("stream", caps=caps.to_string())
        except Exception as exc:  # noqa: BLE001
            log(f"cannot report stream caps: {exc}")
        return self.Gst.PadProbeReturn.REMOVE

    def _count_frame(self, _pad, _info):
        self.frames += 1
        return self.Gst.PadProbeReturn.OK

    def _source_bin(self, label: str, source: dict):
        """A bin with ghost pads "video" and "audio" in the output's raw format."""
        Gst = self.Gst
        video_tail = (
            f"videoconvert name=vin ! videoscale add-borders=true ! videorate ! {self.raw_caps}"
            f" ! queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 name=vout"
        )
        audio_tail = f"audioconvert name=ain ! audioresample ! {AUDIO_CAPS} ! queue name=aout"
        if source["kind"] == "test":
            text = label.replace('"', "'")
            desc = (
                f"videotestsrc is-live=true pattern={source.get('pattern', 'smpte')}"
                f' ! textoverlay text="{text}" font-desc="Sans Bold 60" shaded-background=true'
                f" ! {video_tail}"
                f" audiotestsrc is-live=true volume=0.05 freq={source.get('freq', 440)}"
                f" ! {audio_tail}"
            )
            bin_ = Gst.parse_bin_from_description(desc, False)
        elif source["kind"] == "uri":
            desc = f"{video_tail} {audio_tail}"
            bin_ = Gst.parse_bin_from_description(desc, False)
            src = Gst.ElementFactory.make("fallbacksrc", None)
            src.set_property("uri", source["uri"])
            bin_.add(src)
            heads = {
                "video": bin_.get_by_name("vin").get_static_pad("sink"),
                "audio": bin_.get_by_name("ain").get_static_pad("sink"),
            }
            src.connect("pad-added", lambda _src, pad: _link_by_kind(pad, heads))
            for pad in src.srcpads:
                _link_by_kind(pad, heads)
        else:
            raise ValueError(f"unknown source kind {source['kind']!r}")

        for name, element in (("video", "vout"), ("audio", "aout")):
            target = bin_.get_by_name(element).get_static_pad("src")
            bin_.add_pad(Gst.GhostPad.new(name, target))
        return bin_

    # --- switching ------------------------------------------------------------

    def play(self, label: str, source: dict) -> None:
        if self.pending is not None:
            self._drop(self.pending)
            emit("switch_failed", label=self.pending.label, reason="replaced by a newer switch")
            self.pending = None
        try:
            bin_ = self._source_bin(label, source)
        except Exception as exc:  # noqa: BLE001 – a broken source must not stop the output
            emit("switch_failed", label=label, reason=str(exc))
            return

        slot = Slot(label, bin_)
        self.pipeline.add(bin_)
        vmix, amix = self.pipeline.get_by_name("vmix"), self.pipeline.get_by_name("amix")
        slot.video_pad = vmix.request_pad_simple("sink_%u")
        slot.video_pad.set_property("alpha", 0.0)
        slot.video_pad.set_property("zorder", self.next_zorder)
        self.next_zorder += 1
        slot.audio_pad = amix.request_pad_simple("sink_%u")
        slot.audio_pad.set_property("volume", 0.0)
        for ghost, mixer, pad in (
            (bin_.get_static_pad("video"), vmix, slot.video_pad),
            (bin_.get_static_pad("audio"), amix, slot.audio_pad),
        ):
            ghost.link(pad)
            slot.pads.append((ghost, mixer, pad))

        def first_frame(_pad, _info):
            self.GLib.idle_add(self._on_ready, slot)
            return self.Gst.PadProbeReturn.REMOVE

        slot.video_pad.add_probe(self.Gst.PadProbeType.BUFFER, first_frame)
        self.pending = slot
        bin_.sync_state_with_parent()
        self.GLib.timeout_add(int(START_TIMEOUT_S * 1000), self._on_start_timeout, slot)

    def _on_ready(self, slot: Slot) -> bool:
        if slot is not self.pending:
            return False
        slot.ready = True
        old, self.current, self.pending = self.current, slot, None
        started = time.monotonic()

        def fade() -> bool:
            t = min(1.0, (time.monotonic() - started) / FADE_S)
            slot.video_pad.set_property("alpha", t)
            slot.audio_pad.set_property("volume", t)
            if old is not None:
                old.audio_pad.set_property("volume", 1.0 - t)
            if t < 1.0:
                return True
            if old is not None:
                self._drop(old)
            emit("playing", label=slot.label)
            return False

        self.GLib.timeout_add(20, fade)
        return False

    def _on_start_timeout(self, slot: Slot) -> bool:
        if slot is self.pending and not slot.ready:
            self.pending = None
            self._drop(slot)
            emit("switch_failed", label=slot.label, reason="no picture within 15 s")
        return False

    def _drop(self, slot: Slot) -> None:
        slot.bin.set_state(self.Gst.State.NULL)
        for ghost, mixer, pad in slot.pads:
            ghost.unlink(pad)
            mixer.release_request_pad(pad)
        self.pipeline.remove(slot.bin)

    # --- bus, overlay, stats --------------------------------------------------

    def _slot_of(self, element) -> Slot | None:
        while element is not None:
            for slot in (self.current, self.pending):
                if slot is not None and element is slot.bin:
                    return slot
            element = element.get_parent()
        return None

    def _on_message(self, _bus, message) -> None:
        Gst = self.Gst
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            slot = self._slot_of(message.src)
            if slot is not None:
                log(f"input {slot.label!r} failed: {error.message} ({debug})")
                if slot is self.pending:
                    self.pending = None
                    self._drop(slot)
                    emit("switch_failed", label=slot.label, reason=error.message)
                return
            emit("error", message=f"{error.message} ({debug})")
            self.loop.quit()
        elif message.type == Gst.MessageType.WARNING:
            warning, _debug = message.parse_warning()
            log(f"warning from {message.src.get_name()}: {warning.message}")

    def _update_overlay(self) -> bool:
        now = time.time()
        label = self.current.label if self.current else "Background"
        clock = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
        self.pipeline.get_by_name("info").set_property("text", f"{label}  ·  {clock}")
        return True

    def _report_stats(self) -> bool:
        now = time.monotonic()
        fps = self.frames / (now - self._stats_since)
        self.frames, self._stats_since = 0, now
        emit("stats", fps=round(fps, 1))
        return True

    # --- control ----------------------------------------------------------------

    def _read_commands(self) -> None:
        for line in sys.stdin:
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                log(f"ignoring invalid command: {line.strip()}")
                continue
            self.GLib.idle_add(self._handle, command)
        self.GLib.idle_add(self.loop.quit)  # parent closed stdin: shut down

    def _handle(self, command: dict) -> bool:
        if command.get("cmd") == "play":
            self.play(command.get("label", "Input"), command["source"])
        elif command.get("cmd") == "stop":
            self.loop.quit()
        return False

    def run(self) -> int:
        result = self.pipeline.set_state(self.Gst.State.PLAYING)
        if result == self.Gst.StateChangeReturn.FAILURE:
            emit("error", message="pipeline did not start (is MediaMTX running?)")
            return 1
        self._stats_since = time.monotonic()
        self.GLib.timeout_add(15, self._update_overlay)
        self.GLib.timeout_add(2000, self._report_stats)
        threading.Thread(target=self._read_commands, daemon=True).start()
        emit("ready", encoder=self.args.encoder)
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        self.pipeline.set_state(self.Gst.State.NULL)
        return 0


def _link_by_kind(pad, heads: dict) -> None:
    caps = pad.get_current_caps() or pad.query_caps(None)
    name = caps.get_structure(0).get_name() if caps and caps.get_size() else pad.get_name()
    kind = "video" if name.startswith("video") else "audio" if name.startswith("audio") else None
    if kind and not heads[kind].is_linked():
        pad.link(heads[kind])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="lan-streamer output worker")
    parser.add_argument("--encoder", required=True, help="GStreamer element, e.g. x264enc")
    parser.add_argument(
        "--publish", required=True,
        help="SRT URL of MediaMTX, e.g. srt://127.0.0.1:8890?streamid=publish:out1&latency=20",
    )  # fmt: skip
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--bitrate", type=int, default=8000, help="video bitrate in kbit/s")
    args = parser.parse_args(argv)

    Gst = gst.init()
    from gi.repository import GLib

    try:
        output = Output(Gst, GLib, args)
    except Exception as exc:  # noqa: BLE001 – report any setup problem to the parent
        emit("error", message=str(exc))
        return 1
    return output.run()


if __name__ == "__main__":
    sys.exit(main())
