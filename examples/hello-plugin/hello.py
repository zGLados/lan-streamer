"""Example input plugin for lan-streamer (plugin API 1).

An input plugin answers three questions:
  is_active()   – do I have something to show right now?
  get_source()  – what should be played when I go on air?
  can_release() – on equal priority: may another input take over now?
Settings are a Pydantic model; the admin page builds its form from it.
"""

import time

from pydantic import BaseModel, Field

from lanstreamer_sdk import InputPlugin, Metadata, TestSource


class Settings(BaseModel):
    pattern: str = Field("colors", description="Test picture: colors, smpte, ball, snow, ...")
    active_seconds: int = Field(60, ge=1, le=60, description="Active during this many seconds of each minute")


class Plugin(InputPlugin[Settings]):
    Settings = Settings
    poll_interval_s = 1.0  # how often is_active() is asked

    async def start(self) -> None:
        self.ctx.log.info("hello plugin started with %s", self.settings)
        self.ctx.set_metadata(Metadata(title="Hello from an uploaded plugin"))

    async def is_active(self) -> bool:
        return time.localtime().tm_sec < self.settings.active_seconds

    async def get_source(self):
        return TestSource(pattern=self.settings.pattern, freq=550)
