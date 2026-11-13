import ctypes
import sys
import types

from lanstreamer import power


def test_keep_awake_holds_the_pc_awake_on_windows(monkeypatch):
    calls = []
    kernel32 = types.SimpleNamespace(SetThreadExecutionState=lambda flags: calls.append(flags) or 1)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=kernel32), raising=False)
    assert power.keep_awake() is True
    assert power.keep_awake(False) is True
    # sleep is blocked, the screen is left to the power plan (the pages hold it on)
    assert calls == [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED, power.ES_CONTINUOUS]


def test_keep_awake_does_nothing_elsewhere(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert power.keep_awake() is False
