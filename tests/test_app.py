from pathlib import Path

from lanstreamer.app import App
from lanstreamer.core.config import demo_output


def test_demo_output_is_two_test_patterns_plus_a_playlist_per_source(tmp_path):
    clip = tmp_path / "clip.mp4"
    out = demo_output(60, 20, [str(clip), "https://example.com/live/clip-http.mp4"])
    assert [(i.name, i.plugin) for i in out.inputs] == [
        ("Test input A", "test-pattern"),
        ("Test input B", "test-pattern"),
        ("clip.mp4", "playlist"),
        ("clip-http.mp4", "playlist"),
    ]
    assert out.inputs[2].settings == {"items": [str(clip)]}
    assert {i.priority for i in out.inputs} == {10}
    assert out.inputs[0].min_duration_s == 20


class Args:
    smoke_test = 0
    simulate = True


def test_worker_events_update_status():
    app = App(Args(), [demo_output(60, 20, [])])
    out = app.first
    out._on_worker_event({"event": "stats", "fps": 59.8})
    out._on_worker_event({"event": "stream", "caps": "video/x-h264, profile=(string)constrained-baseline"})
    out._on_worker_event({"event": "ts_table", "pmt": "PMT v0: PID 65 H.264", "sent": True})
    assert (out.status.fps, out.status.ts_table) == (59.8, "PMT v0: PID 65 H.264")
    assert not app.failed.is_set()
    out._on_worker_event({"event": "exited", "message": "boom"})
    assert app.failed.is_set() and out.status.state == "failed"


def test_status_lists_the_inputs_and_the_outputs():
    second = demo_output(60, 5, [])
    second.id, second.name = "out2", "Output 2"
    app = App(Args(), [demo_output(30, 5, [str(Path("a.mp4"))]), second])
    status = app.status_dict()
    assert status["sources"] == ["Test input A", "Test input B", "a.mp4"]
    assert status["target_fps"] == 30 and status["whep_path"] == "out1"
    assert [o["id"] for o in status["outputs"]] == ["out1", "out2"]
    assert app.status_dict("out2")["whep_path"] == "out2"
    assert app.outputs["out2"].log_name == "output-out2.log" and app.first.log_name == "output.log"


def test_outputs_are_added_and_removed_while_running(tmp_path, monkeypatch):
    import asyncio

    from lanstreamer import paths
    from lanstreamer.core.config import OutputConfig
    from lanstreamer.core.store import Store

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    store = Store(tmp_path / "db")
    app = App(Args(), store.outputs(), store)

    async def steps():
        await app.start()
        added = store.add_output(OutputConfig(id="", name="Hall"))
        await app.apply_config(added.id)
        assert list(app.outputs) == ["out1", added.id]
        for _ in range(100):  # it starts in the background
            if app.outputs[added.id].status.state == "running":
                break
            await asyncio.sleep(0.05)
        assert app.outputs[added.id].status.state == "running"
        store.delete_output(added.id)
        await app.apply_config(added.id)
        assert list(app.outputs) == ["out1"]
        await app.stop()

    asyncio.run(steps())


def test_open_port_reports_a_port_in_use():
    import pytest

    from lanstreamer.app import open_port

    first = open_port(0)
    port = first.getsockname()[1]
    try:
        with pytest.raises(RuntimeError, match=f"port {port} is in use"):
            open_port(port)
    finally:
        first.close()


def test_the_debug_clock_is_off_and_can_be_turned_on_while_running(tmp_path, monkeypatch):
    import asyncio

    from lanstreamer import paths
    from lanstreamer.core.store import Store

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    store = Store(tmp_path / "db")
    app = App(Args(), store.outputs(), store)

    async def steps():
        await app.start()
        output = app.outputs["out1"]
        assert (output.worker.clock_on, output.status.debug_clock) == (False, False)
        store.update_output("out1", debug_clock=True)
        await app.apply_config("out1")
        assert (output.worker.clock_on, output.status.debug_clock) == (True, True)
        await app.stop()

    asyncio.run(steps())


def test_lan_streamer_ends_even_if_the_web_server_hangs_on_shutdown(tmp_path, monkeypatch):
    # CI 2026-10-26 (Windows): a reset client connection kept uvicorn waiting for ever
    import asyncio
    import time

    import uvicorn

    from lanstreamer import app as app_module
    from lanstreamer import paths
    from lanstreamer.core.store import Store

    async def hanging_serve(self, sockets=None):
        self.started = True
        await asyncio.Event().wait()  # never ends by itself, whatever should_exit says

    async def run_once(self, web=None):
        return 0

    monkeypatch.setattr(uvicorn.Server, "serve", hanging_serve)
    monkeypatch.setattr(app_module.App, "run", run_once)
    monkeypatch.setattr(app_module, "SHUTDOWN_S", 0.2)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

    class ServeArgs(Args):
        port = 0
        no_browser = True
        source = ()
        fps = None

    store = Store(tmp_path / "db")
    app = App(ServeArgs(), store.outputs(), store)
    started = time.monotonic()
    assert asyncio.run(app_module._serve(app, ServeArgs(), "http://localhost", None)) == 0
    assert time.monotonic() - started < 15
