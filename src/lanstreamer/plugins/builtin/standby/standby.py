"""Built-in input: a still picture with silence, always active (the default fallback)."""

from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from lanstreamer_sdk import ImageSource, InputPlugin, Metadata

DEFAULT = resources.files("lanstreamer.assets").joinpath("standby.png")


class Settings(BaseModel):
    image: str = Field("", title="Picture", description="PNG or JPEG: upload one, or a file on this PC; "
                       "empty = the lan-streamer slate", json_schema_extra={"format": "image"})  # fmt: skip
    size: Literal["auto", "fit", "fill"] = Field(
        "auto", title="Size", description="auto: a picture smaller than the screen stays its size, centered; "
        "a larger one fills the screen · fit: always scaled to the screen (black bars if needed) · "
        "fill: covers the whole screen, edges are cut off",
    )  # fmt: skip

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
        return ImageSource(self.settings.image or str(DEFAULT), size=self.settings.size)
