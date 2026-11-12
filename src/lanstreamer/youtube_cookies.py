"""The YouTube cookies uploaded on the admin page, for yt-dlp (plugins/ytdlp.py).

YouTube sometimes answers "Sign in to confirm you're not a bot" (often on a network
with many devices behind one address). The cookies of a signed-in browser get past
it. They are as good as a login: kept only in lan-streamer's data folder (Linux:
readable by its user only), never sent back by the API – the admin page only learns
whether there are any.

The file is the Netscape cookies.txt format that browser extensions export and
yt-dlp reads: one cookie per line, seven tab-separated fields
(domain, subdomains, path, secure, expires, name, value); "#HttpOnly_" before the
domain marks an HTTP-only cookie, other lines starting with "#" are comments.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

MAX_BYTES = 1 * 2**20


def youtube_count(text: str) -> int:
    """How many valid cookies for youtube.com the file has. Raises ValueError if it is
    not a cookies.txt at all."""
    cookies = youtube = 0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7 or fields[1].upper() not in ("TRUE", "FALSE") or not fields[4].lstrip("-").isdigit():
            raise ValueError(f"not a cookies.txt file (line {line[:40]!r})")
        cookies += 1
        domain = fields[0].lstrip(".").lower()
        if domain == "youtube.com" or domain.endswith(".youtube.com"):
            youtube += 1
    if not cookies:
        raise ValueError("no cookies in the file")
    return youtube


def state(path: Path) -> dict[str, Any]:
    """What the admin page may know: whether there are cookies, never the cookies."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return {"set": True, "youtube_cookies": youtube_count(text), "updated_at": path.stat().st_mtime}
    except (OSError, ValueError):
        return {"set": False, "youtube_cookies": 0, "updated_at": None}


def save(path: Path, data: bytes) -> dict[str, Any]:
    """Checks and stores an upload (blocking). Raises ValueError for a wrong file."""
    if len(data) > MAX_BYTES:
        raise ValueError(f"the file is larger than {MAX_BYTES // 2**20} MB")
    text = data.decode("utf-8", errors="replace")
    if youtube_count(text) == 0:
        raise ValueError("no cookies for youtube.com in the file – export them while youtube.com is open")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    if sys.platform != "win32":  # created readable by lan-streamer's user only
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
    else:  # in the user's own %LOCALAPPDATA%
        partial.write_bytes(data)
    partial.replace(path)
    return state(path)


def remove(path: Path) -> None:
    path.unlink(missing_ok=True)
