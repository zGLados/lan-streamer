import pytest
from fastapi.testclient import TestClient

from lanstreamer import localpage
from lanstreamer.core.auth import Auth
from lanstreamer.core.store import Store
from lanstreamer.localpage import LocalPage
from lanstreamer.web.api import Backend, Changes, create_app


class FakeKiosk:
    def __init__(self, cmd):
        self.cmd = cmd
        self.ended = False

    def poll(self):
        return 0 if self.ended else None

    def terminate(self):
        self.ended = True

    def wait(self, _timeout):
        return 0


@pytest.fixture
def page(tmp_path, monkeypatch):
    opened, started = [], []
    monkeypatch.setattr(localpage.webbrowser, "open", opened.append)
    monkeypatch.setattr(localpage.subprocess, "Popen", lambda cmd: started.append(FakeKiosk(cmd)) or started[-1])
    monkeypatch.setattr(localpage, "find_edge", lambda: "msedge.exe")
    store = Store(tmp_path / "db")
    yield LocalPage(store, "http://localhost:8080", tmp_path / "kiosk"), store, opened, started
    store.close()


def test_player_page_by_default(page):
    lp, _, opened, started = page
    assert lp.mode() == "player"
    assert lp.open() == "Player page opened."
    assert opened == ["http://localhost:8080"] and started == []


def test_fullscreen_starts_edge_kiosk_on_the_tv_view(page):
    lp, store, opened, started = page
    lp.set_mode("fullscreen")
    assert store.get_setting("local_page") == "fullscreen"
    lp.open()
    cmd = started[0].cmd
    assert cmd[:3] == ["msedge.exe", "--kiosk", "http://localhost:8080/tv"]
    assert "--edge-kiosk-type=fullscreen" in cmd and "--autoplay-policy=no-user-gesture-required" in cmd
    assert any(arg.startswith("--user-data-dir=") for arg in cmd) and opened == []
    assert lp.state()["kiosk_open"]
    lp.open()  # open again: the first kiosk window is closed first
    assert started[0].ended and not started[1].ended


def test_fullscreen_without_edge_opens_the_tv_view(page, monkeypatch):
    lp, _, opened, started = page
    monkeypatch.setattr(localpage, "find_edge", lambda: None)
    lp.set_mode("fullscreen")
    assert "Edge not found" in lp.open()
    assert opened == ["http://localhost:8080/tv"] and started == []


def test_off_and_switching_away_closes_the_kiosk(page):
    lp, _, opened, started = page
    lp.set_mode("fullscreen")
    lp.open()
    lp.set_mode("off")
    assert lp.open() == "Nothing opened (set to off)."
    assert started[0].ended and opened == []
    with pytest.raises(ValueError):
        lp.set_mode("maximized")


def test_admin_api(page, tmp_path):
    lp, store, opened, _ = page
    backend = Backend(
        status=lambda output=None: {}, skip=lambda output=None: None, store=store, auth=Auth(store),
        plugins=dict, apply=None, logs=tmp_path, changes=Changes(), local_page=lp,
    )  # fmt: skip
    client = TestClient(create_app(backend))
    assert client.get("/api/admin/local-page").status_code == 401
    assert client.post("/api/admin/setup", json={"password": "secret password"}).status_code == 204
    assert client.get("/api/admin/local-page").json()["mode"] == "player"
    assert client.patch("/api/admin/local-page", json={"mode": "nonsense"}).status_code == 422
    assert client.patch("/api/admin/local-page", json={"mode": "off"}).json()["mode"] == "off"
    assert client.post("/api/admin/local-page/open").json()["message"] == "Nothing opened (set to off)."
    assert opened == []
    client.close()


def test_no_desktop_no_section(tmp_path):
    store = Store(tmp_path / "db")
    backend = Backend(
        status=lambda output=None: {}, skip=lambda output=None: None, store=store, auth=Auth(store),
        plugins=dict, apply=None, logs=tmp_path, changes=Changes(),
    )  # fmt: skip
    client = TestClient(create_app(backend))
    client.post("/api/admin/setup", json={"password": "secret password"})
    assert client.get("/api/admin/local-page").status_code == 501
    client.close()
    store.close()
