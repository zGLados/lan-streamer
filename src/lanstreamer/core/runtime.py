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
PENDING_TIMEOUT_S = 30.0  # the worker answers a play within 15 s (+ fade); after this it hangs
OVERLAY_TITLE_MAX = 70
NO_OVERLAY = ("", "")


class Worker(Protocol):
    async def play(self, play_id: str, label: str, source: dict[str, Any]) -> None: ...
    async def clear(self) -> None: ...
    async def overlay(self, title: str, subtitle: str, duration_s: float) -> None: ...


@dataclass
class InputRuntime:
    cfg: InputConfig
    plugin_dir: Path | None
    proc: PluginProcess | None = None
    raw_active: bool = False
    reported: bool = False  # sent its first activity report
    wants_next: bool = False  # the plugin asked for its next item (ctx.next_source())
    retired: bool = False  # replaced or removed: its process's events are ignored
    info: str = ""  # the plugin's status line (ctx.set_info)
    viewer: dict[str, Any] | None = None  # its viewer form: {"title", "schema"} or None
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
            "title": self.metadata.get("title", ""), "info": self.info,
            "viewer_form": self.viewer is not None,
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
        on_stuck: Callable[[str], None] = lambda _reason: None,
    ):
        self.cfg = cfg
        self.plugins = plugins
        self.data_dir = data_dir
        self.worker = worker
        self.on_change = on_change
        self.clock = clock
        self.on_stuck = on_stuck  # the worker did not answer a switch: restart it
        self.inputs = {i.id: InputRuntime(i, plugins.get(i.plugin)) for i in cfg.inputs}
        self.on_air: OnAir | None = None
        self.pending: tuple[str, str] | None = None  # (play id, input id)
        self.pending_since = 0.0
        self.worker_ok = True  # False while the app restarts a hung or ended worker
        self.pending_kind = ""  # source kind of the pending play ("uri", "card", ...)
        self.on_air_kind = ""
        self.overlay_text: tuple[str, str] | None = NO_OVERLAY  # last sent; None: send anyway
        self.play_ids = count(1)
        self.switches = 0
        self.failed_switches = 0
        self.skip_requested = False
        self.viewer_version = 0  # changes when a viewer form or its items change
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
            try:
                await self.tick()
            except Exception:  # the output must keep switching
                log.exception("Output %s: scheduling failed", self.cfg.name)
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
            # a switch to it stays pending until the worker answers (on_worker_event)
            await self._retire(inp)
        updated: dict[str, InputRuntime] = {}
        for icfg in cfg.inputs:
            inp = self.inputs.get(icfg.id)
            if inp is None:
                inp = InputRuntime(icfg, self.plugins.get(icfg.plugin))
                log.info("Input added: %s", icfg.name)
                self._spawn(self._start_plugin(inp))
            elif (inp.cfg.plugin, inp.cfg.settings) != (icfg.plugin, icfg.settings):
                log.info("Input changed, restarting: %s", icfg.name)
                await self._retire(inp)
                inp = InputRuntime(icfg, self.plugins.get(icfg.plugin))
                self._spawn(self._start_plugin(inp))
            else:
                inp.cfg = icfg
            updated[icfg.id] = inp  # keeps the configured order (rotation, ties)
        self.inputs = updated
        overlay_changed = (cfg.overlay, cfg.overlay_s) != (self.cfg.overlay, self.cfg.overlay_s)
        self.cfg = cfg
        if overlay_changed:
            self.overlay_text = None  # mode or duration changed: send it with the new ones
            self._refresh_overlay(again=True)
        self.on_change()

    async def reload_plugin(self, plugin_id: str, plugins: dict[str, Path]) -> None:
        """A plugin was uploaded again or removed: restart the inputs that use it."""
        self.plugins = plugins
        for input_id, inp in list(self.inputs.items()):
            if inp.cfg.plugin != plugin_id:
                continue
            log.info("Plugin %s changed, restarting input %s", plugin_id, inp.cfg.name)
            await self._retire(inp)
            fresh = InputRuntime(inp.cfg, plugins.get(plugin_id))
            self.inputs[input_id] = fresh
            self._spawn(self._start_plugin(fresh))
        self.on_change()

    async def _retire(self, inp: InputRuntime) -> None:
        """Stops an input's process for good (its "exited" must not restart it)."""
        inp.retired = True
        if inp.proc:
            await inp.proc.stop()

    async def _start_plugin(self, inp: InputRuntime) -> None:
        if inp.plugin_dir is None:
            inp.error = f"plugin {inp.cfg.plugin!r} is not installed"
            log.error("Input %s: %s", inp.cfg.name, inp.error)
            return
        inp.proc = PluginProcess(
            inp.cfg.id, inp.plugin_dir, self.data_dir / "plugin-data" / inp.cfg.id,
            inp.cfg.settings, lambda _id, event: self._on_plugin_event(inp, event),
        )  # fmt: skip
        try:
            await inp.proc.start()
            inp.error = ""
            inp.viewer = await inp.proc.viewer_schema()
            if inp.viewer is not None:
                self.viewer_version += 1
                self.on_change()
        except (PluginError, TimeoutError) as exc:
            inp.error = str(exc)
            log.error("Input %s did not start: %s", inp.cfg.name, exc)

    def _restart_later(self, inp: InputRuntime) -> None:
        delay = RESTART_BACKOFF_S[min(inp.restarts, len(RESTART_BACKOFF_S) - 1)]
        inp.restarts += 1
        log.warning("Input %s stopped (%s); restarting in %s s", inp.cfg.name, inp.error, delay)

        async def restart() -> None:
            await asyncio.sleep(delay)
            if not self._stopped and not inp.retired and self.inputs.get(inp.cfg.id) is inp:
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
        if not self._is_settled(now) or not self.worker_ok:
            return
        await self._poll_release(now)
        if self.pending is not None and now - self.pending_since > PENDING_TIMEOUT_S:
            self._give_up_switch()
        if self.pending is not None:
            return  # a switch is in progress
        target = decide(self.states(now), self.on_air, now)
        self.skip_requested = False
        current = self.inputs.get(self.on_air.id) if self.on_air else None
        if current is not None and target == current.cfg.id and current.wants_next:
            current.wants_next = False  # it stays on air: play its next item
            await self._switch_to(current)
            return
        if target is None:
            if self.on_air is not None:
                log.info("No input active – showing the background")
                self.on_air = None
                self._send_overlay(NO_OVERLAY, again=False)
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
        """Asks the plugin on air whether it may hand over (only matters with peers).

        Asked at once when it wants its next item: a playlist whose item ended
        may hand over now, and deciding with the old answer would replay it.
        """
        if self.on_air is None:
            return
        inp = self.inputs.get(self.on_air.id)
        urgent = inp is not None and inp.wants_next and not inp.releasable
        if not urgent and now - self._last_release_poll < RELEASE_POLL_S:
            return
        self._last_release_poll = now
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
        if inp.retired or self.inputs.get(inp.cfg.id) is not inp:
            return  # removed or replaced while it answered: decide again on the next tick
        inp.wants_next = False
        play_id = f"{inp.cfg.id}#{next(self.play_ids)}"
        self.pending, self.pending_kind = (play_id, inp.cfg.id), str(source.get("kind", ""))
        self.pending_since = self.clock()
        inp.releasable = False  # until the plugin says otherwise
        await self.worker.play(play_id, inp.cfg.name, source)

    def _give_up_switch(self) -> None:
        """The worker never answered a play (CI 2026-10-27: its main loop hung).

        Counts it as a failed switch, so the scheduler goes on, and reports the
        worker as stuck (the app restarts it).
        """
        assert self.pending is not None
        play_id = self.pending[0]
        reason = f"no answer from the output worker within {PENDING_TIMEOUT_S:.0f} s"
        log.error("Switch to %s: %s", play_id, reason)
        self.on_worker_event({"event": "switch_failed", "id": play_id, "reason": reason})
        self.pending = None  # also if the input was removed meanwhile
        self.on_stuck(reason)

    def worker_lost(self) -> None:
        """The worker hung or ended: wait for replace_worker, send it nothing."""
        self.worker_ok, self.pending = False, None

    def replace_worker(self, worker: Worker) -> None:
        """A fresh worker after the old one hung or ended: it shows only the
        background, so whatever should be on air is played again on the next tick."""
        self.worker, self.worker_ok = worker, True
        self.pending, self.on_air, self.on_air_kind = None, None, ""
        self.overlay_text = NO_OVERLAY
        self.on_change()

    # --- overlay (lower third) ----------------------------------------------------

    def _overlay_for(self, inp: InputRuntime) -> tuple[str, str]:
        """Title and the line below it for the input on air; nothing for the
        fallback (Standby) and for announcement cards, which show it themselves."""
        if self.cfg.overlay == "off" or inp.cfg.fallback or self.on_air_kind == "card":
            return NO_OVERLAY
        meta = inp.metadata
        title = str(meta.get("title") or "")
        below = [inp.cfg.name] if title else []
        if meta.get("channel"):
            below.append(str(meta["channel"]))
        if meta.get("submitter"):
            below.append(f"submitted by {meta['submitter']}")
        title = title or inp.cfg.name
        if len(title) > OVERLAY_TITLE_MAX:
            title = title[: OVERLAY_TITLE_MAX - 1].rstrip() + "…"
        return title, " · ".join(below)

    def _refresh_overlay(self, again: bool) -> None:
        inp = self.inputs.get(self.on_air.id) if self.on_air else None
        self._send_overlay(self._overlay_for(inp) if inp else NO_OVERLAY, again)

    def _send_overlay(self, text: tuple[str, str], again: bool) -> None:
        """again: show it once more even if unchanged (a new switch)."""
        if not self.worker_ok:
            return  # the fresh worker gets it after its first switch
        if text == self.overlay_text and (not again or text == NO_OVERLAY or self.cfg.overlay == "always"):
            return
        self.overlay_text = text
        duration = 0.0 if self.cfg.overlay == "always" else self.cfg.overlay_s
        self._spawn(self.worker.overlay(text[0], text[1], duration))

    # --- events -------------------------------------------------------------------

    def _input_of(self, play_id: str) -> InputRuntime | None:
        return self.inputs.get(play_id.split("#", 1)[0])

    def on_worker_event(self, event: dict[str, Any]) -> None:
        kind, play_id = event.get("event"), str(event.get("id", ""))
        inp = self._input_of(play_id) if play_id else None
        if inp is None and self.pending and self.pending[0] == play_id and kind in ("playing", "switch_failed"):
            # the input was removed during its switch: end the switch; if its source is
            # on screen now, count it as on air so the next tick replaces or clears it
            self.pending = None
            if kind == "playing":
                self.on_air = OnAir(play_id.split("#", 1)[0], self.clock())
            self.on_change()
            return
        if kind == "playing" and self.pending and self.pending[0] == play_id and inp:
            self.pending = None
            self.on_air = OnAir(inp.cfg.id, self.clock())
            self.switches += 1
            log.info("On air: %s", inp.cfg.name)
            self._playback(inp, "started")
            self.on_air_kind = self.pending_kind
            self._refresh_overlay(again=True)
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

    def _on_plugin_event(self, inp: InputRuntime, event: dict[str, Any]) -> None:
        if inp.retired or self.inputs.get(inp.cfg.id) is not inp:
            return  # an old process of a replaced or removed input
        kind = event.get("event")
        if kind == "active":
            inp.raw_active = bool(event["value"])
            if not inp.reported:  # the first report counts at once, later ones are debounced
                inp.reported = True
                inp.debounce = Debounce(ACTIVITY_HOLD_S, initial=inp.raw_active)
        elif kind == "metadata":
            inp.metadata = event.get("value", {})
            if self.on_air is not None and self.on_air.id == inp.cfg.id:
                self._refresh_overlay(again=False)  # e.g. a live stream's new title
        elif kind == "next_source":
            inp.wants_next = True
        elif kind == "info":
            inp.info = str(event.get("value", ""))
        elif kind == "changed":
            self.viewer_version += 1
        elif kind == "fatal":
            inp.error = event.get("message", "plugin error")
        elif kind == "exited" and not self._stopped:
            inp.error = inp.error or event.get("message") or "plugin process ended"
            inp.raw_active = False
            self._restart_later(inp)
        self.on_change()

    # --- viewer forms (F-IN-04) -------------------------------------------------------

    def _viewer_input(self, input_id: str) -> InputRuntime:
        inp = self.inputs.get(input_id)
        if inp is None or inp.viewer is None or inp.proc is None or not inp.proc.running:
            raise KeyError(input_id)
        return inp

    async def viewer_forms(self) -> list[dict[str, Any]]:
        """The forms of all inputs that have one, with their current items."""
        forms = []
        for inp in self.inputs.values():
            if inp.viewer is None or inp.proc is None or not inp.proc.running:
                continue
            try:
                items = await inp.proc.viewer_items()
            except (PluginError, TimeoutError):
                items = []
            forms.append({
                "input_id": inp.cfg.id, "name": inp.cfg.name,
                "title": inp.viewer.get("title") or inp.cfg.name,
                "schema": inp.viewer.get("schema", {}), "items": items,
            })  # fmt: skip
        return forms

    async def viewer_submit(self, input_id: str, form: dict[str, Any], device: str, ip: str) -> str:
        """Raises KeyError (no such form) or PluginRejected (message for the viewer)."""
        return await self._viewer_input(input_id).proc.viewer_submit(form, device, ip)

    async def items(self, input_id: str) -> list[dict[str, Any]]:
        return await self._viewer_input(input_id).proc.viewer_items()

    async def remove_item(self, input_id: str, item_id: str) -> None:
        await self._viewer_input(input_id).proc.remove_item(item_id)
        self.viewer_version += 1
        self.on_change()

    # --- status for the web page --------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = self.clock()
        on_air_id = self.on_air.id if self.on_air else None
        on_air = self.inputs[on_air_id].cfg.name if on_air_id in self.inputs else ""
        meta = self.inputs[on_air_id].metadata if on_air_id in self.inputs else {}
        return {
            "current": on_air,
            "now": {key: meta.get(key) for key in ("title", "channel", "submitter", "thumbnail")},
            "viewer_version": self.viewer_version,
            "switches": self.switches,
            "failed_switches": self.failed_switches,
            "inputs": [i.status(on_air_id, now) for i in self.inputs.values()],
        }
