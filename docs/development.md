# Development

## Tests

| What | How | Needs |
| --- | --- | --- |
| Unit tests (scheduler, store, runtime with real plugin processes, API, …) | `pytest` | Python 3.12+ and `pip install -e ".[dev]"` |
| Local end-to-end run: web API + admin page in a real browser | `python tests/e2e/run_local.py` | additionally `pip install playwright` (no browser download); on Windows/WSL it drives the installed Microsoft Edge headless |
| Video pipeline, encoders, installer, WebRTC playback | GitHub Actions (`.github/workflows/windows.yml`) | GStreamer – only in CI and on the test PC |

`run_local.py` starts lan-streamer with `--simulate`: everything runs for real (web server, admin page, API, scheduler, plugin processes) except the video pipeline, which only reports "playing" / "ended". It uses a fresh data folder, so it never touches a real configuration. `--keep-running` leaves it running at `http://localhost:18080/admin` (password `ci-password-123`) to look at it yourself.

Run the local tests before pushing; push (and so build) only what needs GStreamer to be checked.

## Writing a plugin

Start from `examples/hello-plugin/` (`plugin.toml` + one Python file). Zip the folder and upload it in the admin page. The API is described in `docs/architecture.md` (Plugin system) and in `src/lanstreamer_sdk/`.

## Simulation mode

`python -m lanstreamer --simulate` – no GStreamer, no MediaMTX. Useful to work on the web pages and the scheduler; the player shows no video.
