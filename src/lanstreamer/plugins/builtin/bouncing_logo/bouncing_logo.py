"""Built-in input: a picture that bounces off the screen's edges like the DVD logo, always active."""

from importlib import resources
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from lanstreamer_sdk import BounceSource, InputPlugin, Metadata

DEFAULT = resources.files("lanstreamer.assets").joinpath("icon.png")


class Settings(BaseModel):
    image: str = Field("", title="Picture", description="PNG or JPEG: upload one, or a file on this PC; a white logo "
                       "on a transparent background works best · empty = the lan-streamer icon",
                       json_schema_extra={"format": "image"})  # fmt: skip
    size: int = Field(20, title="Size (%)", ge=5, le=60, description="Width of the picture in percent of the screen")
    speed: int = Field(240, title="Speed", ge=20, le=2000, description="Pixels per second on a 1080p screen")
    change_color: bool = Field(True, title="Random color", description="A new random color at every bounce "
                               "(the picture is tinted: white takes the color, black stays black)")  # fmt: skip

    @field_validator("image")
    @classmethod
    def exists(cls, value: str) -> str:
        if value and not Path(value).is_file():
            raise ValueError(f"file not found: {value}")
        return value


class Plugin(InputPlugin[Settings]):
    Settings = Settings

    async def start(self) -> None:
        self.ctx.set_metadata(Metadata(title="Bouncing logo"))

    async def get_source(self):
        s = self.settings
        return BounceSource(s.image or str(DEFAULT), size=s.size, speed=s.speed, change_color=s.change_color)
