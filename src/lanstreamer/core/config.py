"""Configuration of outputs and their input instances (stored in SQLite from M2 step 2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from lanstreamer.scheduler import Override

OverlayMode = Literal["switch", "always", "off"]


@dataclass
class InputConfig:
    id: str  # stable instance id, e.g. "test-a"
    name: str  # shown on the page and in logs ("On air: <name>")
    plugin: str  # plugin id from plugin.toml, e.g. "test-pattern"
    settings: dict[str, Any] = field(default_factory=dict)
    priority: int = 10  # higher = more important
    fallback: bool = False  # always active, plays when nothing else is
    min_duration_s: float = 0.0
    max_duration_s: float | None = None
    override: Override = "none"


@dataclass
class OutputConfig:
    id: str = "out1"
    name: str = "Output 1"
    width: int = 1920
    height: int = 1080
    fps: int = 60
    bitrate_kbps: int = 0  # 0 = automatic (8000 at 60 fps, 6000 at 30 fps)
    encoder: str = "auto"
    # Lower third with what is on air (input, title, channel, submitter), F-OUT-04:
    # "switch" shows it for overlay_s after every switch, "always" keeps it, "off".
    overlay: OverlayMode = "switch"
    overlay_s: float = 8.0
    inputs: list[InputConfig] = field(default_factory=list)


def demo_output(fps: int, hold_s: float, sources: list[str]) -> OutputConfig:
    """Two test patterns plus one playlist per --source, all of equal priority.

    Equal priority means they take turns: each test pattern stays at least
    `hold_s`, a playlist hands over when its item has ended.
    """
    inputs = [
        InputConfig("test-a", "Test input A", "test-pattern",
                     {"pattern": "smpte", "tone_hz": 440}, min_duration_s=hold_s),
        InputConfig("test-b", "Test input B", "test-pattern",
                     {"pattern": "ball", "tone_hz": 660}, min_duration_s=hold_s),
    ]  # fmt: skip
    for n, item in enumerate(sources, start=1):
        name = Path(item).name if "://" not in item else item.rstrip("/").rsplit("/", 1)[-1]
        inputs.append(InputConfig(f"source-{n}", name or item, "playlist", {"items": [item]}))
    return OutputConfig(fps=fps, inputs=inputs)
