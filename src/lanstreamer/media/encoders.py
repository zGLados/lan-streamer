"""H.264 encoder selection: NVIDIA, AMD, Intel or CPU, auto-detected or forced.

Each vendor has several candidate GStreamer elements per platform. A candidate
only counts as available after it has encoded real test frames, because an
element can be registered without a matching GPU or driver.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal

Vendor = Literal["nvidia", "amd", "intel", "cpu"]
Preference = Literal["auto", "nvidia", "amd", "intel", "cpu"]

AUTO_ORDER: tuple[Vendor, ...] = ("nvidia", "amd", "intel", "cpu")

# Low-latency settings. Each entry lists the property names the different
# elements use for the same thing; the first one the element has is set.
_COMMON: tuple[tuple[tuple[str, ...], str], ...] = (
    (("bframes", "b-frames"), "0"),
    (("gop-size", "key-int-max"), "30"),  # 1 s at 30 fps
    (("rate-control", "rc-mode"), "cbr"),
)


@dataclass(frozen=True)
class EncoderSpec:
    vendor: Vendor
    element: str
    settings: tuple[tuple[tuple[str, ...], str], ...] = field(default=())

    def all_settings(self) -> tuple[tuple[tuple[str, ...], str], ...]:
        return _COMMON + self.settings


_NVIDIA = (("zerolatency",), "true"), (("preset",), "p1")
_AMD = (("usage",), "ultra-low-latency"), (("preset",), "speed")
_INTEL = (("low-latency",), "true"), (("target-usage",), "7")
_X264 = (("speed-preset",), "veryfast"), (("tune",), "zerolatency")

CANDIDATES: dict[str, dict[Vendor, tuple[EncoderSpec, ...]]] = {
    "win32": {
        "nvidia": (
            EncoderSpec("nvidia", "nvd3d11h264enc", _NVIDIA),
            EncoderSpec("nvidia", "nvcudah264enc", _NVIDIA),
            EncoderSpec("nvidia", "nvh264enc", _NVIDIA),
        ),
        "amd": (EncoderSpec("amd", "amfh264enc", _AMD),),
        "intel": (
            EncoderSpec("intel", "qsvh264enc", _INTEL),
            EncoderSpec("intel", "mfh264enc", ((("low-latency",), "true"),)),
        ),
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


def make_encoder(Gst, spec: EncoderSpec, bitrate_kbps: int = 6000):
    """Creates the element with low-latency settings, or returns None."""
    element = Gst.ElementFactory.make(spec.element, None)
    if element is None:
        return None
    for names, value in spec.all_settings() + ((("bitrate",), str(bitrate_kbps)),):
        for name in names:
            if element.find_property(name) is not None:
                Gst.util_set_object_arg(element, name, value)
                break
    return element


def probe(Gst, spec: EncoderSpec, timeout_s: float = 15.0) -> tuple[bool, str]:
    """Encodes 60 1080p test frames. Returns (works, detail)."""
    encoder = make_encoder(Gst, spec)
    if encoder is None:
        return False, "element not installed"

    pipeline = Gst.Pipeline.new(None)
    src = Gst.ElementFactory.make("videotestsrc", None)
    src.set_property("num-buffers", 60)
    caps = Gst.ElementFactory.make("capsfilter", None)
    caps.set_property(
        "caps", Gst.Caps.from_string("video/x-raw,width=1920,height=1080,framerate=30/1")
    )
    convert = Gst.ElementFactory.make("videoconvert", None)
    parse = Gst.ElementFactory.make("h264parse", None)
    sink = Gst.ElementFactory.make("fakesink", None)
    chain = (src, caps, convert, encoder, parse, sink)
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


def select(Gst, preference: Preference = "auto") -> tuple[EncoderSpec | None, list[str]]:
    """Returns the first working encoder for the preference and a log of the tries."""
    log: list[str] = []
    for spec in candidates(preference):
        works, detail = probe(Gst, spec)
        log.append(f"{spec.vendor:<6} {spec.element:<16} {detail}")
        if works:
            return spec, log
    return None, log
