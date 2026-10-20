"""Core side of a plugin instance: starts the host process and talks to it."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable
from itertools import count
from pathlib import Path
from typing import Any

log = logging.getLogger("lanstreamer.plugins")


class PluginError(RuntimeError):
    pass


class PluginProcess:
    """One running plugin instance. Events arrive through `on_event(instance_id, event)`."""

    def __init__(
        self,
        instance_id: str,
        plugin_dir: Path,
        data_dir: Path,
        settings: dict[str, Any],
        on_event: Callable[[str, dict[str, Any]], None],
    ):
        self.instance_id = instance_id
        self.plugin_dir = plugin_dir
        self.data_dir = data_dir
        self.settings = settings
        self.on_event = on_event
        self.proc: asyncio.subprocess.Process | None = None
        self.ready = asyncio.Event()
        self.fatal: str | None = None
        self._ids = count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []

    async def start(self, timeout_s: float = 30.0) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "lanstreamer.plugins.host",
            "--plugin", str(self.plugin_dir), "--instance", self.instance_id,
            "--data-dir", str(self.data_dir), "--settings", json.dumps(self.settings),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]
        waiter = asyncio.create_task(self.ready.wait())
        done, _ = await asyncio.wait({waiter, self._tasks[0]}, timeout=timeout_s, return_when="FIRST_COMPLETED")
        if waiter not in done:
            waiter.cancel()
            await self.stop()
            raise PluginError(self.fatal or "plugin did not start")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def call(self, method: str, timeout_s: float = 30.0) -> Any:
        if not self.running:
            raise PluginError(self.fatal or "plugin is not running")
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"id": request_id, "method": method})
        try:
            return await asyncio.wait_for(future, timeout_s)
        finally:
            self._pending.pop(request_id, None)

    async def get_source(self) -> dict[str, Any]:
        return await self.call("get_source")

    async def can_release(self) -> bool:
        return bool(await self.call("can_release", timeout_s=5.0))

    async def playback(self, kind: str, detail: str = "") -> None:
        if self.running:
            await self._send({"method": "playback", "params": {"kind": kind, "detail": detail}})

    async def stop(self, timeout_s: float = 5.0) -> None:
        if self.running:
            assert self.proc is not None
            try:
                await self._send({"method": "stop"})
                await asyncio.wait_for(self.proc.wait(), timeout_s)
            except (TimeoutError, ConnectionError, BrokenPipeError):
                self.proc.kill()
                await self.proc.wait()
        for task in self._tasks:
            task.cancel()

    async def _send(self, message: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                log.info("[%s] %s", self.instance_id, raw.decode(errors="replace").rstrip())
                continue
            if "id" in message:
                future = self._pending.get(message["id"])
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(PluginError(message["error"]))
                    else:
                        future.set_result(message.get("result"))
                continue
            if message.get("event") == "ready":
                self.ready.set()
            elif message.get("event") == "fatal":
                self.fatal = message.get("message", "unknown error")
            self.on_event(self.instance_id, message)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(PluginError(self.fatal or "plugin process ended"))
        self.on_event(self.instance_id, {"event": "exited", "message": self.fatal or ""})

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for raw in self.proc.stderr:
            log.info("%s", raw.decode(errors="replace").rstrip())
