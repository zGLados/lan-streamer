"""A local HTTP proxy that fetches a media file in pieces (YouTube's servers).

YouTube throttles a request for a long byte range to about 1.7 times the
media's bitrate, while a request for at most 10 MB comes at full speed
(measured 2026-11-12: 86 KB/s against 6.7 MB/s) - yt-dlp downloads in 10 MB
pieces for this reason. GStreamer's HTTP source asks for the whole rest of
the file at once, so a YouTube video buffered again and again and started
slowly. The output worker hands such URLs to this proxy instead: GStreamer
reads from 127.0.0.1 as before (ranges and seeks included), and the proxy
asks the server for one piece after another.

Runs in its own threads; the GStreamer main loop never waits for it.
"""

from __future__ import annotations

import http.server
import re
import secrets
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

PIECE = 10 * 1024 * 1024  # bytes per request to the server
BLOCK = 64 * 1024  # bytes per write to GStreamer
TIMEOUT_S = 15.0
TRIES = 3  # per piece; then the connection is closed (GStreamer's source retries)
HOSTS = ("googlevideo.com",)  # servers that throttle long requests (YouTube)

_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


def wanted(uri: str) -> bool:
    """Whether a source URI should go through the proxy."""
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and any(host == h or host.endswith("." + h) for h in HOSTS)


class Proxy:
    """One per output worker, started on first use; serves the URIs added to it."""

    def __init__(self, log: Callable[[str], None] = lambda _text: None, piece: int = PIECE):
        self.log = log
        self.piece = piece
        self._uris: dict[str, str] = {}
        self._lock = threading.Lock()
        self._server: http.server.ThreadingHTTPServer | None = None

    def url(self, uri: str) -> str:
        """The local address for `uri`. It stays valid while the worker runs."""
        with self._lock:
            if self._server is None:
                self._server = _Server(("127.0.0.1", 0), _Handler, self)
                threading.Thread(target=self._server.serve_forever, name="chunked proxy", daemon=True).start()
            token = secrets.token_urlsafe(12)
            self._uris[token] = uri
            port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/{token}"

    def forget(self, url: str) -> None:
        """The source that used `url` is gone."""
        with self._lock:
            self._uris.pop(url.rsplit("/", 1)[-1], None)

    def lookup(self, token: str) -> str | None:
        with self._lock:
            return self._uris.get(token)

    def close(self) -> None:
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()


class _ClientGone(Exception):
    pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, proxy: Proxy):
        super().__init__(address, handler)
        self.proxy = proxy


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"
    position = 0  # the next byte to send

    def log_message(self, *_args) -> None:  # not on stderr: the worker's log has its own lines
        pass

    def do_HEAD(self) -> None:
        self._serve(body=False)

    def do_GET(self) -> None:
        self._serve(body=True)

    def _serve(self, body: bool) -> None:
        proxy = self.server.proxy
        uri = proxy.lookup(self.path.lstrip("/"))
        if uri is None:
            self.send_error(404)
            return
        start, end = 0, None  # end: inclusive, None = to the end of the file
        asked = _RANGE.match(self.headers.get("Range", "").strip())
        if asked and asked.group(1):
            start = int(asked.group(1))
            end = int(asked.group(2)) if asked.group(2) else None
        try:
            first = _fetch(uri, start, _piece_end(start, end, proxy.piece))
        except OSError as exc:  # URLError too
            proxy.log(f"proxy: {_host(uri)} did not answer: {exc}")
            self.send_error(502)
            return
        with first:
            if first.status == 416:
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            served = _CONTENT_RANGE.match(first.headers.get("Content-Range", ""))
            if first.status != 206 or served is None or served.group(3) == "*":
                self._pass_through(first, body)  # no ranges: as it comes
                return
            total = int(served.group(3))
            last = total - 1 if end is None else min(end, total - 1)
            if asked and asked.group(1):
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{last}/{total}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", first.headers.get("Content-Type", "application/octet-stream"))
            self.send_header("Content-Length", str(last - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if not body:
                return
            self.position = start
            try:
                self._copy(first)
            except _ClientGone:
                return
            except OSError:
                pass  # the server broke off: on from where it got to
        failures = 0
        while self.position <= last:
            before, error = self.position, None
            try:
                with _fetch(uri, self.position, _piece_end(self.position, last, proxy.piece)) as reply:
                    if reply.status != 206:
                        raise OSError(f"HTTP {reply.status} for a range")
                    self._copy(reply)
            except _ClientGone:
                return
            except OSError as exc:  # URLError too
                error = exc
            failures = 0 if self.position > before else failures + 1
            if failures >= TRIES:
                proxy.log(f"proxy: {_host(uri)} failed at byte {self.position}: {error or 'no data'}")
                self.close_connection = True
                return

    def _copy(self, reply) -> None:
        """Copies a reply to GStreamer, counting self.position on."""
        while True:
            data = reply.read(BLOCK)
            if not data:
                return
            try:
                self.wfile.write(data)
            except OSError as exc:  # GStreamer closed it: a seek, or the source stopped
                raise _ClientGone from exc
            self.position += len(data)

    def _pass_through(self, reply, body: bool) -> None:
        self.send_response(reply.status)
        for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if reply.headers.get(name):
                self.send_header(name, reply.headers[name])
        if not reply.headers.get("Content-Length"):
            self.close_connection = True  # the end of the body is the end of the connection
        self.end_headers()
        if body:
            self.position = 0
            try:
                self._copy(reply)
            except (_ClientGone, OSError):
                pass


def _piece_end(start: int, end: int | None, piece: int) -> int:
    last = start + piece - 1
    return last if end is None else min(last, end)


def _fetch(uri: str, start: int, end: int):
    request = urllib.request.Request(uri, headers={"Range": f"bytes={start}-{end}"})
    try:
        return urllib.request.urlopen(request, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:
            return exc  # an HTTPError is a response too
        raise


def _host(uri: str) -> str:
    return urlsplit(uri).hostname or "?"
