import asyncio
import hashlib
import http.server
import threading

import pytest
from fastapi.testclient import TestClient

from lanstreamer import updater as up
from lanstreamer.core.auth import Auth
from lanstreamer.core.store import Store
from lanstreamer.web.api import Backend, Changes, create_app

SETUP = b"pretend this is lan-streamer-setup-0.2.0.exe" * 1000


def test_version_order():
    order = ["0.0.9", "0.1.0-test.5", "0.1.0-test.40", "0.1.0", "0.1.1-test.1", "0.1.1", "0.2.0", "1.0.0"]
    mixed = [order[i] for i in (5, 2, 7, 0, 3, 6, 1, 4)]
    assert sorted(mixed, key=up.version_key) == order
    assert up.is_newer("v0.2.0", "0.1.1-test.51")
    assert not up.is_newer("0.1.0", "0.1.1-test.51")  # a test build of 0.1.1 is past 0.1.0
    assert not up.is_newer("0.1.1", "0.1.1")
    assert not up.is_newer("nightly", "0.1.0")


@pytest.fixture
def server():
    """Serves the fake installer at /setup.exe."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # the http.server name
            self.send_response(200)
            self.send_header("Content-Length", str(len(SETUP)))
            self.end_headers()
            self.wfile.write(SETUP)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/setup.exe"
    httpd.shutdown()


def release(url="http://x/setup.exe", digest=None, tag="0.2.0"):
    return {
        "tag_name": tag, "html_url": f"https://github.com/zGLados/lan-streamer/releases/tag/{tag}",
        "assets": [
            {"name": "lan-streamer_0.2.0_amd64.deb", "browser_download_url": "http://x/deb", "size": 1},
            {"name": f"lan-streamer-setup-{tag}.exe", "browser_download_url": url, "size": len(SETUP),
             "digest": digest if digest is not None else "sha256:" + hashlib.sha256(SETUP).hexdigest()},
        ],
    }  # fmt: skip


def make(tmp_path, answer, current="0.1.1", installed=True, task=False, beta=None, tasks=(up.TASK, up.BETA_TASK)):
    """answer: the latest release (or a function giving it); beta: the beta feed's answer."""
    launched, quits = [], []

    def fetch(url):
        given = beta if url == "http://beta" else answer
        if isinstance(given, Exception):
            raise given
        return given() if callable(given) else given

    u = up.Updater(current, Store(tmp_path / "db"), tmp_path, lambda: quits.append(1), fps=30,
                   feed="http://feed", beta_feed="http://beta", fetch=fetch,
                   launch=lambda path, args, elevate: launched.append((path, args)),
                   has_task=lambda name: task and name in tasks,
                   start_task=lambda root, relaunch, log, name: launched.append(("task", relaunch, name)))  # fmt: skip
    u.root = tmp_path / "program" if installed else None
    return u, launched, quits


def test_check_finds_a_newer_release(tmp_path):
    u, _, _ = make(tmp_path, release())
    state = asyncio.run(u.check())
    assert (state["latest"], state["available"], state["installable"]) == ("0.2.0", True, True)
    assert state["notes_url"].endswith("/0.2.0") and state["auto_check"] is True and not state["error"]


def test_up_to_date_and_errors(tmp_path):
    u, _, _ = make(tmp_path, release(tag="0.1.1"))
    assert asyncio.run(u.check())["available"] is False

    def offline():
        raise OSError("no internet")

    u, _, _ = make(tmp_path, offline)
    state = asyncio.run(u.check())
    assert "no internet" in state["error"] and state["state"] == "idle"


def test_auto_check_setting(tmp_path):
    u, _, _ = make(tmp_path, release())
    u.set_auto_check(False)
    assert u.state()["auto_check"] is False


def test_install_downloads_checks_and_starts_the_installer(tmp_path, monkeypatch, server):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    u, launched, quits = make(tmp_path, release(server))
    asyncio.run(u.check())
    asyncio.run(u.install())
    assert not u.status.error, u.status.error
    path, args = launched[0]
    assert path.read_bytes() == SETUP and path.parent == tmp_path / "updates"
    assert "/VERYSILENT" in args and "/LAUNCH=1" in args and "/LAUNCHFPS=30" in args
    assert f'/LOG="{tmp_path / "updates" / "install.log"}"' in args
    assert quits == [1]  # lan-streamer ends, the installer replaces it


