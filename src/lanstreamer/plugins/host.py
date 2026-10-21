"""Plugin host: runs one plugin instance in its own process (F-PLG-05).

    python -m lanstreamer.plugins.host --plugin <folder> --instance <id> --data-dir <dir>
                                       [--settings '<json>']
    python -m lanstreamer.plugins.host --plugin <folder> --check [--settings '<json>']
        prints {"schema": <JSON schema of the settings>, "settings": <validated>,
        "errors": [...]} and exits – used by the admin API, so the core never
        imports plugin code itself.

Protocol, one JSON object per line:
    stdout: {"event": "ready", "api_version": 1}
            {"event": "active", "value": true}
            {"event": "metadata", "value": {...}}
            {"event": "next_source"}   play the input's next item (it stays on air)
            {"event": "info", "value": "3 waiting"}   status line for the admin page
            {"event": "changed"}       viewer_items() changed
            {"event": "fatal", "message": "..."}
            {"id": 7, "result": ...} | {"id": 7, "error": "..."}
    stdin:  {"id": 7, "method": "get_source" | "can_release" | "viewer_schema" | "viewer_items"}
            {"id": 8, "method": "viewer_submit", "params": {"form": {...}, "viewer": {...}}}
            {"id": 9, "method": "remove_item", "params": {"id": "..."}}
            {"method": "playback", "params": {"kind": "ended", "detail": ""}}
            {"method": "stop"}

Everything a plugin logs goes to stderr, which the core writes to its log.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from lanstreamer_sdk import (
    API_VERSION,
    InputPlugin,
    ManifestError,
    PlaybackEvent,
    PluginContext,
    Rejected,
    Store,
    Viewer,
    load_manifest,
    source_to_dict,
)

log = logging.getLogger("lanstreamer.plugins.host")


# stdout is the channel to the core. Plugin code that prints (or a library's
# progress output) would corrupt it, so everything else goes to stderr – the
# core writes that to its log.
_channel = sys.stdout
sys.stdout = sys.stderr


def send(message: dict[str, Any]) -> None:
    _channel.write(json.dumps(message) + "\n")
    _channel.flush()


def load_plugin(folder: Path) -> type[InputPlugin]:
    manifest = load_manifest(folder)
    module_name, class_name = manifest.entry.split(":")
    sys.path.insert(0, str(folder))
    if (folder / ".deps").is_dir():  # requirements installed at upload (plugins/install.py)
        sys.path.insert(0, str(folder / ".deps"))
    cls = getattr(importlib.import_module(module_name), class_name)
    if not (isinstance(cls, type) and issubclass(cls, InputPlugin)):
        raise ManifestError(f"{manifest.entry} is not an InputPlugin")
    return cls


async def serve(plugin: InputPlugin) -> None:
    loop = asyncio.get_running_loop()
    commands: asyncio.Queue[dict | None] = asyncio.Queue()

    def read_stdin() -> None:  # asyncio cannot read pipes on Windows' stdin
        for line in sys.stdin:
            try:
                loop.call_soon_threadsafe(commands.put_nowait, json.loads(line))
            except json.JSONDecodeError:
                log.warning("ignoring invalid command: %s", line.strip())
        loop.call_soon_threadsafe(commands.put_nowait, None)

    threading.Thread(target=read_stdin, daemon=True).start()

    async def watch() -> None:
        async for active in plugin.watch_active():
            send({"event": "active", "value": bool(active)})

    async def call(request_id: int, method: str, params: dict[str, Any]) -> None:
        try:
            if method == "get_source":
                result: Any = source_to_dict(await plugin.get_source())
            elif method == "can_release":
                result = bool(await plugin.can_release())
            elif method == "viewer_schema":
                form = type(plugin).ViewerForm
                result = None if form is None else {
                    "title": type(plugin).viewer_title, "schema": form.model_json_schema(),
                }  # fmt: skip
            elif method == "viewer_submit":
                form_cls = type(plugin).ViewerForm
                if form_cls is None:
                    raise Rejected("this input takes no submissions")
                try:
                    form = form_cls.model_validate(params.get("form") or {})
                except ValidationError as exc:
                    raise Rejected("; ".join(
                        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                    )) from exc  # fmt: skip
                result = await plugin.on_viewer_submit(form, Viewer(**params["viewer"]))
            elif method == "viewer_items":
                result = [asdict(item) for item in await plugin.viewer_items()]
            elif method == "remove_item":
                await plugin.remove_item(str(params["id"]))
                result = None
            else:
                raise ValueError(f"unknown method {method}")
            send({"id": request_id, "result": result})
        except Rejected as exc:
            send({"id": request_id, "error": str(exc), "rejected": True})
        except Exception as exc:
            log.exception("%s failed", method)
            send({"id": request_id, "error": f"{exc.__class__.__name__}: {exc}"})

    await plugin.start()
    send({"event": "ready", "api_version": API_VERSION})
    watcher = asyncio.create_task(watch())
    tasks: set[asyncio.Task] = set()
    try:
        while (command := await commands.get()) is not None:
            method = command.get("method")
            if method == "stop":
                break
            if method == "playback":
                task = asyncio.create_task(plugin.on_playback(PlaybackEvent(**command["params"])))
            else:
                task = asyncio.create_task(call(command["id"], method, command.get("params") or {}))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        watcher.cancel()
        await plugin.stop()


def check(folder: Path, settings_json: str) -> int:
    """Settings schema and validation for the admin API."""
    try:
        cls = load_plugin(folder)
    except (ManifestError, ImportError, AttributeError, ValueError) as exc:
        send({"fatal": f"{exc.__class__.__name__}: {exc}"})
        return 2
    result: dict[str, Any] = {"schema": cls.Settings.model_json_schema(), "errors": []}
    try:
        result["settings"] = cls.Settings.model_validate(json.loads(settings_json)).model_dump(mode="json")
    except ValidationError as exc:
        result["errors"] = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
        ]
    except ValueError as exc:
        result["errors"] = [{"field": "", "message": f"invalid JSON: {exc}"}]
    send(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="lan-streamer plugin host")
    parser.add_argument("--plugin", type=Path, required=True, help="folder with plugin.toml")
    parser.add_argument("--instance", default="check")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--settings", default="{}", help="settings as JSON")
    parser.add_argument("--check", action="store_true", help="print schema + validation, exit")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.plugin, args.settings)
    if args.data_dir is None:
        parser.error("--data-dir is required")

    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format=f"[{args.instance}] %(levelname)s %(message)s"
    )
    try:
        cls = load_plugin(args.plugin)
        settings = cls.Settings.model_validate(json.loads(args.settings))
    except (ManifestError, ImportError, AttributeError, ValidationError, ValueError) as exc:
        send({"event": "fatal", "message": f"{exc.__class__.__name__}: {exc}"})
        return 2

    args.data_dir.mkdir(parents=True, exist_ok=True)
    ctx = PluginContext(
        instance_id=args.instance,
        data_dir=args.data_dir,
        log=logging.getLogger(args.instance),
        store=Store(args.data_dir / "store.json"),
        _publish_metadata=lambda m: send({"event": "metadata", "value": asdict(m)}),
        _request_next=lambda: send({"event": "next_source"}),
        _publish_info=lambda text: send({"event": "info", "value": str(text)}),
        _publish_changed=lambda: send({"event": "changed"}),
    )
    try:
        asyncio.run(serve(cls(ctx, settings)))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.exception("plugin crashed")
        send({"event": "fatal", "message": f"{exc.__class__.__name__}: {exc}"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
