"""Core side of an output worker process (lanstreamer.media.output)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable
from typing import IO, Any

log = logging.getLogger("lanstreamer")


class WorkerError(RuntimeError):
    pass


class OutputWorker:
    """Starts the output worker and passes its events to `on_event(event)`."""

    def __init__(
        self,
        args: list[str],
        on_event: Callable[[dict[str, Any]], None],
        stderr: IO | int | None = None,
    ):
        self.args = args
        self.on_event = on_event
        self.stderr = stderr
        self.proc: asyncio.subprocess.Process | None = None
        self.ready = asyncio.Event()
        self.error = ""
        self._reader: asyncio.Task | None = None

    async def start(self, timeout_s: float = 30.0) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "lanstreamer.media.output", *self.args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.stderr,
        )  # fmt: skip
        self._reader = asyncio.create_task(self._read())
        waiter = asyncio.create_task(self.ready.wait())
        done, _ = await asyncio.wait({waiter, self._reader}, timeout=timeout_s, return_when="FIRST_COMPLETED")
        if waiter not in done or self.error:
            waiter.cancel()
            if self.proc.returncode is None:  # hung: do not leave it running
                self.proc.kill()
                await self.proc.wait()
            raise WorkerError(f"output worker did not start: {self.error or 'timeout'}")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def play(self, play_id: str, label: str, source: dict[str, Any]) -> None:
        await self._send({"cmd": "play", "id": play_id, "label": label, "source": source})

    async def clear(self) -> None:
        await self._send({"cmd": "clear"})

    async def overlay(self, title: str, subtitle: str, duration_s: float) -> None:
        await self._send({"cmd": "overlay", "title": title, "subtitle": subtitle, "duration_s": duration_s})

    async def debug_clock(self, on: bool) -> None:
        await self._send({"cmd": "debug_clock", "on": on})

    async def stop(self, timeout_s: float = 5.0) -> None:
        if not self.running:
            return
        assert self.proc is not None
        try:
            await self._send({"cmd": "stop"})
            await asyncio.wait_for(self.proc.wait(), timeout_s)
        except (TimeoutError, ConnectionError):
            self.proc.kill()
            await self.proc.wait()

    async def _send(self, message: dict[str, Any]) -> None:
        if not self.running:
            raise WorkerError(self.error or "output worker is not running")
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                log.info("worker: %s", raw.decode(errors="replace").rstrip())
                continue
            if event.get("event") == "ready":
                self.ready.set()
            elif event.get("event") == "error":
                self.error = event.get("message", "unknown error")
            self.on_event(event)
        self.on_event({"event": "exited", "message": self.error})
