"""yt-dlp for the built-in inputs (short-video submissions, YouTube), in worker threads.

yt-dlp updates itself because the video sites change often: on start a newer
version is downloaded in the background (pip into the instance's data folder)
and used from the next start on – a running process never swaps the library
under its feet, and nothing waits for the download. Offline, the version that
came with lan-streamer keeps working.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FAKE = os.environ.get("LANSTREAMER_FAKE_DOWNLOADS") == "1"  # tests: no network, no yt-dlp


@dataclass
class Stream:
    """What to play for one video: picture and sound may come separately (YouTube)."""

    url: str
    audio_url: str
    title: str
    channel: str
    live: bool


class YtDlp:
    """yt-dlp in a worker thread.

    <folder>/current  the updated yt-dlp in use (if any), else the bundled one
    <folder>/next     a newer download, moved to current at the next start
    <folder>/download a download in progress, moved to next when complete
    <folder>/old-*    replaced versions, deleted as soon as nothing holds them
    """

    def __init__(self, folder: Path, auto_update: bool, log):
        self.folder = folder
        self.log = log
        self.version = "?"
        current, upcoming = folder / "current", folder / "next"
        if (upcoming / "yt_dlp").is_dir():  # downloaded during the last run
            self._swap_in(current, upcoming)
        for old in folder.glob("old-*"):
            shutil.rmtree(old, ignore_errors=True)
        if (current / "yt_dlp").is_dir():
            sys.path.insert(0, str(current))
        self._update_task = asyncio.create_task(self._update()) if auto_update and not FAKE else None

    def _swap_in(self, current: Path, upcoming: Path) -> None:
        # Moved aside, not deleted: on Windows a file in use can stop a delete
        # halfway, which would leave a broken current behind. A failed update
        # must never stop the input, the version in use just stays.
        old = self.folder / f"old-{os.getpid()}"
        try:
            if current.exists():
                current.rename(old)
            upcoming.rename(current)
        except OSError as e:
            if old.exists() and not current.exists():
                old.rename(current)
            self.log.warning("could not switch to the downloaded yt-dlp – keeping the one in use: %s", e)

    async def _update(self) -> None:
        target = self.folder / "download"
        shutil.rmtree(target, ignore_errors=True)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
            "--target", str(target), "yt-dlp",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 180)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            shutil.rmtree(target, ignore_errors=True)
            self.log.warning("yt-dlp update timed out – using the installed version")
            return
        if proc.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            self.log.warning("yt-dlp update failed (offline?) – using the installed version: %s",
                             out.decode(errors="replace").strip().splitlines()[-1:])  # fmt: skip
            return
        upcoming = self.folder / "next"
        shutil.rmtree(upcoming, ignore_errors=True)
        try:
            target.rename(upcoming)
        except OSError as e:
            self.log.warning("could not keep the downloaded yt-dlp – using the installed version: %s", e)
            return
        self.log.info("downloaded the latest yt-dlp – used from the next start")

    async def module(self):
        import yt_dlp
        import yt_dlp.version

        self.version = yt_dlp.version.__version__
        return yt_dlp

    async def probe(self, link: str) -> dict[str, Any]:
        if FAKE:
            if ".invalid" in link:  # like a real lookup of a link that is no video
                raise RuntimeError("no video")
            return {"title": f"Video {link.rsplit('/', 1)[-1]}", "duration": 10, "uploader": "fake"}
        yt_dlp = await self.module()

        def run() -> dict[str, Any]:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True,
                                   "noplaylist": True, "skip_download": True}) as ydl:  # fmt: skip
                return ydl.extract_info(link, download=False) or {}

        return await asyncio.wait_for(asyncio.to_thread(run), 60)

    async def download(self, link: str, folder: Path, name: str) -> tuple[Path, Path | None]:
        """The video file, and a second file with the sound if picture and sound come separately."""
        folder.mkdir(parents=True, exist_ok=True)
        if FAKE:  # "split" in a link: picture and sound separately
            target = folder / f"{name}.mp4"
            target.write_bytes(b"")
            if "split" not in link:
                return target, None
            sound = folder / f"{name}.audio.m4a"
            sound.write_bytes(b"")
            return target, sound
        yt_dlp = await self.module()
        options = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True}
        # 1080p at most, also upright (Shorts: 1080x1920). YouTube often has no file with both,
        # so separate picture and sound come first (no ffmpeg needed: the output plays them together).
        fit = "[width<=1920][height<=1920]"
        chooser = {**options, "format": f"bv*{fit}[vcodec^=avc1]+ba[acodec^=mp4a]/bv*{fit}+ba"
                                        f"/b[ext=mp4]{fit}/b{fit}/b"}  # fmt: skip

        def fetch(format_id: str, stem: str) -> Path:
            # looked up again: the download URLs only work with the session that got them
            target = {**options, "format": format_id, "outtmpl": str(folder / f"{stem}.%(ext)s")}
            with yt_dlp.YoutubeDL(target) as ydl:
                info = ydl.extract_info(link, download=True) or {}
                downloads = info.get("requested_downloads") or [{}]
                return Path(downloads[0].get("filepath") or ydl.prepare_filename(info))

        def run() -> tuple[Path, Path | None]:
            with yt_dlp.YoutubeDL(chooser) as ydl:
                info = ydl.extract_info(link, download=False) or {}
            parts = info.get("requested_formats") or [info]
            video = fetch(parts[0]["format_id"], name)
            return video, fetch(parts[1]["format_id"], f"{name}.audio") if len(parts) > 1 else None

        return await asyncio.wait_for(asyncio.to_thread(run), 600)

    async def stream(self, link: str, max_height: int = 1080) -> Stream:
        """The playable URLs of a video or live stream (raises if it has none, e.g. offline)."""
        if FAKE:  # "/live" links are live, "offline" in a link: not streaming
            if "offline" in link:
                raise RuntimeError("This channel is not currently live")
            name = link.rstrip("/").rsplit("/", 1)[-1].rsplit("=", 1)[-1]
            return Stream(f"https://fake.invalid/{name}/video", f"https://fake.invalid/{name}/audio",
                          f"Video {name}", "fake channel", link.rstrip("/").endswith("/live"))  # fmt: skip
        yt_dlp = await self.module()
        # H.264 + AAC decode cheapest; separate picture and sound is fine (no ffmpeg needed)
        h = max_height
        options = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "format": f"bv*[height<={h}][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<={h}]+ba/b[height<={h}]/b",
        }  # fmt: skip

        def run() -> Stream:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(link, download=False) or {}
            parts = info.get("requested_formats") or [info]
            video = next((f for f in parts if f.get("vcodec") not in (None, "none")), parts[0])
            audio = next((f for f in parts if f is not video and f.get("acodec") not in (None, "none")), None)
            return Stream(
                url=video["url"], audio_url=audio["url"] if audio else "",
                title=str(info.get("title") or ""), channel=str(info.get("uploader") or info.get("channel") or ""),
                live=bool(info.get("is_live")),
            )  # fmt: skip

        return await asyncio.wait_for(asyncio.to_thread(run), 60)

    async def entries(self, link: str) -> list[dict[str, str]]:
        """The videos of a playlist or channel ([{"url", "title"}]); a single video: itself."""
        if FAKE:  # "list=" links are playlists of three videos
            if "list=" not in link:
                return [{"url": link, "title": "Video"}]
            return [{"url": f"https://www.youtube.com/watch?v=v{n}", "title": f"Video v{n}"} for n in (1, 2, 3)]
        yt_dlp = await self.module()

        def run() -> list[dict[str, str]]:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}) as ydl:
                info = ydl.extract_info(link, download=False) or {}
            if info.get("_type") != "playlist":
                return [{"url": link, "title": str(info.get("title") or link)}]
            return [{"url": str(e["url"]), "title": str(e.get("title") or "")}
                    for e in info.get("entries") or [] if e and e.get("url")]  # fmt: skip

        return await asyncio.wait_for(asyncio.to_thread(run), 120)
