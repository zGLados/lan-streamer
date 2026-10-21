"""Built-in input: short videos that viewers submit on the player page.

Any link yt-dlp can read (TikTok, YouTube Shorts, Instagram Reels, plain video
URLs, ...). A submitted video is checked (length, per-device limit, already
queued), downloaded into the instance's data folder and played when this input
goes on air; afterwards the file is deleted. Active while a video is ready.

yt-dlp updates itself because the video sites change often: on start a newer
version is downloaded in the background (pip into the data folder) and used
from the next start on – a running process never swaps the library under its
feet, and submissions never wait for the download. Offline, the version that
came with lan-streamer keeps working.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from lanstreamer_sdk import (
    FileSource,
    InputPlugin,
    Metadata,
    PlaybackEvent,
    Rejected,
    Viewer,
    ViewerItem,
)

FAKE = os.environ.get("LANSTREAMER_FAKE_DOWNLOADS") == "1"  # tests: no network, no yt-dlp


class Settings(BaseModel):
    max_length_s: int = Field(180, ge=5, le=3600, description="Longer videos are refused")
    per_device: int = Field(3, ge=1, le=50, description="Videos one device may have waiting at once")
    max_queue: int = Field(50, ge=1, le=500, description="Videos waiting at most")
    auto_update: bool = Field(True, description="Update yt-dlp on start (needs internet)")


class SubmitForm(BaseModel):
    link: str = Field(title="Link", description="TikTok, YouTube Shorts, Instagram Reels, …",
                      min_length=8, max_length=500)  # fmt: skip
    name: str = Field(title="Your name", min_length=1, max_length=32)

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


class YtDlp:
    """yt-dlp in a worker thread.

    <folder>/current  the updated yt-dlp in use (if any), else the bundled one
    <folder>/next     a newer download, moved to current at the next start
    """

    def __init__(self, folder: Path, auto_update: bool, log):
        self.folder = folder
        self.log = log
        self.version = "?"
        current, upcoming = folder / "current", folder / "next"
        if (upcoming / "yt_dlp").is_dir():  # downloaded during the last run
            shutil.rmtree(current, ignore_errors=True)
            upcoming.rename(current)
        if (current / "yt_dlp").is_dir():
            sys.path.insert(0, str(current))
        self._update_task = asyncio.create_task(self._update()) if auto_update and not FAKE else None

    async def _update(self) -> None:
        target = self.folder / "next"
        shutil.rmtree(target, ignore_errors=True)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
            "--target", str(target), "yt-dlp",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 180)
        except TimeoutError:
            proc.kill()
            self.log.warning("yt-dlp update timed out – using the installed version")
            return
        if proc.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            self.log.warning("yt-dlp update failed (offline?) – using the installed version: %s",
                             out.decode(errors="replace").strip().splitlines()[-1:])  # fmt: skip
            return
        self.log.info("downloaded the latest yt-dlp – used from the next start")

    async def module(self):
        import yt_dlp
        import yt_dlp.version

        self.version = yt_dlp.version.__version__
        return yt_dlp

    async def probe(self, link: str) -> dict[str, Any]:
        if FAKE:
            if ".invalid" in link:  # like a real lookup of a link that is no video
                raise RuntimeError("no video")
            return {"title": f"Video {link.rsplit('/', 1)[-1]}", "duration": 10, "uploader": "fake"}
        yt_dlp = await self.module()

        def run() -> dict[str, Any]:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True,
                                   "noplaylist": True, "skip_download": True}) as ydl:  # fmt: skip
                return ydl.extract_info(link, download=False) or {}

        return await asyncio.wait_for(asyncio.to_thread(run), 60)

    async def download(self, link: str, folder: Path, name: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        if FAKE:
            target = folder / f"{name}.mp4"
            target.write_bytes(b"")
            return target
        yt_dlp = await self.module()
        options = {
            "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            # one file with picture and sound (no ffmpeg needed to merge), at most 1080p
            "format": "b[ext=mp4][height<=1080]/b[height<=1080]/b",
            "outtmpl": str(folder / f"{name}.%(ext)s"),
        }  # fmt: skip

        def run() -> Path:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(link, download=True) or {}
                downloads = info.get("requested_downloads") or [{}]
                return Path(downloads[0].get("filepath") or ydl.prepare_filename(info))

        return await asyncio.wait_for(asyncio.to_thread(run), 600)


class Plugin(InputPlugin[Settings]):
    Settings = Settings
    ViewerForm = SubmitForm
    viewer_title = "Submit a video"
    poll_interval_s = 1.0

    async def start(self) -> None:
        self.videos = self.ctx.data_dir / "videos"
        self.videos.mkdir(parents=True, exist_ok=True)
        self.ytdlp = YtDlp(self.ctx.data_dir / "yt-dlp", self.settings.auto_update, self.ctx.log)
        self.queue: list[dict[str, Any]] = []
        self.playing: str | None = None
        self.released = True
        self.tasks: set[asyncio.Task] = set()
        for entry in self.ctx.store.get("queue", []):
            if entry["state"] == "ready" and Path(entry["file"]).is_file():
                self.queue.append(entry)
            elif entry["state"] == "loading":
                self.queue.append(entry)
                self._spawn(self._download(entry))
        known = {e.get("file") for e in self.queue}
        for file in self.videos.iterdir():
            if str(file) not in known:
                file.unlink(missing_ok=True)  # left over from a crash
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
            detail = f"by {entry['name']}"
            if entry["id"] == self.playing:
                detail += " · on air"
            elif entry["state"] == "loading":
                detail += " · loading"
            items.append(ViewerItem(entry["id"], entry["title"], detail))
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
        entry = next(e for e in self.queue if e["state"] == "ready")
        self.playing, self.released = entry["id"], False
        self.ctx.set_metadata(Metadata(title=entry["title"], channel=entry["channel"], submitter=entry["name"]))
        self.ctx.changed()
        return FileSource(entry["file"])

    async def can_release(self) -> bool:
        return self.released

    async def on_playback(self, event: PlaybackEvent) -> None:
        if event.kind in ("ended", "failed") and self.playing:
            self._finished()

    def _finished(self) -> None:
        self._drop(self.playing)
        self.playing, self.released = None, True
        if any(e["state"] == "ready" for e in self.queue):
            self.ctx.next_source()

    # --- queue ----------------------------------------------------------------------

    async def _download(self, entry: dict[str, Any]) -> None:
        try:
            file = await self.ytdlp.download(entry["link"], self.videos, entry["id"])
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.warning("download failed for %s: %s", entry["link"], exc)
            self._drop(entry["id"])
            return
        if entry in self.queue:
            entry["state"], entry["file"] = "ready", str(file)
            self._save()
        else:
            file.unlink(missing_ok=True)  # removed while it was loading

    def _drop(self, item_id: str | None) -> None:
        for entry in [e for e in self.queue if e["id"] == item_id]:
            self.queue.remove(entry)
            if entry["file"]:
                Path(entry["file"]).unlink(missing_ok=True)
        self._save()

    def _save(self) -> None:
        self.ctx.store.set("queue", self.queue)
        waiting = sum(1 for e in self.queue if e["id"] != self.playing)
        self.ctx.set_info(f"{waiting} waiting · yt-dlp {self.ytdlp.version}")
        self.ctx.changed()
