# Instructions for AI coding agents

lan-streamer is a self-hosted streaming platform for the LAN: input plugins → scheduler → re-encoded 1080p WebRTC in the browser (Python, GStreamer, MediaMTX). Read [CONTRIBUTING.md](CONTRIBUTING.md) as well: its rules apply to you too. Humans will review what you produce.

## Commands

```
pip install -e ".[dev]"            # in a virtual environment; Python 3.12+
python -m ruff check .             # must pass
python -m pytest                   # must pass; needs no GStreamer
python tests/e2e/run_local.py      # web pages or API changed; needs playwright and Edge or Chrome
python -m lanstreamer --simulate   # the whole app without video
```

Report test results as they are. If you could not run something, say so; do not claim that it works. A green unit test run says nothing about real video: pipeline changes (`src/lanstreamer/media/`) need `python tests/local_linux.py` (Docker) or the full CI, which the maintainer starts.

## Where things are

| Path | What |
| --- | --- |
| `src/lanstreamer_sdk/` | **Public** plugin API, also published to PyPI as `lanstreamer-sdk`. May only import the standard library and pydantic. |
| `src/lanstreamer/` | The app, internal. Plugins must not import it. |
| `src/lanstreamer/plugins/builtin/` | Built-in input plugins |
| `src/lanstreamer/media/` | GStreamer pipeline, runs in the output worker process |
| `src/lanstreamer/core/` | Runtime, store (SQLite), worker control |
| `src/lanstreamer/web/` | FastAPI and the pages (plain HTML) |
| `packaging/` | Windows installer, Linux packages, SDK package |
| `docs/` | Architecture, plugin guide, interfaces |

## Rules that are easy to break

- **Do not break plugin API v1.** Only additions with defaults. Never edit `tests/test_plugin_contract.py` to make it pass: a failure there means your change breaks third-party plugins. SDK changes also need a higher version in `packaging/sdk/pyproject.toml`.
- **New plugin? It usually does not belong here.** Plugins are normally separate projects built with `lanstreamer-sdk`. Do not add a built-in plugin unless the maintainer agreed in an issue. If so, follow the checklist in CONTRIBUTING.md.
- **New GStreamer element:** add it to `packaging/windows/gstreamer-plugins.txt` and the self-test. A missing entry passes on a developer machine and fails only for installer users.
- **Endpoints** have to be listed in `docs/interfaces.md` (`tests/test_docs.py`).
- **Never block the event loop** in plugins or the runtime; use `asyncio.to_thread()`. In a plugin process stdout is the channel to the core: log with `self.ctx.log`, do not print.
- **Windows and Linux both.** On Windows, files can be locked while in use and a process may not be stoppable the way it is on Linux.
- **Everything in English:** code, comments, docs, commit messages.
- **Match the surrounding code:** its comment density, naming and idiom. No reformatting or refactoring outside the task.

## What not to do without being asked

- Push to `main`, create tags or releases, publish packages, or start CI runs. Work on a branch; the change arrives through a pull request.
- Change the version in `pyproject.toml` or `src/lanstreamer/__init__.py` (they change only for releases).
- Add dependencies, or install software outside a virtual environment.
- Weaken, skip or delete a test to get a green run. Find out why it fails.
- Commit secrets, generated files or screenshots from test runs.
