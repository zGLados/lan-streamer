import random

from lanstreamer.media import bounce
from lanstreamer_sdk import BounceSource, source_to_dict


def test_the_picture_gets_a_share_of_the_screen_width_and_keeps_its_shape():
    assert bounce.fit((400, 200), (1920, 1080), 20) == (384, 192)
    assert bounce.fit((401, 199), (1280, 720), 25) == (320, 158)  # even numbers
    assert bounce.fit((100, 400), (1920, 1080), 50) == (216, 864)  # never higher than 80 %


def test_it_moves_diagonally_and_turns_around_at_the_edges():
    path = bounce.Bounce((100, 50), (500, 250), speed=100 * 2**0.5, rng=random.Random(1))
    path.start_x = path.start_y = 0  # top left corner, going right and down
    assert path.at(0) == (0, 0, 0)
    assert path.at(1) == (100, 100, 0)
    assert path.at(2) == (200, 200, 1)  # reached the bottom edge (200) right now
    assert path.at(3) == (300, 100, 1)  # going up again
    assert path.at(4) == (400, 0, 3)  # a corner: the right edge (400) and the top at once
    assert path.at(5) == (300, 100, 3)
    for t in range(200):
        x, y, _ = path.at(t / 7)
        assert 0 <= x <= 400 and 0 <= y <= 200


def test_a_picture_as_large_as_the_screen_stays_put():
    path = bounce.Bounce((500, 250), (500, 250), speed=300)
    assert path.at(0) == path.at(10) == (0, 0, 0)


def test_every_new_color_is_bright_and_another_hue():
    rng = random.Random(7)
    color = None
    for _ in range(100):
        new = bounce.next_color(color, rng)
        assert max(new) == 255  # full brightness
        assert new != color
        color = new


def test_tinting_multiplies_the_pixels_and_keeps_the_transparency():
    pixels = bytes([255, 255, 255, 255, 0, 0, 0, 128, 128, 255, 0, 0])  # white, black, half red
    assert bounce.tint(pixels, (255, 0, 100)) == bytes([255, 0, 100, 255, 0, 0, 0, 128, 128, 0, 0, 0])


def test_bounce_source_goes_to_the_worker_with_its_settings(tmp_path):
    logo = tmp_path / "logo.png"
    assert source_to_dict(BounceSource(str(logo))) == {
        "kind": "bounce", "path": str(logo.resolve()), "size": 20.0, "speed": 240.0, "change_color": True,
    }  # fmt: skip
