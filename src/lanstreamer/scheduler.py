"""Decides which input of an output is on air. Pure functions, no I/O.

Rules (requirements F-SCH-01..04, F-AKT-04/05; docs/architecture.md):

1. A blocked input is ignored. A forced input goes on air, whatever its state.
2. Among active inputs the highest priority wins. Fallback inputs are always active.
3. A higher priority interrupts the current input at once, even before its
   minimum duration.
4. On equal priority the current input keeps playing until its minimum
   duration is over and its plugin releases it, or until its maximum duration
   is reached. Then the next input of that priority (in list order) follows.
5. If no input is active, nothing is on air (the output shows its background).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Override = Literal["none", "force", "block"]


@dataclass(frozen=True)
class InputState:
    id: str
    priority: int  # higher = more important; equal values are allowed
    active: bool
    fallback: bool = False
    min_duration_s: float = 0.0
    max_duration_s: float | None = None
    releasable: bool = True  # the plugin's can_release(), refreshed by the core
    override: Override = "none"


@dataclass(frozen=True)
class OnAir:
    id: str
    since: float  # monotonic seconds


def decide(inputs: Sequence[InputState], on_air: OnAir | None, now: float) -> str | None:
    """Returns the id of the input that should be on air now (None: none)."""
    usable = [i for i in inputs if i.override != "block"]

    forced = [i for i in usable if i.override == "force"]
    if forced:
        if on_air and any(i.id == on_air.id for i in forced):
            return on_air.id
        return max(forced, key=lambda i: i.priority).id

    candidates = [i for i in usable if i.active or i.fallback]
    if not candidates:
        return None
    top = max(i.priority for i in candidates)
    current = next((i for i in candidates if on_air and i.id == on_air.id), None)

    if current is None or current.priority < top:
        return _first(candidates, top).id
    peers = [i for i in candidates if i.priority == top]
    if len(peers) == 1:
        return current.id
    assert on_air is not None
    played = now - on_air.since
    if played < current.min_duration_s:
        return current.id
    over_max = current.max_duration_s is not None and played >= current.max_duration_s
    if current.releasable or over_max:
        return _after(peers, current).id
    return current.id


def _first(candidates: Sequence[InputState], priority: int) -> InputState:
    return next(i for i in candidates if i.priority == priority)


def _after(peers: Sequence[InputState], current: InputState) -> InputState:
    """The next peer after `current` in list order, wrapping around."""
    index = next(n for n, i in enumerate(peers) if i.id == current.id)
    return peers[(index + 1) % len(peers)]


class Debounce:
    """Accepts a new activity state only after it has held for `hold_s` seconds.

    Protects the output from inputs that flap between active and inactive.
    """

    def __init__(self, hold_s: float, initial: bool = False):
        self.hold_s = hold_s
        self.value = initial
        self._pending: bool | None = None
        self._since = 0.0

    def update(self, raw: bool, now: float) -> bool:
        if raw == self.value:
            self._pending = None
        elif raw != self._pending:
            self._pending, self._since = raw, now
        elif now - self._since >= self.hold_s:
            self.value, self._pending = raw, None
        return self.value
