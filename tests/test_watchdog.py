"""The output worker's watchdog (media/output.py), without GStreamer."""

import json
import threading
import time

from lanstreamer.media import output


def test_a_hung_main_loop_is_reported_once_with_every_threads_stack(capfd, monkeypatch):
    monkeypatch.setattr(output, "STALL_S", 0.5)
    out = object.__new__(output.Output)  # no pipeline: only the watchdog's fields
    out.current = out.pending = None
    out.heartbeat, out.watching = time.monotonic(), True
    watcher = threading.Thread(target=out._watch, daemon=True)
    watcher.start()
    time.sleep(3.2)  # the main loop is "hung": nobody calls _beat
    for _ in range(15):  # it runs again
        out._beat()
        time.sleep(0.1)
    out.watching = False
    watcher.join(3)

    captured = capfd.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert [e["event"] for e in events] == ["stalled"]  # once per hang
    assert "stacks of all threads" in captured.err
    assert "test_a_hung_main_loop" in captured.err  # the main thread's stack
    assert "main loop answers again" in captured.err
