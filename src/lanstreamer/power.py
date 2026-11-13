"""Keeps the streaming PC from going to sleep while lan-streamer runs (Windows).

Windows puts an idle PC to sleep (30 min on many desktops), which ends the
stream for everyone. Only sleep is prevented: the screen may still turn off
by the power plan. The player and /tv pages keep it on themselves while they
show the stream (Screen Wake Lock), so a TV behind the PC stays on.
"""

from __future__ import annotations

import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(on: bool = True) -> bool:
    """Holds (or releases) the PC awake for the calling thread; False where not supported."""
    if sys.platform != "win32":
        return False
    import ctypes

    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
