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
    "h264parse", "fallbacksrc", "uridecodebin3", "rtspclientsink",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--encoder", default="auto", choices=("auto", *encoders.AUTO_ORDER))
    args = parser.parse_args(argv)

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

    print(f"Encoders   (preference: {args.encoder})")
    spec, log = encoders.select(Gst, args.encoder)
    for line in log:
        print(f"           {line}")
    print("Selected   " + (f"{spec.element} ({spec.vendor})" if spec else "NONE"))

    return 0 if spec and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
