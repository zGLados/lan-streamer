import pytest
from fastapi.testclient import TestClient

from lanstreamer.core.auth import Auth
from lanstreamer.core.store import Store
from lanstreamer.plugins import choice
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

    async def plugins_changed(plugin_id):
        applied.append(f"plugin:{plugin_id}")

    def status(output=None):
        if output not in (None, "out1"):
            raise KeyError(output)
        return {"state": "running", "current": "Test input A"}

    backend = Backend(
        status=status,
        skip=lambda output=None: skipped.append(output or 1),
        store=store,
        auth=Auth(store),
        plugins=lambda: discover(tmp_path / "plugins"),
        apply=apply,
        logs=tmp_path / "logs",
        changes=Changes(),
        plugins_dir=tmp_path / "plugins",
        plugins_changed=plugins_changed,
    )
    choice.set_enabled(store, "test-pattern", True)  # the tests add test-pattern inputs
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
    assert "lan-streamer admin" in client.get("/admin").text
    assert client.post("/api/admin/next").status_code == 401 and skipped == []
    login(client)
    assert client.post("/api/admin/next").status_code == 204 and skipped == [1]


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
                    json={"name": "Night slate", "plugin": "test-pattern", "settings": {"pattern": "snow"},
                          "priority": 0, "fallback": True})  # fmt: skip
    assert r.status_code == 201
    standby = r.json()
    assert standby["id"] == "night-slate" and standby["settings"] == {"pattern": "snow", "tone_hz": 440}
    assert applied == ["out1"]

    r = client.patch("/api/admin/inputs/night-slate", json={"priority": 50, "override": "force"})
    assert (r.json()["priority"], r.json()["override"]) == (50, "force")

    ids = [i.id for i in store.output("out1").inputs]
    assert client.put("/api/admin/outputs/out1/order", json={"ids": ids[::-1]}).status_code == 204
    assert [i.id for i in store.output("out1").inputs] == ids[::-1]
    assert client.put("/api/admin/outputs/out1/order", json={"ids": ids[:1]}).status_code == 422

    assert client.delete("/api/admin/inputs/night-slate").status_code == 204
    assert client.delete("/api/admin/inputs/night-slate").status_code == 404
    assert len(applied) == 4


def test_invalid_settings_and_unknown_plugin(setup):
    client = setup[0]
    login(client)
    r = client.post("/api/admin/outputs/out1/inputs",
                    json={"name": "x", "plugin": "test-pattern", "settings": {"tone_hz": 5}})  # fmt: skip
    assert r.status_code == 422 and r.json()["detail"]["errors"][0]["field"] == "tone_hz"
    r = client.post("/api/admin/outputs/out1/inputs", json={"name": "x", "plugin": "nope"})
    assert r.status_code == 422 and "not installed" in r.json()["detail"]
    client.post("/api/admin/outputs/out1/inputs", json={"name": "A", "plugin": "test-pattern"})
    r = client.patch("/api/admin/inputs/a", json={"settings": {"pattern": "rainbow"}})
    assert r.status_code == 422


def test_output_patch_reports_restart(setup):
    client = setup[0]
    login(client)
    assert client.patch("/api/admin/outputs/out1", json={"name": "Main"}).json()["restart_required"] is False
    r = client.patch("/api/admin/outputs/out1", json={"fps": 30})
    assert r.json()["restart_required"] is True and r.json()["output"]["fps"] == 30
    assert client.patch("/api/admin/outputs/out1", json={"fps": 25}).status_code == 422
    r = client.patch("/api/admin/outputs/out1", json={"overlay": "always", "overlay_s": 5})
    assert r.json()["restart_required"] is False
    assert (r.json()["output"]["overlay"], r.json()["output"]["overlay_s"]) == ("always", 5)
    assert client.patch("/api/admin/outputs/out1", json={"overlay": "sometimes"}).status_code == 422
    r = client.patch("/api/admin/outputs/out1", json={"restream_url": "rtmp://a.rtmp.youtube.com/live2/abc-123"})
    assert r.json()["restart_required"] is False and r.json()["output"]["restream_url"].endswith("/abc-123")
    assert client.patch("/api/admin/outputs/out1", json={"restream_url": "http://x"}).status_code == 422
    assert client.patch("/api/admin/outputs/out1", json={"restream_url": "rtmp://host/only-key"}).status_code == 422
    assert client.patch("/api/admin/outputs/out1", json={"restream_url": ""}).json()["output"]["restream_url"] == ""


def test_logs(setup):
    client = setup[0]
    login(client)
    assert client.get("/api/admin/logs/lan-streamer.log?lines=2").text == "two\nthree"
    assert client.get("/api/admin/logs/output.log").text == ""
    assert client.get("/api/admin/logs/secret.txt").status_code == 404


def test_upload_use_and_remove_a_plugin(setup):
    import io
    import zipfile
    from pathlib import Path

    client, _, applied, _ = setup
    login(client)
    example = Path(__file__).resolve().parents[1] / "examples" / "hello-plugin"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for f in example.iterdir():
            z.write(f, f.name)

    r = client.put("/api/admin/plugins", content=buffer.getvalue(), headers={"Content-Type": "application/zip"})
    assert r.status_code == 201, r.text
    assert r.json() == {"id": "hello", "name": "Hello (example)", "version": "1.0.0"}
    assert "plugin:hello" in applied
    plugins = {p["id"]: p for p in client.get("/api/admin/plugins").json()}
    assert plugins["hello"]["builtin"] is False and plugins["playlist"]["builtin"] is True
    assert "active_seconds" in plugins["hello"]["schema"]["properties"]

    r = client.post("/api/admin/outputs/out1/inputs", json={"name": "Hello", "plugin": "hello"})
    assert r.status_code == 201
    r = client.delete("/api/admin/plugins/hello")
    assert r.status_code == 409 and "Hello" in r.json()["detail"]
    client.delete("/api/admin/inputs/hello")
    assert client.delete("/api/admin/plugins/hello").status_code == 204
    assert "hello" not in {p["id"] for p in client.get("/api/admin/plugins").json()}

    assert client.delete("/api/admin/plugins/playlist").status_code == 409
    r = client.put("/api/admin/plugins", content=b"not a zip")
    assert r.status_code == 422 and "not a zip" in r.json()["detail"]