@pytest.mark.parametrize("digest, message", [("sha256:" + "0" * 64, "does not match"), ("", "no SHA-256")])
def test_a_wrong_or_missing_checksum_installs_nothing(tmp_path, monkeypatch, server, digest, message):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    u, launched, quits = make(tmp_path, release(server, digest=digest))
    asyncio.run(u.check())
    try:
        asyncio.run(u.install())
    except RuntimeError as exc:
        u.status.error = str(exc)
    assert message in u.status.error and not launched and not quits
    assert not list((tmp_path / "updates").glob("*.exe"))  # nothing unchecked is left lying around


def test_not_installed_shows_the_update_only(tmp_path):
    u, _, _ = make(tmp_path, release(), installed=False)
    state = asyncio.run(u.check())
    assert state["available"] is True and state["installable"] is False and state["how"]


async def _no_sleep(_seconds):
    return None


def test_api(tmp_path):
    store = Store(tmp_path / "db")
    u, _, _ = make(tmp_path, release())
    u.store = store

    async def nothing(*_):
        return None

    backend = Backend(status=lambda o=None: {}, skip=lambda o=None: None, store=store, auth=Auth(store),
                      plugins=dict, apply=nothing, logs=tmp_path, changes=Changes(), updater=u)  # fmt: skip
    client = TestClient(create_app(backend))
    assert client.get("/api/admin/update").status_code == 401
    assert client.post("/api/admin/setup", json={"password": "a-long-password"}).status_code == 204
    assert client.get("/api/admin/update").json()["current"] == "0.1.1"
    assert client.post("/api/admin/update/check").json()["available"] is True
    assert client.patch("/api/admin/update", json={"auto_check": False}).json()["auto_check"] is False
    changed = client.patch("/api/admin/update", json={"channel": "beta"}).json()
    assert (changed["channel"], changed["auto_check"]) == ("beta", False)  # only what was sent changes
    assert client.patch("/api/admin/update", json={"channel": "nightly"}).status_code == 422
    u.status.available = False
    assert client.post("/api/admin/update/install").status_code == 409


def test_an_install_for_all_users_goes_through_the_task(tmp_path, monkeypatch):
    # no download in lan-streamer and no admin prompt: the SYSTEM task does it
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    u, launched, quits = make(tmp_path, release(), task=True)
    asyncio.run(u.check())
    asyncio.run(u.install())
    assert not u.status.error and quits == [1]
    kind, relaunch, task = launched[0]
    assert kind == "task" and relaunch[1:] == ["-m", "lanstreamer", "--tray", "--no-browser", "--fps", "30"]
    assert task == up.TASK
    assert not (tmp_path / "updates").exists()


