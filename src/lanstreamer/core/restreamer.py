"""Core side of a restream (lanstreamer.media.restream): keeps it running.

One process per output with a restream URL. When it fails (server down,
wrong key, network gone) it is started again after a growing pause; the
state is shown on the admin page.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from collections.abc import Callable
from typing import IO, Any

log = logging.getLogger("lanstreamer")

RETRY_S = (5, 10, 30, 60)


def without_key(url: str) -> str:
    """The URL for logs and the page: the stream key (last path part) hidden."""
    return re.sub(r"/[^/]+$", "/•••", url) if url.count("/") > 3 else url


class Restreamer:
    def __init__(
        self,
        name: str,
        source: str,
        target: str,
        on_change: Callable[[], None] = lambda: None,
        stderr: IO | int | None = None,
        command: list[str] | None = None,  # tests: a stand-in for the GStreamer process
    ):
        self.name = name
        self.source = source
        self.target = target
        self.on_change = on_change
        self.stderr = stderr
        self.command = command or [sys.executable, "-m", "lanstreamer.media.restream"]
        self.state = "starting"
        self.failures = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        log.info("Restream of %s to %s", self.name, without_key(self.target))
        self._task = asyncio.create_task(self._keep_running())

    async def stop(self) -> None:
        self._stopped = True
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _set(self, state: str) -> None:
        self.state = state
        self.on_change()

    async def _keep_running(self) -> None:
        while not self._stopped:
            self._set("connecting")
            error = await self._run_once()
            if self._stopped:
                return
            delay = RETRY_S[min(self.failures, len(RETRY_S) - 1)]
            self.failures += 1
            log.warning("Restream of %s stopped (%s) – again in %d s", self.name, error, delay)
            self._set(f"retrying in {delay} s: {error}")
            await asyncio.sleep(delay)

    async def _run_once(self) -> str:
        self._proc = await asyncio.create_subprocess_exec(
            *self.command, "--source", self.source, "--target", self.target,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.stderr,
        )  # fmt: skip
        error = "it stopped"
        assert self._proc.stdout is not None
        async for line in self._proc.stdout:
            try:
                event: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "live":
                self.failures = 0
                log.info("Restream of %s is live", self.name)
                self._set("live")
            elif event.get("event") == "error":
                error = str(event.get("message") or error)
        await self._proc.wait()
        return self._hide_key(error)

    def _hide_key(self, text: str) -> str:
        """Error texts go to the public status: never with the stream key."""
        key = self.target.rstrip("/").rsplit("/", 1)[-1]
        text = text.replace(self.target, without_key(self.target))
        return text.replace(key, "•••") if len(key) >= 6 else text
