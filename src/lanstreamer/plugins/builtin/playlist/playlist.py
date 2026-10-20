"""Built-in input: plays files or URLs one after another (the usual fallback).

Each time it goes on air it plays the next item. When an item ends it may hand
over to another input of the same priority.
"""

from pathlib import Path

from pydantic import BaseModel, Field

from lanstreamer_sdk import FileSource, InputPlugin, Metadata, PlaybackEvent, UriSource


class Settings(BaseModel):
    items: list[str] = Field(default_factory=list, description="Files or URLs, one per entry")
    loop: bool = Field(True, description="Start again after the last item")


class Plugin(InputPlugin[Settings]):
    Settings = Settings

    async def start(self) -> None:
        self.position = int(self.ctx.store.get("position", 0))
        self.item_ended = False

    async def is_active(self) -> bool:
        if not self.settings.items:
            return False
        return self.settings.loop or self.position < len(self.settings.items)

    async def get_source(self):
        item = self.settings.items[self.position % len(self.settings.items)]
        self.item_ended = False
        self.ctx.set_metadata(Metadata(title=Path(item).name or item))
        return UriSource(item) if "://" in item else FileSource(item)

    async def can_release(self) -> bool:
        return self.item_ended

    async def on_playback(self, event: PlaybackEvent) -> None:
        if event.kind in ("ended", "failed"):
            self.item_ended = True
            self.position += 1
            self.ctx.store.set("position", self.position)
