"""Simulated output worker for local tests without GStreamer (python -m lanstreamer --simulate).

Behaves like the real worker towards the core: answers "play" with "playing"
after a short fade, reports "ended" when a file or URL source would end, sends
stats and handles "clear". Everything else – web server, admin page, API,
scheduler, plugin processes – runs for real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

FADE_S = 0.3
SIM_CLIP_S = 10.0  # how long a simulated file or URL plays before it "ends"


class SimulatedWorker:
    def __init__(self, on_event: Callable[[dict[str, Any]], None], fps: int = 60):
        self.on_event = on_event
        self.fps = fps
        self.running = False
        self._tasks: set[asyncio.Task] = set()
        self._current: str | None = None
        self.overlay_text = ("", "")
        self.clock_on = False

    async def start(self) -> None:
        self.running = True
        self._spawn(self._stats())
        self.on_event({"event": "ready", "encoder": "simulated"})
        self.on_event({"event": "stream", "caps": "video/x-h264, profile=(string)simulated, level=(string)–"})

    async def play(self, play_id: str, label: str, source: dict[str, Any]) -> None:
        async def go() -> None:
            await asyncio.sleep(FADE_S)
            self._current = play_id
            self.on_event({"event": "playing", "id": play_id, "label": label})
            if source.get("kind") == "uri":
                await asyncio.sleep(SIM_CLIP_S)
                if self._current == play_id:
                    self.on_event({"event": "ended", "id": play_id})

        self._spawn(go())

    async def clear(self) -> None:
        self._current = None
        self.on_event({"event": "cleared"})

    async def overlay(self, title: str, subtitle: str, duration_s: float) -> None:
        self.overlay_text = (title, subtitle)

    async def debug_clock(self, on: bool) -> None:
        self.clock_on = on

    async def stop(self) -> None:
        self.running = False
        for task in list(self._tasks):
            task.cancel()

    async def _stats(self) -> None:
        while self.running:
            await asyncio.sleep(2)
            self.on_event({"event": "stats", "fps": float(self.fps)})

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
