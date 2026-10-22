# Development

## Tests

| What | How | Needs |
| --- | --- | --- |
| Unit tests (scheduler, store, runtime with real plugin processes, API, …) | `pytest` | Python 3.12+ and `pip install -e ".[dev]"` |
| Local end-to-end run: web API + admin page in a real browser | `python tests/e2e/run_local.py` | additionally `pip install playwright` (no browser download); on Windows/WSL it drives the installed Microsoft Edge headless |
| Linux packages for real: gst-plugins-rs build, `.deb` installed with apt, self-test, the app with GStreamer, API checks, admin and viewer pages, ingest, restream, smoke test, purge, `install.sh` | `python tests/local_linux.py [--keep]` | Docker (throwaway Ubuntu 24.04 containers; images `lanstreamer-*`), `dpkg-deb`; the first run builds the Rust plugins (~15 min, cached) |
| Windows installer, NVENC, WebRTC playback, systemd | GitHub Actions (`windows.yml`, `linux.yml`) | only in CI and on the test PC |

`run_local.py` starts lan-streamer with `--simulate`: everything runs for real (web server, admin page, API, scheduler, plugin processes) except the video pipeline, which only reports "playing" / "ended". It uses a fresh data folder, so it never touches a real configuration. `--keep-running` leaves it running at `http://localhost:18080/admin` (password `ci-password-123`) to look at it yourself.

`local_linux.py` runs the steps of `linux.yml` in containers: it catches broken packages, missing GStreamer elements and runtime errors with the real pipeline before CI does. Not covered there: systemd (containers have none) and WebRTC playback (the browser cannot reach the container's media ports). `--keep` leaves the app at `http://localhost:18180`; remove it with `docker rm -f lanstreamer-debtest`, the images with `docker rmi lanstreamer-rs:0.13 lanstreamer-deps:local lanstreamer-debtest:local`.

Run the local tests before pushing – `run_local.py` always, `local_linux.py` when the packaging, the pipeline or a GStreamer-dependent feature changed; push (and so build) only what needs the Windows runner or the test PC to be checked.

## A display PC or TV (kiosk mode)

`http://<server>:8080/tv` shows only the stream. In a normal browser a click turns on the sound and goes full screen. A display PC can open it without anyone at it, with sound:

```
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --kiosk http://<server>:8080/tv --edge-kiosk-type=fullscreen --autoplay-policy=no-user-gesture-required --no-first-run
```

Put that in the PC's autostart (`shell:startup`). Alt+F4 ends the kiosk mode.

## Writing a plugin

Start from `examples/hello-plugin/` (`plugin.toml` + one Python file). Zip the folder and upload it in the admin page. The API is described in `docs/architecture.md` (Plugin system) and in `src/lanstreamer_sdk/`.

## Simulation mode

`python -m lanstreamer --simulate` – no GStreamer, no MediaMTX. Useful to work on the web pages and the scheduler; the player shows no video.
