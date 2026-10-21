"""Installs uploaded plugins (.zip) into the plugins folder (F-PLG-02, F-PLG-04).

1. The zip is checked (size, no paths outside the plugin folder) and unpacked
   into a staging folder. plugin.toml may sit at the top or in one folder.
2. The manifest is checked, including the plugin API version.
3. Its `requirements` are installed with pip into the plugin's own `.deps`
   folder (needs internet at that moment; plugins without requirements don't).
4. The plugin is imported once in a plugin host process (`--check`).
5. Only then it replaces an older version of the same plugin.

Security: a plugin is code that runs on this machine. Only the admin can
upload, and lan-streamer is meant for the local network (NF-06).
"""

from __future__ import annotations

import asyncio
import io
import secrets
import shutil
import sys
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

from lanstreamer.plugins.process import PluginError, check_settings
from lanstreamer_sdk import Manifest, ManifestError, load_manifest

MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
DEPS = ".deps"

Pip = Callable[[list[str], Path], Awaitable[None]]


class InstallError(ValueError):
    pass


async def run_pip(requirements: list[str], target: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
        "--target", str(target), *requirements,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 600)
    except TimeoutError:
        proc.kill()
        raise InstallError("installing the requirements took longer than 10 minutes") from None
    if proc.returncode != 0:
        tail = out.decode(errors="replace").strip().splitlines()[-8:]
        raise InstallError("installing the requirements failed:\n" + "\n".join(tail))


def _unpack(data: bytes, staging: Path) -> Path:
    """Unpacks the zip safely; returns the folder that holds plugin.toml."""
    if len(data) > MAX_ZIP_BYTES:
        raise InstallError(f"the zip is larger than {MAX_ZIP_BYTES // 2**20} MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InstallError("this is not a zip file") from exc
    with archive:
        infos = archive.infolist()
        if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
            raise InstallError(f"the unpacked plugin is larger than {MAX_UNPACKED_BYTES // 2**20} MB")
        for info in infos:
            name = PurePosixPath(info.filename.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts or (name.parts and ":" in name.parts[0]):
                raise InstallError(f"unsafe path in the zip: {info.filename}")
        archive.extractall(staging)
    if (staging / "plugin.toml").is_file():
        return staging
    candidates = [p for p in staging.iterdir() if p.is_dir() and (p / "plugin.toml").is_file()]
    if len(candidates) != 1:
        raise InstallError("plugin.toml not found (at the top of the zip or in one folder)")
    return candidates[0]


async def install_zip(
    data: bytes, plugins_dir: Path, builtin_ids: set[str], pip: Pip = run_pip
) -> Manifest:
    plugins_dir.mkdir(parents=True, exist_ok=True)
    staging = plugins_dir / f".staging-{secrets.token_hex(4)}"
    staging.mkdir()
    try:
        root = _unpack(data, staging)
        try:
            manifest = load_manifest(root)
        except ManifestError as exc:
            raise InstallError(str(exc)) from exc
        if manifest.id in builtin_ids:
            raise InstallError(f"'{manifest.id}' is the id of a built-in plugin – choose another id")
        if manifest.requirements:
            await pip(manifest.requirements, root / DEPS)
        try:
            await check_settings(root)
        except PluginError as exc:
            raise InstallError(f"the plugin cannot be loaded: {exc}") from exc

        final = plugins_dir / manifest.id
        old = plugins_dir / f".old-{manifest.id}-{secrets.token_hex(4)}"
        try:
            if final.exists():
                final.rename(old)
            root.rename(final)
        except OSError as exc:  # Windows: files of a running plugin are locked
            if old.exists() and not final.exists():
                old.rename(final)
            raise InstallError(
                "the installed version is in use and cannot be replaced right now – "
                f"block or delete its inputs, then upload again ({exc.strerror})"
            ) from exc
        shutil.rmtree(old, ignore_errors=True)
        return load_manifest(final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def remove(plugins_dir: Path, plugin_id: str) -> None:
    folder = plugins_dir / plugin_id
    if not (folder / "plugin.toml").is_file():
        raise InstallError(f"no uploaded plugin '{plugin_id}'")
    try:
        shutil.rmtree(folder)
    except OSError as exc:  # Windows: a file is still in use
        raise InstallError(f"the plugin's files are in use, try again in a moment ({exc.strerror})") from exc
