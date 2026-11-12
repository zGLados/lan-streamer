"""The piece proxy for YouTube downloads (media/chunked.py), against a local server."""

import http.server
import re
import threading
import urllib.request

import pytest

from lanstreamer.media import chunked

DATA = bytes(range(256)) * 20  # 5120 bytes


class Upstream(http.server.ThreadingHTTPServer):
    """Serves DATA with ranges; records them. ranges=False: ignores Range (200).
    cut: the first reply stops after this many bytes (a dropped connection)."""

    daemon_threads = True

    def __init__(self, ranges=True, cut=0):
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)
        self.ranges, self.cut, self.asked = ranges, cut, []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def uri(self):
        return f"http://127.0.0.1:{self.server_address[1]}/video"


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        server = self.server
        asked = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        server.asked.append(self.headers.get("Range", ""))
        if asked and server.ranges:
            start = int(asked.group(1))
            end = min(int(asked.group(2)) if asked.group(2) else len(DATA) - 1, len(DATA) - 1)
            if start >= len(DATA):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = DATA[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        else:
            body = DATA
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if server.cut:
            body, server.cut = body[: server.cut], 0
            self.wfile.write(body)
            self.close_connection = True
            return
        self.wfile.write(body)


@pytest.fixture
def proxy():
    p = chunked.Proxy(piece=1000)
    yield p
    p.close()


def get(url, range_=None):
    request = urllib.request.Request(url, headers={"Range": range_} if range_ else {})
    with urllib.request.urlopen(request, timeout=10) as reply:
        return reply.status, dict(reply.headers), reply.read()


def test_the_whole_file_comes_in_pieces(proxy):
    upstream = Upstream()
    status, headers, body = get(proxy.url(upstream.uri))
    assert status == 200 and body == DATA and headers["Content-Length"] == str(len(DATA))
    assert upstream.asked == [f"bytes={n}-{min(n + 999, len(DATA) - 1)}" for n in range(0, len(DATA), 1000)]


def test_an_open_range_from_a_seek(proxy):
    upstream = Upstream()
    status, headers, body = get(proxy.url(upstream.uri), "bytes=1234-")
    assert status == 206 and body == DATA[1234:]
    assert headers["Content-Range"] == f"bytes 1234-{len(DATA) - 1}/{len(DATA)}"
    assert upstream.asked[0] == "bytes=1234-2233"  # never the whole rest at once


def test_a_closed_range(proxy):
    upstream = Upstream()
    status, headers, body = get(proxy.url(upstream.uri), "bytes=100-2599")
    assert headers["Content-Range"] == f"bytes 100-2599/{len(DATA)}"
    assert status == 206 and body == DATA[100:2600]
    assert upstream.asked == ["bytes=100-1099", "bytes=1100-2099", "bytes=2100-2599"]


def test_a_server_without_ranges_is_passed_through(proxy):
    upstream = Upstream(ranges=False)
    status, _headers, body = get(proxy.url(upstream.uri))
    assert status == 200 and body == DATA


def test_a_dropped_connection_goes_on_where_it_broke_off(proxy):
    upstream = Upstream(cut=300)
    status, _headers, body = get(proxy.url(upstream.uri))
    assert status == 200 and body == DATA
    assert upstream.asked[:2] == ["bytes=0-999", "bytes=300-1299"]


def test_past_the_end(proxy):
    upstream = Upstream()
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(proxy.url(upstream.uri), f"bytes={len(DATA)}-")
    assert exc.value.code == 416


def test_unknown_and_forgotten_addresses(proxy):
    upstream = Upstream()
    url = proxy.url(upstream.uri)
    proxy.forget(url)
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(url)
    assert exc.value.code == 404


@pytest.mark.parametrize(("uri", "expected"), [
    ("https://rr3---sn-4g5edndd.googlevideo.com/videoplayback?expire=1", True),
    ("https://googlevideo.com/x", True),
    ("https://example.com/googlevideo.com", False),
    ("https://notgooglevideo.com/x", False),
    ("https://usher.ttvnw.net/api/channel/hls/x.m3u8", False),
    ("file:///C:/videos/a.mp4", False),
])  # fmt: skip
def test_which_downloads_go_through_it(uri, expected):
    assert chunked.wanted(uri) is expected
