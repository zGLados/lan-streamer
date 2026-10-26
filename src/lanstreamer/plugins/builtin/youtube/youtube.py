"""Built-in input: YouTube – a live stream, or videos one after another.

The link decides what it does:
- a channel's live page (https://www.youtube.com/@name/live) or a live video:
  active while it is live, checked every `live_check_s`; on equal priority it
  holds the output while live.
- a playlist, a channel's videos or a single video: always active, plays the
  videos in order (or shuffled) and starts over at the end; another input of
  equal priority may take over after each video. The list is read again
  every 30 minutes.

Picture and sound come as separate streams (up to `max_height`), so HD works
without ffmpeg; the URLs are resolved right before playing because they expire.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from pydantic import BaseModel, Field, field_validator

from lanstreamer.plugins.ytdlp import YtDlp
from lanstreamer_sdk import InputPlugin, Metadata, PlaybackEvent, UriSource

LIST_REFRESH_S = 1800
TRIES = 3  # videos tried in a row before get_source gives up (removed, private, ...)


class Settings(BaseModel):
    url: str = Field(..., title="Link", description="A video, playlist or channel link, or a channel's /live page")
    max_height: int = Field(1080, title="Max. height (pixels)", ge=360, le=1080, description="Highest resolution to fetch")
    shuffle: bool = Field(False, title="Shuffle", description="Play the videos in random order")
    live_check_s: int = Field(60, title="Live check every (s)", ge=15, le=3600, description="How often a live page is checked")
    auto_update: bool = Field(True, title="Update yt-dlp", description="Update yt-dlp on start (needs internet)")

    @field_validator("url")
    @classmethod
    def youtube_link(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("https://", "http://")):
            raise ValueError("must be a link (https://…)")
        return value


class Plugin(InputPlugin[Settings]):
    Settings = Settings
    poll_interval_s = 1.0

    async def start(self) -> None:
        self.ytdlp = YtDlp(self.ctx.data_dir / "yt-dlp", self.settings.auto_update, self.ctx.log)
        self.live_page = self.settings.url.rstrip("/").endswith("/live")
        self.live_now = False
        self.items: list[dict[str, str]] = []
        self.index = 0
        self.released = True
        self.problem = ""
        self.tasks: set[asyncio.Task] = set()
        self._spawn(self._watch())

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # --- what the link is -----------------------------------------------------------

    async def _watch(self) -> None:
        while True:
            if self.live_page:
                await self._check_live()
                await asyncio.sleep(self.settings.live_check_s)
            else:
                await self._read_list()
                await asyncio.sleep(self.settings.live_check_s if not self.items else LIST_REFRESH_S)

    async def _check_live(self) -> None:
        try:
            stream = await self.ytdlp.stream(self.settings.url, self.settings.max_height)
            self.live_now, self.problem = stream.live, ""
            title = stream.title if stream.live else ""
        except Exception as exc:  # noqa: BLE001 – offline channels raise
            self.live_now, self.problem, title = False, "", ""
            self.ctx.log.info("not live: %s", str(exc).splitlines()[0][:200])
        self._report(title)

    async def _read_list(self) -> None:
        try:
            items = await self.ytdlp.entries(self.settings.url)
        except Exception as exc:  # noqa: BLE001
            self.problem = str(exc).splitlines()[0][:200]
            self.ctx.log.warning("cannot read %s: %s", self.settings.url, self.problem)
            self._report()
            return
        if len(items) == 1 and not self.items:  # a single video – or a live one
            try:
                stream = await self.ytdlp.stream(items[0]["url"], self.settings.max_height)
            except Exception:  # noqa: BLE001 – played (and reported) when it is its turn
                stream = None
            if stream is not None and stream.live:
                self.live_page, self.live_now = True, True
                self._report(stream.title)
                return
        if self.settings.shuffle:
            random.shuffle(items)
        self.items, self.problem = items, ""
        self.index %= max(1, len(items))
        self._report()

    def _report(self, title: str = "") -> None:
        if self.problem:
            text = f"problem: {self.problem}"
        elif self.live_page:
            text = f"live: {title}" if self.live_now else f"not live – checked every {self.settings.live_check_s} s"
        elif self.items:
            upcoming = self.items[self.index % len(self.items)]["title"]
            text = f"{len(self.items)} video{'s' if len(self.items) != 1 else ''} · next: {upcoming}"
        else:
            text = "reading the list …"
        self.ctx.set_info(f"{text} · yt-dlp {self.ytdlp.version}")

    # --- playing --------------------------------------------------------------------

    async def is_active(self) -> bool:
        return self.live_now if self.live_page else bool(self.items)

    async def get_source(self):
        if self.live_page:
            stream = await self.ytdlp.stream(self.settings.url, self.settings.max_height)
            return self._play(stream)
        error: Exception | None = None
        for _ in range(min(TRIES, len(self.items))):
            item = self.items[self.index % len(self.items)]
            try:
                return self._play(await self.ytdlp.stream(item["url"], self.settings.max_height))
            except Exception as exc:  # noqa: BLE001 – skip videos that cannot be played
                self.ctx.log.warning("skipping %s: %s", item["url"], str(exc).splitlines()[0][:200])
                self.index += 1
                error = exc
        raise RuntimeError(f"no playable video: {error}")

    def _play(self, stream: Any) -> UriSource:
        self.released = False
        self.ctx.set_metadata(Metadata(title=stream.title, channel=stream.channel))
        return UriSource(stream.url, audio_uri=stream.audio_url)

    async def can_release(self) -> bool:
        return not self.live_now if self.live_page else self.released

    async def on_playback(self, event: PlaybackEvent) -> None:
        if event.kind not in ("ended", "failed"):
            return
        self.released = True
        if self.live_page:  # the stream ended or broke: is it still live?
            self._spawn(self._check_live())
            return
        self.index += 1
        self._report()
        if event.kind == "ended" and self.items:
            self.ctx.next_source()
