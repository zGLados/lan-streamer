"""Example input plugin for lan-streamer (plugin API 1): a queue of stream links.

Viewers add links to videos or streams on the player page; they play one after
another. It shows every part of the plugin API once:

  Settings            – the settings form in the admin page (a Pydantic model)
  start() / stop()    – set up and clean up; the queue survives restarts (ctx.store)
  watch_active()      – reports the activity at once when the queue changes
  get_source()        – what to play: the first link of the queue
  can_release()       – on equal priority: hand over only between two links
  on_playback()       – a link ended or failed: drop it, play the next one
  ViewerForm          – the form on the player page; on_viewer_submit() answers it
  viewer_items()      – the queue under the form; remove_item() for the admin
  ctx.set_metadata()  – title and submitter for the player page and the overlay
  ctx.set_info()      – a status line in the admin page

Guide: docs/plugin-guide.md
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from lanstreamer_sdk import (
    InputPlugin,
    Metadata,
    PlaybackEvent,
    Rejected,
    UriSource,
    Viewer,
    ViewerItem,
)

SCHEMES = ("http", "https", "rtsp", "rtsps", "srt")


class Settings(BaseModel):
    max_queue: int = Field(20, ge=1, le=500, description="How many links may wait")
    per_device: int = Field(3, ge=1, le=100, description="Waiting links per viewer device")


class LinkForm(BaseModel):
    """The player page builds the form from this model (titles and descriptions)."""

    url: str = Field(title="Link", description="MP4, HLS (.m3u8), RTSP or SRT address")
    title: str = Field("", max_length=80, title="Title (optional)")
    name: str = Field("", max_length=40, title="Your name (optional)")


class Plugin(InputPlugin[Settings]):
    Settings = Settings
    ViewerForm = LinkForm
    viewer_title = "Queue a stream"

    async def start(self) -> None:
        # [{"id", "url", "title", "name", "device"}, …] – kept across restarts
        self.queue: list[dict[str, str]] = self.ctx.store.get("queue", [])
        self.on_air = ""  # id of the link handed out by get_source(), until it ends
        self.queue_changed = asyncio.Event()
        self._publish()

    async def is_active(self) -> bool:
        return bool(self.queue)

    async def watch_active(self) -> AsyncIterator[bool]:
        # Instead of polling is_active() every poll_interval_s: report at once.
        last = None
        while True:
            if (active := await self.is_active()) != last:
                yield active
                last = active
            await self.queue_changed.wait()
            self.queue_changed.clear()

    async def get_source(self) -> UriSource:
        if not self.queue:
            raise RuntimeError("the queue is empty")  # the core tries again later
        link = self.queue[0]
        self.on_air = link["id"]
        self.ctx.set_metadata(Metadata(title=link["title"] or link["url"], submitter=link["name"]))
        return UriSource(link["url"])

    async def can_release(self) -> bool:
        return not self.on_air  # never cut a link off for an input of the same priority

    async def on_playback(self, event: PlaybackEvent) -> None:
        if event.kind == "started":
            return
        if event.kind == "failed":
            self.ctx.log.warning("link %s failed: %s", self.on_air, event.detail)
        # by id: the admin may have removed the link meanwhile
        self.queue = [link for link in self.queue if link["id"] != self.on_air]
        self.on_air = ""
        self._publish()
        if self.queue:
            self.ctx.next_source()  # stay on air with the next link

    async def on_viewer_submit(self, form: BaseModel, viewer: Viewer) -> str:
        assert isinstance(form, LinkForm)
        url = form.url.strip()
        if urlparse(url).scheme.lower() not in SCHEMES:
            raise Rejected("Please send an http(s), rtsp or srt link.")
        if len(self.queue) >= self.settings.max_queue:
            raise Rejected("The queue is full – try again later.")
        if sum(link["device"] == viewer.device for link in self.queue) >= self.settings.per_device:
            raise Rejected(f"You already have {self.settings.per_device} links waiting.")
        self.queue.append({
            "id": secrets.token_hex(4), "url": url, "title": form.title.strip(),
            "name": form.name.strip(), "device": viewer.device,
        })  # fmt: skip
        self._publish()
        return f"Added – number {len(self.queue)} in the queue."

    async def viewer_items(self) -> list[ViewerItem]:
        return [
            ViewerItem(id=link["id"], text=link["title"] or link["url"],
                       detail=f"by {link['name']}" if link["name"] else "")
            for link in self.queue
        ]  # fmt: skip

    async def remove_item(self, item_id: str) -> None:
        # a link on air keeps playing to its end
        self.queue = [link for link in self.queue if link["id"] != item_id]
        self._publish()

    def _publish(self) -> None:
        """After every change: store the queue, wake watch_active(), refresh the pages."""
        self.ctx.store.set("queue", self.queue)
        self.queue_changed.set()
        self.ctx.changed()
        self.ctx.set_info(f"{len(self.queue)} waiting")
