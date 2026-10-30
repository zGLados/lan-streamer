"""`python -m lanstreamer_sdk new`: a new plugin folder that works from the first minute."""

from __future__ import annotations

import json
from pathlib import Path

from lanstreamer_sdk.manifest import ID_PATTERN, ManifestError, check_id_is_free

_MANIFEST = """\
id = "@ID@"
name = @NAME@
version = "0.1.0"
api_version = 1
entry = "@MODULE@:Plugin"
description = "One sentence for the admin page."
# requirements = ["requests>=2.32"]  # packages from PyPI, installed at upload
"""

_MODULE = '''\
"""@TITLE@ – an input plugin for lan-streamer (plugin API 1).

It already runs: it is always active and plays a test picture. Replace is_active()
and get_source() with your source, e.g. `return UriSource(self.settings.url)`.
"""

from pydantic import BaseModel, Field

from lanstreamer_sdk import InputPlugin, Metadata, TestSource


class Settings(BaseModel):
    # the admin page builds its form from this model
    pattern: str = Field("smpte", description="Test picture: smpte, colors, ball, snow, ...")


class Plugin(InputPlugin[Settings]):
    Settings = Settings

    async def is_active(self) -> bool:
        """Do I have something to show right now? Asked every poll_interval_s seconds."""
        return True

    async def get_source(self):
        """What should be played when this input goes on air?"""
        self.ctx.set_metadata(Metadata(title=@NAME@))
        return TestSource(pattern=self.settings.pattern)
'''

_CONFTEST = """\
import sys
from pathlib import Path

# the plugin module sits in the folder above, as in lan-streamer's plugin process
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
"""

_TEST = """\
import asyncio

from @MODULE@ import Plugin, Settings

from lanstreamer_sdk import TestSource
from lanstreamer_sdk.testing import make_context


def test_plays_the_test_picture(tmp_path):
    async def steps():
        ctx, seen = make_context(tmp_path)
        plugin = Plugin(ctx, Settings(pattern="ball"))
        await plugin.start()
        assert await plugin.is_active()
        assert await plugin.get_source() == TestSource(pattern="ball")
        assert seen.metadata[-1].title == @NAME@

    asyncio.run(steps())
"""


def module_name(plugin_id: str) -> str:
    """Always ends in _plugin, so that it cannot shadow a package the plugin imports."""
    name = plugin_id.replace("-", "_")
    if not name.endswith("_plugin"):
        name += "_plugin"
    return f"plugin_{name}" if name[0].isdigit() else name


def new_plugin(folder: Path, name: str = "") -> Path:
    """Creates a working plugin in folder; its name is the plugin's id. Returns the module file."""
    plugin_id = folder.resolve().name
    if not ID_PATTERN.match(plugin_id):
        raise ManifestError(
            f"'{plugin_id}' cannot be a plugin id: 2–49 characters, a-z, 0-9 and '-', "
            "starting with a letter or digit"
        )
    check_id_is_free(plugin_id)
    if folder.exists() and any(folder.iterdir()):
        raise ManifestError(f"{folder} already exists and is not empty")

    module = module_name(plugin_id)
    title = name or plugin_id.replace("-", " ").capitalize()
    def fill(template: str) -> str:
        return (template.replace("@ID@", plugin_id).replace("@MODULE@", module)
                .replace("@NAME@", json.dumps(title, ensure_ascii=False)).replace("@TITLE@", title))  # fmt: skip

    (folder / "tests").mkdir(parents=True, exist_ok=True)
    for path, template in (
        (folder / "plugin.toml", _MANIFEST),
        (folder / f"{module}.py", _MODULE),
        (folder / "tests" / "conftest.py", _CONFTEST),
        (folder / "tests" / f"test_{module}.py", _TEST),
    ):
        path.write_text(fill(template), encoding="utf-8")
    return folder / f"{module}.py"
