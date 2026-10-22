"""OutputRuntime with real plugin processes and a fake output worker."""

import asyncio

import pytest

from lanstreamer.core import runtime as rt
from lanstreamer.core.config import InputConfig, OutputConfig
from lanstreamer.plugins.registry import discover


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(rt, "ACTIVITY_HOLD_S", 0.05)
    monkeypatch.setattr(rt, "TICK_S", 0.02)
    monkeypatch.setattr(rt, "RELEASE_POLL_S", 0.05)


class FakeWorker:
    """Answers every play with "playing" (or "switch_failed" for ids in `fail`)."""

    def __init__(self, fail=()):
        self.plays: list[str] = []
        self.clears = 0
        self.fail = set(fail)
        self.sources: list[dict] = []
        self.overlays: list[tuple] = []
        self.last_id = ""
        self.runtime = None

    async def play(self, play_id, label, source):
        self.plays.append(play_id.split("#")[0])
        self.sources.append(source)
        self.last_id = play_id
        event = "switch_failed" if play_id.split("#")[0] in self.fail else "playing"
        asyncio.get_running_loop().call_later(0.02, self.runtime.on_worker_event, {"event": event, "id": play_id})

    async def clear(self):
        self.clears += 1

    async def overlay(self, title, subtitle, duration_s):
        self.overlays.append((title, subtitle, duration_s))


def plugins():
    return {pid: m.path for pid, m in discover().items()}


async def run(cfg, tmp_path, seconds, worker=None, during=None):
    worker = worker or FakeWorker()
    runtime = rt.OutputRuntime(cfg, plugins(), tmp_path, worker)
    worker.runtime = runtime
    await runtime.start()
    loop = asyncio.create_task(runtime.run())
    try:
        if during:
            await during(runtime, worker)
        await asyncio.sleep(seconds)
    finally:
        await runtime.stop()
        loop.cancel()
    return runtime, worker


def pattern(id, priority=10, **kw):
    return InputConfig(id, id.upper(), "test-pattern", {}, priority=priority, **kw)


def test_equal_priority_inputs_take_turns(tmp_path):
    cfg = OutputConfig(inputs=[pattern("a", min_duration_s=0.3), pattern("b", min_duration_s=0.3)])
    _, worker = asyncio.run(run(cfg, tmp_path, 1.6))
    assert worker.plays[:3] == ["a", "b", "a"]


def test_start_waits_until_every_input_has_reported(tmp_path):
    # however the plugin processes happen to start, the low input never goes on air first
    cfg = OutputConfig(inputs=[pattern("low", 10), pattern("high", 20)])
    _, worker = asyncio.run(run(cfg, tmp_path, 0.8))
    assert worker.plays == ["high"]


def test_higher_priority_wins_and_stays(tmp_path):
    cfg = OutputConfig(inputs=[pattern("low", 10), pattern("high", 20)])
    runtime, worker = asyncio.run(run(cfg, tmp_path, 0.8))
    assert set(worker.plays) == {"high"}
    assert runtime.status()["current"] == "HIGH"


@pytest.mark.parametrize("poll_s", [0.05, 30.0])
def test_playlist_hands_over_when_its_item_ended(tmp_path, monkeypatch, poll_s):
    # with a slow release poll the answer must still be fresh when the item ended
    monkeypatch.setattr(rt, "RELEASE_POLL_S", poll_s)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")
    cfg = OutputConfig(inputs=[
        InputConfig("list", "Playlist", "playlist", {"items": [str(clip)]}),
        pattern("a"),
    ])  # fmt: skip

    async def end_playlist(runtime, worker):
        await asyncio.sleep(0.6)
        assert worker.plays == ["list"]  # the playlist keeps playing until its item ends
        runtime.on_worker_event({"event": "ended", "id": runtime.pending[0] if runtime.pending else "list#1"})

    _, worker = asyncio.run(run(cfg, tmp_path, 0.6, during=end_playlist))
    assert worker.plays[:2] == ["list", "a"]


def test_fallback_plays_when_nothing_else_is_active(tmp_path):
    cfg = OutputConfig(inputs=[
        InputConfig("empty", "Empty playlist", "playlist", {"items": []}, priority=10),
        pattern("standby", priority=0, fallback=True),
    ])  # fmt: skip
    _, worker = asyncio.run(run(cfg, tmp_path, 0.6))
    assert set(worker.plays) == {"standby"}


def test_failed_input_sits_out_and_the_next_one_plays(tmp_path):
    cfg = OutputConfig(inputs=[pattern("broken", 20), pattern("ok", 10)])
    runtime, worker = asyncio.run(run(cfg, tmp_path, 0.8, FakeWorker(fail={"broken"})))
    assert worker.plays[0] == "broken" and worker.plays[-1] == "ok"
    assert runtime.failed_switches == 1
    states = {i["id"]: i["state"] for i in runtime.status()["inputs"]}
    assert states == {"broken": "cooling down", "ok": "on air"}


def test_blocking_the_only_input_clears_the_output(tmp_path):
    cfg = OutputConfig(inputs=[pattern("only")])

    async def block(runtime, worker):
        await asyncio.sleep(0.4)
        runtime.inputs["only"].cfg.override = "block"

    runtime, worker = asyncio.run(run(cfg, tmp_path, 0.4, during=block))
    assert worker.plays == ["only"] and worker.clears == 1
    assert runtime.status()["current"] == ""


