"""Built-in input: a generated test picture with a tone, always active."""

from typing import Literal

from pydantic import BaseModel, Field

from lanstreamer_sdk import InputPlugin, Metadata, TestSource


class Settings(BaseModel):
    pattern: Literal["smpte", "ball", "snow", "checkers-8", "colors", "bar"] = "smpte"
    tone_hz: int = Field(440, ge=20, le=20000, description="Frequency of the test tone")


class Plugin(InputPlugin[Settings]):
    Settings = Settings

    async def start(self) -> None:
        self.ctx.set_metadata(Metadata(title=f"Test pattern ({self.settings.pattern})"))

    async def get_source(self):
        return TestSource(pattern=self.settings.pattern, freq=self.settings.tone_hz)
