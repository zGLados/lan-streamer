"""Minimal web server for the M1 prototype (FastAPI replaces it in M2).

    GET  /             player page
    GET  /api/status   JSON status of the output
    POST /api/next     switch to the next input now
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

PAGE = resources.files(__package__).joinpath("index.html")


def start(port: int, app: Any) -> ThreadingHTTPServer:
    """Serves the page in a background thread. `app` has .status and .next_source()."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE.read_bytes())
            elif self.path == "/api/status":
                data = dataclasses.asdict(app.status)
                data["uptime_s"] = round(time.time() - app.status.started)
                self._send(200, "application/json", json.dumps(data).encode())
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self) -> None:
            if self.path == "/api/next":
                app.next_source()
                self._send(204, "text/plain", b"")
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:  # keep the console quiet
            pass

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
