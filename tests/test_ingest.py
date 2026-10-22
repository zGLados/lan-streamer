"""The built-in RTMP/SRT ingest input, in a real plugin process, with a fake MediaMTX API."""

import asyncio
import http.server
import json
import threading
from typing import ClassVar

from lanstreamer.plugins.process import PluginProcess
from lanstreamer.plugins.registry import BUILTIN

PLUGIN = BUILTIN / "ingest"


class FakeMediaMTX(http.server.BaseHTTPRequestHandler):
    published: ClassVar[dict[str, list[str]]] = {}  # path → tracks

    def do_GET(self):
        name = self.path.removeprefix("/v3/paths/get/")
        if name not in self.published:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"name": name, "ready": True, "tracks": self.published[name]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_active_while_someone_publishes(tmp_path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMediaMTX)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{server.server_address[1]}"
    FakeMediaMTX.published = {}

    async def steps():
        events = []
        proc = PluginProcess("cam", PLUGIN, tmp_path / "data", {"key": "stage", "api": api},
                             lambda _i, e: events.append(e))  # fmt: skip

        def last(kind):
            return [e for e in events if e.get("event") == kind][-1]["value"]

        await proc.start()
        try:
            await asyncio.sleep(0.5)
            assert last("active") is False
            info = last("info")
            assert info.startswith("waiting for a stream")
            assert "rtmp://" in info and ":1935/ingest  key stage" in info
            assert "?streamid=publish:ingest/stage" in info

            FakeMediaMTX.published["ingest/stage"] = ["H264", "MPEG-4 Audio"]
            for _ in range(60):
                if last("active"):
                    break
                await asyncio.sleep(0.1)
            assert last("active") is True and last("info").startswith("receiving H264, MPEG-4 Audio")
            assert await proc.get_source() == {"kind": "uri", "uri": "rtspt://127.0.0.1:8554/ingest/stage"}
            assert await proc.can_release() is False  # stays on air while it is sent

            del FakeMediaMTX.published["ingest/stage"]
            for _ in range(60):
                if not last("active"):
                    break
                await asyncio.sleep(0.1)
            assert last("active") is False and await proc.can_release() is True
        finally:
            await proc.stop()

    try:
        asyncio.run(steps())
    finally:
        server.shutdown()


def test_a_key_is_made_up_and_kept(tmp_path):
    async def info_line():
        events = []
        proc = PluginProcess("cam", PLUGIN, tmp_path / "data", {"api": "http://127.0.0.1:9"},
                             lambda _i, e: events.append(e))  # fmt: skip
        await proc.start()
        try:
            # a refused connection takes ~2 s on Windows (it retries), so wait for the answer
            for _ in range(100):
                info = [e for e in events if e.get("event") == "info"][-1]["value"]
                if info.startswith("MediaMTX not reachable"):
                    return info
                await asyncio.sleep(0.1)
            return info
        finally:
            await proc.stop()

    first, second = asyncio.run(info_line()), asyncio.run(info_line())
    assert first.startswith("MediaMTX not reachable")
    key = first.split("key ", 1)[1].split(" ", 1)[0]
    assert len(key) == 8 and f"key {key}" in second
