import asyncio
from pathlib import Path

import pytest

from lanstreamer.plugins.process import PluginError, PluginProcess
from lanstreamer_sdk import API_VERSION, ManifestError, load_manifest

BUILTIN = Path(__file__).resolve().parents[1] / "src" / "lanstreamer" / "plugins" / "builtin"


def write_plugin(folder: Path, toml: str, code: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.toml").write_text(toml)
    if code:
        (folder / "mod.py").write_text(code)
    return folder


MANIFEST = 'id = "demo"\nname = "Demo"\nversion = "1.0"\napi_version = {api}\nentry = "mod:Plugin"\n'


# --- manifest -----------------------------------------------------------------------


def test_builtin_manifests_are_valid():
    for folder in BUILTIN.iterdir():
        manifest = load_manifest(folder)
        assert manifest.api_version == API_VERSION


def test_incompatible_api_version_is_reported_clearly(tmp_path):
    folder = write_plugin(tmp_path / "p", MANIFEST.format(api=API_VERSION + 1))
    with pytest.raises(ManifestError, match="needs plugin API 2"):
        load_manifest(folder)


@pytest.mark.parametrize(
    "toml, message",
    [
        ('name = "x"', "'id' is missing"),
        (MANIFEST.format(api=1).replace('"demo"', '"Bad ID"'), "'id' must be"),
        (MANIFEST.format(api=1).replace('"mod:Plugin"', '"mod.Plugin"'), "'entry' must"),
        ("id = ", "not valid TOML"),
    ],
)
def test_bad_manifests(tmp_path, toml, message):
    with pytest.raises(ManifestError, match=message):
        load_manifest(write_plugin(tmp_path / "p", toml))


# --- plugin processes ---------------------------------------------------------------


async def run_plugin(folder, tmp_path, settings=None, steps=None):
    events = []
    proc = PluginProcess("inst-1", folder, tmp_path / "data", settings or {}, lambda _i, e: events.append(e))
    await proc.start()
    try:
        return await steps(proc, events)
    finally:
        await proc.stop()


async def wait_for(events, kind, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if any(e.get("event") == kind for e in events):
            return [e for e in events if e.get("event") == kind]
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {kind} event in {events}")


def test_test_pattern_plugin(tmp_path):
    async def steps(proc, events):
        active = await wait_for(events, "active")
        source = await proc.get_source()
        meta = await wait_for(events, "metadata")
        return active, source, meta, await proc.can_release()

    active, source, meta, releasable = asyncio.run(
        run_plugin(BUILTIN / "test_pattern", tmp_path, {"pattern": "ball", "tone_hz": 660}, steps)
    )
    assert active[0]["value"] is True
    assert source == {"kind": "test", "pattern": "ball", "freq": 660}
    assert meta[0]["value"]["title"] == "Test pattern (ball)"
    assert releasable is True


def test_playlist_advances_and_releases_after_an_item_ended(tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"")

    async def steps(proc, events):
        first = await proc.get_source()
        before = await proc.can_release()
        await proc.playback("ended")
        await asyncio.sleep(0.3)
        after = await proc.can_release()
        second = await proc.get_source()
        return first, before, after, second

    first, before, after, second = asyncio.run(
        run_plugin(BUILTIN / "playlist", tmp_path, {"items": [str(clip), "https://x/live.m3u8"]}, steps)
    )
    assert first == {"kind": "uri", "uri": clip.resolve().as_uri()}
    assert (before, after) == (False, True)
    assert second == {"kind": "uri", "uri": "https://x/live.m3u8"}


def test_empty_playlist_is_inactive(tmp_path):
    async def steps(proc, events):
        return await wait_for(events, "active")

    active = asyncio.run(run_plugin(BUILTIN / "playlist", tmp_path, {"items": []}, steps))
    assert active[0]["value"] is False


def test_invalid_settings_stop_the_plugin_with_a_message(tmp_path):
    proc = PluginProcess("x", BUILTIN / "test_pattern", tmp_path, {"tone_hz": 5}, lambda *_: None)
    with pytest.raises(PluginError, match="ValidationError"):
        asyncio.run(proc.start())


def test_a_crashing_plugin_only_fails_its_own_calls(tmp_path):
    code = (
        "from lanstreamer_sdk import InputPlugin\n"
        "class Plugin(InputPlugin):\n"
        "    async def get_source(self):\n"
        "        raise RuntimeError('source offline')\n"
    )
    folder = write_plugin(tmp_path / "p", MANIFEST.format(api=1), code)

    async def steps(proc, events):
        with pytest.raises(PluginError, match="source offline"):
            await proc.get_source()
        return proc.running, await proc.can_release()

    running, releasable = asyncio.run(run_plugin(folder, tmp_path, None, steps))
    assert running and releasable
