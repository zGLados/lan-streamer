"""OutputRuntime with real plugin processes and a fake output worker."""

import asyncio

import pytest

from lanstreamer.core import runtime as rt
from lanstreamer.core.config import InputConfig, OutputConfig
from lanstreamer.plugins.process import PluginProcess
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


def test_an_input_removed_while_switching_to_it_does_not_block_the_output(tmp_path, monkeypatch):
    # CI 2026-10-26: the admin deleted an input while the core waited for its source;
    # the switch stayed "in progress" for ever and nothing went on air any more.
    real_get_source = PluginProcess.get_source
    asked, removed = asyncio.Event(), asyncio.Event()

    async def get_source(self):  # b's answer arrives only after b was removed
        source = await real_get_source(self)
        if self.instance_id == "b":
            asked.set()
            await removed.wait()
        return source

    monkeypatch.setattr(PluginProcess, "get_source", get_source)
    only_a = OutputConfig(inputs=[pattern("a", 10)])

    async def during(runtime, worker):
        await asyncio.sleep(0.5)
        await runtime.apply(OutputConfig(inputs=[pattern("a", 10), pattern("b", 20)]))
        await asyncio.wait_for(asked.wait(), 10)
        await runtime.apply(only_a)
        removed.set()
        await asyncio.sleep(0.4)
        await runtime.apply(OutputConfig(inputs=[pattern("a", 10), pattern("c", 30)]))

    _, worker = asyncio.run(run(only_a, tmp_path, 1.0, during=during))
    assert "b" not in worker.plays
    assert worker.plays[-1] == "c"


def test_an_input_removed_before_its_switch_completed_is_replaced(tmp_path):
    # removed after the play command, before the worker said "playing"
    class SlowWorker(FakeWorker):
        async def play(self, play_id, label, source):
            if play_id.startswith("b#"):
                self.plays.append("b")
                self.last_id = play_id
                return  # "playing" is sent by the test, after b was removed
            await super().play(play_id, label, source)

    only_a = OutputConfig(inputs=[pattern("a", 10)])

    async def during(runtime, worker):
        await asyncio.sleep(0.5)
        await runtime.apply(OutputConfig(inputs=[pattern("a", 10), pattern("b", 20)]))
        for _ in range(200):
            if "b" in worker.plays:
                break
            await asyncio.sleep(0.02)
        await runtime.apply(only_a)
        runtime.on_worker_event({"event": "playing", "id": worker.last_id})  # b's source on screen

    runtime, worker = asyncio.run(run(only_a, tmp_path, 0.6, during=during, worker=SlowWorker()))
    assert worker.plays == ["a", "b", "a"]  # a replaces the removed input's picture
    assert runtime.on_air is not None and runtime.on_air.id == "a"


def test_a_switch_the_worker_never_answers_gives_up_and_reports_the_worker(tmp_path, monkeypatch):
    # Windows CI 2026-10-27: the worker hung, the runtime waited for ever for "playing"
    monkeypatch.setattr(rt, "PENDING_TIMEOUT_S", 0.3)

    class MuteWorker(FakeWorker):
        async def play(self, play_id, label, source):
            if play_id.startswith("b#"):
                self.plays.append("b")
                return  # never answers
            await super().play(play_id, label, source)

    stuck = []
    cfg = OutputConfig(inputs=[pattern("a", 10)])

    async def during(runtime, worker):
        runtime.on_stuck = stuck.append
        await asyncio.sleep(0.4)
        await runtime.apply(OutputConfig(inputs=[pattern("a", 10), pattern("b", 20)]))
        await asyncio.sleep(0.8)
        # the app restarts the worker; the fresh one plays what should be on air
        fresh = FakeWorker()
        fresh.runtime = runtime
        runtime.replace_worker(fresh)
        await asyncio.sleep(0.3)
        assert fresh.plays == ["a"]  # b sits out its cooldown

    runtime, worker = asyncio.run(run(cfg, tmp_path, 0.1, worker=MuteWorker(), during=during))
    assert worker.plays == ["a", "b"]  # a stayed on air meanwhile
    assert len(stuck) == 1 and "no answer" in stuck[0]
    assert runtime.failed_switches == 1
    assert runtime.on_air is not None and runtime.on_air.id == "a"
