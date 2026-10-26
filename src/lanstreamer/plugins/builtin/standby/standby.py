"""Built-in input: a still picture with silence, always active (the default fallback)."""

from importlib import resources
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from lanstreamer_sdk import ImageSource, InputPlugin, Metadata

DEFAULT = resources.files("lanstreamer.assets").joinpath("standby.png")


class Settings(BaseModel):
    image: str = Field("", title="Picture", description="PNG or JPEG file on this PC; empty = the lan-streamer slate")

    @field_validator("image")
    @classmethod
    def exists(cls, value: str) -> str:
        if value and not Path(value).is_file():
            raise ValueError(f"file not found: {value}")
        return value


class Plugin(InputPlugin[Settings]):
    Settings = Settings

    async def start(self) -> None:
        self.ctx.set_metadata(Metadata(title="Standby"))

    async def get_source(self):
        return ImageSource(self.settings.image or str(DEFAULT))
