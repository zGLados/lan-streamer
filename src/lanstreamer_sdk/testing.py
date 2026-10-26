"""Helpers to test a plugin without lan-streamer running (e.g. with pytest).

    from lanstreamer_sdk.testing import make_context

    async def test_title(tmp_path):
        ctx, seen = make_context(tmp_path)
        plugin = Plugin(ctx, Settings())
        await plugin.start()
        source = await plugin.get_source()
        assert seen.metadata[-1].title == "…"

The plugin talks to the context exactly as in lan-streamer; `seen` records what
it published. To test it in a real plugin process, see docs/plugin-guide.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from lanstreamer_sdk.plugin import Metadata, PluginContext, Store


@dataclass
class Recorded:
    """What a plugin published through its context, oldest first."""

    metadata: list[Metadata] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    next_requests: int = 0  # ctx.next_source() calls
    changes: int = 0  # ctx.changed() calls


def make_context(data_dir: Path, instance_id: str = "test") -> tuple[PluginContext, Recorded]:
    """A PluginContext with its own data folder and store, recording what is published."""
    data_dir.mkdir(parents=True, exist_ok=True)
    seen = Recorded()

    def request_next() -> None:
        seen.next_requests += 1

    def changed() -> None:
        seen.changes += 1

    ctx = PluginContext(
        instance_id=instance_id,
        data_dir=data_dir,
        log=logging.getLogger(instance_id),
        store=Store(data_dir / "store.json"),
        _publish_metadata=seen.metadata.append,
        _request_next=request_next,
        _publish_info=lambda text: seen.info.append(str(text)),
        _publish_changed=changed,
    )
    return ctx, seen