def test_apply_adds_removes_and_changes_inputs_while_running(tmp_path):
    cfg = OutputConfig(inputs=[pattern("a"), pattern("b", priority=5)])

    async def reconfigure(runtime, worker):
        await asyncio.sleep(0.4)
        assert worker.plays == ["a"]
        # a new, more important input takes over
        await runtime.apply(OutputConfig(inputs=[pattern("a"), pattern("b", priority=5), pattern("c", priority=20)]))
        await asyncio.sleep(0.5)
        assert worker.plays[-1] == "c"
        # removing it and raising b above a: b goes on air
        await runtime.apply(OutputConfig(inputs=[pattern("a"), pattern("b", priority=30)]))
        await asyncio.sleep(0.4)
        assert worker.plays[-1] == "b"
        assert set(runtime.inputs) == {"a", "b"}

    asyncio.run(run(cfg, tmp_path, 0.1, during=reconfigure))


def test_changed_settings_restart_the_plugin(tmp_path):
    cfg = OutputConfig(inputs=[pattern("a")])

    async def change(runtime, worker):
        await asyncio.sleep(0.3)
        old_proc = runtime.inputs["a"].proc
        new = pattern("a")
        new.settings = {"pattern": "ball"}
        await runtime.apply(OutputConfig(inputs=[new]))
        await asyncio.sleep(0.6)
        assert runtime.inputs["a"].proc is not old_proc and runtime.inputs["a"].proc.running

    asyncio.run(run(cfg, tmp_path, 0.1, during=change))


def test_instance_data_is_not_in_the_plugin_folder(tmp_path):
    cfg = OutputConfig(inputs=[pattern("a")])
    asyncio.run(run(cfg, tmp_path, 0.2))
    assert (tmp_path / "plugin-data" / "a").is_dir()
    assert not (tmp_path / "plugins").exists()


def test_a_playlist_on_its_own_plays_its_next_item_after_one_ended(tmp_path):
    clips = [tmp_path / "1.mp4", tmp_path / "2.mp4"]
    for clip in clips:
        clip.write_bytes(b"")
    cfg = OutputConfig(inputs=[InputConfig("list", "Playlist", "playlist", {"items": [str(c) for c in clips]})])

    async def end_item(runtime, worker):
        await asyncio.sleep(0.5)
        runtime.on_worker_event({"event": "ended", "id": worker.last_id})
        await asyncio.sleep(0.6)

    _, worker = asyncio.run(run(cfg, tmp_path, 0.1, during=end_item))
    assert worker.plays[:2] == ["list", "list"]
    assert [s["uri"].rsplit("/", 1)[-1] for s in worker.sources[:2]] == ["1.mp4", "2.mp4"]


def test_a_replaced_input_leaves_no_process_behind(tmp_path):
    cfg = OutputConfig(inputs=[pattern("a")])

    async def change(runtime, worker):
        await asyncio.sleep(0.3)
        old_input = runtime.inputs["a"]
        old = old_input.proc
        new = pattern("a")
        new.settings = {"pattern": "ball"}
        await runtime.apply(OutputConfig(inputs=[new]))
        await asyncio.sleep(rt.RESTART_BACKOFF_S[0] + 0.5)  # long enough for a wrong restart
        assert old_input.proc is old and not old.running  # not restarted as a "crash"
        assert runtime.inputs["a"].proc is not old and runtime.inputs["a"].proc.running
        assert runtime.inputs["a"].restarts == 0 and not runtime.inputs["a"].error

    asyncio.run(run(cfg, tmp_path, 0.1, during=change))


def test_the_lower_third_shows_what_is_on_air(tmp_path):
    cfg = OutputConfig(inputs=[pattern("a", 20), pattern("std", 0, fallback=True)])

    async def steps(runtime, worker):
        await asyncio.sleep(0.5)
        assert worker.overlays == [("Test pattern (smpte)", "A", 8.0)]  # its title, the input below
        inp = runtime.inputs["a"]
        meta = {"title": "Cat video", "channel": "@cats", "submitter": "Anna"}
        runtime._on_plugin_event(inp, {"event": "metadata", "value": meta})
        await asyncio.sleep(0.05)
        assert worker.overlays[-1] == ("Cat video", "A · @cats · submitted by Anna", 8.0)
        runtime._on_plugin_event(inp, {"event": "metadata", "value": meta})  # unchanged: not again
        await asyncio.sleep(0.05)
        assert len(worker.overlays) == 2

        await runtime.apply(OutputConfig(inputs=cfg.inputs, overlay="always"))
        await asyncio.sleep(0.05)
        assert worker.overlays[-1] == ("Cat video", "A · @cats · submitted by Anna", 0.0)
        runtime.on_air_kind = "card"  # an announcement card shows the title itself
        runtime._refresh_overlay(again=True)
        await asyncio.sleep(0.05)
        assert worker.overlays[-1][0] == ""
        runtime.on_air_kind = "uri"

        await runtime.apply(OutputConfig(inputs=cfg.inputs, overlay="off"))
        await asyncio.sleep(0.05)
        assert worker.overlays[-1][0] == ""
        await runtime.apply(OutputConfig(inputs=cfg.inputs, overlay="switch"))
        await asyncio.sleep(0.05)
        assert worker.overlays[-1] == ("Cat video", "A · @cats · submitted by Anna", 8.0)
        count = len(worker.overlays)
        await runtime.apply(OutputConfig(inputs=[pattern("a", 20, override="block"), cfg.inputs[1]]))
        await asyncio.sleep(0.6)  # Standby (the fallback) takes over: no lower third
        assert worker.plays[-1] == "std"
        assert worker.overlays[count:] == [("", "", 8.0)]

    asyncio.run(run(cfg, tmp_path, 0.1, during=steps))
