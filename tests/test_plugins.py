import asyncio
from pathlib import Path

import pytest

from lanstreamer.plugins.process import PluginError, PluginProcess, PluginRejected
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


def test_playlist_plays_youtube_links_and_skips_one_that_cannot_be_played(tmp_path, monkeypatch):
    monkeypatch.setenv("LANSTREAMER_FAKE_DOWNLOADS", "1")  # fake yt-dlp answers
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"")
    items = [str(clip), "https://www.youtube.com/watch?v=abc", "https://youtu.be/offline", "https://youtu.be/def"]

    async def steps(proc, events):
        sources, titles = [], []
        for _ in range(4):
            sources.append(await proc.get_source())
            titles.append([e for e in events if e.get("event") == "metadata"][-1]["value"]["title"])
            await proc.playback("ended")
            await asyncio.sleep(0.2)
        return sources, titles

    sources, titles = asyncio.run(run_plugin(BUILTIN / "playlist", tmp_path, {"items": items}, steps))
    assert sources[0] == {"kind": "uri", "uri": clip.resolve().as_uri()}
    # looked up right before playing; picture and sound as separate streams
    assert sources[1] == {"kind": "uri", "uri": "https://fake.invalid/abc/video", "audio_uri": "https://fake.invalid/abc/audio"}
    assert sources[2]["uri"] == "https://fake.invalid/def/video"  # the offline one was skipped
    assert sources[3] == sources[0]  # starts over
    assert titles == ["a.mp4", "Video abc", "Video def", "a.mp4"]


def test_playlist_without_youtube_links_does_not_load_yt_dlp(tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"")

    async def steps(proc, events):
        return await proc.get_source()

    asyncio.run(run_plugin(BUILTIN / "playlist", tmp_path, {"items": [str(clip), "https://x/live.m3u8"]}, steps))
    assert not (tmp_path / "data" / "yt-dlp").exists()


def test_a_playlist_without_loop_does_not_start_over_when_skipping(tmp_path, monkeypatch):
    monkeypatch.setenv("LANSTREAMER_FAKE_DOWNLOADS", "1")

    async def steps(proc, events):
        first = await proc.get_source()
        await proc.playback("ended")
        await asyncio.sleep(0.2)
        try:
            await proc.get_source()
        except PluginError as exc:
            return first, str(exc)
        raise AssertionError("played the list again")

    first, error = asyncio.run(run_plugin(
        BUILTIN / "playlist", tmp_path, {"items": ["https://youtu.be/one", "https://youtu.be/offline"], "loop": False},
        steps))  # fmt: skip
    assert first["uri"] == "https://fake.invalid/one/video"
    assert "playlist has ended" in error


def test_empty_playlist_is_inactive(tmp_path):
    async def steps(proc, events):
        return await wait_for(events, "active")

    active = asyncio.run(run_plugin(BUILTIN / "playlist", tmp_path, {"items": []}, steps))
    assert active[0]["value"] is False


def test_playlist_has_no_viewer_form_unless_viewers_may_add_videos(tmp_path):
    async def steps(proc, events):
        schema = await proc.viewer_schema()
        with pytest.raises(PluginRejected, match="takes no submissions"):
            await proc.viewer_submit({"link": "https://youtu.be/abc"}, "dev-a", "10.0.0.2")
        return schema

    assert asyncio.run(run_plugin(BUILTIN / "playlist", tmp_path, {"items": []}, steps)) is None


