"""Tests of the example plugin – run them with `pytest examples/stream-queue`.

They call the plugin directly with a test context (lanstreamer_sdk.testing); no
lan-streamer, GStreamer or network is needed.
"""

import asyncio

import pytest
from stream_queue import LinkForm, Plugin, Settings

from lanstreamer_sdk import PlaybackEvent, Rejected, UriSource, Viewer
from lanstreamer_sdk.testing import make_context

ANNA = Viewer(device="device-anna", ip="192.168.1.20")


def run(coro):
    return asyncio.run(coro)


async def started(tmp_path, **settings):
    ctx, seen = make_context(tmp_path)
    plugin = Plugin(ctx, Settings(**settings))
    await plugin.start()
    return plugin, seen


def test_queued_links_play_in_order(tmp_path):
    async def steps():
        plugin, seen = await started(tmp_path)
        assert not await plugin.is_active()
        await plugin.on_viewer_submit(LinkForm(url="https://example.org/a.mp4", name="Anna"), ANNA)
        await plugin.on_viewer_submit(LinkForm(url="srt://192.168.1.5:9000", title="Camera"), ANNA)
        assert await plugin.is_active()

        assert await plugin.get_source() == UriSource("https://example.org/a.mp4")
        assert seen.metadata[-1].submitter == "Anna"
        assert not await plugin.can_release()  # not in the middle of a link

        await plugin.on_playback(PlaybackEvent("ended"))
        assert seen.next_requests == 1  # stays on air with the next link
        assert await plugin.can_release()
        assert await plugin.get_source() == UriSource("srt://192.168.1.5:9000")
        assert seen.metadata[-1].title == "Camera"

        await plugin.on_playback(PlaybackEvent("failed", "connection refused"))
        assert not await plugin.is_active()
        assert seen.next_requests == 1
        assert seen.info[-1] == "0 waiting"

    run(steps())


def test_activity_is_pushed_when_the_queue_changes(tmp_path):
    async def steps():
        plugin, _ = await started(tmp_path)
        activity = plugin.watch_active()
        assert await anext(activity) is False
        await plugin.on_viewer_submit(LinkForm(url="https://example.org/a.mp4"), ANNA)
        assert await asyncio.wait_for(anext(activity), 1) is True

    run(steps())


@pytest.mark.parametrize(
    "url, settings, message",
    [
        ("file:///etc/passwd", {}, "http"),
        ("https://example.org/b.mp4", {"max_queue": 1}, "full"),
        ("https://example.org/b.mp4", {"per_device": 1}, "already have 1"),
    ],
)
def test_submissions_are_limited(tmp_path, url, settings, message):
    async def steps():
        plugin, _ = await started(tmp_path, **settings)
        await plugin.on_viewer_submit(LinkForm(url="https://example.org/a.mp4"), ANNA)
        with pytest.raises(Rejected, match=message):
            await plugin.on_viewer_submit(LinkForm(url=url), ANNA)

    run(steps())


def test_the_queue_survives_a_restart_and_the_admin_can_remove_links(tmp_path):
    async def steps():
        plugin, _ = await started(tmp_path)
        await plugin.on_viewer_submit(LinkForm(url="https://example.org/a.mp4", name="Anna"), ANNA)
        await plugin.on_viewer_submit(LinkForm(url="https://example.org/b.mp4"), ANNA)

        again, _ = await started(tmp_path)  # same data folder = same store
        items = await again.viewer_items()
        assert [(i.text, i.detail) for i in items] == [
            ("https://example.org/a.mp4", "by Anna"), ("https://example.org/b.mp4", ""),
        ]  # fmt: skip
        await again.remove_item(items[0].id)
        assert [i.text for i in await again.viewer_items()] == ["https://example.org/b.mp4"]

    run(steps())
