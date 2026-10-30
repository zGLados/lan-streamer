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
    """Anything GStreamer can open by URI: http(s) HLS/MP4, rtsp, srt, file.

    audio_uri: the sound from a second URI, when picture and sound come
    separately (YouTube in HD); uri then gives only the picture.
    """

    uri: str
    audio_uri: str = ""
    kind: Literal["uri"] = "uri"


@dataclass(frozen=True)
class FileSource:
    """A local media file.

    cut: switch to it without crossfade, so nothing of its start is lost
    (e.g. a video that follows an announcement card).
    audio_path: the sound from a second file, when picture and sound come
    separately; path then gives only the picture.
    """

    path: str
    cut: bool = False
    kind: Literal["file"] = "file"
    audio_path: str = ""


@dataclass(frozen=True)
class ImageSource:
    """A still picture (PNG or JPEG) with silence – e.g. a standby slate.

    It never ends by itself; an input that shows images decides how long
    (e.g. the playlist's image duration).
    """

    path: str
    kind: Literal["image"] = "image"


@dataclass(frozen=True)
class CardSource:
    """A dark card with text and silence – e.g. "Up next" / <title> / "by Anna".

    Like an image it never ends by itself.
    """

    title: str
    heading: str = ""  # small, above the title
    subtitle: str = ""  # below the title
    kind: Literal["card"] = "card"


MediaSource = TestSource | UriSource | FileSource | ImageSource | CardSource


def source_to_dict(source: MediaSource) -> dict[str, Any]:
    """The form the core sends to the output worker."""
    if isinstance(source, FileSource):
        uri: dict[str, Any] = {"kind": "uri", "uri": Path(source.path).resolve().as_uri()}
        if source.audio_path:
            uri["audio_uri"] = Path(source.audio_path).resolve().as_uri()
        return {**uri, "cut": True} if source.cut else uri
    if isinstance(source, ImageSource):
        return {"kind": "image", "path": str(Path(source.path).resolve())}
    if isinstance(source, UriSource):
        uri = {"kind": "uri", "uri": source.uri}
        return {**uri, "audio_uri": source.audio_uri} if source.audio_uri else uri
    return asdict(source)


@dataclass
class Metadata:
    """What is playing right now (F-IN-06); shown on the viewer page and in overlays."""

    title: str = ""
    channel: str = ""
    thumbnail: str = ""
    duration_s: float | None = None
    submitter: str = ""


@dataclass(frozen=True)
class Viewer:
    """Who sent a viewer form: a device id (browser cookie) and the LAN address."""

    device: str
    ip: str


@dataclass(frozen=True)
class ViewerItem:
    """One line the input shows on the viewer page, e.g. a queue entry."""

    id: str
    text: str
    detail: str = ""


class Rejected(Exception):
    """Raised in on_viewer_submit(): the message is shown to the viewer as it is."""


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
    _request_next: Callable[[], None] = field(repr=False, default=lambda: None)
    _publish_info: Callable[[str], None] = field(repr=False, default=lambda _text: None)
    _publish_changed: Callable[[], None] = field(repr=False, default=lambda: None)

    def set_metadata(self, metadata: Metadata) -> None:
        """Publishes what is playing now."""
        self._publish_metadata(metadata)

    def next_source(self) -> None:
        """Asks for get_source() to be called again while this input stays on air.

        For inputs with several items (a playlist): when an item has ended and
        no other input takes over, the next item follows without a gap.
        """
        self._request_next()

    def set_info(self, text: str) -> None:
        """A short status line for the admin page, e.g. "3 videos waiting · yt-dlp 2026.10.1"."""
        self._publish_info(text)

    def changed(self) -> None:
        """Tells the pages that viewer_items() changed (e.g. a new queue entry)."""
        self._publish_changed()


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
    #: A form viewers can send from the player page (F-IN-04), e.g. "submit a link".
    #: The page builds it from the model's JSON Schema; see on_viewer_submit().
    ViewerForm: ClassVar[type[BaseModel] | None] = None
    #: Heading of the form and its list on the player page.
    viewer_title: ClassVar[str] = ""

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

    async def on_viewer_submit(self, form: BaseModel, viewer: Viewer) -> str:
        """A viewer sent the ViewerForm. Return a message for them, or raise Rejected."""
        raise Rejected("this input takes no submissions")

    async def viewer_items(self) -> list[ViewerItem]:
        """Lines shown under the form on the player page (e.g. the queue)."""
        return []

    async def remove_item(self, item_id: str) -> None:
        """The admin removes one of the viewer_items() (e.g. a queue entry)."""
