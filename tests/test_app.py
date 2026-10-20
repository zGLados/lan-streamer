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


def test_worker_events_update_status():
    app = App(Args(), demo_output(60, 20, []))
    app._on_worker_event({"event": "stats", "fps": 59.8})
    app._on_worker_event({"event": "stream", "caps": "video/x-h264, profile=(string)constrained-baseline"})
    app._on_worker_event({"event": "ts_table", "pmt": "PMT v0: PID 65 H.264", "sent": True})
    assert (app.status.fps, app.status.ts_table) == (59.8, "PMT v0: PID 65 H.264")
    assert not app.failed.is_set()
    app._on_worker_event({"event": "exited", "message": "boom"})
    assert app.failed.is_set() and app.status.state == "failed"


def test_status_lists_the_inputs():
    app = App(Args(), demo_output(30, 5, [str(Path("a.mp4"))]))
    assert app.status.sources == ["Test input A", "Test input B", "a.mp4"]
    assert app.status.target_fps == 30
