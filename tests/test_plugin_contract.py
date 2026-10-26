"""Contract of the plugin API v1 – what plugin authors may rely on.

A plugin written for API 1 must keep working with every later lan-streamer that
still accepts api_version = 1. These tests pin that promise: names, signatures,
fields and defaults of the SDK, the manifest keys, and the behaviour of a real
plugin process. Additions are allowed (a new optional field, a new hook with a
default); a failing test here means a breaking change – that needs API 2 (see
docs/plugin-guide.md, "Compatibility").
"""

import asyncio
import dataclasses
import inspect
from pathlib import Path

import pytest

import lanstreamer_sdk as sdk
from lanstreamer.plugins.process import PluginProcess, PluginRejected
from lanstreamer_sdk import InputPlugin, PluginContext, load_manifest
from lanstreamer_sdk.testing import make_context

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

V1_NAMES = {
    "API_VERSION", "CardSource", "FileSource", "ImageSource", "InputPlugin", "Manifest",
    "ManifestError", "MediaSource", "Metadata", "NoSettings", "PlaybackEvent", "PluginContext",
    "Rejected", "Store", "TestSource", "UriSource", "Viewer", "ViewerItem", "load_manifest",
    "source_to_dict",
}  # fmt: skip

# hook → its parameters after self; all are coroutines (watch_active: async generator)
V1_HOOKS = {
    "start": [], "stop": [], "is_active": [], "watch_active": [], "get_source": [],
    "can_release": [], "on_playback": ["event"], "on_viewer_submit": ["form", "viewer"],
    "viewer_items": [], "remove_item": ["item_id"],
}  # fmt: skip

# data type → {field: default}; ... = required
V1_FIELDS = {
    sdk.TestSource: {"pattern": "smpte", "freq": 440, "kind": "test"},
    sdk.UriSource: {"uri": ..., "audio_uri": "", "kind": "uri"},
    sdk.FileSource: {"path": ..., "cut": False, "kind": "file"},
    sdk.ImageSource: {"path": ..., "kind": "image"},
    sdk.CardSource: {"title": ..., "heading": "", "subtitle": "", "kind": "card"},
    sdk.Metadata: {"title": "", "channel": "", "thumbnail": "", "duration_s": None, "submitter": ""},
    sdk.Viewer: {"device": ..., "ip": ...},
    sdk.ViewerItem: {"id": ..., "text": ..., "detail": ""},
    sdk.PlaybackEvent: {"kind": ..., "detail": ""},
}


def test_api_version_is_1():
    assert sdk.API_VERSION == 1


def test_every_v1_name_is_still_exported():
    assert V1_NAMES <= set(sdk.__all__)
    for name in V1_NAMES:
        assert hasattr(sdk, name), name


@pytest.mark.parametrize("hook, params", V1_HOOKS.items())
def test_hooks_keep_their_signature(hook, params):
    method = getattr(InputPlugin, hook)
    assert list(inspect.signature(method).parameters)[1:] == params
    if hook == "watch_active":
        assert inspect.isasyncgenfunction(method)
    else:
        assert inspect.iscoroutinefunction(method)


def test_class_attributes_and_their_defaults():
    assert InputPlugin.Settings is sdk.NoSettings
    assert InputPlugin.poll_interval_s == 5.0
    assert InputPlugin.ViewerForm is None
    assert InputPlugin.viewer_title == ""


def test_defaults_of_the_hooks(tmp_path):
    async def steps():
        ctx, _ = make_context(tmp_path)
        plugin = InputPlugin(ctx, sdk.NoSettings())
        assert (plugin.ctx, plugin.settings) == (ctx, sdk.NoSettings())
        assert await plugin.is_active() is True
        assert await plugin.can_release() is True
        assert await plugin.viewer_items() == []
        with pytest.raises(NotImplementedError):
            await plugin.get_source()
        with pytest.raises(sdk.Rejected):
            await plugin.on_viewer_submit(sdk.NoSettings(), sdk.Viewer("d", "ip"))

    asyncio.run(steps())


@pytest.mark.parametrize("cls, fields", V1_FIELDS.items(), ids=lambda v: getattr(v, "__name__", ""))
def test_data_types_keep_their_fields_and_defaults(cls, fields):
    present = {f.name: f for f in dataclasses.fields(cls)}
    for name, default in fields.items():
        assert name in present, name
        has_default = present[name].default is not dataclasses.MISSING
        assert has_default == (default is not ...), name
        if has_default:
            assert present[name].default == default, name
    for name in present.keys() - fields.keys():  # added later: must be optional
        field = present[name]
        assert field.default is not dataclasses.MISSING or field.default_factory is not dataclasses.MISSING


