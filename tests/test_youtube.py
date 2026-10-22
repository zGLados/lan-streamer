"""The built-in YouTube input, in a real plugin process, with fake yt-dlp answers."""

import asyncio

from lanstreamer.plugins.process import PluginError, PluginProcess
from lanstreamer.plugins.registry import BUILTIN

PLUGIN = BUILTIN / "youtube"


async def with_plugin(tmp_path, settings, steps):
    events = []
    proc = PluginProcess("yt", PLUGIN, tmp_path / "data", settings, lambda _i, e: events.append(e))
    await proc.start()
    try:
        return await steps(proc, events)
    finally:
        await proc.stop()


def last(events, kind):
    found = [e for e in events if e.get("event") == kind]
    return found[-1]["value"] if found else None


async def until(check, timeout=5):
    for _ in range(int(timeout / 0.05)):
        if check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


def test_a_playlist_plays_its_videos_in_order_and_starts_over(tmp_path, monkeypatch):
    monkeypatch.setenv("LANSTREAMER_FAKE_DOWNLOADS", "1")

    async def steps(proc, events):
        await until(lambda: last(events, "active") is True)
        assert last(events, "info").startswith("3 videos · next: Video v1")
        played = []
        for _ in range(4):
            source = await proc.get_source()
            played.append(last(events, "metadata")["title"])
            assert await proc.can_release() is False  # not in the middle of a video
            await proc.playback("ended")
            await until(lambda: len([e for e in events if e.get("event") == "next_source"]) == len(played))
            assert await proc.can_release() is True  # another input may take over now
        assert played == ["Video v1", "Video v2", "Video v3", "Video v1"]
        assert source == {"kind": "uri", "uri": "https://fake.invalid/v1/video", "audio_uri": "https://fake.invalid/v1/audio"}

    asyncio.run(with_plugin(tmp_path, {"url": "https://www.youtube.com/playlist?list=PL1"}, steps))


def test_a_live_page_is_active_only_while_live(tmp_path, monkeypatch):
    monkeypatch.setenv("LANSTREAMER_FAKE_DOWNLOADS", "1")

    async def live(proc, events):
        await until(lambda: last(events, "active") is True)
        assert last(events, "info").startswith("live: Video live")
        await proc.get_source()
        assert await proc.can_release() is False  # holds the output while live
        return last(events, "metadata")["channel"]

    async def offline(proc, events):
        await until(lambda: last(events, "info") and "not live" in last(events, "info"))
        assert last(events, "active") is False
        try:
            await proc.get_source()
        except PluginError:
            return True
        return False

    assert asyncio.run(with_plugin(tmp_path, {"url": "https://www.youtube.com/@band/live"}, live)) == "fake channel"
    assert asyncio.run(with_plugin(tmp_path, {"url": "https://www.youtube.com/@offline/live"}, offline))


def test_settings_need_a_link():
    from lanstreamer.plugins.process import check_settings

    result = asyncio.run(check_settings(PLUGIN, {}))
    assert [e["field"] for e in result["errors"]] == ["url"]
    assert asyncio.run(check_settings(PLUGIN, {"url": "youtube"}))["errors"][0]["field"] == "url"
