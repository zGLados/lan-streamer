"""Draws the lan-streamer icon: a play button sending to three screens on a LAN.

    python make_icon.py <out.ico> [<out.png>]      (needs Pillow; used by build.ps1)
"""

import sys

from PIL import Image, ImageDraw

SIZE = 512  # drawn large, scaled down for the .ico sizes


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    # background: rounded square with a diagonal blue → violet gradient
    grad = Image.new("RGBA", (SIZE, SIZE))
    top, bottom = (47, 128, 255), (124, 77, 255)
    for y in range(SIZE):
        ImageDraw.Draw(grad).line([(0, y), (SIZE, y)], fill=(*lerp(top, bottom, y / SIZE), 255))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((16, 16, SIZE - 16, SIZE - 16), radius=112, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    # play triangle (the stream), upper half
    d.polygon([(196, 92), (196, 268), (340, 180)], fill=white)
    # LAN: a line from the stream down to a bus with three screens
    d.rounded_rectangle((250, 280, 266, 330), radius=8, fill=white)
    d.rounded_rectangle((104, 322, 408, 338), radius=8, fill=white)
    for x in (104, 220, 336):
        d.rounded_rectangle((x + 30, 330, x + 42, 360), radius=6, fill=white)
        d.rounded_rectangle((x, 356, x + 72, 412), radius=12, fill=white)
    return img


if __name__ == "__main__":
    image = draw()
    image.save(sys.argv[1], sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    if len(sys.argv) > 2:
        image.resize((256, 256), Image.LANCZOS).save(sys.argv[2])
