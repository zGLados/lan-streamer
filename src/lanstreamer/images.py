"""Still pictures: their size, read from the PNG or JPEG header, and how one is
placed on the output (ImageSource.size). No GStreamer, so the web API uses it too.
"""

from __future__ import annotations

import struct
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
# JPEG frame headers with the size; C4 (Huffman), C8 and CC are no frames
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def kind(data: bytes) -> str | None:
    """"png", "jpeg" or None, from the first bytes."""
    if data.startswith(PNG):
        return "png"
    if data.startswith(JPEG):
        return "jpeg"
    return None


def size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG or JPEG; None if it is neither or broken."""
    if data.startswith(PNG) and data[12:16] == b"IHDR" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width and height else None
    if not data.startswith(JPEG):
        return None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD8)):  # no length
            pos += 2
            continue
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        if marker in _SOF:
            if pos + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
            return (width, height) if width and height else None
        if marker == 0xDA or length < 2:  # image data starts: no frame header before it
            return None
        pos += 2 + length
    return None


def file_size(path: str | Path) -> tuple[int, int] | None:
    try:
        return size(Path(path).read_bytes())
    except OSError:
        return None


def layout(mode: str, picture: tuple[int, int] | None, screen: tuple[int, int]) -> tuple[int, int, int, int, int, int] | None:
    """How a picture is placed: (width, height) to scale it to, then videobox's
    left, right, top, bottom (positive: cut off, negative: black border).

    None: scaled to fit the screen with black bars (mode "fit", the size is unknown,
    or it is exactly the screen's).
    "auto": a picture that fits on the screen keeps its size, centered; a larger one fits.
    "fill": scaled to cover the whole screen, the edges that stick out are cut off.
    """
    if picture is None or mode not in ("auto", "fill"):
        return None
    (w, h), (sw, sh) = picture, screen
    if mode == "auto":
        if w > sw or h > sh:
            return None
    else:
        scale = max(sw / w, sh / h)
        w, h = max(sw, round(w * scale)), max(sh, round(h * scale))
    dx, dy = w - sw, h - sh
    if dx == dy == 0:  # exactly the screen: nothing to do
        return None
    return w, h, dx // 2, dx - dx // 2, dy // 2, dy - dy // 2
