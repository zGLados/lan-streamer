"""YouTube cookies for yt-dlp: checked on upload, stored privately, never sent back."""

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanstreamer import paths, youtube_cookies
from lanstreamer.core.auth import Auth
from lanstreamer.core.store import Store
from lanstreamer.plugins.ytdlp import YtDlp
from lanstreamer.web.api import Backend, Changes, create_app

SECRET = "AKfycb-not-a-real-session-value"
COOKIES = (
    "# Netscape HTTP Cookie File\n"
    "# exported by a browser extension\n"
    f".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\t{SECRET}\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\tLOGIN_INFO\tsomething\n"
    ".google.com\tTRUE\t/\tTRUE\t1893456000\tNID\tother\n"
)


def test_counts_the_youtube_cookies():
    assert youtube_cookies.youtube_count(COOKIES) == 2  # the HTTP-only one counts, google.com not


@pytest.mark.parametrize("text", ["", "# only comments\n", "<html>not cookies</html>\n", "a\tb\tc\n"])
def test_other_files_are_refused(text):
    with pytest.raises(ValueError):
        youtube_cookies.youtube_count(text)


def test_save_needs_youtube_cookies_and_keeps_them_private(tmp_path):
    target = tmp_path / "youtube-cookies.txt"
    with pytest.raises(ValueError, match="youtube.com"):
        youtube_cookies.save(target, b".google.com\tTRUE\t/\tTRUE\t0\tNID\tx\n")
    assert not target.exists()
    state = youtube_cookies.save(target, COOKIES.encode())
    assert state["set"] is True and state["youtube_cookies"] == 2 and SECRET not in str(state)
    if sys.platform != "win32":
        assert (os.stat(target).st_mode & 0o777) == 0o600
    youtube_cookies.remove(target)
    assert youtube_cookies.state(target)["set"] is False


def test_api_never_sends_the_cookies_back(tmp_path):
    store = Store(tmp_path / "db")

    async def nothing(*_):
        return None

    backend = Backend(status=lambda o=None: {}, skip=lambda o=None: None, store=store, auth=Auth(store),
                      plugins=dict, apply=nothing, logs=tmp_path, changes=Changes(),
                      youtube_cookies=tmp_path / "youtube-cookies.txt")  # fmt: skip
    client = TestClient(create_app(backend))
    assert client.get("/api/admin/youtube-cookies").status_code == 401
    assert client.put("/api/admin/youtube-cookies", content=COOKIES).status_code == 401
    assert client.post("/api/admin/setup", json={"password": "a-long-password"}).status_code == 204
    assert client.get("/api/admin/youtube-cookies").json()["set"] is False
    assert client.put("/api/admin/youtube-cookies", content="<html>").status_code == 422
    uploaded = client.put("/api/admin/youtube-cookies", content=COOKIES)
    assert uploaded.status_code == 200 and SECRET not in uploaded.text
    state = client.get("/api/admin/youtube-cookies")
    assert state.json()["youtube_cookies"] == 2 and SECRET not in state.text
    assert client.delete("/api/admin/youtube-cookies").status_code == 204
    assert client.get("/api/admin/youtube-cookies").json()["set"] is False


def test_ytdlp_gets_a_copy_that_is_gone_afterwards(tmp_path, monkeypatch):
    upload = tmp_path / "youtube-cookies.txt"
    monkeypatch.setattr(paths, "youtube_cookies", lambda: upload)

    class Log:
        def warning(self, *args):
            raise AssertionError(args)

    ytdlp = YtDlp(tmp_path / "yt-dlp", auto_update=False, log=Log())
    with ytdlp.signed({"quiet": True}) as options:  # nothing uploaded: unchanged
        assert options == {"quiet": True}
    upload.write_text(COOKIES, encoding="utf-8")
    with ytdlp.signed({"quiet": True}) as options:
        copy = options["cookiefile"]
        assert copy != str(upload) and Path(copy).read_text(encoding="utf-8") == COOKIES
        with open(copy, "a", encoding="utf-8") as file:  # yt-dlp writes cookies back on close
            file.write("changed\n")
    assert not os.path.exists(copy) and upload.read_text(encoding="utf-8") == COOKIES
