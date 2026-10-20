"""Checks that this machine can run lan-streamer: ``python -m lanstreamer.doctor``."""

from __future__ import annotations

import argparse
import platform
import sys

from lanstreamer import __version__
from lanstreamer.media import encoders, gst

# Elements the output pipeline needs (see docs/architecture.md).
REQUIRED_ELEMENTS = (
    "videotestsrc", "videoconvert", "videoscale", "videorate", "compositor",
    "audioconvert", "audioresample", "audiomixer", "audioloudnorm", "opusenc",
    "h264parse", "fallbacksrc", "uridecodebin3", "mpegtsmux", "srtsink", "textoverlay",
)

# Windows plugins for GPU encoding and audio; they must load even without the hardware.
WINDOWS_PLUGINS = ("d3d11", "d3d12", "nvcodec", "amfcodec", "qsv", "mediafoundation", "wasapi2")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--encoder", default="auto", choices=("auto", *encoders.AUTO_ORDER))
    parser.add_argument("--fps", type=int, default=60, choices=(30, 60))
    parser.add_argument("--pause", action="store_true", help="wait for Enter at the end")
    args = parser.parse_args(argv)
    code = check(args)
    if args.pause:
        input("\nPress Enter to close this window ...")
    return code


def check(args: argparse.Namespace) -> int:

    print(f"lan-streamer {__version__}")
    print(f"Python     {platform.python_version()} ({sys.platform})")
    try:
        Gst = gst.init()
    except Exception as exc:  # noqa: BLE001 – report any import problem
        print(f"GStreamer  NOT AVAILABLE: {exc}")
        return 1
    location = gst.root()
    print(f"GStreamer  {Gst.version_string()}" + (f" ({location})" if location else ""))

    missing = [name for name in REQUIRED_ELEMENTS if Gst.ElementFactory.find(name) is None]
    print("Elements   " + ("all present" if not missing else "MISSING: " + ", ".join(missing)))

    # A plugin that cannot be loaded (missing DLL) is absent from the registry. A
    # plugin without matching hardware still loads, it just registers no elements.
    broken: list[str] = []
    if sys.platform == "win32":
        broken = [p for p in WINDOWS_PLUGINS if Gst.Registry.get().find_plugin(p) is None]
        print("Plugins    " + ("all loaded" if not broken else "NOT LOADED: " + ", ".join(broken)))

    print(f"Encoders   (preference: {args.encoder}, 1080p{args.fps}, each tested in its own process)")
    spec, log = encoders.select(args.encoder, args.fps)
    for line in log:
        print(f"           {line}")
    print("Selected   " + (f"{spec.element} ({spec.vendor})" if spec else "NONE"))

    return 0 if spec and not missing and not broken else 1


if __name__ == "__main__":
    sys.exit(main())
