"""Example input plugin for lan-streamer (plugin API 1): an external live stream.

The usual shape of a plugin for a camera, a streaming service or another
server: it checks every few seconds whether the stream is online and is active
while it is. What it shows:

  Settings with validation  – a URL check, limits, an optional field
  polling is_active()       – the default watch_active() calls it every poll_interval_s
  blocking work off the loop – network checks in asyncio.to_thread()
  live sources              – a live stream never "ends"; can_release() lets
                              inputs of the same priority take turns after min_on_air_s
  metadata                  – title and channel from a status URL, updated while on air
  ctx.set_info()            – what the last check found, for the admin page

Only the standard library is used; a plugin may also list packages in
plugin.toml (`requirements`). Guide: docs/plugin-guide.md
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
import urllib.request
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from lanstreamer_sdk import InputPlugin, Metadata, PlaybackEvent, UriSource

SCHEMES = ("http", "https", "rtsp", "rtsps", "srt")
TIMEOUT_S = 5.0


class Settings(BaseModel):
    url: str = Field(description="The stream: HLS (.m3u8), MP4, RTSP or SRT address")
    status_url: str = Field(
        "", description='Optional: a JSON address with "online", "title" and "channel"'
    )
    check_s: float = Field(10, ge=2, le=300, description="Check every … seconds")
    min_on_air_s: float = Field(
        60, ge=0, le=3600, description="On equal priority: stay on air at least this long"
    )

    @field_validator("url")
    @classmethod
    def known_scheme(cls, value: str) -> str:
        if urlparse(value).scheme.lower() not in SCHEMES:
            raise ValueError("must start with http(s)://, rtsp(s):// or srt://")
        return value


def reachable(url: str) -> bool:
    """Blocking: does the stream answer? HTTP: a successful request; others: the port is open."""
    parts = urlparse(url)
    if parts.scheme in ("http", "https"):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
                return 200 <= response.status < 300
        except OSError:
            return False
    port = parts.port or {"rtsp": 554, "rtsps": 322}.get(parts.scheme, 0)
    if parts.scheme == "srt":  # UDP: nothing to ask without a full handshake – use status_url
        return True
    try:
        with socket.create_connection((parts.hostname or "", port), timeout=TIMEOUT_S):
            return True
    except OSError:
        return False


def read_status(url: str) -> dict:
    """Blocking: the JSON status, e.g. {"online": true, "title": "Match 3", "channel": "Hall A"}."""
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


class Plugin(InputPlugin[Settings]):
    Settings = Settings

    async def start(self) -> None:
        self.poll_interval_s = self.settings.check_s  # used by the default watch_active()
        self.on_air_since: float | None = None
        self.status: dict = {}

    async def is_active(self) -> bool:
        if self.settings.status_url:
            try:
                self.status = await asyncio.to_thread(read_status, self.settings.status_url)
            except (OSError, ValueError) as exc:
                self.ctx.set_info(f"status URL: {exc}")
                return False
            online = bool(self.status.get("online"))
            if online and self.on_air_since is not None:
                self._publish_metadata()  # a new title while on air reaches the overlay
        else:
            online = await asyncio.to_thread(reachable, self.settings.url)
        self.ctx.set_info(f"{'online' if online else 'offline'} · checked {time.strftime('%H:%M:%S')}")
        return online

    async def get_source(self) -> UriSource:
        self._publish_metadata()
        return UriSource(self.settings.url)

    async def on_playback(self, event: PlaybackEvent) -> None:
        if event.kind == "started":
            self.on_air_since = time.monotonic()
        else:  # a live stream "ends" when the sender stops; lan-streamer retries after "failed"
            self.on_air_since = None
            if event.kind == "failed":
                self.ctx.log.warning("stream failed: %s", event.detail)

    async def can_release(self) -> bool:
        if self.on_air_since is None:
            return True
        return time.monotonic() - self.on_air_since >= self.settings.min_on_air_s

    def _publish_metadata(self) -> None:
        self.ctx.set_metadata(Metadata(
            title=str(self.status.get("title") or ""), channel=str(self.status.get("channel") or ""),
        ))  # fmt: skip
