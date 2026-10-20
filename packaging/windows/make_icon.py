"""Draws the app icon (blue rounded square with a white play triangle) as .ico.

    python make_icon.py <out.ico>      (needs Pillow; used by build.ps1)
"""

import sys

from PIL import Image, ImageDraw

SIZE = 256


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, SIZE - 8, SIZE - 8), radius=56, fill=(79, 140, 255, 255))
    d.polygon([(116, 68), (116, 188), (212, 128)], fill=(255, 255, 255, 255))
    # three "network" bars bottom left
    for i, h in enumerate((24, 40, 56)):
        x = 44 + i * 20
        d.rounded_rectangle((x, 204 - h, x + 12, 204), radius=4, fill=(255, 255, 255, 200))
    return img


if __name__ == "__main__":
    draw().save(sys.argv[1], sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