def test_sources_are_positional_in_v1_order():
    assert sdk.UriSource("http://x/a.m3u8", "http://x/b.m4a").audio_uri == "http://x/b.m4a"
    assert sdk.CardSource("Title", "Up next", "by Anna").subtitle == "by Anna"
    assert sdk.PlaybackEvent("failed", "404").detail == "404"


def test_context_offers_the_v1_services(tmp_path):
    ctx, seen = make_context(tmp_path, "inst")
    assert isinstance(ctx, PluginContext)
    assert (ctx.instance_id, ctx.data_dir) == ("inst", tmp_path)
    ctx.log.info("logging works")
    ctx.store.set("k", [1, "x"])
    assert sdk.Store(tmp_path / "store.json").get("k") == [1, "x"]  # persisted as JSON
    assert ctx.store.get("missing", 7) == 7
    ctx.set_metadata(sdk.Metadata(title="t"))
    ctx.next_source()
    ctx.set_info("3 waiting")
    ctx.changed()
    assert (seen.metadata, seen.next_requests, seen.info, seen.changes) == (
        [sdk.Metadata(title="t")], 1, ["3 waiting"], 1,
    )  # fmt: skip


def test_manifest_keys_of_v1(tmp_path):
    (tmp_path / "plugin.toml").write_text(
        'id = "my-input"\nname = "Mine"\nversion = "2.0"\napi_version = 1\n'
        'entry = "mod:Plugin"\ndescription = "d"\nrequirements = ["requests>=2"]\n'
        'homepage = "unknown keys are ignored"\n'
    )
    manifest = load_manifest(tmp_path)
    assert (manifest.id, manifest.name, manifest.version, manifest.api_version, manifest.entry,
            manifest.description, manifest.requirements) == (
        "my-input", "Mine", "2.0", 1, "mod:Plugin", "d", ["requests>=2"],
    )  # fmt: skip


def test_a_plugin_in_its_own_process(tmp_path):
    """The example plugin through the real plugin host, as lan-streamer runs it."""
    events = []

    async def wait_for(kind, count=1):
        for _ in range(200):
            found = [e for e in events if e.get("event") == kind]
            if len(found) >= count:
                return found
            await asyncio.sleep(0.05)
        raise AssertionError(f"no {kind} event in {events}")

    async def steps():
        proc = PluginProcess("inst-1", EXAMPLES / "stream-queue", tmp_path / "data", {"per_device": 1},
                             lambda _i, e: events.append(e))  # fmt: skip
        await proc.start()
        try:
            assert (await wait_for("ready"))[0]["api_version"] == 1
            assert (await wait_for("active"))[0]["value"] is False
            assert (await proc.viewer_schema())["title"] == "Queue a stream"

            answer = await proc.viewer_submit({"url": "https://example.org/a.mp4", "name": "Anna"}, "dev", "ip")
            assert "number 1" in answer
            with pytest.raises(PluginRejected, match="already have 1"):
                await proc.viewer_submit({"url": "https://example.org/b.mp4"}, "dev", "ip")
            with pytest.raises(PluginRejected, match="url"):  # form validated against ViewerForm
                await proc.viewer_submit({"name": "no url"}, "other", "ip")
            assert (await wait_for("active", 2))[1]["value"] is True
            assert (await wait_for("info", 2))[-1]["value"] == "1 waiting"
            await wait_for("changed", 2)

            items = await proc.viewer_items()
            assert [(i["text"], i["detail"]) for i in items] == [("https://example.org/a.mp4", "by Anna")]
            assert await proc.get_source() == {"kind": "uri", "uri": "https://example.org/a.mp4"}
            assert (await wait_for("metadata"))[-1]["value"]["submitter"] == "Anna"
            assert await proc.can_release() is False

            await proc.viewer_submit({"url": "rtsp://cam/live"}, "other", "ip")
            await proc.playback("ended")
            await wait_for("next_source")
            assert await proc.get_source() == {"kind": "uri", "uri": "rtsp://cam/live"}
            await proc.remove_item((await proc.viewer_items())[0]["id"])
            await proc.playback("ended")
            assert (await wait_for("active", 3))[-1]["value"] is False
        finally:
            await proc.stop()

    asyncio.run(steps())
