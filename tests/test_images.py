import struct
import zlib

from lanstreamer import images


def png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return images.PNG + struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xda\x00\x02"


def test_the_size_comes_from_the_png_or_jpeg_header():
    assert images.size(png(512, 512)) == (512, 512)
    assert images.size(jpeg(1920, 1080)) == (1920, 1080)
    assert (images.kind(png(1, 1)), images.kind(jpeg(1, 1))) == ("png", "jpeg")


def test_other_files_have_no_size():
    for data in (b"", b"GIF89a....", b"\xff\xd8\xff\xda\x00\x02", png(0, 10), images.PNG):
        assert images.size(data) is None
    assert images.kind(b"<svg>") is None


def test_auto_keeps_a_small_picture_centered_and_fits_a_large_one():
    screen = (1920, 1080)
    # 512x512: borders of 704 left and right, 284 top and bottom
    assert images.layout("auto", (512, 512), screen) == (512, 512, -704, -704, -284, -284)
    assert images.layout("auto", (511, 301), screen) == (511, 301, -705, -704, -390, -389)
    assert images.layout("auto", (1920, 1080), screen) is None  # exactly the screen
    assert images.layout("auto", (3840, 2160), screen) is None  # larger: fit, as before
    assert images.layout("auto", (1080, 1920), screen) is None  # too high: fit
    assert images.layout("auto", None, screen) is None  # size unknown


def test_fit_leaves_the_scaling_to_the_pipeline_and_fill_cuts_off_the_edges():
    screen = (1920, 1080)
    assert images.layout("fit", (512, 512), screen) is None
    assert images.layout("fill", (512, 512), screen) == (1920, 1920, 0, 0, 420, 420)
    assert images.layout("fill", (1920, 1200), screen) == (1920, 1200, 0, 0, 60, 60)
    assert images.layout("fill", (3840, 2160), screen) is None  # scales to exactly the screen


def test_file_size_reads_the_picture(tmp_path):
    (tmp_path / "a.png").write_bytes(png(640, 480))
    assert images.file_size(tmp_path / "a.png") == (640, 480)
    assert images.file_size(tmp_path / "missing.png") is None
