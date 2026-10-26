import asyncio
import zipfile
from pathlib import Path

import pytest

from lanstreamer.plugins.install import install_zip
from lanstreamer.plugins.registry import discover
from lanstreamer_sdk import ManifestError
from lanstreamer_sdk.tools import check, main, pack

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE_DIRS = sorted(p for p in EXAMPLES.iterdir() if (p / "plugin.toml").is_file())


def test_there_are_examples():
    assert {p.name for p in EXAMPLE_DIRS} >= {"hello-plugin", "stream-queue"}


@pytest.mark.parametrize("folder", EXAMPLE_DIRS, ids=lambda p: p.name)
def test_every_example_checks_packs_and_installs(folder, tmp_path):
    result = check(folder)
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


def test_cli(tmp_path, capsys):
    assert main(["check", str(EXAMPLES / "hello-plugin")]) == 0
    assert '"id": "hello"' in capsys.readouterr().out
    assert main(["check", str(EXAMPLES / "hello-plugin"), "--settings", '{"active_seconds": 0}']) == 1
    assert main(["pack", str(EXAMPLES / "hello-plugin"), "-o", str(tmp_path / "h.zip")]) == 0
    assert (tmp_path / "h.zip").is_file()
