"""Output worker: one GStreamer pipeline per output, controlled over stdin/stdout.

The pipeline never stops while the worker runs (F-SCH-03):

    background (black + silence, always live) ─┐
    input slot (current)                       ├─► compositor / audiomixer ─► H.264 + Opus ─► SRT ─► MediaMTX
    input slot (next, prerolling)              ─┘

Switching is "make before break": the next input starts in a new slot at zero
opacity and volume. Once its first frame has arrived it fades in, and only then
is the old slot removed. An input that never delivers a frame is dropped and
the current one keeps playing.

Protocol, one JSON object per line ("id" identifies one play command):
    stdin:  {"cmd": "play", "id": "7", "label": "...", "source": {"kind": "test", "pattern": "smpte"}}
            {"cmd": "play", "id": "8", "label": "...", "source": {"kind": "uri", "uri": "https://..."}}
            {"cmd": "play", "id": "9", "label": "...", "source": {"kind": "card", "heading": "...", "title": "...", "subtitle": "..."}}
            a source with "cut": true replaces the current one at its first frame, without fading
            {"cmd": "clear"}   fade out to the background
            {"cmd": "overlay", "title": "...", "subtitle": "...", "duration_s": 8}
                the lower third; duration 0 keeps it, an empty title hides it
            {"cmd": "debug_clock", "on": true}
                debug: the input's name and the time (ms) in the top right, to measure the delay
            {"cmd": "stop"}
    stdout: {"event": "ready"} {"event": "playing", "id": "7", "label": "..."}
            {"event": "switch_failed", "id": "8", "label": "...", "reason": "..."}
            {"event": "ended", "id": "7"}   the source on air reached its end
            {"event": "failed", "id": "7", "reason": "..."}   the source on air broke
            {"event": "cleared"}
            {"event": "stats", "fps": 59.9} {"event": "error", "message": "..."}
            {"event": "stream", "caps": "video/x-h264, profile=..., level=..."}
            {"event": "ts_table", "pmt": "PMT v0: PID 65 H.264 ...", "sent": true}
            {"event": "stalled", "seconds": 5.2}   the main loop answers no more (see _watch);
                every thread's stack went to stderr (output.log)
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field

from lanstreamer import images
from lanstreamer.media import encoders, gst, loudness
from lanstreamer.media.mpegts import TableReader

FADE_S = 1.0
OVERLAY_FADE_S = 0.3
OVERLAY_SHADING = 110  # textoverlay's shaded background at full opacity
START_TIMEOUT_S = 15.0
MUX_HOLD_S = 1.0  # see _hold_mux_output
AUDIO_CAPS = "audio/x-raw,rate=48000,channels=2"
# opusenc takes only 16-bit integers: a decoded or resampled source often peaks above
# full scale, and those samples were cut off hard there - crackling at loud passages
# (owner 2026-11-11). The mix is float; this soft knee bends what is above -2 dBFS
# down instead, peaks up to about +4 dBFS stay below full scale.
LIMITER = "audiodynamic mode=compressor characteristics=soft-knee threshold=0.8 ratio=0.25"
# The mixers run live: sound that reaches audiomixer after its deadline is thrown
# away. Media sources (fallbacksrc) release their buffers right on time, so every
# hiccup of a download or decoder became a gap in the sound (YouTube, owner
# 2026-11-12); the test tone, made in the pipeline, never was late. This much
# lateness is waited for instead; it adds to the output's delay.
AUDIO_MIX_LATENCY_MS = 500
# The muxer waits as long for a missing track (more than the mixer may be late): with
# latency 0 it did not wait at all when its latency query failed at the start, created
# its streams with the first picture and the sound still without caps – "Could not
# create handler for stream" (Linux CI start flake, frequent since the mixer latency).
# Both tracks flowing, it waits for nothing; the SRT sink does not sync to it.
MUX_LATENCY_MS = 1000
CARD_COLOR = 0xFF111217  # the standby picture's background
HEARTBEAT_S = 0.5
STALL_S = 5.0  # the main loop silent this long: report it (_watch)
FLOW_S = 10.0  # how often the current input's flow goes into the log


def emit(event: str, **data) -> None:
    print(json.dumps({"event": event, **data}), flush=True)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Flow:
    """How fast a source delivers, measured where it enters its bin (before videorate):
    frames per second and seconds of sound per second. Below the output's frame
    rate or below 1.0 the source cannot keep up – the picture stutters, the
    sound breaks up. Counted in the streaming threads, read in the main loop.
    fallbacksrc fills in black and silence while its source is down, and those
    count too: its state (source_state) tells whether it is the real thing."""

    def __init__(self, ok) -> None:
        self.ok = ok  # Gst.PadProbeReturn.OK
        self.frames = 0
        self.sound_ns = 0
        self.since = time.monotonic()

    def video(self, _pad, _info):
        self.frames += 1
        return self.ok

    def audio(self, _pad, info):
        duration = info.get_buffer().duration
        if 0 < duration < 10**10:  # not CLOCK_TIME_NONE
            self.sound_ns += duration
        return self.ok

    def take(self) -> tuple[float, float]:
        """(frames per second, seconds of sound per second) since the last call."""
        now = time.monotonic()
        seconds = max(now - self.since, 1e-3)
        result = (self.frames / seconds, self.sound_ns / 1e9 / seconds)
        self.frames, self.sound_ns, self.since = 0, 0, now
        return result


def source_state(status: str, statistics: str) -> str:
    """One fallbacksrc for the log, e.g. "running, buffered 100 %, 2 retries (last: timeout)".
    statistics is its "statistics" structure as text (Structure.get_value fails on
    some types without overrides, see _report_caps)."""
    fields = dict(re.findall(r"([\w-]+)=\([\w]+\)\"?([^,;\"]*)", statistics))
    text = f"{status}, buffered {fields.get('buffering-percent', '?')} %"
    retries = fields.get("num-retry", "0")
    if retries != "0":
        text += f", {retries} {'retry' if retries == '1' else 'retries'} (last: {fields.get('last-retry-reason', '?')})"
    return text


@dataclass
class Slot:
    id: str
    label: str
    bin: object
    pads: list = field(default_factory=list)  # (ghost pad, mixer, mixer pad)
    video_pad: object = None
    audio_pad: object = None
    ready: bool = False
    failed: bool = False
    cut: bool = False
    started: float = field(default_factory=time.monotonic)
    flow: Flow | None = None
    sources: list = field(default_factory=list)  # (what, fallbacksrc) of a media input
    status: dict = field(default_factory=dict)  # what -> its last logged status


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
        self.overlay_gen = 0  # counts overlay commands; older fades and timers stop
        self.frames = 0
        self.dropping: set = set()  # input bins being stopped (_drop)
        self.muxed: set[str] = set()  # mux inputs that have delivered data
        self.muxed_all_at: float | None = None  # when the last one did
        self.tables = TableReader()  # diagnostics: which tracks MediaMTX gets
        self.clock_on = False  # debug clock in the picture (debug_clock)
        self.tables_until: float | None = None
        self.reported_sent = False
        self.heartbeat = time.monotonic()  # the main loop's last sign of life
        self.watching = False
        self.pipeline = self._build()

    # --- pipeline ---------------------------------------------------------------

    def _build(self):
        Gst, a = self.Gst, self.args
        pipeline = Gst.parse_launch(
            f"compositor name=vmix background=black ignore-inactive-pads=true"
            f" ! {self.raw_caps}"
            f" ! textoverlay name=info halignment=right valignment=top"
            f'   font-desc="Sans 16" shaded-background=true'
            # the lower third; font sizes are relative to 640 px width (auto-resize)
            f" ! textoverlay name=ovtitle {OVERLAY_TEXT} ypad=118 font-desc=\"Sans Bold 14\""
            f" ! textoverlay name=ovsub {OVERLAY_TEXT} ypad=64 font-desc=\"Sans 10\""
            f" ! videoconvert name=vconv n-threads=4"
            f" ! queue name=encq leaky=downstream max-size-buffers=3 max-size-bytes=0 max-size-time=0"
            f" h264parse name=hparse config-interval=-1 ! queue ! mux."
            f" audiomixer name=amix ignore-inactive-pads=true latency={AUDIO_MIX_LATENCY_MS * Gst.MSECOND}"
            f" ! {AUDIO_CAPS}"
            f" ! {LIMITER} ! audioconvert ! opusenc bitrate=128000 frame-size=10 ! queue ! mux."
            # SRT instead of RTSP: rtspclientsink's TCP writes fail on Windows
            # after some seconds (GLib writev bug), SRT brings its own sockets.
            f" mpegtsmux name=mux alignment=7 latency={MUX_LATENCY_MS * Gst.MSECOND} pat-interval=9000 pmt-interval=9000"
            # mpegtsmux puts a PAT/PMT copy into its caps ("streamheader") once,
            # possibly when only one track existed, and never updates it; srtsink
            # sends it before any data and MediaMTX keeps the first PMT it sees,
            # so it ignored the other track (black picture on the test PC).
            # capssetter passes the caps on without the streamheader.
            f' ! capssetter replace=true caps="video/mpegts,systemstream=true,packetsize=188"'
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
        # output back until video and audio both reached the muxer and a moment
        # longer: the muxer may already have written a PMT with only the track
        # that came first (seen with NVENC, whose first frame is very fast). It
        # repeats the PMT regularly, so the first one MediaMTX sees lists both.
        mux = pipeline.get_by_name("mux")
        for pad in mux.sinkpads:
            pad.add_probe(Gst.PadProbeType.BUFFER, self._on_mux_input)
        mux_src = mux.get_static_pad("src")
        mux_src.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._report_streamheader)
        data = Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST
        mux_src.add_probe(data, self._inspect_mux_output)  # runs before the hold
        mux_src.add_probe(data, self._hold_mux_output)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        # only the messages used: PyGObject on Windows cannot read the type of the
        # "extended" ones (stream collection, ... above 2**31, no C long) and printed an
        # OverflowError for each, even when caught in _on_message (owner logs 0.1.8, 0.1.9)
        for kind in ("error", "warning"):
            bus.connect(f"message::{kind}", self._on_message)
        return pipeline

    def _on_mux_input(self, pad, _info):
        self.muxed.add(pad.get_name())
        if len(self.muxed) == 2:
            self.muxed_all_at = time.monotonic()
        return self.Gst.PadProbeReturn.REMOVE

    def _report_streamheader(self, _pad, info):
        """Logs once which PMT mpegtsmux put into its caps (removed by capssetter)."""
        try:
            event = info.get_event()
            if event.type != self.Gst.EventType.CAPS:
                return self.Gst.PadProbeReturn.OK
            text = event.parse_caps().to_string()
            if "streamheader" not in text:
                return self.Gst.PadProbeReturn.OK
            reader = TableReader()
            for chunk in re.findall(r"\b47[0-9a-f]{374,}\b", text):
                for pmt in reader.feed(bytes.fromhex(chunk)):
                    emit("ts_table", pmt=f"caps streamheader (not sent) {pmt.describe()}", sent=False)
        except Exception as exc:  # noqa: BLE001 – never raise in the streaming thread
            log(f"cannot read streamheader: {exc}")
        return self.Gst.PadProbeReturn.REMOVE

    def _mux_ready(self) -> bool:
        return self.muxed_all_at is not None and time.monotonic() - self.muxed_all_at >= MUX_HOLD_S

    def _hold_mux_output(self, _pad, _info):
        return self.Gst.PadProbeReturn.REMOVE if self._mux_ready() else self.Gst.PadProbeReturn.DROP

    def _inspect_mux_output(self, _pad, info):
        """Reports every new stream table (PMT) the muxer writes, held back or sent.

        Stops 10 s after the output started. Must never raise (streaming thread).
        """
        try:
            sent = self._mux_ready()
            if sent and self.tables_until is None:
                self.tables_until = time.monotonic() + 10
            if sent and not self.reported_sent and self.tables.last is not None:
                # the table MediaMTX gets first: the current one when the hold opens
                self.reported_sent = True
                emit("ts_table", pmt=self.tables.last.describe(), sent=True)
            buffers = info.get_buffer_list() if info.type & self.Gst.PadProbeType.BUFFER_LIST else None
            items = [buffers.get(i) for i in range(buffers.length())] if buffers else [info.get_buffer()]
            for buffer in items:
                for pmt in self.tables.feed(buffer.extract_dup(0, buffer.get_size())):
                    emit("ts_table", pmt=pmt.describe(), sent=sent)
            if self.tables_until is not None and time.monotonic() > self.tables_until:
                return self.Gst.PadProbeReturn.REMOVE
        except Exception as exc:  # noqa: BLE001
            log(f"cannot inspect MPEG-TS output: {exc}")
            return self.Gst.PadProbeReturn.REMOVE
        return self.Gst.PadProbeReturn.OK

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
        elif source["kind"] == "image":
            path = str(source["path"]).replace("\\", "/").replace('"', "")
            place = images.layout(str(source.get("size", "fit")), images.file_size(source["path"]),
                                  (self.args.width, self.args.height))  # fmt: skip
            if place is None:  # fit: video_tail scales it, with black bars
                frame = ""
            else:  # scaled (or kept) and centered once, before the picture is repeated
                w, h, left, right, top, bottom = place
                frame = (
                    f" ! videoconvert ! videoscale ! video/x-raw,format=AYUV,width={w},height={h},pixel-aspect-ratio=1/1"
                    f" ! videobox left={left} right={right} top={top} bottom={bottom} fill=black"
                )
            desc = (
                f'filesrc location="{path}" ! decodebin{frame} ! imagefreeze is-live=true'
                f" ! {video_tail}"
                f" audiotestsrc is-live=true wave=silence ! {audio_tail}"
            )
            bin_ = Gst.parse_bin_from_description(desc, False)
        elif source["kind"] == "card":
            desc = (
                f"videotestsrc is-live=true pattern=solid-color foreground-color={CARD_COLOR}"
                f" ! textoverlay name=heading {CARD_TEXT} ypos=0.36 font-desc=\"Sans 12\" color=0xFF9AA0B4"
                f" ! textoverlay name=title {CARD_TEXT} ypos=0.5 font-desc=\"Sans Bold 22\" color=0xFFFFFFFF"
                f" ! textoverlay name=subtitle {CARD_TEXT} ypos=0.64 font-desc=\"Sans 14\" color=0xFFC8CCD8"
                f" ! {video_tail}"
                f" audiotestsrc is-live=true wave=silence ! {audio_tail}"
            )
            bin_ = Gst.parse_bin_from_description(desc, False)
            for name in ("heading", "title", "subtitle"):
                bin_.get_by_name(name).set_property("text", _plain(source.get(name, "")))
        elif source["kind"] == "uri":
            # media (files, streams, YouTube, OBS): its own gain, levelled (loudness.py)
            levelled = (f"audioconvert name=ain ! audioresample ! {AUDIO_CAPS},format=F32LE"
                        f" ! volume name=level ! queue name=aout")  # fmt: skip
            desc = f"{video_tail} {levelled}"
            bin_ = Gst.parse_bin_from_description(desc, False)
            loudness.attach(Gst, bin_.get_by_name("level"), log)
            heads = {
                "video": bin_.get_by_name("vin").get_static_pad("sink"),
                "audio": bin_.get_by_name("ain").get_static_pad("sink"),
            }
            if source.get("audio_uri"):  # picture and sound from separate URIs
                parts = [(source["uri"], {"video": heads["video"]}), (source["audio_uri"], {"audio": heads["audio"]})]
            else:
                parts = [(source["uri"], heads)]
            start_s = float(source.get("start_s") or 0)
            sources = []
            for uri, wanted in parts:
                what = "" if len(wanted) == 2 else "picture" if "video" in wanted else "sound"
                src = Gst.ElementFactory.make("fallbacksrc", f"source {what}".strip())
                src.set_property("uri", uri)
                src.set_property("enable-video", "video" in wanted)
                src.set_property("enable-audio", "audio" in wanted)
                sources.append(src)
                bin_.add(src)
                src.connect("pad-added", lambda _src, pad, wanted=wanted: _link_by_kind(pad, wanted))
                for pad in src.srcpads:
                    _link_by_kind(pad, wanted)
            if len(sources) > 1 or start_s > 0:
                self._start_together(sources, start_s)
        else:
            raise ValueError(f"unknown source kind {source['kind']!r}")

        for name, element in (("video", "vout"), ("audio", "aout")):
            target = bin_.get_by_name(element).get_static_pad("src")
            bin_.add_pad(Gst.GhostPad.new(name, target))
        return bin_

    def _start_together(self, sources: list, start_s: float) -> None:
        """Picture and sound from separate URIs (YouTube) start at the same moment, and a
        resumed input (after a pause) at start_s.

        Each fallbacksrc maps its stream to the output's time when its first data passes,
        so two of them let go one by one are as far apart as their data was: a sound
        server 0.5 s slower put the sound 0.5 s behind the whole video, and a big picture
        stream over a slower line put the sound ahead (measured 2026-11-12). So every one
        holds its output back (manual-unblock; live streams ignore it): once each has its
        decoder they are seeked if needed, once each decoder has data (from start_s on)
        and each buffer is full they are let go together. Not seekable (live): it starts where the stream is. A
        source not ready after a while does not hold the others: they play anyway."""
        Gst, GLib = self.Gst, self.GLib
        decoders: dict = {}  # fallbacksrc -> its uridecodebin3, once it has an output
        arrived: set = set()  # decoders whose (seeked) data has come
        state = {"seeked": False, "seeking": False, "free": False}
        began = time.monotonic()
        target = int(start_s * Gst.SECOND) if start_s > 0 else 0
        # one demuxer seeks picture and sound together, to a keyframe; separate sources
        # each seek on their own, so to the exact position, or the picture would start
        # at an earlier keyframe than the sound
        flags = Gst.SeekFlags.FLUSH | (Gst.SeekFlags.ACCURATE if len(sources) > 1 else Gst.SeekFlags.KEY_UNIT)

        def unblock(reason=None) -> bool:
            if state["free"]:
                return False
            state["free"] = True
            if len(sources) > 1:  # for output.log: how long the start waited, and why
                log(f"picture and sound start together after {time.monotonic() - began:.1f} s"
                    f" ({reason or 'all have data'})")  # fmt: skip
            for src in sources:
                src.emit("unblock")
            return False

        def buffered(src) -> bool:
            # fallbacksrc buffers while held back (status stays "buffering" until let go),
            # and after letting go it holds a source until its buffer is full: a picture
            # still buffering would start later than the sound
            try:
                found = re.search(r"buffering-percent=\(int\)(-?\d+)", src.get_property("statistics").to_string())
            except Exception:  # noqa: BLE001 – unknown: do not wait for it
                return True
            return found is None or int(found.group(1)) >= 100

        def all_arrived() -> bool:
            if state["free"]:
                return False
            if state["seeked"] and len(arrived) >= len(decoders) and all(buffered(src) for src in decoders):
                unblock()
                return False
            return state["seeked"]  # polled until then (every 100 ms, see seek)

        def first(_pad, info, decoder, after_seek) -> object:
            # streaming thread. A demuxer may land before the target even on an accurate
            # seek (Ogg starts at the page before: the sound began 0.55 s early) – what
            # ends before it is dropped, so every source starts there
            if not after_seek and state["seeking"]:  # the seek's own probe counts instead
                return Gst.PadProbeReturn.REMOVE
            buffer = info.get_buffer()
            duration = buffer.duration if 0 < buffer.duration < 10**10 else 0
            if after_seek and 0 <= buffer.pts < 10**18 and buffer.pts + duration <= target:
                return Gst.PadProbeReturn.DROP
            arrived.add(decoder)
            GLib.idle_add(lambda: all_arrived() and False)  # once; the 100 ms poll does the rest
            return Gst.PadProbeReturn.REMOVE

        def seek(reason=None) -> bool:
            if state["seeked"]:
                return False
            state["seeked"] = True
            if len(decoders) < len(sources):
                log(f"{len(sources) - len(decoders)} of {len(sources)} sources not ready after"
                    f" {START_TIMEOUT_S / 2:.0f} s: started anyway")  # fmt: skip
            if not decoders:
                return unblock(reason)
            seekable = []
            for decoder in decoders.values():
                query = Gst.Query.new_seeking(Gst.Format.TIME)
                if target and decoder.query(query) and query.parse_seeking()[1]:
                    seekable.append(decoder)
            if seekable:
                state["seeking"] = True
                arrived.clear()  # a flushing seek drops what came: count from the target on
                for decoder in seekable:
                    for pad in decoder.srcpads:
                        pad.add_probe(Gst.PadProbeType.BUFFER, first, decoder, True)
                    decoder.seek_simple(Gst.Format.TIME, flags, target)
                arrived.update(d for d in decoders.values() if d not in seekable)
                log(f"resuming at {start_s:.1f} s")
            GLib.timeout_add(int(START_TIMEOUT_S * 500), unblock, "timeout waiting for data")
            if all_arrived():
                GLib.timeout_add(100, all_arrived)  # the buffers fill without telling
            return False

        def has_decoder(src, decoder) -> bool:
            decoders.setdefault(src, decoder)
            if len(decoders) == len(sources):
                seek()
            return False

        for src in sources:
            src.set_property("manual-unblock", True)

            def added(_bin, _sub, element, src=src) -> None:
                factory = element.get_factory()
                if factory is not None and factory.get_name() == "uridecodebin3":
                    element.connect("pad-added", lambda _dec, pad, src=src: decoded(src, element, pad))

            def decoded(src, decoder, pad) -> None:  # streaming thread: watch it at once
                pad.add_probe(Gst.PadProbeType.BUFFER, first, decoder, False)
                GLib.idle_add(has_decoder, src, decoder)

            src.connect("deep-element-added", added)
        GLib.timeout_add(int(START_TIMEOUT_S * 500), seek, "timeout waiting for decoders")

    # --- switching ------------------------------------------------------------

    def play(self, id_: str, label: str, source: dict) -> None:
        if self.pending is not None:
            old = self.pending
            self.pending = None
            self._drop(old)
            emit("switch_failed", id=old.id, label=old.label, reason="replaced by a newer switch")
        try:
            bin_ = self._source_bin(label, source)
        except Exception as exc:  # noqa: BLE001 – a broken source must not stop the output
            emit("switch_failed", id=id_, label=label, reason=str(exc))
            return

        slot = Slot(id_, label, bin_, cut=bool(source.get("cut")), flow=Flow(self.Gst.PadProbeReturn.OK))
        # diagnostics: what the source delivers (logged every FLOW_S) and which decoders it got
        bin_.get_by_name("vin").get_static_pad("sink").add_probe(self.Gst.PadProbeType.BUFFER, slot.flow.video)
        bin_.get_by_name("ain").get_static_pad("sink").add_probe(self.Gst.PadProbeType.BUFFER, slot.flow.audio)
        bin_.connect("deep-element-added", lambda _bin, _sub, element: self._log_decoder(label, element))
        for what in ("", "picture", "sound"):  # named in _source_bin (Gst.Iterator needs overrides)
            element = bin_.get_by_name(f"source {what}".strip())
            if element is not None:
                slot.sources.append((what, element))
                element.connect("notify::status", lambda src, _spec, what=what: self._log_status(slot, what, src))
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

        def end_of_stream(_pad, info):
            if info.get_event().type == self.Gst.EventType.EOS:
                self.GLib.idle_add(self._on_eos, slot)
            return self.Gst.PadProbeReturn.OK

        slot.video_pad.add_probe(self.Gst.PadProbeType.BUFFER, first_frame)
        slot.video_pad.add_probe(self.Gst.PadProbeType.EVENT_DOWNSTREAM, end_of_stream)
        self.pending = slot
        bin_.sync_state_with_parent()
        self.GLib.timeout_add(int(START_TIMEOUT_S * 1000), self._on_start_timeout, slot)

    def _on_ready(self, slot: Slot) -> bool:
        if slot is not self.pending:
            return False
        slot.ready = True
        old, self.current, self.pending = self.current, slot, None
        started = time.monotonic()
        duration = 0.0 if slot.cut else FADE_S

        def fade() -> bool:
            t = min(1.0, (time.monotonic() - started) / duration) if duration else 1.0
            slot.video_pad.set_property("alpha", t)
            slot.audio_pad.set_property("volume", t)
            if old is not None:
                old.audio_pad.set_property("volume", 1.0 - t)
            if t < 1.0:
                return True
            if old is not None:
                self._drop(old)
            emit("playing", id=slot.id, label=slot.label)
            return False

        if fade():
            self.GLib.timeout_add(20, fade)
        return False

    def _on_start_timeout(self, slot: Slot) -> bool:
        if slot is self.pending and not slot.ready:
            self.pending = None
            self._drop(slot)
            emit("switch_failed", id=slot.id, label=slot.label, reason="no picture within 15 s")
        return False

    def _on_eos(self, slot: Slot) -> bool:
        if slot is self.current:
            emit("ended", id=slot.id)
        elif slot is self.pending:
            self.pending = None
            self._drop(slot)
            emit("switch_failed", id=slot.id, label=slot.label, reason="ended before the first picture")
        return False

    def clear(self) -> None:
        """Fades the current input out; the background stays."""
        if self.pending is not None:
            old = self.pending
            self.pending = None
            self._drop(old)
        slot, self.current = self.current, None
        if slot is None:
            emit("cleared")
            return
        started = time.monotonic()

        def fade() -> bool:
            t = min(1.0, (time.monotonic() - started) / FADE_S)
            slot.video_pad.set_property("alpha", 1.0 - t)
            slot.audio_pad.set_property("volume", 1.0 - t)
            if t < 1.0:
                return True
            self._drop(slot)
            emit("cleared")
            return False

        self.GLib.timeout_add(20, fade)

    def _drop(self, slot: Slot) -> None:
        """Takes an input out: out of the pipeline and off the mixers right away, stopped
        (state NULL) in a thread. Stopping waits for its streaming threads – on the test
        PC over 5 s at times (Windows decoder, a download), and in the main loop that
        stalled the whole output until the watchdog restarted the worker (2026-11-12)."""
        self.dropping.add(slot.bin)  # its last messages (e.g. "not linked") are no errors
        self.pipeline.remove(slot.bin)  # also unlinks its ghost pads from the mixers
        for _ghost, mixer, pad in slot.pads:
            mixer.release_request_pad(pad)
        # an input that failed while starting (e.g. a missing picture) left the pipeline
        # waiting for it in PAUSED, and the next input then started paused too: on again
        self.pipeline.set_state(self.Gst.State.PLAYING)
        threading.Thread(target=self._stop_bin, args=(slot.bin, slot.label), daemon=True).start()

    def _stop_bin(self, bin_, label: str) -> None:
        began = time.monotonic()
        try:
            bin_.set_state(self.Gst.State.NULL)
        except Exception as exc:  # noqa: BLE001 – only a dropped input
            log(f"input {label!r}: stopping failed: {exc}")
        took = time.monotonic() - began
        if took > 1.0:  # was the reason for the main loop stalls, see _drop
            log(f"input {label!r} took {took:.1f} s to stop")
        self.GLib.idle_add(self.dropping.discard, bin_)

    def _dropped(self, element) -> bool:
        while element is not None:
            if element in self.dropping:
                return True
            element = element.get_parent()
        return False

    # --- lower third ------------------------------------------------------------

    def overlay(self, title: str, subtitle: str, duration_s: float) -> None:
        """Shows title + subtitle (fading in), for duration_s or until replaced."""
        self.overlay_gen += 1
        gen = self.overlay_gen
        try:
            if not title:
                self._fade_overlay(gen, 1.0, 0.0)
                return
            for name, text in (("ovtitle", title), ("ovsub", subtitle)):
                self.pipeline.get_by_name(name).set_property("text", _plain(text))
            self.pipeline.get_by_name("ovsub").set_property("silent", not subtitle)
            self.pipeline.get_by_name("ovtitle").set_property("silent", False)
            self._fade_overlay(gen, 0.0, 1.0)
            if duration_s > 0:
                self.GLib.timeout_add(int(duration_s * 1000), self._fade_overlay, gen, 1.0, 0.0)
            log(f"Overlay shown: {title} | {subtitle}")
        except Exception as exc:  # noqa: BLE001 – the stream goes on without it
            log(f"Overlay failed: {exc}")

    def _fade_overlay(self, gen: int, start: float, end: float) -> bool:
        began = time.monotonic()

        def step() -> bool:
            if gen != self.overlay_gen:
                return False  # a newer overlay command took over
            t = min(1.0, (time.monotonic() - began) / OVERLAY_FADE_S)
            self._overlay_opacity(start + (end - start) * t)
            return t < 1.0

        if step():
            self.GLib.timeout_add(20, step)
        return False

    def _overlay_opacity(self, opacity: float) -> None:
        alpha = int(255 * opacity) << 24
        for name, rgb in (("ovtitle", 0xFFFFFF), ("ovsub", 0xD0D4E0)):
            element = self.pipeline.get_by_name(name)
            element.set_property("color", alpha | rgb)
            element.set_property("shading-value", max(1, int(OVERLAY_SHADING * opacity)))
            if opacity == 0.0:
                element.set_property("silent", True)

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
        try:
            kind = message.type
        except (OverflowError, SystemError):
            # the "extended" types (stream collection, ...) are above 2**31: they do not
            # fit a C long on Windows, PyGObject cannot read them. None of them is used here.
            return
        if kind == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            if self._dropped(message.src):
                log(f"error from an input being taken out (ignored): {error.message}")
                return
            slot = self._slot_of(message.src)
            if slot is not None:
                log(f"input {slot.label!r} failed: {error.message} ({debug})")
                if slot is self.pending:
                    self.pending = None
                    self._drop(slot)
                    emit("switch_failed", id=slot.id, label=slot.label, reason=error.message)
                elif not slot.failed:
                    slot.failed = True  # report once; the core switches away
                    emit("failed", id=slot.id, reason=error.message)
                return
            if message.src.get_name() == "mux":
                for pad in message.src.sinkpads:  # what the muxer had when it failed
                    caps = pad.get_current_caps()
                    log(f"muxer input {pad.get_name()}: {caps.to_string() if caps else 'no caps'}")
            emit("error", message=f"{error.message} ({debug})")
            self.loop.quit()
        elif kind == Gst.MessageType.WARNING:
            warning, _debug = message.parse_warning()
            log(f"warning from {message.src.get_name()}: {warning.message}")

    def debug_clock(self, on: bool) -> None:
        # empty text instead of the "silent" property: the overlay stays as it always ran
        self.clock_on = on
        if not on:
            self.pipeline.get_by_name("info").set_property("text", "")

    def _update_overlay(self) -> bool:
        if not self.clock_on:
            return True
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
        slot = self.current
        if slot is not None and now - slot.flow.since >= FLOW_S:
            frames, sound = slot.flow.take()
            states = "; ".join(f"{what + ' ' if what else ''}source {self._source_state(src)}" for what, src in slot.sources)
            log(f"input {slot.label!r} delivers {frames:.1f} frames/s, {sound:.2f} s of sound per second"
                + (f"; {states}" if states else ""))  # fmt: skip
        return True

    @staticmethod
    def _source_state(src) -> str:
        try:
            return source_state(src.get_property("status").value_nick, src.get_property("statistics").to_string())
        except Exception as exc:  # noqa: BLE001 – only diagnostics
            return f"unknown ({exc})"

    @staticmethod
    def _log_status(slot: Slot, what: str, src) -> None:
        # any thread: only a log line. Not "running": the input shows fallback black/silence.
        # fallbacksrc notifies often without a change, so only changes are logged.
        try:
            status = src.get_property("status").value_nick
        except Exception:  # noqa: BLE001
            return
        if slot.status.get(what) != status:
            slot.status[what] = status
            log(f"input {slot.label!r}{' ' + what if what else ''}: source {status}")

    @staticmethod
    def _log_decoder(label: str, element) -> None:
        # streaming thread possibly: only a log line
        factory = element.get_factory()
        klass = (factory.get_metadata("klass") or "") if factory is not None else ""
        if "Decoder" in klass and "Bin" not in klass:  # not decodebin3 and friends
            log(f"input {label!r}: decoder {factory.get_name()}")

    # --- watchdog -----------------------------------------------------------------

    def _beat(self) -> bool:
        self.heartbeat = time.monotonic()
        return True

    def _watch(self) -> None:
        """Reports a main loop that hangs (seen once on Windows CI after a switch:
        no answers, no overlay any more, the picture went on). Dumps every
        thread's stack into output.log once per hang; the core restarts the worker."""
        stalled = False
        while self.watching:
            time.sleep(1.0)
            silent = time.monotonic() - self.heartbeat
            if silent < STALL_S:
                if stalled:
                    log(f"watchdog: main loop answers again after {silent:.1f} s")
                stalled = False
                continue
            if stalled or not self.watching:  # shutting down: set_state(NULL) may take a while
                continue
            stalled = True
            current = self.current.label if self.current else "background"
            pending = self.pending.label if self.pending else "none"
            log(f"watchdog: main loop silent for {silent:.1f} s (on air: {current}, pending: {pending});"
                " stacks of all threads:")  # fmt: skip
            try:
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            except Exception as exc:  # noqa: BLE001 – stderr may not be a real file
                log(f"watchdog: cannot dump stacks: {exc}")
            emit("stalled", seconds=round(silent, 1))

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
            self.play(str(command.get("id", "")), command.get("label", "Input"), command["source"])
        elif command.get("cmd") == "clear":
            self.clear()
        elif command.get("cmd") == "overlay":
            self.overlay(str(command.get("title", "")), str(command.get("subtitle", "")),
                         float(command.get("duration_s", 0)))  # fmt: skip
        elif command.get("cmd") == "debug_clock":
            self.debug_clock(bool(command.get("on")))
        elif command.get("cmd") == "stop":
            self.loop.quit()
        return False

    def run(self) -> int:
        log("worker: pipeline built, starting it")
        result = self.pipeline.set_state(self.Gst.State.PLAYING)
        if result == self.Gst.StateChangeReturn.FAILURE:
            emit("error", message="pipeline did not start (is MediaMTX running?)")
            return 1
        self._stats_since = time.monotonic()
        self.GLib.timeout_add(15, self._update_overlay)
        self.GLib.timeout_add(2000, self._report_stats)
        self.heartbeat, self.watching = time.monotonic(), True
        self.GLib.timeout_add(int(HEARTBEAT_S * 1000), self._beat)
        threading.Thread(target=self._watch, daemon=True).start()
        threading.Thread(target=self._read_commands, daemon=True).start()
        emit("ready", encoder=self.args.encoder)
        log(f"worker: running ({result})")
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        self.watching = False
        self.pipeline.set_state(self.Gst.State.NULL)
        return 0


OVERLAY_TEXT = (
    "silent=true halignment=left valignment=bottom xpad=64 shaded-background=true"
    " draw-outline=false draw-shadow=false"
)
CARD_TEXT = "halignment=center valignment=position xpos=0.5 shaded-background=false draw-shadow=false"


def _plain(text: str) -> str:
    """textoverlay reads Pango markup: keep its special characters out."""
    return str(text).translate(str.maketrans({"&": "+", "<": "‹", ">": "›"}))


def _link_by_kind(pad, heads: dict) -> None:
    caps = pad.get_current_caps() or pad.query_caps(None)
    name = caps.get_structure(0).get_name() if caps and caps.get_size() else pad.get_name()
    kind = "video" if name.startswith("video") else "audio" if name.startswith("audio") else None
    head = heads.get(kind) if kind else None  # a source may give only one kind
    if head is not None and not head.is_linked():
        pad.link(head)


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

    log(f"worker: starting ({args.encoder}, {args.width}x{args.height} @ {args.fps})")
    # mpegtsmux gives the reason for a failed stream setup only in its debug log
    # (seen once on Linux, GStreamer 1.24: "Could not create handler for stream")
    os.environ.setdefault("GST_DEBUG", "basetsmux:2,mpegtsmux:2")
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
