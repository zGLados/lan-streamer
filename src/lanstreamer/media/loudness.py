"""Every input about equally loud (owner, 2026-11-11).

Each media source's sound is measured as it flows (a pad probe in front of its
own volume element) and its gain is moved slowly towards a common level: loud
sources are turned down, quiet ones up (to MAX_BOOST_DB at most). Silence and
very quiet parts do not count, so a pause is not pulled up into noise. No
look-ahead, so no extra delay; the limiter in front of the encoder catches the
peaks of a sudden loud part until the gain has followed.

`Leveler` is the arithmetic (no GStreamer), `attach` puts it on a volume element.
"""

from __future__ import annotations

import math
from array import array

TARGET_DB = -20.0  # RMS of the mix in dBFS – roughly what broadcast loudness aims at
GATE_DB = -50.0  # quieter than this: silence or a fade, the gain stays
MAX_BOOST_DB = 12.0
MAX_CUT_DB = -24.0
SETTLE_S = 3.0  # the measured level follows this slowly ...
WARMUP_S = 2.0  # ... except in the first seconds of sound, when it follows fast (0.5 s)
SLEW_DB_PER_S = 12.0  # the gain moves at most this fast
RATE = 48000
CHANNELS = 2
DECIMATE = 8  # measure every 8th sample: plenty for a level, cheap in Python


class Leveler:
    def __init__(self) -> None:
        self.power: float | None = None  # mean square, smoothed
        self.heard_s = 0.0  # seconds of sound above the gate so far
        self.gain_db = 0.0

    @property
    def gain(self) -> float:
        return 10 ** (self.gain_db / 20)

    def feed(self, mean_square: float, seconds: float) -> float:
        """One buffer's mean square (full scale = 1.0) and length → the gain to use (linear)."""
        if mean_square <= 0 or 10 * math.log10(mean_square) < GATE_DB:
            return self.gain
        self.heard_s += seconds
        tau = 0.5 if self.heard_s < WARMUP_S else SETTLE_S
        share = 1 - math.exp(-seconds / tau)
        self.power = mean_square if self.power is None else self.power + share * (mean_square - self.power)
        wanted = min(MAX_BOOST_DB, max(MAX_CUT_DB, TARGET_DB - 10 * math.log10(self.power)))
        step = SLEW_DB_PER_S * seconds
        self.gain_db += min(step, max(-step, wanted - self.gain_db))
        return self.gain


def mean_square(data: bytes) -> tuple[float, float]:
    """Interleaved F32 samples → (mean square, seconds)."""
    samples = array("f")
    samples.frombytes(data[: len(data) - len(data) % 4])
    part = samples[::DECIMATE]
    if not part:
        return 0.0, 0.0
    return sum(v * v for v in part) / len(part), len(samples) / CHANNELS / RATE


def attach(Gst, volume, log) -> Leveler:
    """Measures what enters `volume` (F32, 48 kHz, stereo) and sets its gain."""
    leveler = Leveler()

    def probe(_pad, info):
        # streaming thread: must never raise
        try:
            buffer = info.get_buffer()
            square, seconds = mean_square(buffer.extract_dup(0, buffer.get_size()))
            before = leveler.gain_db
            gain = leveler.feed(square, seconds)
            if abs(leveler.gain_db - before) > 0.05:
                volume.set_property("volume", gain)
        except Exception as exc:  # noqa: BLE001
            log(f"loudness: measuring stopped: {exc}")
            return Gst.PadProbeReturn.REMOVE
        return Gst.PadProbeReturn.OK

    volume.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, probe)
    return leveler
