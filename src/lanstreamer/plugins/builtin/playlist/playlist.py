"""Built-in input: plays files, folders or URLs one after another.

A folder stands for the media files in it (sorted by name). Pictures are shown
for `image_duration_s`. After each item the playlist may hand over to another
input of the same priority; if none takes over, the next item follows at once.

YouTube video links are looked up with yt-dlp right before they play (their
stream URLs expire); one that cannot be played is skipped. Whole YouTube
playlists and channels are the YouTube input's job. yt-dlp is only loaded
(and updated) when the list has a YouTube link.

With "Viewers can add videos" the player page shows a form for single YouTube
videos. They play one after another after the current item; then the list
goes on where it was. The queue survives a restart; the admin can remove
entries. A link that cannot be played any more is skipped.
"""

import asyncio
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from lanstreamer.plugins.ytdlp import YtDlp
from lanstreamer_sdk import (
    FileSource,
    ImageSource,
    InputPlugin,
    Metadata,
    PlaybackEvent,
    Rejected,
    UriSource,
    Viewer,
    ViewerItem,
)

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
    viewers_add: bool = Field(False, title="Viewers can add videos", description="Viewers add YouTube videos on the "
                              "player page; they play after the current item")  # fmt: skip
    viewer_max_length_s: int = Field(900, title="Viewer videos: max. length (s)", ge=0, le=14400,
                                     description="Longer videos are refused (0: no limit)")  # fmt: skip
    viewer_per_device: int = Field(1, title="Viewer videos per device", ge=0, le=20,
                                   description="Videos one device may have waiting at once (0: no limit)")  # fmt: skip
    viewer_max_queue: int = Field(20, title="Viewer videos: max. queue length", ge=0, le=200,
                                  description="Videos waiting at most (0: no limit)")  # fmt: skip


class AddForm(BaseModel):
    link: str = Field(title="YouTube link", description="A single YouTube video", min_length=8, max_length=500)
    name: str = Field("", title="Your name (optional)", max_length=32)

    @field_validator("link")
    @classmethod
    def youtube_link(cls, value: str) -> str:
        value = value.strip()
        if not is_youtube(value):
            raise ValueError("must be a YouTube video link (https://www.youtube.com/… or https://youtu.be/…)")
        return value

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return " ".join(value.split())


def span(seconds: float) -> str:
    """A length for viewers: '45 s', '15 min', '1 h 30 min'."""
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


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
    ViewerForm = AddForm
    viewer_title = "Add a video"
    poll_interval_s = 1.0

    async def start(self) -> None:
        self.entries = expand(self.settings.items)
        self.position = int(self.ctx.store.get("position", 0))
        self.item_ended = False
        self.timer: asyncio.Task | None = None
        self.ytdlp = None
        self.queue: list[dict[str, Any]] = []  # videos viewers added, in order
        self.playing: str | None = None  # the viewer video on air
        if self.settings.viewers_add:
            self.queue = list(self.ctx.store.get("viewer_queue", []))
        else:
            self.ViewerForm = None  # no form on the player page (the host asks the instance)
            self.ctx.store.set("viewer_queue", [])
        if self.settings.viewers_add or any(is_youtube(e) for e in self.entries):
            self.ytdlp = YtDlp(self.ctx.data_dir / "yt-dlp", self.settings.auto_update, self.ctx.log)
        self._save()

    async def is_active(self) -> bool:
        if self.queue:
            return True
        if not self.entries:
            return False
        return self.settings.loop or self.position < len(self.entries)

    # --- viewers --------------------------------------------------------------------

    async def on_viewer_submit(self, form: AddForm, viewer: Viewer) -> str:
        who = viewer.device or viewer.ip
        per_device = self.settings.viewer_per_device
        if per_device and sum(1 for e in self.queue if e["who"] == who) >= per_device:
            raise Rejected(f"You already have {per_device} video{'s' if per_device > 1 else ''} waiting – "
                           "wait until it has played.")  # fmt: skip
        if self.settings.viewer_max_queue and len(self.queue) >= self.settings.viewer_max_queue:
            raise Rejected("The queue is full – try again later.")
        if any(e["link"] == form.link for e in self.queue):
            raise Rejected("This video is already in the queue.")
        try:
            info = await self.ytdlp.probe(form.link)
        except Exception as exc:
            self.ctx.log.info("probe failed for %s: %s", form.link, exc)
            raise Rejected("This link cannot be played (no YouTube video).") from exc
        if info.get("_type") in ("playlist", "multi_video"):
            raise Rejected("Only single videos, no playlists or channels.")
        if info.get("is_live") or info.get("live_status") == "is_upcoming":
            raise Rejected("Live streams cannot be added.")
        duration = info.get("duration")
        if duration and self.settings.viewer_max_length_s and duration > self.settings.viewer_max_length_s:
            raise Rejected(f"Too long: {span(duration)} (at most {span(self.settings.viewer_max_length_s)}).")
        self.queue.append({
            "id": secrets.token_hex(4), "link": form.link, "name": form.name, "who": who,
            "title": str(info.get("title") or form.link)[:120],
            "channel": str(info.get("uploader") or info.get("channel") or ""),
        })  # fmt: skip
        self._save()
        return f"Added – position {len(self.queue)} in the queue."

    async def viewer_items(self) -> list[ViewerItem]:
        items = []
        for entry in self.queue:
            parts = [f"by {entry['name']}"] if entry["name"] else []
            if entry["id"] == self.playing:
                parts.append("on air")
            items.append(ViewerItem(entry["id"], entry["title"], " · ".join(parts)))
        return items

    async def remove_item(self, item_id: str) -> None:
        if item_id == self.playing:
            self._advance()
        else:
            self._drop(item_id)

    def _drop(self, item_id: str | None) -> None:
        self.queue = [e for e in self.queue if e["id"] != item_id]
        self._save()

    def _save(self) -> None:
        self.ctx.store.set("viewer_queue", self.queue)
        if self.settings.viewers_add:
            waiting = sum(1 for e in self.queue if e["id"] != self.playing)
            self.ctx.set_info(f"{waiting} viewer video{'' if waiting == 1 else 's'} waiting")
        self.ctx.changed()

    # --- playing --------------------------------------------------------------------

    def current(self) -> str:
        return self.entries[self.position % len(self.entries)]

    async def get_source(self):
        while self.queue:  # viewer videos first, after the item that was on air
            entry = self.queue[0]
            self.item_ended = False
            try:
                stream = await self.ytdlp.stream(entry["link"])
            except Exception as exc:  # noqa: BLE001 – skip videos that cannot be played
                self.ctx.log.warning("skipping viewer video %s: %s", entry["link"],
                                     (str(exc).splitlines() or [repr(exc)])[0][:200])  # fmt: skip
                self._drop(entry["id"])
                continue
            self.playing = entry["id"]
            self._save()
            self.ctx.set_metadata(Metadata(title=stream.title or entry["title"], channel=stream.channel or entry["channel"],
                                           submitter=entry["name"]))  # fmt: skip
            return UriSource(stream.url, audio_uri=stream.audio_url)
        self.playing = None
        if not self.entries:  # the last viewer video was removed after the core saw this input active
            raise RuntimeError("the playlist is empty")
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
        if event.kind == "started" and not self.playing and self.entries and is_image(self.current()):
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
        if self.playing:  # a viewer video ended: the list goes on where it was
            ended, self.playing = self.playing, None
            self._drop(ended)
        else:
            self.position += 1
            self.ctx.store.set("position", self.position)
        if self.queue or (self.entries and (self.settings.loop or self.position < len(self.entries))):
            self.ctx.next_source()