def test_viewers_add_youtube_videos_that_play_after_the_current_item(tmp_path, monkeypatch):
    monkeypatch.setenv("LANSTREAMER_FAKE_DOWNLOADS", "1")
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"")
    b.write_bytes(b"")
    settings = {"items": [str(a), str(b)], "viewers_add": True, "viewer_per_device": 1}

    async def steps(proc, events):
        schema = await proc.viewer_schema()
        assert schema["title"] == "Add a video" and set(schema["schema"]["properties"]) == {"link", "name"}
        assert (await proc.get_source())["uri"] == a.resolve().as_uri()

        msg = await proc.viewer_submit({"link": "https://www.youtube.com/shorts/one", "name": "Anna"}, "dev-a", "10.0.0.2")
        assert msg == "Added – position 1 in the queue."
        with pytest.raises(PluginRejected, match="already have 1 video waiting"):
            await proc.viewer_submit({"link": "https://youtu.be/two"}, "dev-a", "10.0.0.2")
        with pytest.raises(PluginRejected, match="already in the queue"):
            await proc.viewer_submit({"link": "https://www.youtube.com/shorts/one"}, "dev-b", "10.0.0.3")
        with pytest.raises(PluginRejected, match="must be a YouTube video link"):
            await proc.viewer_submit({"link": "https://www.tiktok.com/@a/video/1"}, "dev-b", "10.0.0.3")
        await proc.viewer_submit({"link": "https://youtu.be/two"}, "dev-b", "10.0.0.3")
        assert [i["text"] for i in await proc.viewer_items()] == ["Video one", "Video two"]

        await proc.playback("ended")  # a.mp4 ended: the viewer videos come first
        await asyncio.sleep(0.2)
        first = await proc.get_source()
        meta = [e for e in events if e.get("event") == "metadata"][-1]["value"]
        assert (first["uri"], meta["title"], meta["submitter"]) == ("https://fake.invalid/one/video", "Video one", "Anna")
        assert await proc.can_release() is False
        assert (await proc.viewer_items())[0]["detail"] == "by Anna · on air"

        await proc.remove_item((await proc.viewer_items())[0]["id"])  # the admin removes the one on air
        await asyncio.sleep(0.2)
        assert (await proc.get_source())["uri"] == "https://fake.invalid/two/video"
        await proc.playback("ended")
        await asyncio.sleep(0.2)
        assert await proc.viewer_items() == []
        assert (await proc.get_source())["uri"] == b.resolve().as_uri()  # the list goes on where it was

    asyncio.run(run_plugin(BUILTIN / "playlist", tmp_path, settings, steps))


def test_viewer_limits_of_zero_mean_no_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("LANSTREAMER_FAKE_DOWNLOADS", "1")
    settings = {"items": [], "viewers_add": True, "viewer_max_length_s": 0, "viewer_per_device": 0, "viewer_max_queue": 0}

    async def steps(proc, events):
        for n in range(3):
            await proc.viewer_submit({"link": f"https://youtu.be/v{n}"}, "dev-a", "10.0.0.2")
        return len(await proc.viewer_items())

    assert asyncio.run(run_plugin(BUILTIN / "playlist", tmp_path, settings, steps)) == 3


def test_the_viewer_queue_survives_a_restart_and_skips_videos_that_cannot_play(tmp_path, monkeypatch):
    monkeypatch.setenv("LANSTREAMER_FAKE_DOWNLOADS", "1")
    settings = {"items": [], "viewers_add": True, "viewer_per_device": 5}

    async def add(proc, events):
        for link in ("https://youtu.be/offline", "https://youtu.be/two"):
            await proc.viewer_submit({"link": link}, "dev-a", "10.0.0.2")
        await asyncio.sleep(1.5)  # the next activity poll
        return (await wait_for(events, "active"))[-1]["value"]

    async def play(proc, events):
        active = (await wait_for(events, "active"))[0]["value"]
        source = await proc.get_source()
        await proc.playback("ended")
        await asyncio.sleep(1.5)  # the next activity poll
        return active, source, await proc.viewer_items(), [e["value"] for e in events if e.get("event") == "active"]

    assert asyncio.run(run_plugin(BUILTIN / "playlist", tmp_path, settings, add)) is True
    active, source, items, changes = asyncio.run(run_plugin(BUILTIN / "playlist", tmp_path, settings, play))
    assert active is True and source["uri"] == "https://fake.invalid/two/video"  # "offline" was skipped
    assert items == [] and changes[-1] is False  # empty list, no viewer videos: inactive again

    async def schema(proc, events):
        return await proc.viewer_schema(), await proc.viewer_items()

    off = {"items": [], "viewers_add": False}
    asyncio.run(run_plugin(BUILTIN / "playlist", tmp_path, settings, add))
    assert asyncio.run(run_plugin(BUILTIN / "playlist", tmp_path, off, schema)) == (None, [])  # turned off: queue gone