def test_a_failed_task_update_is_shown_after_the_restart(tmp_path):
    import json
    from datetime import UTC, datetime

    u, _, _ = make(tmp_path, release())
    (u.root / "updates").mkdir(parents=True)
    (u.root / "updates" / "result.json").write_text(json.dumps({
        "ok": False, "message": "the installer ended with code 5", "version": "",
        "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }), encoding="utf-8-sig")  # fmt: skip
    u._last_result()
    assert u.state()["error"] == "the last update failed: the installer ended with code 5"


def beta_release(version="0.2.1-test.7"):
    """The pre-release "beta" as GitHub gives it: tag "beta", the version in the installer's name."""
    answer = release(tag=version)
    answer["tag_name"] = "beta"
    answer["html_url"] = "https://github.com/zGLados/lan-streamer/releases/tag/beta"
    return answer


def test_release_version_from_the_tag_or_the_installer_name():
    assert up.release_version(release(tag="v0.2.0")) == "0.2.0"
    assert up.release_version(beta_release("0.1.10-test.85")) == "0.1.10-test.85"
    assert up.release_version({"tag_name": "beta", "assets": [{"name": "notes.txt"}]}) == ""


def test_stable_channel_never_looks_at_the_beta(tmp_path):
    u, _, _ = make(tmp_path, release(), beta=beta_release())
    state = asyncio.run(u.check())
    assert (state["channel"], state["latest"], state["beta"]) == ("stable", "0.2.0", False)


def test_beta_channel_offers_the_newer_of_beta_and_release(tmp_path):
    u, _, _ = make(tmp_path, release(), beta=beta_release("0.2.1-test.7"))
    u.set_channel("beta")
    state = asyncio.run(u.check())
    assert (state["latest"], state["beta"], state["available"], state["installable"]) == ("0.2.1-test.7", True, True, True)
    assert state["notes_url"].endswith("/beta")
    # a release newer than the beta: back to the releases
    u, _, _ = make(tmp_path, release(tag="0.2.1"), current="0.2.1-test.7", beta=beta_release("0.2.1-test.7"))
    u.set_channel("beta")
    state = asyncio.run(u.check())
    assert (state["latest"], state["beta"], state["available"]) == ("0.2.1", False, True)


def test_no_beta_still_finds_the_release(tmp_path):
    u, _, _ = make(tmp_path, release(), beta=OSError("HTTP Error 404: Not Found"))
    u.set_channel("beta")
    state = asyncio.run(u.check())
    assert (state["latest"], state["beta"], state["error"]) == ("0.2.0", False, "")


def test_the_beta_is_installed_by_the_beta_task(tmp_path, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    u, launched, quits = make(tmp_path, release(), task=True, beta=beta_release())
    u.set_channel("beta")
    asyncio.run(u.check())
    asyncio.run(u.install())
    assert not u.status.error and quits == [1] and launched[0][2] == up.BETA_TASK


def test_an_install_without_the_beta_task_says_so(tmp_path, monkeypatch):
    # installed by an installer before 0.1.10: only the release task exists
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    u, launched, quits = make(tmp_path, release(), task=True, beta=beta_release(), tasks=(up.TASK,))
    u.set_channel("beta")
    asyncio.run(u.check())
    with pytest.raises(RuntimeError, match="no beta update task"):
        asyncio.run(u.install())
    assert not launched and not quits


def test_channel_setting(tmp_path):
    u, _, _ = make(tmp_path, release())
    assert u.channel == "stable"
    u.set_channel("beta")
    assert u.state()["channel"] == "beta"
    with pytest.raises(ValueError):
        u.set_channel("nightly")
    u.store.set_setting(up.CHANNEL, "something old")  # never trusted blindly
    assert u.channel == "stable"


def test_stable_on_a_beta_offers_the_way_back_to_the_release(tmp_path, monkeypatch):
    # beta 0.2.1-test.7 installed, channel back to stable: the older release 0.2.0 is offered
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    u, launched, quits = make(tmp_path, release(), current="0.2.1-test.7", task=True, beta=beta_release())
    state = asyncio.run(u.check())
    assert (state["latest"], state["available"], state["back"], state["beta"]) == ("0.2.0", True, True, False)
    asyncio.run(u.install())  # by the release task, not the beta one
    assert not u.status.error and quits == [1] and launched[0][2] == up.TASK


def test_the_way_back_only_from_a_beta_on_the_stable_channel(tmp_path):
    cases = [
        ("0.2.1-test.7", "beta", "0.2.0", False),  # the beta channel keeps the beta
        ("0.2.1", "stable", "0.2.0", False),  # a release is never replaced by an older one
        ("0.2.0", "stable", "0.2.0", False),
        ("0.2.0-test.3", "stable", "0.2.0", False),  # the release is newer: a normal update
    ]
    for current, channel, latest, back in cases:
        u, _, _ = make(tmp_path, release(tag=latest), current=current, beta=beta_release(current))
        u.set_channel(channel)
        state = asyncio.run(u.check())
        assert state["back"] is back, (current, channel)
    assert up.is_back("0.1.13", "0.1.14-test.101") and not up.is_back("nightly", "0.1.14-test.101")
