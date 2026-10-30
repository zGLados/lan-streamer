import asyncio
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from lanstreamer.plugins.install import install_zip
from lanstreamer.plugins.registry import discover
from lanstreamer_sdk import API_VERSION, ManifestError
from lanstreamer_sdk.manifest import BUILTIN_IDS
from lanstreamer_sdk.scaffold import module_name
from lanstreamer_sdk.tools import check, main, pack

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
EXAMPLE_DIRS = sorted(p for p in EXAMPLES.iterdir() if (p / "plugin.toml").is_file())
REQUIRED_SETTINGS = {"web-stream": {"url": "https://example.org/live.m3u8"}}


def test_there_are_examples():
    assert {p.name for p in EXAMPLE_DIRS} >= {"hello-plugin", "stream-queue", "web-stream"}


@pytest.mark.parametrize("folder", EXAMPLE_DIRS, ids=lambda p: p.name)
def test_every_example_checks_packs_and_installs(folder, tmp_path):
    result = check(folder, REQUIRED_SETTINGS.get(folder.name))
    assert result["errors"] == [] and result["api_version"] == 1

    archive = pack(folder, tmp_path / "plugin.zip")
    names = zipfile.ZipFile(archive).namelist()
    assert "plugin.toml" in names
    assert not [n for n in names if "__pycache__" in n or n.startswith("tests/") or n.endswith(".zip")]

    async def no_pip(requirements, target):
        raise AssertionError("the examples need no requirements")

    manifest = asyncio.run(install_zip(archive.read_bytes(), tmp_path / "plugins", set(discover()), no_pip))
    assert manifest.id == result["id"]


def test_check_reports_the_forms_and_invalid_settings():
    result = check(EXAMPLES / "stream-queue", {"max_queue": 0})
    assert result["viewer_form"]["title"] == "Queue a stream"
    assert "url" in result["viewer_form"]["schema"]["properties"]
    assert result["errors"][0]["field"] == "max_queue"


def test_check_of_a_broken_plugin(tmp_path):
    (tmp_path / "plugin.toml").write_text(
        'id = "broken"\nname = "B"\nversion = "1"\napi_version = 1\nentry = "nothere:Plugin"\n'
    )
    with pytest.raises(ImportError):
        check(tmp_path)
    assert main(["check", str(tmp_path)]) == 2
    (tmp_path / "plugin.toml").write_text('id = "broken"\n')
    with pytest.raises(ManifestError):
        check(tmp_path)


def test_new_plugin_passes_everything_a_plugin_has_to_pass(tmp_path):
    folder = tmp_path / "my-camera"
    assert main(["new", str(folder), "--name", 'My "camera"']) == 0

    result = check(folder)
    assert result["id"] == "my-camera" and result["errors"] == []
    assert (folder / "my_camera_plugin.py").is_file()

    tests = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(folder)],
                           cwd=folder, check=False, capture_output=True, text=True)  # fmt: skip
    assert tests.returncode == 0, tests.stdout + tests.stderr

    async def no_pip(requirements, target):
        raise AssertionError("a new plugin needs no requirements")

    archive = pack(folder, tmp_path / "plugin.zip")
    manifest = asyncio.run(install_zip(archive.read_bytes(), tmp_path / "plugins", set(discover()), no_pip))
    assert (manifest.id, manifest.name) == ("my-camera", 'My "camera"')


@pytest.mark.parametrize("plugin_id, module", [
    ("camera", "camera_plugin"), ("my-plugin", "my_plugin"), ("4k-feed", "plugin_4k_feed_plugin"),
])  # fmt: skip
def test_module_names_are_importable_and_shadow_nothing(plugin_id, module):
    assert module_name(plugin_id) == module and module.isidentifier()


@pytest.mark.parametrize("name", ["Bad_Name", "x", "youtube"])
def test_new_refuses_unusable_ids(tmp_path, name, capsys):
    assert main(["new", str(tmp_path / name)]) == 2
    assert "error" in capsys.readouterr().err
    assert not (tmp_path / name).exists()


def test_new_does_not_touch_an_existing_folder(tmp_path):
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / "notes.txt").write_text("keep")
    assert main(["new", str(folder)]) == 2
    assert [p.name for p in folder.iterdir()] == ["notes.txt"]


def test_builtin_ids_are_known_to_the_sdk():
    assert BUILTIN_IDS == set(discover())


def test_check_and_pack_refuse_a_builtin_id(tmp_path):
    (tmp_path / "plugin.toml").write_text(
        'id = "youtube"\nname = "Y"\nversion = "1"\napi_version = 1\nentry = "y:Plugin"\n'
    )
    assert main(["check", str(tmp_path)]) == 2
    assert main(["pack", str(tmp_path)]) == 2


def test_sdk_works_without_lan_streamer():
    """Plugin authors install only lanstreamer-sdk (packaging/sdk)."""
    code = (
        "import sys; sys.modules['lanstreamer'] = None\n"
        "import lanstreamer_sdk, lanstreamer_sdk.testing, lanstreamer_sdk.tools, lanstreamer_sdk.scaffold"
    )
    run = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_sdk_package_version_follows_the_plugin_api():
    project = tomllib.loads((ROOT / "packaging/sdk/pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"].split(".")[0] == str(API_VERSION)


def test_cli(tmp_path, capsys):
    assert main(["check", str(EXAMPLES / "hello-plugin")]) == 0
    assert '"id": "hello"' in capsys.readouterr().out
    assert main(["check", str(EXAMPLES / "hello-plugin"), "--settings", '{"active_seconds": 0}']) == 1
    assert main(["pack", str(EXAMPLES / "hello-plugin"), "-o", str(tmp_path / "h.zip")]) == 0
    assert (tmp_path / "h.zip").is_file()
