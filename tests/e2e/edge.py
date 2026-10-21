"""Use the Microsoft Edge that is installed on Windows for browser tests from WSL.

Nothing is installed: Edge runs headless with its own throwaway profile in the
Windows temp folder and is driven over the Chrome DevTools Protocol. WSL cannot
reach Windows' loopback, so each DevTools connection is relayed through a
small PowerShell process (stdin/stdout ⇄ TCP on Windows' 127.0.0.1).

    with windows_edge() as ws_endpoint:
        browser = playwright.chromium.connect_over_cdp(ws_endpoint)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

EDGE = Path("/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
EDGE_PORT = 9333  # DevTools port of Edge on Windows

RELAY_PS = f"""
$c = New-Object System.Net.Sockets.TcpClient('127.0.0.1', {EDGE_PORT})
$n = $c.GetStream()
$i = [Console]::OpenStandardInput(); $o = [Console]::OpenStandardOutput()
$a = $i.CopyToAsync($n); $b = $n.CopyToAsync($o)
[void][System.Threading.Tasks.Task]::WaitAny(@($a, $b))
$c.Close()
"""


def available() -> bool:
    return EDGE.exists() and shutil.which("powershell.exe") is not None


async def _relay_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    proc = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", RELAY_PS,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip

    async def pump(src, dst_write, dst_drain, close) -> None:
        try:
            while data := await src.read(65536):
                dst_write(data)
                await dst_drain()
        except (ConnectionError, OSError):
            pass
        finally:
            close()

    assert proc.stdin and proc.stdout
    await asyncio.gather(
        pump(reader, proc.stdin.write, proc.stdin.drain, proc.stdin.close),
        pump(proc.stdout, writer.write, writer.drain, writer.close),
    )
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


def _start_relay() -> tuple[int, asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    started = threading.Event()
    port_box: list[int] = []

    def run() -> None:
        asyncio.set_event_loop(loop)
        server = loop.run_until_complete(asyncio.start_server(_relay_connection, "127.0.0.1", 0))
        port_box.append(server.sockets[0].getsockname()[1])
        started.set()
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    started.wait(10)
    return port_box[0], loop


def _windows_temp() -> tuple[str, Path]:
    win = subprocess.run(["cmd.exe", "/c", "echo %TEMP%"], check=True, capture_output=True,
                         text=True, cwd="/mnt/c").stdout.strip()  # fmt: skip
    wsl = subprocess.run(["wslpath", "-u", win], check=True, capture_output=True, text=True).stdout.strip()
    return win, Path(wsl)


@contextlib.contextmanager
def windows_edge() -> Iterator[str]:
    """Starts headless Edge on Windows and yields a DevTools WebSocket URL usable from WSL."""
    win_temp, wsl_temp = _windows_temp()
    profile_name = f"lan-streamer-e2e-edge-{int(time.time())}"
    edge = subprocess.Popen(
        [str(EDGE), "--headless=new", f"--remote-debugging-port={EDGE_PORT}",
         f"--user-data-dir={win_temp}\\{profile_name}", "--no-first-run",
         "--autoplay-policy=no-user-gesture-required", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/mnt/c",
    )  # fmt: skip
    port, loop = _start_relay()
    try:
        deadline = time.monotonic() + 30
        info = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=10) as r:
                    info = json.load(r)
                break
            except (OSError, ValueError):
                time.sleep(0.5)
        if info is None:
            raise RuntimeError("Edge's DevTools endpoint did not answer")
        ws = info["webSocketDebuggerUrl"].replace(f"127.0.0.1:{EDGE_PORT}", f"127.0.0.1:{port}")
        yield ws.replace(f"localhost:{EDGE_PORT}", f"127.0.0.1:{port}")
    finally:
        edge.terminate()
        subprocess.run(["taskkill.exe", "/F", "/T", "/PID", str(_windows_pid(edge))],
                       check=False, capture_output=True, cwd="/mnt/c")  # fmt: skip
        loop.call_soon_threadsafe(loop.stop)
        time.sleep(1)
        shutil.rmtree(wsl_temp / profile_name, ignore_errors=True)


def _windows_pid(proc: subprocess.Popen) -> int:
    """The Windows PID of an interop process (the WSL PID is a different one)."""
    query = (
        f"(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' AND CommandLine LIKE "
        f"'%--remote-debugging-port={EDGE_PORT}%'\" | "
        "Where-Object { $_.CommandLine -notlike '*--type=*' } | Select-Object -First 1).ProcessId"
    )
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", query],
                         check=False, capture_output=True, text=True, cwd="/mnt/c").stdout.strip()  # fmt: skip
    return int(out) if out.isdigit() else 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
