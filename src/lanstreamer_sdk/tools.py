"""Tools for plugin authors: start a plugin, check its folder, pack it for the upload.

    python -m lanstreamer_sdk new <folder> [--name '<display name>']
        creates a working plugin; the folder's name becomes the plugin's id.
    python -m lanstreamer_sdk check <folder> [--settings '<json>']
        loads the plugin like lan-streamer does and prints its settings form
        (JSON Schema), the validated settings and its viewer form, or the error.
    python -m lanstreamer_sdk pack <folder> [-o <file.zip>]
        zips the plugin for the admin page (Plugins → Upload), without caches,
        tests and installed requirements.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from lanstreamer_sdk.manifest import ManifestError, check_id_is_free, load_manifest
from lanstreamer_sdk.plugin import InputPlugin
from lanstreamer_sdk.scaffold import new_plugin

DEPS = ".deps"  # requirements installed at upload (lanstreamer/plugins/install.py)
# never in the upload: caches, installed requirements, the plugin's own tests
SKIP_DIRS = {"__pycache__", DEPS, "tests", ".git", ".venv", "venv", ".pytest_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".zip"}


def load_plugin(folder: Path) -> type[InputPlugin]:
    """Imports the plugin class named in folder/plugin.toml (its `entry`)."""
    manifest = load_manifest(folder)
    module_name, class_name = manifest.entry.split(":")
    sys.path.insert(0, str(folder))
    if (folder / DEPS).is_dir():
        sys.path.insert(0, str(folder / DEPS))
    cls = getattr(importlib.import_module(module_name), class_name)
    if not (isinstance(cls, type) and issubclass(cls, InputPlugin)):
        raise ManifestError(f"{manifest.entry} is not an InputPlugin")
    return cls


def check(folder: Path, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the admin page gets from the plugin; raises on a broken plugin."""
    manifest = load_manifest(folder)
    check_id_is_free(manifest.id)
    cls = load_plugin(folder)
    result: dict[str, Any] = {
        "id": manifest.id, "version": manifest.version, "api_version": manifest.api_version,
        "schema": cls.Settings.model_json_schema(), "errors": [],
        "viewer_form": None if cls.ViewerForm is None else {
            "title": cls.viewer_title, "schema": cls.ViewerForm.model_json_schema(),
        },
    }  # fmt: skip
    try:
        result["settings"] = cls.Settings.model_validate(settings or {}).model_dump(mode="json")
    except ValidationError as exc:
        result["errors"] = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
        ]
    return result


def pack(folder: Path, out: Path | None = None) -> Path:
    """Zips the plugin folder (plugin.toml at the top of the zip)."""
    manifest = load_manifest(folder)
    check_id_is_free(manifest.id)
    out = out or folder.parent / f"{manifest.id}-{manifest.version}.zip"
    files = sorted(
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix not in SKIP_SUFFIXES
        and not any(part in SKIP_DIRS for part in p.relative_to(folder).parts[:-1])
    )  # fmt: skip
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(folder).as_posix())
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lanstreamer_sdk", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    commands = parser.add_subparsers(dest="command", required=True)
    new_cmd = commands.add_parser("new", help="create a working plugin in a new folder")
    new_cmd.add_argument("folder", type=Path)
    new_cmd.add_argument("--name", default="", help="display name in the admin page")
    check_cmd = commands.add_parser("check", help="load the plugin and show its forms")
    check_cmd.add_argument("folder", type=Path)
    check_cmd.add_argument("--settings", default="{}", help="settings to validate, as JSON")
    pack_cmd = commands.add_parser("pack", help="zip the plugin for the upload")
    pack_cmd.add_argument("folder", type=Path)
    pack_cmd.add_argument("-o", "--output", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command == "new":
            print(f"created {new_plugin(args.folder, args.name)}")
            return 0
        if args.command == "check":
            result = check(args.folder, json.loads(args.settings))
            print(json.dumps(result, indent=2))
            return 1 if result["errors"] else 0
        print(pack(args.folder, args.output))
        return 0
    except (ManifestError, ImportError, AttributeError, ValueError) as exc:
        print(f"error: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
