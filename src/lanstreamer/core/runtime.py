"""Runs one output: its input plugins, the scheduler and the output worker.

Every few hundred milliseconds the runtime asks the scheduler which input should
be on air (scheduler.decide) and, if that changed, asks the input's plugin for
its source and tells the output worker to switch. Playback events from the
worker (started, ended, failed) go back to the plugin.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any, Protocol

from lanstreamer.core.config import InputConfig, OutputConfig
from lanstreamer.plugins.process import PluginError, PluginProcess
from lanstreamer.scheduler import Debounce, InputState, OnAir, decide

log = logging.getLogger("lanstreamer")

TICK_S = 0.25
ACTIVITY_HOLD_S = 1.0  # an activity change must hold this long (Debounce)
FAILED_COOLDOWN_S = 30.0  # an input whose source failed sits out this long
RELEASE_POLL_S = 1.0
RESTART_BACKOFF_S = (2, 5, 15, 60)
STARTUP_GRACE_S = 5.0  # wait at most this long for every input's first activity report


class Worker(Protocol):
    async def play(self, play_id: str, label: str, source: dict[str, Any]) -> None: ...
    async def clear(self) -> None: ...


@dataclass
class InputRuntime:
    cfg: InputConfig
    plugin_dir: Path | None
    proc: PluginProcess | None = None
    raw_active: bool = False
    reported: bool = False  # sent its first activity report
    debounce: Debounce = field(default_factory=lambda: Debounce(ACTIVITY_HOLD_S))
    releasable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    cooldown_until: float = 0.0
    restarts: int = 0

    def status(self, on_air_id: str | None, now: float) -> dict[str, Any]:
        if self.cfg.id == on_air_id:
            state = "on air"
        elif self.error:
            state = "error"
        elif now < self.cooldown_until:
            state = "cooling down"
        else:
            state = "active" if (self.debounce.value or self.cfg.fallback) else "inactive"
        return {
            "id": self.cfg.id, "name": self.cfg.name, "plugin": self.cfg.plugin,
            "priority": self.cfg.priority, "fallback": self.cfg.fallback,
            "override": self.cfg.override, "state": state, "error": self.error,
            "title": self.metadata.get("title", ""),
        }  # fmt: skip


class OutputRuntime:
    def __init__(
        self,
        cfg: OutputConfig,
        plugins: dict[str, Path],
        data_dir: Path,
        worker: Worker,
        on_change: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg
        self.plugins = plugins
        self.data_dir = data_dir
        self.worker = worker
        self.on_change = on_change
        self.clock = clock
        self.inputs = {i.id: InputRuntime(i, plugins.get(i.plugin)) for i in cfg.inputs}
        self.on_air: OnAir | None = None
        self.pending: tuple[str, str] | None = None  # (play id, input id)
        self.play_ids = count(1)
        self.switches = 0
        self.failed_switches = 0
        self.skip_requested = False
        self._last_release_poll = 0.0
        self._started_at: float | None = None
        self._settled = False  # every input has reported once (or the grace time is over)
        self._tasks: set[asyncio.Task] = set()
        self._stopped = False

    # --- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        await asyncio.gather(*(self._start_plugin(i) for i in self.inputs.values()))

    async def run(self) -> None:
        while not self._stopped:
            await self.tick()
            await asyncio.sleep(TICK_S)

    async def stop(self) -> None:
        self._stopped = True
        await asyncio.gather(
            *(i.proc.stop() for i in self.inputs.values() if i.proc), return_exceptions=True
        )

    async def apply(self, cfg: OutputConfig, plugins: dict[str, Path] | None = None) -> None:
        """Takes over a changed configuration while running (admin changes).

        New inputs start, removed ones stop, inputs whose plugin or settings
        changed restart; everything else (name, priority, override, durations)
        changes in place. Output settings (format, encoder) need a restart.
        """
        if plugins is not None:
            self.plugins = plugins
        wanted = {i.id: i for i in cfg.inputs}
        for input_id in [i for i in self.inputs if i not in wanted]:
            inp = self.inputs.pop(input_id)
            log.info("Input removed: %s", inp.cfg.name)
            if self.pending and self.pending[1] == input_id:
                self.pending = None
            if inp.proc:
                await inp.proc.stop()
        updated: dict[str, InputRuntime] = {}
        for icfg in cfg.inputs:
            inp = self.inputs.get(icfg.id)
            if inp is None:
                inp = InputRuntime(icfg, self.plugins.get(icfg.plugin))
                log.info("Input added: %s", icfg.name)
                self._spawn(self._start_plugin(inp))
            elif (inp.cfg.plugin, inp.cfg.settings) != (icfg.plugin, icfg.settings):
                log.info("Input changed, restarting: %s", icfg.name)
                if inp.proc:
                    await inp.proc.stop()
                inp = InputRuntime(icfg, self.plugins.get(icfg.plugin))
                self._spawn(self._start_plugin(inp))
            else:
                inp.cfg = icfg
            updated[icfg.id] = inp  # keeps the configured order (rotation, ties)
        self.inputs = updated
        self.cfg = cfg
        self.on_change()

    async def _start_plugin(self, inp: InputRuntime) -> None:
        if inp.plugin_dir is None:
            inp.error = f"plugin {inp.cfg.plugin!r} is not installed"
            log.error("Input %s: %s", inp.cfg.name, inp.error)
            return
        inp.proc = PluginProcess(
            inp.cfg.id, inp.plugin_dir, self.data_dir / "plugin-data" / inp.cfg.id,
            inp.cfg.settings, self._on_plugin_event,
        )  # fmt: skip
        try:
            await inp.proc.start()
            inp.error = ""
        except PluginError as exc:
            inp.error = str(exc)
            log.error("Input %s did not start: %s", inp.cfg.name, exc)

    def _restart_later(self, inp: InputRuntime) -> None:
        delay = RESTART_BACKOFF_S[min(inp.restarts, len(RESTART_BACKOFF_S) - 1)]
        inp.restarts += 1
        log.warning("Input %s stopped (%s); restarting in %s s", inp.cfg.name, inp.error, delay)

        async def restart() -> None:
            await asyncio.sleep(delay)
            if not self._stopped:
                await self._start_plugin(inp)

        self._spawn(restart())

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- the scheduling loop ------------------------------------------------------

    def skip(self) -> None:
        """The "Next input" button: the current input hands over now, as if released."""
        self.skip_requested = True

    def states(self, now: float) -> list[InputState]:
        result = []
        for inp in self.inputs.values():
            running = inp.proc is not None and inp.proc.running
            usable = running and now >= inp.cooldown_until
            current = self.on_air is not None and self.on_air.id == inp.cfg.id
            skip = current and self.skip_requested
            result.append(InputState(
                id=inp.cfg.id, priority=inp.cfg.priority,
                active=usable and inp.debounce.value,
                fallback=inp.cfg.fallback and usable,
                min_duration_s=0.0 if skip else inp.cfg.min_duration_s,
                max_duration_s=inp.cfg.max_duration_s,
                releasable=True if skip else inp.releasable,
                override=inp.cfg.override,
            ))  # fmt: skip
        return result

    async def tick(self) -> None:
        now = self.clock()
        for inp in self.inputs.values():
            inp.debounce.update(inp.raw_active, now)
        if not self._is_settled(now):
            return
        await self._poll_release(now)
        if self.pending is not None:
            return  # a switch is in progress
        target = decide(self.states(now), self.on_air, now)
        self.skip_requested = False
        if target is None:
            if self.on_air is not None:
                log.info("No input active – showing the background")
                self.on_air = None
                await self.worker.clear()
                self.on_change()
        elif self.on_air is None or target != self.on_air.id:
            await self._switch_to(self.inputs[target])

    def _is_settled(self, now: float) -> bool:
        """At start, decide only when every input has said whether it is active.

        Otherwise the input whose plugin process happens to start first would go
        on air for a moment before a more important one takes over.
        """
        if self._settled:
            return True
        if self._started_at is None:
            self._started_at = now
        running = [i for i in self.inputs.values() if i.proc is not None and i.proc.running]
        if all(i.reported for i in running) or now - self._started_at >= STARTUP_GRACE_S:
            self._settled = True
        return self._settled

    async def _poll_release(self, now: float) -> None:
        """Asks the plugin on air whether it may hand over (only matters with peers)."""
        if self.on_air is None or now - self._last_release_poll < RELEASE_POLL_S:
            return
        self._last_release_poll = now
        inp = self.inputs.get(self.on_air.id)
        if inp and inp.proc and inp.proc.running:
            try:
                inp.releasable = await inp.proc.can_release()
            except (PluginError, TimeoutError):
                inp.releasable = True

    async def _switch_to(self, inp: InputRuntime) -> None:
        assert inp.proc is not None
        try:
            source = await inp.proc.get_source()
        except (PluginError, TimeoutError) as exc:
            log.warning("Input %s has no source: %s", inp.cfg.name, exc)
            inp.cooldown_until = self.clock() + FAILED_COOLDOWN_S
            self.failed_switches += 1
            return
        play_id = f"{inp.cfg.id}#{next(self.play_ids)}"
        self.pending = (play_id, inp.cfg.id)
        inp.releasable = False  # until the plugin says otherwise
        await self.worker.play(play_id, inp.cfg.name, source)

    # --- events -------------------------------------------------------------------

    def _input_of(self, play_id: str) -> InputRuntime | None:
        return self.inputs.get(play_id.split("#", 1)[0])

    def on_worker_event(self, event: dict[str, Any]) -> None:
        kind, play_id = event.get("event"), str(event.get("id", ""))
        inp = self._input_of(play_id) if play_id else None
        if kind == "playing" and self.pending and self.pending[0] == play_id and inp:
            self.pending = None
            self.on_air = OnAir(inp.cfg.id, self.clock())
            self.switches += 1
            log.info("On air: %s", inp.cfg.name)
            self._playback(inp, "started")
        elif kind == "switch_failed" and inp:
            if self.pending and self.pending[0] == play_id:
                self.pending = None
            self.failed_switches += 1
            inp.cooldown_until = self.clock() + FAILED_COOLDOWN_S
            log.warning("Switch to %s failed: %s", inp.cfg.name, event.get("reason", ""))
            self._playback(inp, "failed", event.get("reason", ""))
        elif kind == "ended" and inp:
            log.info("%s: the source ended", inp.cfg.name)
            self._playback(inp, "ended")
        elif kind == "failed" and inp:
            log.warning("%s: the source broke: %s", inp.cfg.name, event.get("reason", ""))
            inp.cooldown_until = self.clock() + FAILED_COOLDOWN_S
            self._playback(inp, "failed", event.get("reason", ""))
        else:
            return
        self.on_change()

    def _playback(self, inp: InputRuntime, kind: str, detail: str = "") -> None:
        if inp.proc is not None:
            self._spawn(inp.proc.playback(kind, detail))

    def _on_plugin_event(self, instance_id: str, event: dict[str, Any]) -> None:
        inp = self.inputs.get(instance_id)
        if inp is None:
            return
        kind = event.get("event")
        if kind == "active":
            inp.raw_active = bool(event["value"])
            if not inp.reported:  # the first report counts at once, later ones are debounced
                inp.reported = True
                inp.debounce = Debounce(ACTIVITY_HOLD_S, initial=inp.raw_active)
        elif kind == "metadata":
            inp.metadata = event.get("value", {})
        elif kind == "fatal":
            inp.error = event.get("message", "plugin error")
        elif kind == "exited" and not self._stopped:
            inp.error = inp.error or event.get("message") or "plugin process ended"
            inp.raw_active = False
            self._restart_later(inp)
        self.on_change()

    # --- status for the web page --------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = self.clock()
        on_air_id = self.on_air.id if self.on_air else None
        on_air = self.inputs[on_air_id].cfg.name if on_air_id in self.inputs else ""
        return {
            "current": on_air,
            "switches": self.switches,
            "failed_switches": self.failed_switches,
            "inputs": [i.status(on_air_id, now) for i in self.inputs.values()],
        }
