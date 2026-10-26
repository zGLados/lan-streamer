"""Tests of the example plugin – run them with `pytest examples/web-stream`.

A small HTTP server in the test plays the stream and the status URL, so no
internet and no lan-streamer is needed.
"""

import asyncio
import http.server
import json
import threading

import pytest
from pydantic import ValidationError
from web_stream import Plugin, Settings

from lanstreamer_sdk import PlaybackEvent, UriSource
from lanstreamer_sdk.testing import make_context


@pytest.fixture
def server():
    """http://127.0.0.1:<port>/live.m3u8 and /status; `state` controls the answers."""
    state = {"online": True, "title": "Match 3", "channel": "Hall A"}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/status":
                body = json.dumps(state).encode()
            elif self.path == "/live.m3u8" and state["online"]:
                body = b"#EXTM3U\n"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", state
    httpd.shutdown()


async def started(tmp_path, **settings):
    ctx, seen = make_context(tmp_path)
    plugin = Plugin(ctx, Settings(**settings))
    await plugin.start()
    return plugin, seen


def test_active_while_the_stream_answers(server, tmp_path):
    base, state = server

    async def steps():
        plugin, seen = await started(tmp_path, url=f"{base}/live.m3u8", check_s=2)
        assert plugin.poll_interval_s == 2
        assert await plugin.is_active()
        assert seen.info[-1].startswith("online")
        assert await plugin.get_source() == UriSource(f"{base}/live.m3u8")
        state["online"] = False
        assert not await plugin.is_active()

    asyncio.run(steps())


def test_title_from_the_status_url_and_turns_on_equal_priority(server, tmp_path):
    base, state = server

    async def steps():
        plugin, seen = await started(tmp_path, url=f"{base}/live.m3u8", status_url=f"{base}/status",
                                     min_on_air_s=0)  # fmt: skip
        assert await plugin.is_active()
        await plugin.get_source()
        assert (seen.metadata[-1].title, seen.metadata[-1].channel) == ("Match 3", "Hall A")
        await plugin.on_playback(PlaybackEvent("started"))
        state["title"] = "Match 4"
        assert await plugin.is_active()
        assert seen.metadata[-1].title == "Match 4"  # updated while on air
        assert await plugin.can_release()  # min_on_air_s = 0

    asyncio.run(steps())


def test_stays_on_air_for_the_minimum_time(tmp_path):
    async def steps():
        plugin, _ = await started(tmp_path, url="rtsp://127.0.0.1:1/cam", min_on_air_s=60)
        assert await plugin.can_release()  # not on air
        await plugin.on_playback(PlaybackEvent("started"))
        assert not await plugin.can_release()
        await plugin.on_playback(PlaybackEvent("failed", "connection refused"))
        assert await plugin.can_release()

    asyncio.run(steps())


def test_unreachable_status_url_means_inactive(tmp_path):
    async def steps():
        plugin, seen = await started(tmp_path, url="https://example.invalid/a.m3u8",
                                     status_url="http://127.0.0.1:1/status")  # fmt: skip
        assert not await plugin.is_active()
        assert seen.info[-1].startswith("status URL:")

    asyncio.run(steps())


def test_settings_are_checked():
    with pytest.raises(ValidationError, match="must start with"):
        Settings(url="file:///etc/passwd")
    with pytest.raises(ValidationError):
        Settings(url="https://x/a.m3u8", check_s=0.5)
