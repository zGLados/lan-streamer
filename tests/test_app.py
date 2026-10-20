from pathlib import Path

from lanstreamer.app import App, demo_sources


def test_demo_sources_are_two_test_inputs_plus_extras(tmp_path):
    clip = tmp_path / "clip.mp4"
    sources = demo_sources([str(clip), "https://example.com/live.m3u8"])
    assert [label for label, _ in sources][:2] == ["Test input A", "Test input B"]
    assert sources[2] == ("clip.mp4", {"kind": "uri", "uri": Path(clip).as_uri()})
    assert sources[3][1] == {"kind": "uri", "uri": "https://example.com/live.m3u8"}


class Args:
    fps, width, height, source = 60, 1920, 1080, []


def test_worker_events_update_status():
    app = App(Args())
    app._on_event({"event": "ready"})
    app._on_event({"event": "playing", "label": "Test input A"})
    app._on_event({"event": "switch_failed", "label": "x", "reason": "no picture"})
    app._on_event({"event": "stats", "fps": 59.8})
    assert app.ready.is_set() and not app.failed.is_set()
    assert (app.status.current, app.status.switches, app.status.failed_switches) == ("Test input A", 1, 1)
    assert app.status.fps == 59.8
    app._on_event({"event": "error", "message": "boom"})
    assert app.failed.is_set() and app.status.last_error == "boom"
