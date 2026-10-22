"""The restream manager, with a small stand-in for the GStreamer process."""

import asyncio
import sys

from lanstreamer.core import restreamer as rs

LIVE = "import json, sys; print(json.dumps({'event': 'live'}), flush=True); sys.stdin.read()"
REFUSED = ("import json, sys; key = sys.argv[-1].rsplit('/', 1)[-1];"
           " print(json.dumps({'event': 'error', 'message': f'connection refused for {sys.argv[-1]} ({key})'}),"
           " flush=True); sys.exit(1)")  # fmt: skip
TARGET = "rtmp://live.example.com/app/secret-key-123"


def run(script, seconds, monkeypatch):
    monkeypatch.setattr(rs, "RETRY_S", (0.2,))
    states = []

    async def steps():
        r = rs.Restreamer("Output 1", "srt://source", TARGET, command=[sys.executable, "-c", script])
        r.on_change = lambda: states.append(r.state)
        r.start()
        await asyncio.sleep(seconds)
        await r.stop()
        return r

    return asyncio.run(steps()), states


def test_it_reports_live_and_stops(monkeypatch):
    r, states = run(LIVE, 1.5, monkeypatch)
    assert states[:2] == ["connecting", "live"] and r.failures == 0


def test_it_retries_and_never_shows_the_key(monkeypatch):
    r, states = run(REFUSED, 2.0, monkeypatch)
    retries = [s for s in states if s.startswith("retrying")]
    assert len(retries) >= 2 and r.failures >= 2
    assert "connection refused for rtmp://live.example.com/app/•••" in retries[0]
    assert all("secret-key-123" not in s for s in states)


def test_without_key():
    assert rs.without_key(TARGET) == "rtmp://live.example.com/app/•••"
    assert rs.without_key("rtmp://host/app") == "rtmp://host/app"
