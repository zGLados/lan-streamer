"""Built-in input: short videos that viewers submit on the player page.

Any link yt-dlp can read (TikTok, YouTube Shorts, Instagram Reels, plain video
URLs, ...). A submitted video is checked (length, per-device limit, already
queued), downloaded into the instance's data folder and played when this input
goes on air; afterwards the file is deleted. Active while a video is ready.

When the input goes on air, an "Up next" card shows first; once it is on screen
the first video starts after a moment, cut in without crossfade so nothing of
its start is lost. Further videos in the queue follow directly.

On Windows the output may still hold a file open for a moment after it ended
or failed; such a file is deleted a little later (or on the next start).

yt-dlp updates itself on start (see lanstreamer.plugins.ytdlp); submissions
never wait for that download.
"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from lanstreamer.plugins.ytdlp import FAKE, YtDlp
from lanstreamer_sdk import (
    CardSource,
    FileSource,
    InputPlugin,
    Metadata,
    PlaybackEvent,
    Rejected,
    Viewer,
    ViewerItem,
)


class Settings(BaseModel):
    max_length_s: int = Field(180, title="Max. video length (s)", ge=5, le=3600, description="Longer videos are refused")
    per_device: int = Field(3, title="Videos per device", ge=1, le=50, description="Videos one device may have waiting at once")
    max_queue: int = Field(50, title="Max. queue length", ge=1, le=500, description="Videos waiting at most")
    auto_update: bool = Field(True, title="Update yt-dlp", description="Update yt-dlp on start (needs internet)")
    card_s: float = Field(2.0, title="\"Up next\" card (s)", ge=0, le=10, description="How long the \"Up next\" card shows "
                          "before the first video (0: no card)")  # fmt: skip


class SubmitForm(BaseModel):
    link: str = Field(title="Link", description="TikTok, YouTube Shorts, Instagram Reels, …",
                      min_length=8, max_length=500)  # fmt: skip
    name: str = Field("", title="Your name (optional)", max_length=32)

    @field_validator("link")
    @classmethod
    def http_link(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("must be a link (https://…)")
        return value

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return " ".join(value.split())


class Plugin(InputPlugin[Settings]):
    Settings = Settings
    ViewerForm = SubmitForm
    viewer_title = "Submit a video"
    poll_interval_s = 1.0
    delete_retry_s = 2.0

    async def start(self) -> None:
        self.videos = self.ctx.data_dir / "videos"
        self.videos.mkdir(parents=True, exist_ok=True)
        self.ytdlp = YtDlp(self.ctx.data_dir / "yt-dlp", self.settings.auto_update, self.ctx.log)
        self.queue: list[dict[str, Any]] = []
        self.playing: str | None = None  # the video on air
        self.card: str | None = None  # the video its "Up next" card announces
        self.follow = False  # we asked for the next source: no card before it
        self.released = True
        self.tasks: set[asyncio.Task] = set()
        self.trash: set[Path] = set()  # files that could not be deleted yet
        for entry in self.ctx.store.get("queue", []):
            if entry["state"] == "ready" and Path(entry["file"]).is_file():
                self.queue.append(entry)
            elif entry["state"] == "loading":
                self.queue.append(entry)
                self._spawn(self._download(entry))
        known = {e.get(key) for e in self.queue for key in ("file", "audio")}
        for file in self.videos.iterdir():
            if str(file) not in known:
                self._delete(file)  # left over from a crash or still locked last time
        self._save()
        self._spawn(self._announce_version())

    async def _announce_version(self) -> None:
        if FAKE:
            self.ytdlp.version = "fake"
        else:
            try:
                await self.ytdlp.module()  # after the update on start
            except ImportError:
                self.ctx.log.error("yt-dlp is not installed")
        self._save()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # --- viewers --------------------------------------------------------------------

    async def on_viewer_submit(self, form: SubmitForm, viewer: Viewer) -> str:
        who = viewer.device or viewer.ip
        if sum(1 for e in self.queue if e["who"] == who) >= self.settings.per_device:
            raise Rejected(f"You already have {self.settings.per_device} videos waiting – "
                           "wait until one has played.")  # fmt: skip
        if len(self.queue) >= self.settings.max_queue:
            raise Rejected("The queue is full – try again later.")
        if any(e["link"] == form.link for e in self.queue):
            raise Rejected("This video is already in the queue.")
        try:
            info = await self.ytdlp.probe(form.link)
        except Exception as exc:
            self.ctx.log.info("probe failed for %s: %s", form.link, exc)
            raise Rejected("This link cannot be played (not a video yt-dlp can read).") from exc
        duration = info.get("duration")
        if duration and duration > self.settings.max_length_s:
            raise Rejected(f"Too long: {int(duration)} s (at most {self.settings.max_length_s} s).")
        entry = {
            "id": secrets.token_hex(4), "link": form.link, "name": form.name, "who": who,
            "title": str(info.get("title") or form.link)[:120],
            "channel": str(info.get("uploader") or info.get("channel") or ""),
            "state": "loading", "file": "",
        }  # fmt: skip
        self.queue.append(entry)
        self._save()
        self._spawn(self._download(entry))
        return f"Added – position {len(self.queue)} in the queue."

    async def viewer_items(self) -> list[ViewerItem]:
        items = []
        for entry in self.queue:
            parts = [f"by {entry['name']}"] if entry["name"] else []
            if entry["id"] in (self.playing, self.card):
                parts.append("on air")
            elif entry["state"] == "loading":
                parts.append("loading")
            items.append(ViewerItem(entry["id"], entry["title"], " · ".join(parts)))
        return items

    async def remove_item(self, item_id: str) -> None:
        if item_id == self.playing:
            self._finished()
        else:
            self._drop(item_id)

    # --- playing --------------------------------------------------------------------

    async def is_active(self) -> bool:
        return any(e["state"] == "ready" for e in self.queue)

    async def get_source(self):
        entry = next((e for e in self.queue if e["state"] == "ready"), None)
        if entry is None:  # removed after the core saw this input active
            raise RuntimeError("no video is ready")
        follow, self.follow, self.released = self.follow, False, False
        self.ctx.set_metadata(Metadata(title=entry["title"], channel=entry["channel"], submitter=entry["name"]))
        self.ctx.changed()
        if not follow and self.settings.card_s > 0:  # just switched to this input
            self.card, self.playing = entry["id"], None
            return CardSource(entry["title"], heading="Up next",
                              subtitle=f"submitted by {entry['name']}" if entry["name"] else "")  # fmt: skip
        self.card, self.playing = None, entry["id"]
        return FileSource(entry["file"], cut=self.settings.card_s > 0, audio_path=entry.get("audio", ""))

    async def can_release(self) -> bool:
        return self.released

    async def on_playback(self, event: PlaybackEvent) -> None:
        if event.kind == "started" and self.card:
            self._spawn(self._after_card(self.card))
        elif event.kind == "failed" and self.card:
            self.card, self.released = None, True
        elif event.kind in ("ended", "failed") and self.playing:
            self._finished()

    async def _after_card(self, card: str) -> None:
        await asyncio.sleep(self.settings.card_s)
        if self.card != card:  # off air meanwhile
            return
        self.card = None
        if any(e["state"] == "ready" for e in self.queue):
            self.follow = True  # stays unreleased: the video follows, not another input
            self.ctx.next_source()
        else:  # removed while its card was shown
            self.released = True

    def _finished(self) -> None:
        self._drop(self.playing)
        self.playing, self.released = None, True
        if any(e["state"] == "ready" for e in self.queue):
            self.follow = True
            self.ctx.next_source()

    # --- queue ----------------------------------------------------------------------

    async def _download(self, entry: dict[str, Any]) -> None:
        try:
            file, audio = await self.ytdlp.download(entry["link"], self.videos, entry["id"])
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.warning("download failed for %s: %s", entry["link"], exc)
            self._drop(entry["id"])
            return
        if entry in self.queue:
            entry["state"], entry["file"], entry["audio"] = "ready", str(file), str(audio) if audio else ""
            self._save()
        else:  # removed while it was loading
            self._delete(file)
            if audio:
                self._delete(audio)

    def _drop(self, item_id: str | None) -> None:
        for entry in [e for e in self.queue if e["id"] == item_id]:
            self.queue.remove(entry)
            for name in (entry["file"], entry.get("audio")):
                if name:
                    self._delete(Path(name))
        self._save()

    def _delete(self, file: Path) -> None:
        """Deletes a video; a file still in use is retried until it is gone."""
        try:
            file.unlink(missing_ok=True)
        except OSError:
            if not self.trash:
                self._spawn(self._empty_trash())
            self.trash.add(file)

    async def _empty_trash(self) -> None:
        while self.trash:
            await asyncio.sleep(self.delete_retry_s)
            for file in list(self.trash):
                try:
                    file.unlink(missing_ok=True)
                except OSError:
                    continue
                self.trash.discard(file)

    def _save(self) -> None:
        self.ctx.store.set("queue", self.queue)
        waiting = sum(1 for e in self.queue if e["id"] != self.playing)
        self.ctx.set_info(f"{waiting} waiting · yt-dlp {self.ytdlp.version}")
        self.ctx.changed()
