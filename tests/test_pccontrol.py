"""The "This PC" admin controls: autorun, restart/shut down, run a command."""

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from lanstreamer import pccontrol
from lanstreamer.core.auth import Auth
from lanstreamer.core.store import Store
from lanstreamer.pccontrol import PcControl
from lanstreamer.web.api import Backend, Changes, create_app


def make_control(tmp_path):
    return PcControl(Store(tmp_path / "db"))


# --- autorun (registry on Windows) --------------------------------------------------


class FakeRegistry:
    """A stand-in for winreg: one HKCU Run key in a dict."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 1
    KEY_QUERY_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict[str, str] = {}

    def OpenKey(self, root, path, reserved=0, access=0):
        reg = self

        class Handle:
            def __enter__(self_):
                return reg

            def __exit__(self_, *a):
                return False

        return Handle()

    def SetValueEx(self, key, name, reserved, type_, value):
        self.values[name] = value

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_autorun_command_launches_the_tray_windowless():
    cmd = pccontrol.autorun_command()
    assert cmd.endswith("-m lanstreamer --tray") and cmd.startswith('"')


def test_set_and_read_autorun_on_windows(tmp_path, monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    pc = make_control(tmp_path)
    assert pc.autorun_enabled() is False
    pc.set_autorun(True)
    assert pc.autorun_enabled() is True and pccontrol.RUN_NAME in fake.values
    pc.set_autorun(False)
    assert pc.autorun_enabled() is False
    pc.set_autorun(False)  # already off: no error


def test_power_and_autorun_refuse_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    pc = make_control(tmp_path)
    assert pc.available() is False and pc.state()["can_power"] is False
    for call in (pc.restart, pc.shutdown, pc.cancel, lambda: pc.set_autorun(True)):
        with pytest.raises(RuntimeError, match="only on Windows"):
            call()


def test_restart_calls_shutdown_with_a_delay(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **k: calls.append(args) or None)
    make_control(tmp_path).restart(5)
    assert calls == [["shutdown", "/r", "/t", "5"]]


# --- run a command ------------------------------------------------------------------


def test_running_is_off_by_default(tmp_path):
    pc = make_control(tmp_path)
    assert pc.commands_enabled() is False
    with pytest.raises(PermissionError, match="turned off"):
        pc.run("echo hello")


def test_run_captures_output_when_enabled(tmp_path):
    pc = make_control(tmp_path)
    pc.set_commands_enabled(True)
    result = pc.run("echo hello-there", wait=True)
    assert result["returncode"] == 0 and "hello-there" in result["stdout"]
    with pytest.raises(ValueError, match="no command"):
        pc.run("   ")


def test_run_detached_returns_a_pid(tmp_path):
    pc = make_control(tmp_path)
    pc.set_commands_enabled(True)
    result = pc.run("exit 0", wait=False)
    assert result["started"] is True and isinstance(result["pid"], int)


def test_long_output_is_clipped():
    clipped = pccontrol._clip("x" * (pccontrol.OUTPUT_LIMIT + 50))
    assert clipped.endswith("cut off)") and len(clipped) < pccontrol.OUTPUT_LIMIT + 60


# --- program buttons (shortcuts) ----------------------------------------------------


def test_shortcuts_need_commands_enabled_to_change(tmp_path):
    pc = make_control(tmp_path)
    assert pc.shortcuts() == []
    with pytest.raises(PermissionError, match="turn on running commands"):
        pc.set_shortcuts([{"label": "TS3", "command": "start.bat"}])
    pc.set_commands_enabled(True)
    pc.set_shortcuts([
        {"label": "TS3", "command": "start.bat"},
        {"label": "  ", "command": "ignored"},  # dropped: no label
        {"label": "no cmd", "command": "  "},    # dropped: no command
    ])
    assert pc.shortcuts() == [{"label": "TS3", "command": "start.bat"}]
    assert pc.state()["shortcuts"] == [{"label": "TS3", "command": "start.bat"}]


def test_shortcuts_survive_turning_commands_off(tmp_path):
    pc = make_control(tmp_path)
    pc.set_commands_enabled(True)
    pc.set_shortcuts([{"label": "TS3", "command": "start.bat"}])
    pc.set_commands_enabled(False)
    assert pc.shortcuts() == [{"label": "TS3", "command": "start.bat"}]  # still shown and runnable


def test_run_shortcut_bad_index_raises_before_touching_the_os(tmp_path):
    pc = make_control(tmp_path)
    with pytest.raises(IndexError, match="no such program button"):
        pc.run_shortcut(0)


def test_run_shortcut_launches_detached(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    launched = []
    monkeypatch.setattr(pccontrol.PcControl, "_spawn",
                        lambda self, command: launched.append(command) or {"started": True, "pid": 123})  # fmt: skip
    pc = make_control(tmp_path)
    pc.set_commands_enabled(True)
    pc.set_shortcuts([{"label": "TS3", "command": "start.bat"}])
    assert pc.run_shortcut(0) == {"started": True, "pid": 123}
    assert launched == ["start.bat"]


# --- the admin endpoints ------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "db")
    backend = Backend(
        status=lambda output=None: {"state": "running", "current": "x"},
        skip=lambda output=None: None, store=store, auth=Auth(store),
        plugins=dict, apply=None, logs=tmp_path, changes=Changes(),
        pc_control=PcControl(store),
    )  # fmt: skip
    c = TestClient(create_app(backend))
    c.post("/api/admin/setup", json={"password": "a good password"})
    yield c
    c.close()


def test_endpoints_need_login_and_toggle_commands(client, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")  # no reboot here, but the toggles work
    fresh = TestClient(create_app(client.app.state.backend))  # no session cookie
    assert fresh.get("/api/admin/pc").status_code == 401
    fresh.close()

    state = client.get("/api/admin/pc").json()
    assert state["commands_enabled"] is False and state["can_power"] is False

    assert client.post("/api/admin/pc/run", json={"command": "echo hi"}).status_code == 403
    on = client.patch("/api/admin/pc", json={"commands_enabled": True}).json()
    assert on["commands_enabled"] is True
    run = client.post("/api/admin/pc/run", json={"command": "echo hi", "wait": True}).json()
    assert run["returncode"] == 0 and "hi" in run["stdout"]
    assert client.post("/api/admin/pc/power", json={"action": "restart"}).status_code == 501


def test_shortcut_endpoints(client, monkeypatch):
    launched = []
    monkeypatch.setattr(pccontrol.PcControl, "autorun_enabled", lambda self: False)  # no winreg here
    monkeypatch.setattr(pccontrol.PcControl, "_require_windows", lambda self: None)
    monkeypatch.setattr(pccontrol.PcControl, "_spawn",
                        lambda self, command: launched.append(command) or {"started": True, "pid": 7})  # fmt: skip
    body = {"shortcuts": [{"label": "TS3", "command": "start.bat"}]}
    assert client.put("/api/admin/pc/shortcuts", json=body).status_code == 403  # commands off
    client.patch("/api/admin/pc", json={"commands_enabled": True})
    state = client.put("/api/admin/pc/shortcuts", json=body).json()
    assert state["shortcuts"] == [{"label": "TS3", "command": "start.bat"}]
    assert client.post("/api/admin/pc/shortcuts/run", json={"index": 0}).json()["pid"] == 7
    assert launched == ["start.bat"]
    assert client.post("/api/admin/pc/shortcuts/run", json={"index": 9}).status_code == 404


def test_pc_endpoints_are_501_without_the_control(tmp_path):
    store = Store(tmp_path / "db")
    backend = Backend(
        status=lambda output=None: {}, skip=lambda output=None: None, store=store, auth=Auth(store),
        plugins=dict, apply=None, logs=tmp_path, changes=Changes(),  # pc_control stays None
    )  # fmt: skip
    c = TestClient(create_app(backend))
    c.post("/api/admin/setup", json={"password": "a good password"})
    assert c.get("/api/admin/pc").status_code == 501
    c.close()