def test_viewer_video_lengths_read_well():
    from lanstreamer.plugins.builtin.playlist.playlist import span

    assert [span(s) for s in (45, 900, 5400)] == ["45 s", "15 min", "1 h 30 min"]


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


def test_check_settings_returns_schema_and_validation():
    from lanstreamer.plugins.process import check_settings

    ok = asyncio.run(check_settings(BUILTIN / "test_pattern", {"pattern": "ball"}))
    assert ok["errors"] == [] and ok["settings"] == {"pattern": "ball", "tone_hz": 440}
    assert set(ok["schema"]["properties"]) == {"pattern", "tone_hz"}
    bad = asyncio.run(check_settings(BUILTIN / "test_pattern", {"tone_hz": 5}))
    assert bad["errors"][0]["field"] == "tone_hz"


def test_playlist_expands_folders_and_shows_images_for_a_while(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    for name in ("b.mp4", "a.png", "notes.txt"):
        (media / name).write_bytes(b"")

    async def steps(proc, events):
        first = await proc.get_source()  # a.png – sorted by name, notes.txt skipped
        await proc.playback("started")
        await wait_for(events, "next_source", timeout=5)  # the picture's time is over
        second = await proc.get_source()
        return first, second, await proc.can_release()

    first, second, releasable = asyncio.run(
        run_plugin(BUILTIN / "playlist", tmp_path, {"items": [str(media)], "image_duration_s": 1}, steps)
    )
    assert first == {"kind": "image", "path": str((media / "a.png").resolve())}  # fitted, as before
    assert second == {"kind": "uri", "uri": (media / "b.mp4").resolve().as_uri()}
    assert releasable is False  # a new item started


def test_standby_shows_the_built_in_slate_or_a_given_picture(tmp_path):
    async def steps(proc, events):
        return await proc.get_source()

    source = asyncio.run(run_plugin(BUILTIN / "standby", tmp_path, {}, steps))
    assert source["kind"] == "image" and source["path"].endswith("standby.png")
    assert source["size"] == "auto"  # a small picture stays its size, centered
    source = asyncio.run(run_plugin(BUILTIN / "standby", tmp_path, {"size": "fill"}, steps))
    assert source["size"] == "fill"
    proc = PluginProcess("x", BUILTIN / "standby", tmp_path, {"image": "/nope.png"}, lambda *_: None)
    with pytest.raises(PluginError, match="file not found"):
        asyncio.run(proc.start())


def test_bouncing_logo_bounces_the_icon_or_a_given_picture(tmp_path):
    async def steps(proc, events):
        return await proc.get_source()

    source = asyncio.run(run_plugin(BUILTIN / "bouncing_logo", tmp_path, {}, steps))
    assert source["kind"] == "bounce" and source["path"].endswith("icon.png")
    assert (source["size"], source["speed"], source["change_color"]) == (20, 240, True)
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"png")
    settings = {"image": str(logo), "size": 30, "speed": 500, "change_color": False}
    source = asyncio.run(run_plugin(BUILTIN / "bouncing_logo", tmp_path, settings, steps))
    assert source == {"kind": "bounce", "path": str(logo.resolve()), "size": 30, "speed": 500, "change_color": False}
    proc = PluginProcess("x", BUILTIN / "bouncing_logo", tmp_path, {"image": "/nope.png"}, lambda *_: None)
    with pytest.raises(PluginError, match="file not found"):
        asyncio.run(proc.start())


def test_a_plugin_that_prints_does_not_break_the_channel(tmp_path):
    code = (
        "from lanstreamer_sdk import InputPlugin, TestSource\n"
        "print('hello from the plugin module')\n"
        "class Plugin(InputPlugin):\n"
        "    async def get_source(self):\n"
        "        print('[download] 50% of 4 KiB', end='\\r')\n"
        "        return TestSource()\n"
    )
    folder = write_plugin(tmp_path / "p", MANIFEST.format(api=1), code)

    async def steps(proc, events):
        return await proc.get_source(), await proc.get_source()

    first, second = asyncio.run(run_plugin(folder, tmp_path, None, steps))
    assert first == second == {"kind": "test", "pattern": "smpte", "freq": 440}
