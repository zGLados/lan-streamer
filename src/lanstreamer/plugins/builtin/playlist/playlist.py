"""Built-in input: plays files, folders or URLs one after another.

A folder stands for the media files in it (sorted by name). Pictures are shown
for `image_duration_s`. After each item the playlist may hand over to another
input of the same priority; if none takes over, the next item follows at once.

YouTube video links are looked up with yt-dlp right before they play (their
stream URLs expire); one that cannot be played is skipped. Whole YouTube
playlists and channels are the YouTube input's job. yt-dlp is only loaded
(and updated) when the list has a YouTube link.
"""

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from lanstreamer.plugins.ytdlp import YtDlp
from lanstreamer_sdk import FileSource, ImageSource, InputPlugin, Metadata, PlaybackEvent, UriSource

VIDEO = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac"}
IMAGE = {".png", ".jpg", ".jpeg"}
YOUTUBE = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
TRIES = 5  # YouTube links tried in a row before get_source gives up (removed, private, ...)


class Settings(BaseModel):
    items: list[str] = Field(default_factory=list, title="Items",
                             description="Files, folders, URLs or YouTube video links – one per line")  # fmt: skip
    loop: bool = Field(True, title="Loop", description="Start again after the last item")
    image_duration_s: float = Field(10, title="Picture duration (s)", ge=1, le=3600, description="How long a picture is shown")
    auto_update: bool = Field(True, title="Update yt-dlp", description="For YouTube links: update yt-dlp on start "
                              "(needs internet)")  # fmt: skip


def expand(items: list[str]) -> list[str]:
    """Folders → their media files; files and URLs stay as they are."""
    entries: list[str] = []
    for item in items:
        path = Path(item)
        if "://" not in item and path.is_dir():
            entries += sorted(str(p) for p in path.iterdir() if p.suffix.lower() in VIDEO | IMAGE)
        else:
            entries.append(item)
    return entries


def is_image(entry: str) -> bool:
    return "://" not in entry and Path(entry).suffix.lower() in IMAGE


def is_youtube(entry: str) -> bool:
    url = urlparse(entry.strip())
    return url.scheme in ("http", "https") and (url.hostname or "").lower() in YOUTUBE


class Plugin(InputPlugin[Settings]):
    Settings = Settings

    async def start(self) -> None:
        self.entries = expand(self.settings.items)
        self.position = int(self.ctx.store.get("position", 0))
        self.item_ended = False
        self.timer: asyncio.Task | None = None
        self.ytdlp = None
        if any(is_youtube(e) for e in self.entries):
            self.ytdlp = YtDlp(self.ctx.data_dir / "yt-dlp", self.settings.auto_update, self.ctx.log)

    async def is_active(self) -> bool:
        if not self.entries:
            return False
        return self.settings.loop or self.position < len(self.entries)

    def current(self) -> str:
        return self.entries[self.position % len(self.entries)]

    async def get_source(self):
        for _ in range(TRIES):
            if not self.settings.loop and self.position >= len(self.entries):
                raise RuntimeError("the playlist has ended")
            entry = self.current()
            self.item_ended = False
            if is_youtube(entry):
                try:
                    stream = await self.ytdlp.stream(entry.strip())
                except Exception as exc:  # noqa: BLE001 – skip videos that cannot be played
                    self.ctx.log.warning("skipping %s: %s", entry, (str(exc).splitlines() or [repr(exc)])[0][:200])
                    self._skip()
                    continue
                self.ctx.set_metadata(Metadata(title=stream.title or entry, channel=stream.channel))
                return UriSource(stream.url, audio_uri=stream.audio_url)
            self.ctx.set_metadata(Metadata(title=Path(entry).name or entry))
            if is_image(entry):
                return ImageSource(entry)
            return UriSource(entry) if "://" in entry else FileSource(entry)
        raise RuntimeError(f"{TRIES} YouTube links in a row cannot be played")

    def _skip(self) -> None:
        self.position += 1
        self.ctx.store.set("position", self.position)

    async def can_release(self) -> bool:
        return self.item_ended

    async def on_playback(self, event: PlaybackEvent) -> None:
        if event.kind == "started" and is_image(self.current()):
            if self.timer:
                self.timer.cancel()
            self.timer = asyncio.create_task(self._show_image())
        elif event.kind in ("ended", "failed"):
            self._advance()

    async def _show_image(self) -> None:
        await asyncio.sleep(self.settings.image_duration_s)
        self._advance()

    def _advance(self) -> None:
        self.item_ended = True
        self.position += 1
        self.ctx.store.set("position", self.position)
        if self.settings.loop or self.position < len(self.entries):
            self.ctx.next_source()
