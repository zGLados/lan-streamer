"""Built-in input: a stream sent to lan-streamer over RTMP or SRT (OBS, a camera, ...).

MediaMTX, which lan-streamer runs anyway, receives it on the path
"ingest/<stream key>". The input asks MediaMTX's API (on this PC only)
whether someone publishes there and is active while they do; on air it reads
the stream back over RTSP (TCP, on this PC).

The addresses to enter in OBS are shown on the admin page, e.g.
    RTMP  server rtmp://192.168.1.20:1935/ingest   stream key  k3x9q
    SRT   srt://192.168.1.20:8890?streamid=publish:ingest/k3x9q
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import string
import urllib.error
import urllib.parse
import urllib.request

from pydantic import BaseModel, Field

from lanstreamer_sdk import InputPlugin, UriSource

# MediaMTX's default ports (its shipped mediamtx.yml)
RTMP_PORT, SRT_PORT, RTSP_PORT = 1935, 8890, 8554


class Settings(BaseModel):
    key: str = Field("", pattern=r"^[A-Za-z0-9_-]*$", max_length=64,
                     description="Stream key (letters, digits, - and _); empty = one is made up")  # fmt: skip
    api: str = Field("http://127.0.0.1:9997", description="MediaMTX API (change only for a special setup)")


def lan_address() -> str:
    """This PC's address in the LAN (the one with the default route)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # no packet is sent
            return probe.getsockname()[0]
    except OSError:
        return "<this PC>"


class Plugin(InputPlugin[Settings]):
    Settings = Settings
    poll_interval_s = 2.0

    async def start(self) -> None:
        key = self.settings.key or self.ctx.store.get("key")
        if not key:
            key = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
            self.ctx.store.set("key", key)
        self.path = f"ingest/{key}"
        self.tracks: list[str] = []
        self.api_error = ""
        host = lan_address()
        self.addresses = (f"RTMP rtmp://{host}:{RTMP_PORT}/ingest  key {key}"
                          f" · SRT srt://{host}:{SRT_PORT}?streamid=publish:{self.path}")  # fmt: skip
        self._report()

    def _report(self) -> None:
        if self.api_error:
            state = f"MediaMTX not reachable ({self.api_error})"
        elif self.tracks:
            state = "receiving " + ", ".join(self.tracks)
        else:
            state = "waiting for a stream"
        self.ctx.set_info(f"{state} · {self.addresses}")

    def _ask_mediamtx(self) -> list[str]:
        """The tracks published on our path; [] while nobody publishes."""
        url = f"{self.settings.api.rstrip('/')}/v3/paths/get/{urllib.parse.quote(self.path, safe='/')}"
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                path = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # the path exists only while someone publishes
                return []
            raise
        if not (path.get("ready") or path.get("available")):  # "available" in newer versions
            return []
        return [str(t) for t in path.get("tracks") or []] or ["a stream"]

    async def is_active(self) -> bool:
        try:
            tracks = await asyncio.to_thread(self._ask_mediamtx)
            error = ""
        except (OSError, ValueError) as exc:
            tracks, error = [], str(getattr(exc, "reason", exc))
        if (tracks, error) != (self.tracks, self.api_error):
            if error and not self.api_error:
                self.ctx.log.warning("cannot ask MediaMTX: %s", error)
            self.tracks, self.api_error = tracks, error
            self._report()
        return bool(tracks)

    async def get_source(self):
        # rtspt: RTSP over TCP – no UDP ports, nothing for a firewall to block
        return UriSource(f"rtspt://127.0.0.1:{RTSP_PORT}/{self.path}")

    async def can_release(self) -> bool:
        return not self.tracks  # a live stream stays on air while it is sent
