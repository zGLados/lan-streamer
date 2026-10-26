"""Built-in input: plays files, folders or URLs one after another.

A folder stands for the media files in it (sorted by name). Pictures are shown
for `image_duration_s`. After each item the playlist may hand over to another
input of the same priority; if none takes over, the next item follows at once.
"""

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from lanstreamer_sdk import FileSource, ImageSource, InputPlugin, Metadata, PlaybackEvent, UriSource

VIDEO = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac"}
IMAGE = {".png", ".jpg", ".jpeg"}


class Settings(BaseModel):
    items: list[str] = Field(default_factory=list, title="Items", description="Files, folders or URLs – one per line")
    loop: bool = Field(True, title="Loop", description="Start again after the last item")
    image_duration_s: float = Field(10, title="Picture duration (s)", ge=1, le=3600, description="How long a picture is shown")


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


class Plugin(InputPlugin[Settings]):
    Settings = Settings

    async def start(self) -> None:
        self.entries = expand(self.settings.items)
        self.position = int(self.ctx.store.get("position", 0))
        self.item_ended = False
        self.timer: asyncio.Task | None = None

    async def is_active(self) -> bool:
        if not self.entries:
            return False
        return self.settings.loop or self.position < len(self.entries)

    def current(self) -> str:
        return self.entries[self.position % len(self.entries)]

    async def get_source(self):
        entry = self.current()
        self.item_ended = False
        self.ctx.set_metadata(Metadata(title=Path(entry).name or entry))
        if is_image(entry):
            return ImageSource(entry)
        return UriSource(entry) if "://" in entry else FileSource(entry)

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
