"""H.264 encoder selection: NVIDIA, AMD, Intel or CPU, auto-detected or forced.

Each vendor has several candidate GStreamer elements per platform. A candidate
only counts as available after it has encoded real test frames, because an
element can be registered without a matching GPU or driver. Every probe runs in
its own process, so a crashing GPU driver cannot take the app down with it.

Probe one encoder by hand: ``python -m lanstreamer.media.encoders x264enc --fps 60``
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal

Vendor = Literal["nvidia", "amd", "intel", "cpu"]
Preference = Literal["auto", "nvidia", "amd", "intel", "cpu"]

AUTO_ORDER: tuple[Vendor, ...] = ("nvidia", "amd", "intel", "cpu")

# Profiles every browser decodes over WebRTC, in order of preference.
H264_CAPS = "video/x-h264,profile=(string){ constrained-baseline, baseline, main }"

Settings = tuple[tuple[tuple[str, ...], str], ...]


@dataclass(frozen=True)
class EncoderSpec:
    vendor: Vendor
    element: str
    # Each entry lists the property names the different elements use for the
    # same thing; the first one the element has is set.
    settings: Settings = field(default=())


_NVIDIA = (("zerolatency",), "true"), (("preset",), "p1")
_AMD = (("usage",), "ultra-low-latency"), (("preset",), "speed")
_INTEL = (("low-latency",), "true"), (("target-usage",), "7")
_X264 = (("speed-preset",), "superfast"), (("tune",), "zerolatency")

CANDIDATES: dict[str, dict[Vendor, tuple[EncoderSpec, ...]]] = {
    "win32": {
        "nvidia": (
            EncoderSpec("nvidia", "nvd3d11h264enc", _NVIDIA),
            EncoderSpec("nvidia", "nvcudah264enc", _NVIDIA),
            EncoderSpec("nvidia", "nvh264enc", _NVIDIA),
        ),
        "amd": (EncoderSpec("amd", "amfh264enc", _AMD),),
        # Not mfh264enc: Media Foundation may pick a software encoder and would
        # then be reported as a GPU encoder.
        "intel": (EncoderSpec("intel", "qsvh264enc", _INTEL),),
        "cpu": (EncoderSpec("cpu", "x264enc", _X264),),
    },
    "linux": {
        "nvidia": (
            EncoderSpec("nvidia", "nvcudah264enc", _NVIDIA),
            EncoderSpec("nvidia", "nvh264enc", _NVIDIA),
        ),
        "amd": (EncoderSpec("amd", "vah264enc", ((("target-usage",), "7"),)),),
        "intel": (
            EncoderSpec("intel", "qsvh264enc", _INTEL),
            EncoderSpec("intel", "vah264enc", ((("target-usage",), "7"),)),
        ),
        "cpu": (EncoderSpec("cpu", "x264enc", _X264),),
    },
}


def candidates(preference: Preference, platform: str = sys.platform) -> list[EncoderSpec]:
    """Candidates in the order they are tried. CPU is always the last resort."""
    table = CANDIDATES["win32" if platform == "win32" else "linux"]
    vendors: tuple[Vendor, ...] = AUTO_ORDER if preference == "auto" else (preference, "cpu")
    result: list[EncoderSpec] = []
    for vendor in dict.fromkeys(vendors):
        result.extend(table[vendor])
    return result


def find(element: str, platform: str = sys.platform) -> EncoderSpec | None:
    """The candidate for an element name, or None."""
    return next((s for s in candidates("auto", platform) if s.element == element), None)


def low_latency_settings(spec: EncoderSpec, fps: int, bitrate_kbps: int) -> Settings:
    """Settings for one keyframe per second, no B-frames, constant bitrate."""
    return (
        (("bframes", "b-frames"), "0"),
        (("gop-size", "key-int-max"), str(fps)),
        (("rate-control", "rc-mode"), "cbr"),
        (("bitrate",), str(bitrate_kbps)),
        *spec.settings,
    )


def make_encoder(Gst, spec: EncoderSpec, fps: int = 60, bitrate_kbps: int = 8000):
    """Creates the element with low-latency settings, or returns None."""
    element = Gst.ElementFactory.make(spec.element, None)
    if element is None:
        return None
    for names, value in low_latency_settings(spec, fps, bitrate_kbps):
        for name in names:
            if element.find_property(name) is not None:
                Gst.util_set_object_arg(element, name, value)
                break
    return element


def probe(Gst, spec: EncoderSpec, fps: int = 60, timeout_s: float = 20.0) -> tuple[bool, str]:
    """Encodes one second of 1080p test frames in this process. Returns (works, detail)."""
    encoder = make_encoder(Gst, spec, fps)
    if encoder is None:
        return False, "element not installed"

    pipeline = Gst.Pipeline.new(None)
    src = Gst.ElementFactory.make("videotestsrc", None)
    src.set_property("num-buffers", fps)
    raw = Gst.ElementFactory.make("capsfilter", None)
    raw.set_property(
        "caps", Gst.Caps.from_string(f"video/x-raw,width=1920,height=1080,framerate={fps}/1")
    )
    convert = Gst.ElementFactory.make("videoconvert", None)
    h264 = Gst.ElementFactory.make("capsfilter", None)
    h264.set_property("caps", Gst.Caps.from_string(H264_CAPS))
    parse = Gst.ElementFactory.make("h264parse", None)
    sink = Gst.ElementFactory.make("fakesink", None)
    chain = (src, raw, convert, encoder, h264, parse, sink)
    for element in chain:
        pipeline.add(element)
    for left, right in pairwise(chain):
        if not left.link(right):
            return False, f"cannot link {left.get_name()} -> {right.get_name()}"

    pipeline.set_state(Gst.State.PLAYING)
    try:
        message = pipeline.get_bus().timed_pop_filtered(
            int(timeout_s * Gst.SECOND), Gst.MessageType.EOS | Gst.MessageType.ERROR
        )
        if message is None:
            return False, "timeout"
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            return False, error.message
        return True, "ok"
    finally:
        pipeline.set_state(Gst.State.NULL)


def probe_isolated(spec: EncoderSpec, fps: int = 60, timeout_s: float = 45.0) -> tuple[bool, str]:
    """Runs probe() in a child process, so a driver crash only kills the child."""
    cmd = [sys.executable, "-m", "lanstreamer.media.encoders", spec.element, "--fps", str(fps)]
    try:
        done = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    for line in reversed(done.stdout.splitlines()):
        if line.startswith("{"):
            result = json.loads(line)
            return bool(result["ok"]), str(result["detail"])
    return False, f"probe crashed (exit code {done.returncode})"


def select(
    preference: Preference = "auto", fps: int = 60
) -> tuple[EncoderSpec | None, list[str]]:
    """Returns the first working encoder for the preference and a log of the tries."""
    log: list[str] = []
    for spec in candidates(preference):
        works, detail = probe_isolated(spec, fps)
        log.append(f"{spec.vendor:<6} {spec.element:<16} {detail}")
        if works:
            return spec, log
    return None, log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe one H.264 encoder.")
    parser.add_argument("element")
    parser.add_argument("--fps", type=int, default=60)
    args = parser.parse_args(argv)

    spec = find(args.element)
    if spec is None:
        result = {"ok": False, "detail": "unknown encoder"}
    else:
        from lanstreamer.media import gst

        works, detail = probe(gst.init(), spec, args.fps)
        result = {"ok": works, "detail": detail}
    print(json.dumps(result), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
