"""lan-streamer plugin SDK – the only package an input plugin depends on.

A plugin is a folder (or .zip) with a ``plugin.toml`` and a Python module:

    # plugin.toml
    id = "my-input"
    name = "My input"
    version = "1.0.0"
    api_version = 1
    entry = "my_input:Plugin"

    # my_input.py
    from lanstreamer_sdk import InputPlugin, UriSource
    from pydantic import BaseModel

    class Settings(BaseModel):
        url: str

    class Plugin(InputPlugin[Settings]):
        Settings = Settings

        async def is_active(self) -> bool:
            return True

        async def get_source(self):
            return UriSource(self.settings.url)

Every plugin instance runs in its own process (see docs/architecture.md).
"""

from lanstreamer_sdk.manifest import Manifest, ManifestError, load_manifest
from lanstreamer_sdk.plugin import (
    API_VERSION,
    FileSource,
    InputPlugin,
    MediaSource,
    Metadata,
    NoSettings,
    PlaybackEvent,
    PluginContext,
    Store,
    TestSource,
    UriSource,
    source_to_dict,
)

__all__ = [
    "API_VERSION",
    "FileSource",
    "InputPlugin",
    "Manifest",
    "ManifestError",
    "MediaSource",
    "Metadata",
    "NoSettings",
    "PlaybackEvent",
    "PluginContext",
    "Store",
    "TestSource",
    "UriSource",
    "load_manifest",
    "source_to_dict",
]
