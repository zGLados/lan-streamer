"""The bouncing picture (BounceSource): where it is at a time, and its colors.

No GStreamer here, so the tests need none; output.py moves the picture with it.
"""

from __future__ import annotations

import colorsys
import random

MIN_HUE_STEP = 0.15  # a new color differs this much in hue from the last one


def fit(picture: tuple[int, int], screen: tuple[int, int], percent: float) -> tuple[int, int]:
    """The picture's size on the screen: percent of the screen's width, its aspect
    ratio kept, never more than 80 % of the screen in either direction; even numbers
    (the picture is converted to YUV)."""
    (pw, ph), (sw, sh) = picture, screen
    width = sw * min(max(percent, 1.0), 80.0) / 100
    height = width * ph / pw
    if height > sh * 0.8:
        width, height = width * sh * 0.8 / height, sh * 0.8
    return max(2, round(width / 2) * 2), max(2, round(height / 2) * 2)


def _fold(distance: float, length: int) -> tuple[int, int]:
    """(position, bounces so far) after moving distance along an edge of length
    `length`, turning around at both ends."""
    if length <= 0:
        return 0, 0
    bounces, rest = divmod(distance, length)
    return round(rest if bounces % 2 == 0 else length - rest), int(bounces)


class Bounce:
    """Moves a picture of size `picture` over `screen` at `speed` pixels per second
    (diagonal at 45°), from a random start; at(t) gives the position at t seconds."""

    def __init__(self, picture: tuple[int, int], screen: tuple[int, int], speed: float,
                 rng: random.Random | None = None) -> None:  # fmt: skip
        self.rng = rng or random.Random()
        self.range_x = max(0, screen[0] - picture[0])
        self.range_y = max(0, screen[1] - picture[1])
        self.speed = max(0.0, speed) / 2**0.5  # per axis
        # a start somewhere on the path, going in any direction (both are the same path)
        self.start_x = self.rng.uniform(0, 2 * self.range_x)
        self.start_y = self.rng.uniform(0, 2 * self.range_y)

    def at(self, t: float) -> tuple[int, int, int]:
        """(x, y, bounces): the top left corner at t seconds and how often it has
        bounced off an edge since the start (a corner counts twice)."""
        x, bx = _fold(self.start_x + self.speed * t, self.range_x)
        y, by = _fold(self.start_y + self.speed * t, self.range_y)
        return x, y, bx + by


def next_color(previous: tuple[int, int, int] | None, rng: random.Random) -> tuple[int, int, int]:
    """A bright random color (r, g, b), clearly another hue than previous."""
    last = colorsys.rgb_to_hsv(*(c / 255 for c in previous))[0] if previous else None
    while True:
        hue = rng.random()
        if last is None or min(abs(hue - last), 1 - abs(hue - last)) >= MIN_HUE_STEP:
            break
    r, g, b = colorsys.hsv_to_rgb(hue, rng.uniform(0.75, 1.0), 1.0)
    return round(r * 255), round(g * 255), round(b * 255)


def tint(rgba: bytes, color: tuple[int, int, int]) -> bytes:
    """RGBA pixels multiplied with color; the transparency stays."""
    out = bytearray(rgba)
    for channel, value in enumerate(color):
        table = bytes(v * value // 255 for v in range(256))
        out[channel::4] = rgba[channel::4].translate(table)
    return bytes(out)
