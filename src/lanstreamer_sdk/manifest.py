"""plugin.toml: what a plugin is and whether this lan-streamer can run it (F-PLG-04)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from lanstreamer_sdk.plugin import API_VERSION

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
_ENTRY = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    version: str
    api_version: int
    entry: str  # "module:Class"
    description: str = ""
    requirements: list[str] = field(default_factory=list)
    path: Path | None = None  # folder that holds plugin.toml


def load_manifest(folder: Path) -> Manifest:
    """Reads and checks folder/plugin.toml. Raises ManifestError with a clear message."""
    file = folder / "plugin.toml"
    if not file.is_file():
        raise ManifestError("plugin.toml is missing")
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"plugin.toml is not valid TOML: {exc}") from exc

    for key in ("id", "name", "version", "api_version", "entry"):
        if key not in data:
            raise ManifestError(f"plugin.toml: '{key}' is missing")
    if not _ID.match(str(data["id"])):
        raise ManifestError("plugin.toml: 'id' must be 2–49 characters: a-z, 0-9 and '-'")
    if not _ENTRY.match(str(data["entry"])):
        raise ManifestError("plugin.toml: 'entry' must look like 'module:ClassName'")
    if not isinstance(data["api_version"], int):
        raise ManifestError("plugin.toml: 'api_version' must be a whole number")
    if data["api_version"] != API_VERSION:
        raise ManifestError(
            f"plugin needs plugin API {data['api_version']}, "
            f"this lan-streamer provides API {API_VERSION}"
        )
    requirements = data.get("requirements", [])
    if not isinstance(requirements, list) or not all(isinstance(r, str) for r in requirements):
        raise ManifestError("plugin.toml: 'requirements' must be a list of strings")

    return Manifest(
        id=data["id"],
        name=str(data["name"]),
        version=str(data["version"]),
        api_version=data["api_version"],
        entry=data["entry"],
        description=str(data.get("description", "")),
        requirements=requirements,
        path=folder,
    )
