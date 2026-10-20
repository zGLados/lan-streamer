"""Finds installed plugins: the built-in ones and those uploaded by the admin."""

from __future__ import annotations

import logging
from pathlib import Path

from lanstreamer_sdk import Manifest, ManifestError, load_manifest

BUILTIN = Path(__file__).resolve().parent / "builtin"

log = logging.getLogger("lanstreamer.plugins")


def discover(*folders: Path) -> dict[str, Manifest]:
    """Plugin id → manifest. Later folders override earlier ones (uploads over built-ins)."""
    found: dict[str, Manifest] = {}
    for folder in (BUILTIN, *folders):
        if not folder.is_dir():
            continue
        for plugin_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
            try:
                manifest = load_manifest(plugin_dir)
            except ManifestError as exc:
                log.warning("Skipping plugin %s: %s", plugin_dir.name, exc)
                continue
            found[manifest.id] = manifest
    return found
