"""The SDK on PyPI: what is published when, and a changed SDK cannot go unnoticed."""

import importlib.util
import io
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pypi_state", ROOT / "packaging/sdk/pypi_state.py")
pypi_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pypi_state)


def wheel(code: str, generator: str = "hatchling 1") -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("lanstreamer_sdk/plugin.py", code)
        archive.writestr("lanstreamer_sdk-1.0.0.dist-info/WHEEL", generator)
    return data.getvalue()


def test_a_version_not_on_pypi_is_new():
    assert pypi_state.state(wheel("a"), None) == "new"


def test_only_the_code_counts():
    assert pypi_state.state(wheel("a", "hatchling 2"), wheel("a", "hatchling 1")) == "same"
    assert pypi_state.state(wheel("b"), wheel("a")) == "different"


def test_changed_code_without_a_new_version_fails_the_run(tmp_path, monkeypatch, capsys):
    path = tmp_path / "lanstreamer_sdk-1.0.0-py3-none-any.whl"
    path.write_bytes(wheel("b"))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(pypi_state, "wheel_on_pypi", lambda version: wheel("a"))
    assert pypi_state.main([str(path)]) == 1
    assert "publish=false" in output.read_text()
    assert "::error::" in capsys.readouterr().out


def test_new_version_is_published(tmp_path, monkeypatch):
    path = tmp_path / "lanstreamer_sdk-1.1.0-py3-none-any.whl"
    path.write_bytes(wheel("b"))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(pypi_state, "wheel_on_pypi", lambda version: None)
    assert pypi_state.main([str(path)]) == 0
    assert "publish=true" in output.read_text()


def test_workflow_publishes_only_from_tags_and_main_with_the_least_rights():
    flow = yaml.safe_load((ROOT / ".github/workflows/sdk.yml").read_text(encoding="utf-8"))
    publish = flow["jobs"]["publish"]
    assert publish["needs"] == "build"
    assert "needs.build.outputs.publish == 'true'" in publish["if"]
    assert "refs/heads/main" in publish["if"] and "github.ref_type == 'tag'" in publish["if"]
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["environment"] == "pypi"  # PyPI's trusted publisher names this environment
    assert flow["permissions"] == {"contents": "read"}
    assert "id-token" not in flow["jobs"]["build"].get("permissions", {})
