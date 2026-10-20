import pytest
from fastapi.testclient import TestClient

from lanstreamer.core.auth import Auth
from lanstreamer.core.store import Store
from lanstreamer.plugins.registry import discover
from lanstreamer.web.api import Backend, Changes, create_app


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "db")
    applied, skipped = [], []
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "lan-streamer.log").write_text("one\ntwo\nthree\n")

    async def apply(output_id):
        applied.append(output_id)

    backend = Backend(
        status=lambda: {"state": "running", "current": "Test input A"},
        skip=lambda: skipped.append(1),
        store=store,
        auth=Auth(store),
        plugins=discover,
        apply=apply,
        logs=tmp_path / "logs",
        changes=Changes(),
    )
    client = TestClient(create_app(backend))
    yield client, store, applied, skipped
    client.close()


def login(client):
    assert client.post("/api/admin/setup", json={"password": "secret password"}).status_code == 204


def test_public_pages_and_status(setup):
    client, _, _, skipped = setup
    assert "lan-streamer" in client.get("/").text
    assert client.get("/favicon.png").headers["content-type"] == "image/png"
    assert client.get("/api/status").json()["current"] == "Test input A"
    assert client.post("/api/next").status_code == 204 and skipped == [1]


def test_websocket_pushes_status(setup):
    client = setup[0]
    with client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["state"] == "running"


def test_admin_needs_login(setup):
    client = setup[0]
    assert client.get("/api/admin/session").json() == {"password_set": False, "logged_in": False}
    assert client.get("/api/admin/outputs").status_code == 401
    login(client)
    assert client.get("/api/admin/session").json() == {"password_set": True, "logged_in": True}
    assert client.post("/api/admin/setup", json={"password": "another one"}).status_code == 409
    client.post("/api/admin/logout")
    assert client.get("/api/admin/outputs").status_code == 401
    assert client.post("/api/admin/login", json={"password": "secret password"}).status_code == 204
    assert client.get("/api/admin/outputs").status_code == 200


def test_short_password_is_rejected(setup):
    client = setup[0]
    assert client.post("/api/admin/setup", json={"password": "short"}).status_code == 422


def test_plugins_list_has_settings_schema(setup):
    client = setup[0]
    login(client)
    plugins = {p["id"]: p for p in client.get("/api/admin/plugins").json()}
    assert {"test-pattern", "playlist"} <= set(plugins)
    assert "pattern" in plugins["test-pattern"]["schema"]["properties"]


def test_add_change_reorder_and_delete_inputs(setup):
    client, store, applied, _ = setup
    login(client)
    r = client.post("/api/admin/outputs/out1/inputs",
                    json={"name": "Standby", "plugin": "test-pattern", "settings": {"pattern": "snow"},
                          "priority": 0, "fallback": True})  # fmt: skip
    assert r.status_code == 201
    standby = r.json()
    assert standby["id"] == "standby" and standby["settings"] == {"pattern": "snow", "tone_hz": 440}
    assert applied == ["out1"]

    r = client.patch("/api/admin/inputs/standby", json={"priority": 50, "override": "force"})
    assert (r.json()["priority"], r.json()["override"]) == (50, "force")

    ids = [i.id for i in store.output("out1").inputs]
    assert client.put("/api/admin/outputs/out1/order", json={"ids": ids[::-1]}).status_code == 204
    assert [i.id for i in store.output("out1").inputs] == ids[::-1]
    assert client.put("/api/admin/outputs/out1/order", json={"ids": ids[:1]}).status_code == 422

    assert client.delete("/api/admin/inputs/standby").status_code == 204
    assert client.delete("/api/admin/inputs/standby").status_code == 404
    assert len(applied) == 4


def test_invalid_settings_and_unknown_plugin(setup):
    client = setup[0]
    login(client)
    r = client.post("/api/admin/outputs/out1/inputs",
                    json={"name": "x", "plugin": "test-pattern", "settings": {"tone_hz": 5}})  # fmt: skip
    assert r.status_code == 422 and r.json()["detail"]["errors"][0]["field"] == "tone_hz"
    r = client.post("/api/admin/outputs/out1/inputs", json={"name": "x", "plugin": "nope"})
    assert r.status_code == 422 and "not installed" in r.json()["detail"]
    r = client.patch("/api/admin/inputs/test-a", json={"settings": {"pattern": "rainbow"}})
    assert r.status_code == 422


def test_output_patch_reports_restart(setup):
    client = setup[0]
    login(client)
    assert client.patch("/api/admin/outputs/out1", json={"name": "Main"}).json()["restart_required"] is False
    r = client.patch("/api/admin/outputs/out1", json={"fps": 30})
    assert r.json()["restart_required"] is True and r.json()["output"]["fps"] == 30
    assert client.patch("/api/admin/outputs/out1", json={"fps": 25}).status_code == 422


def test_logs(setup):
    client = setup[0]
    login(client)
    assert client.get("/api/admin/logs/lan-streamer.log?lines=2").text == "two\nthree"
    assert client.get("/api/admin/logs/output.log").text == ""
    assert client.get("/api/admin/logs/secret.txt").status_code == 404