def test_viewer_forms_and_submissions(setup, tmp_path):
    client, *_ = setup
    calls = []

    class FakeRuntime:
        async def viewer_forms(self, output=None):
            return [{"input_id": "subs", "name": "Submissions", "title": "Submit a video",
                     "schema": {"properties": {"link": {}}}, "items": []}]  # fmt: skip

        async def viewer_submit(self, input_id, form, device, ip):
            calls.append((input_id, form, device, ip))
            if input_id != "subs":
                raise KeyError(input_id)
            if form.get("link") == "bad":
                from lanstreamer.plugins.process import PluginRejected

                raise PluginRejected("This link cannot be played.")
            return "Added – position 1 in the queue."

        async def items(self, input_id):
            return [{"id": "x1", "text": "Cat", "detail": "by Anna"}]

        async def remove_item(self, input_id, item_id):
            calls.append(("removed", input_id, item_id))

    client.app.state.backend.viewer = FakeRuntime()

    assert client.get("/api/viewer").json()[0]["title"] == "Submit a video"
    r = client.post("/api/viewer/subs", json={"link": "https://x/1", "name": "Anna"})
    assert r.json() == {"message": "Added – position 1 in the queue."}
    device = client.cookies.get("lanstreamer_device")
    assert device and calls[0][2] == device
    client.post("/api/viewer/subs", json={"link": "https://x/2", "name": "Anna"})
    assert calls[1][2] == device  # the same device next time
    r = client.post("/api/viewer/subs", json={"link": "bad"})
    assert r.status_code == 422 and r.json()["detail"] == "This link cannot be played."
    assert client.post("/api/viewer/other", json={}).status_code == 404

    assert client.get("/api/admin/inputs/subs/items").status_code == 401
    login(client)
    assert client.get("/api/admin/inputs/subs/items").json()[0]["text"] == "Cat"
    assert client.delete("/api/admin/inputs/subs/items/x1").status_code == 204
    assert calls[-1] == ("removed", "subs", "x1")


def test_tv_page(setup):
    client = setup[0]
    for path in ("/tv", "/tv/out1"):
        r = client.get(path)
        assert r.status_code == 200 and "<video" in r.text and "lan-streamer TV" in r.text


def test_plugins_can_be_turned_off(setup):
    client = setup[0]
    login(client)
    plugins = {p["id"]: p for p in client.get("/api/admin/plugins").json()}
    assert plugins["test-pattern"]["enabled"] and plugins["standby"]["always_on"]
    assert client.patch("/api/admin/plugins/standby", json={"enabled": False}).status_code == 409
    assert client.post("/api/admin/outputs/out1/inputs", json={"name": "T", "plugin": "test-pattern"}).status_code == 201
    r = client.patch("/api/admin/plugins/test-pattern", json={"enabled": False})
    assert r.status_code == 409 and "T" in r.json()["detail"]  # still in use
    assert client.delete("/api/admin/inputs/t").status_code == 204
    assert client.patch("/api/admin/plugins/test-pattern", json={"enabled": False}).status_code == 200
    assert not {p["id"]: p for p in client.get("/api/admin/plugins").json()}["test-pattern"]["enabled"]
    r = client.post("/api/admin/outputs/out1/inputs", json={"name": "T", "plugin": "test-pattern"})
    assert r.status_code == 422 and "turned off" in r.json()["detail"]
    assert client.patch("/api/admin/plugins/nope", json={"enabled": True}).status_code == 404


def test_outputs_are_added_and_removed(setup):
    client, _, applied, skipped = setup
    login(client)
    assert client.get("/api/status?output=out1").json()["current"] == "Test input A"
    assert client.get("/api/status?output=nope").status_code == 404
    assert client.post("/api/admin/next?output=nope").status_code == 204 and skipped[-1] == "nope"

    assert client.delete("/api/admin/outputs/out1").status_code == 409  # the last one stays
    r = client.post("/api/admin/outputs", json={"name": "Hall"})
    assert r.status_code == 201, r.text
    hall = r.json()
    assert hall["id"] == "hall" and applied[-1] == "hall"
    assert [(i["plugin"], i["fallback"]) for i in hall["inputs"]] == [("standby", True)]
    assert [o["id"] for o in client.get("/api/admin/outputs").json()] == ["out1", "hall"]

    assert client.delete("/api/admin/outputs/hall").status_code == 204 and applied[-1] == "hall"
    assert [o["id"] for o in client.get("/api/admin/outputs").json()] == ["out1"]
    assert client.delete("/api/admin/outputs/hall").status_code == 409  # only one left


def test_logs_of_further_outputs(setup):
    client = setup[0]
    login(client)
    assert client.get("/api/admin/logs/output-hall.log").status_code == 200
    assert client.get("/api/admin/logs/..%2Fsecret.log").status_code == 404
