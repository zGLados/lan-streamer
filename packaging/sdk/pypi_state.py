"""Does the built SDK wheel have to go to PyPI? Used by .github/workflows/sdk.yml.

    python pypi_state.py <wheel>

new        this version is not on PyPI yet: publish it
same       this version is there with the same code: nothing to do
different  this version is there with other code: the version in pyproject.toml has to
           go up (exit code 1), a PyPI version can never be replaced
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

PROJECT = "lanstreamer-sdk"


def code_of(wheel: bytes) -> dict[str, bytes]:
    """The package's files; not the metadata, which changes with the build tool's version."""
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        return {n: archive.read(n) for n in archive.namelist() if n.startswith("lanstreamer_sdk/")}


def wheel_on_pypi(version: str) -> bytes | None:
    """The published wheel of this version, None if the version does not exist."""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{PROJECT}/{version}/json", timeout=30) as answer:
            files = json.load(answer)["urls"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    url = next(f["url"] for f in files if f["packagetype"] == "bdist_wheel")
    with urllib.request.urlopen(url, timeout=60) as answer:
        return answer.read()


def state(wheel: bytes, published: bytes | None) -> str:
    if published is None:
        return "new"
    return "same" if code_of(wheel) == code_of(published) else "different"


def main(argv: list[str]) -> int:
    path = Path(argv[0])
    version = path.name.split("-")[1]  # lanstreamer_sdk-1.0.0-py3-none-any.whl
    result = state(path.read_bytes(), wheel_on_pypi(version))
    print(f"{PROJECT} {version}: {result}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as file:
            file.write(f"publish={'true' if result == 'new' else 'false'}\n")
    if result == "different":
        print(f"::error::The SDK changed but {version} is already on PyPI: "
              "raise the version in packaging/sdk/pyproject.toml")  # fmt: skip
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
