"""The loudness levelling arithmetic (no GStreamer)."""

import itertools
import math
from array import array

from lanstreamer.media import loudness
from lanstreamer.media.loudness import Leveler


def rms_db(gain: float, level_db: float) -> float:
    return level_db + 20 * math.log10(gain)


def run(leveler: Leveler, level_db: float, seconds: float, buffer_s: float = 0.01) -> float:
    square = 10 ** (level_db / 10)
    gain = leveler.gain
    for _ in range(round(seconds / buffer_s)):
        gain = leveler.feed(square, buffer_s)
    return gain


def test_a_loud_and_a_quiet_source_end_up_about_equally_loud():
    loud = run(Leveler(), -8.0, 10)  # loud pop master
    quiet = run(Leveler(), -30.0, 10)  # quiet stream
    assert abs(rms_db(loud, -8.0) - loudness.TARGET_DB) < 0.5
    assert abs(rms_db(quiet, -30.0) - loudness.TARGET_DB) < 0.5


def test_it_settles_within_a_few_seconds_and_moves_smoothly():
    leveler = Leveler()
    gains = [run(leveler, -8.0, 0.1) for _ in range(30)]  # 3 s
    steps = [abs(20 * math.log10(b / a)) for a, b in itertools.pairwise(gains)]
    assert max(steps) <= loudness.SLEW_DB_PER_S * 0.1 + 1e-9  # no jumps
    assert abs(rms_db(gains[-1], -8.0) - loudness.TARGET_DB) < 1.5


def test_silence_is_not_pulled_up_and_the_boost_has_a_limit():
    leveler = Leveler()
    assert run(leveler, -70.0, 10) == 1.0  # below the gate: unchanged
    very_quiet = run(Leveler(), -45.0, 20)
    assert math.isclose(20 * math.log10(very_quiet), loudness.MAX_BOOST_DB, abs_tol=0.01)


def test_a_pause_keeps_the_gain():
    leveler = Leveler()
    before = run(leveler, -10.0, 8)
    assert run(leveler, -80.0, 5) == before


def test_mean_square_of_f32_samples():
    samples = array("f", [0.5, -0.5] * 4800)  # 0.1 s stereo at 48 kHz
    square, seconds = loudness.mean_square(samples.tobytes())
    assert math.isclose(square, 0.25, rel_tol=1e-6) and math.isclose(seconds, 0.1)
    assert loudness.mean_square(b"") == (0.0, 0.0)
