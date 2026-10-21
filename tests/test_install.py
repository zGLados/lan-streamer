import asyncio
import io
import zipfile
from pathlib import Path

import pytest

from lanstreamer.plugins.install import InstallError, install_zip, remove
from lanstreamer.plugins.registry import BUILTIN, discover

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "hello-plugin"


def zip_of(folder: Path, prefix: str = "", extra: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for file in folder.iterdir():
            if file.is_file():
                z.write(file, prefix + file.name)
        for name, text in (extra or {}).items():
            z.writestr(name, text)
    return buffer.getvalue()


def builtin_ids():
    return {p.name.replace("_", "-") for p in BUILTIN.iterdir() if p.is_dir()} | set(discover())


def install(data, plugins, pip=None):
    async def no_pip(requirements, target):
        raise AssertionError("pip must not run for a plugin without requirements")

    return asyncio.run(install_zip(data, plugins, builtin_ids(), pip or no_pip))


def test_example_plugin_installs_and_is_discovered(tmp_path):
    manifest = install(zip_of(EXAMPLE), tmp_path)
    assert (manifest.id, manifest.version) == ("hello", "1.0.0")
    assert discover(tmp_path)["hello"].path == tmp_path / "hello"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]  # no staging leftovers


def test_plugin_in_a_folder_inside_the_zip(tmp_path):
    assert install(zip_of(EXAMPLE, prefix="hello-plugin/"), tmp_path).id == "hello"


def test_reinstall_replaces_the_old_version(tmp_path):
    install(zip_of(EXAMPLE), tmp_path)
    newer = (EXAMPLE / "plugin.toml").read_text().replace('version = "1.0.0"', 'version = "1.1.0"')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("plugin.toml", newer)
        z.write(EXAMPLE / "hello.py", "hello.py")
    assert install(buffer.getvalue(), tmp_path).version == "1.1.0"
    assert discover(tmp_path)["hello"].version == "1.1.0"


def test_requirements_go_to_the_plugins_own_folder(tmp_path):
    calls = []

    async def fake_pip(requirements, target):
        calls.append((requirements, target.name))
        (target / "fakepkg").mkdir(parents=True)
        (target / "fakepkg" / "__init__.py").write_text("VALUE = 42\n")

    toml = (EXAMPLE / "plugin.toml").read_text().replace("requirements = []", 'requirements = ["fakepkg>=1"]')
    code = (EXAMPLE / "hello.py").read_text().replace("import time", "import time\n\nimport fakepkg  # noqa: F401")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("plugin.toml", toml)
        z.writestr("hello.py", code)
    install(buffer.getvalue(), tmp_path, pip=fake_pip)
    assert calls == [(["fakepkg>=1"], ".deps")]
    assert (tmp_path / "hello" / ".deps" / "fakepkg" / "__init__.py").is_file()


@pytest.mark.parametrize(
    "files, message",
    [
        ({"readme.txt": "x"}, "plugin.toml not found"),
        ({"plugin.toml": 'id = "x"'}, "is missing"),
        ({"../evil.py": "x", "plugin.toml": ""}, "unsafe path"),
        ({"plugin.toml": (EXAMPLE / "plugin.toml").read_text().replace('"hello"', '"playlist"'),
          "hello.py": (EXAMPLE / "hello.py").read_text()}, "built-in plugin"),
        ({"plugin.toml": (EXAMPLE / "plugin.toml").read_text().replace("api_version = 1", "api_version = 9"),
          "hello.py": ""}, "needs plugin API 9"),
        ({"plugin.toml": (EXAMPLE / "plugin.toml").read_text(), "hello.py": "raise RuntimeError('boom')"},
         "cannot be loaded"),
    ],
)  # fmt: skip
def test_bad_plugins_are_rejected_with_a_clear_message(tmp_path, files, message):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    with pytest.raises(InstallError, match=message):
        install(buffer.getvalue(), tmp_path)
    assert not (tmp_path / "hello").exists() and not (tmp_path / "playlist").exists()


def test_not_a_zip(tmp_path):
    with pytest.raises(InstallError, match="not a zip"):
        install(b"hello", tmp_path)


def test_remove(tmp_path):
    install(zip_of(EXAMPLE), tmp_path)
    remove(tmp_path, "hello")
    assert "hello" not in discover(tmp_path) or discover(tmp_path)["hello"].path != tmp_path / "hello"
    with pytest.raises(InstallError):
        remove(tmp_path, "hello")
