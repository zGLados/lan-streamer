"""The input plugin API, version 1."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

API_VERSION = 1


# --- What a plugin can play ---------------------------------------------------------


@dataclass(frozen=True)
class TestSource:
    """A generated test picture with a tone."""

    __test__ = False  # not a pytest test class

    pattern: str = "smpte"  # any GStreamer videotestsrc pattern
    freq: int = 440
    kind: Literal["test"] = "test"


@dataclass(frozen=True)
class UriSource:
    """Anything GStreamer can open by URI: http(s) HLS/MP4, rtsp, srt, file."""

    uri: str
    kind: Literal["uri"] = "uri"


@dataclass(frozen=True)
class FileSource:
    """A local media file."""

    path: str
    kind: Literal["file"] = "file"


MediaSource = TestSource | UriSource | FileSource


def source_to_dict(source: MediaSource) -> dict[str, Any]:
    """The form the core sends to the output worker."""
    if isinstance(source, FileSource):
        return {"kind": "uri", "uri": Path(source.path).resolve().as_uri()}
    return asdict(source)


@dataclass
class Metadata:
    """What is playing right now (F-IN-06); shown on the viewer page and in overlays."""

    title: str = ""
    channel: str = ""
    thumbnail: str = ""
    duration_s: float | None = None
    submitter: str = ""


@dataclass
class PlaybackEvent:
    """Sent to the plugin when its source starts, ends or fails on an output."""

    kind: Literal["started", "ended", "failed"]
    detail: str = ""


# --- What the system gives a plugin -----------------------------------------------


class Store:
    """A small persistent key-value store per plugin instance (JSON file)."""

    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._path.write_text(json.dumps(self._data))


@dataclass
class PluginContext:
    instance_id: str
    data_dir: Path
    log: logging.Logger
    store: Store
    _publish_metadata: Callable[[Metadata], None] = field(repr=False)

    def set_metadata(self, metadata: Metadata) -> None:
        """Publishes what is playing now."""
        self._publish_metadata(metadata)


# --- The plugin base class ------------------------------------------------------------


class NoSettings(BaseModel):
    pass


class InputPlugin[S: BaseModel]:
    """Base class of every input plugin. Override what you need.

    Required: get_source(). Usually also is_active(). Everything else has a
    sensible default.
    """

    Settings: ClassVar[type[BaseModel]] = NoSettings
    #: How often the default watch_active() calls is_active() (F-AKT-02).
    poll_interval_s: ClassVar[float] = 5.0

    def __init__(self, ctx: PluginContext, settings: S):
        self.ctx = ctx
        self.settings = settings

    async def start(self) -> None:
        """Called once after creation, before anything else."""

    async def stop(self) -> None:
        """Called once before the process ends."""

    async def is_active(self) -> bool:
        """Whether the input has something to show right now."""
        return True

    async def watch_active(self) -> AsyncIterator[bool]:
        """Yields the activity whenever it changes.

        The default polls is_active(). Override it to push changes at once,
        e.g. from a websocket or a queue (F-AKT-02).
        """
        last: bool | None = None
        while True:
            active = await self.is_active()
            if active != last:
                yield active
                last = active
            await asyncio.sleep(self.poll_interval_s)

    async def get_source(self) -> MediaSource:
        """What to play when this input goes on air."""
        raise NotImplementedError

    async def can_release(self) -> bool:
        """On equal priority: may another input take over now? (e.g. "video has ended")"""
        return True

    async def on_playback(self, event: PlaybackEvent) -> None:
        """Called when this input's source starts, ends or fails on air."""
