"""The built-in submissions input, in a real plugin process."""

import asyncio
import http.server
import threading
from functools import partial

import pytest

from lanstreamer.plugins.process import PluginProcess, PluginRejected
from lanstreamer.plugins.registry import BUILTIN

PLUGIN = BUILTIN / "submissions"


async def wait_until(check, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if await check():
            return True
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def with_plugin(tmp_path, settings, steps):
    events = []
    proc = PluginProcess("subs", PLUGIN, tmp_path / "data", settings, lambda _i, e: events.append(e))
    await proc.start()
    try:
        return await steps(proc, events)
    finally:
        await proc.stop()


def test_queue_limits_and_playback(tmp_path, monkeypatch):
    monkeypatch.setenv("LANSTREAMER_FAKE_DOWNLOADS", "1")

    async def steps(proc, events):
        schema = await proc.viewer_schema()
        assert schema["title"] == "Submit a video" and set(schema["schema"]["properties"]) == {"link", "name"}

        msg = await proc.viewer_submit({"link": "https://www.tiktok.com/@a/video/1", "name": "Anna"}, "dev-a", "10.0.0.2")
        assert msg == "Added – position 1 in the queue."
        await proc.viewer_submit({"link": "https://www.tiktok.com/@a/video/2", "name": "Anna"}, "dev-a", "10.0.0.2")
        with pytest.raises(PluginRejected, match="already have 2 videos waiting"):
            await proc.viewer_submit({"link": "https://www.tiktok.com/@a/video/3", "name": "Anna"}, "dev-a", "10.0.0.2")
        with pytest.raises(PluginRejected, match="already in the queue"):
            await proc.viewer_submit({"link": "https://www.tiktok.com/@a/video/1", "name": "Ben"}, "dev-b", "10.0.0.3")
        with pytest.raises(PluginRejected, match="must be a link"):
            await proc.viewer_submit({"link": "not a link at all", "name": "Ben"}, "dev-b", "10.0.0.3")

        items = await proc.viewer_items()
        assert [(i["text"], i["detail"].split(" ·")[0]) for i in items] == [("Video 1", "by Anna"), ("Video 2", "by Anna")]
        await wait_until(lambda: proc.call("can_release"))  # nothing on air yet: releasable

        source = await proc.get_source()
        assert source["kind"] == "uri" and source["uri"].endswith(".mp4")
        meta = [e for e in events if e.get("event") == "metadata"][-1]["value"]
        assert (meta["title"], meta["submitter"]) == ("Video 1", "Anna")
        assert await proc.can_release() is False
        await proc.playback("ended")
        await asyncio.sleep(0.3)
        assert [e for e in events if e.get("event") == "next_source"]  # Video 2 follows
        assert [i["text"] for i in await proc.viewer_items()] == ["Video 2"]

        await proc.remove_item((await proc.viewer_items())[0]["id"])
        assert await proc.viewer_items() == []
        info = [e for e in events if e.get("event") == "info"][-1]["value"]
        assert info.startswith("0 waiting · yt-dlp")

    asyncio.run(with_plugin(tmp_path, {"per_device": 2}, steps))


def test_too_long_videos_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("LANSTREAMER_FAKE_DOWNLOADS", "1")  # fake videos are 10 s long

    async def steps(proc, events):
        with pytest.raises(PluginRejected, match="Too long: 10 s"):
            await proc.viewer_submit({"link": "https://example.com/v.mp4", "name": "A"}, "d", "1.2.3.4")

    asyncio.run(with_plugin(tmp_path, {"max_length_s": 5}, steps))


def test_the_queue_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("LANSTREAMER_FAKE_DOWNLOADS", "1")

    async def first(proc, events):
        await proc.viewer_submit({"link": "https://example.com/v1", "name": "A"}, "d", "ip")
        await asyncio.sleep(0.3)

    async def second(proc, events):
        return [i["text"] for i in await proc.viewer_items()]

    asyncio.run(with_plugin(tmp_path, {}, first))
    assert asyncio.run(with_plugin(tmp_path, {}, second)) == ["Video v1"]


def test_real_yt_dlp_downloads_a_plain_video_url(tmp_path):
    pytest.importorskip("yt_dlp")
    site = tmp_path / "site"
    site.mkdir()
    (site / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/clip.mp4"

    async def steps(proc, events):
        msg = await proc.viewer_submit({"link": url, "name": "CI"}, "dev", "127.0.0.1")
        assert msg.startswith("Added")
        await wait_until(lambda: proc.call("viewer_items"), timeout=5)
        for _ in range(100):
            items = await proc.viewer_items()
            if items and "loading" not in items[0]["detail"]:
                break
            await asyncio.sleep(0.1)
        source = await proc.get_source()
        return source

    try:
        source = asyncio.run(with_plugin(tmp_path, {"auto_update": False}, steps))
    finally:
        server.shutdown()
    assert source["uri"].startswith("file:") and source["uri"].endswith(".mp4")
