"""One version everywhere: pyproject, the package and the CI test builds."""

import tomllib
from pathlib import Path

import lanstreamer

ROOT = Path(__file__).resolve().parents[1]


def test_versions_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == lanstreamer.__version__
    for workflow in ("windows.yml", "linux.yml"):
        text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        assert f"'{{0}}-test.{{1}}', '{lanstreamer.__version__}'" in text, workflow
