# Development

## Tests

| What | How | Needs |
| --- | --- | --- |
| Unit tests (scheduler, store, runtime with real plugin processes, API, …) | `pytest` | Python 3.12+ and `pip install -e ".[dev]"` |
| Local end-to-end run: web API + admin page in a real browser | `python tests/e2e/run_local.py` | additionally `pip install playwright` (no browser download); on Windows/WSL it drives the installed Microsoft Edge headless |
| Linux packages for real: gst-plugins-rs build, `.deb` installed with apt, self-test, the app with GStreamer, API checks, admin and viewer pages, ingest, restream, smoke test, purge, `install.sh` | `python tests/local_linux.py [--keep]` | Docker (throwaway Ubuntu 24.04 containers; images `lanstreamer-*`), `dpkg-deb`; the first run builds the Rust plugins (~15 min, cached) |
| Windows installer, NVENC, WebRTC playback, systemd | GitHub Actions (`windows.yml`, `linux.yml`) | only in CI and on the test PC |

**The CI runs only when started on purpose**, not on every push to `main`:

- **Full test run** (unit tests, build, install, every end-to-end check, on Windows and Linux): push the commit to the branch `ci` – `git push origin HEAD:ci` – or Actions → Windows / Linux → "Run workflow". A newer push to `ci` cancels a run still going.
- Windows extras: the real update (`check_update.py`, installs a second time) when `updater.py`, `packaging/windows/`, the test or the workflow changed since the last push to `ci` (always on "Run workflow" and for releases); the doc screenshots (`doc_screenshots.py`) only with "Run workflow" and "screenshots" ticked.
- **Release tag:** builds, installs, runs the self-test (Windows also the update test) and attaches the files. The unit tests and the other end-to-end checks run only if this commit has no green full run yet – so run the full test before tagging.

`run_local.py` starts lan-streamer with `--simulate`: everything runs for real (web server, admin page, API, scheduler, plugin processes) except the video pipeline, which only reports "playing" / "ended". It uses a fresh data folder, so it never touches a real configuration. `--keep-running` leaves it running at `http://localhost:18080/admin` (password `ci-password-123`) to look at it yourself.

`local_linux.py` runs the steps of `linux.yml` in containers: it catches broken packages, missing GStreamer elements and runtime errors with the real pipeline before CI does. Not covered there: systemd (containers have none) and WebRTC playback (the browser cannot reach the container's media ports). `--keep` leaves the app at `http://localhost:18180`; remove it with `docker rm -f lanstreamer-debtest`, the images with `docker rmi lanstreamer-rs:0.13 lanstreamer-deps:local lanstreamer-debtest:local`.

Run the local tests before a CI run – `run_local.py` always, `local_linux.py` when the packaging, the pipeline or a GStreamer-dependent feature changed; start the CI (push to `ci`) only for what needs the Windows runner or the test PC to be checked.

## Versions and releases

Versions follow [Semantic Versioning](https://semver.org/); `lanstreamer.__version__` (and `pyproject.toml`) is the version in development. CI builds of `main` are test builds `<version>-test.<run>`; a tag builds exactly the tag's version. Tags have no "v" (`0.1.0`), because Debian versions start with a digit.

A release:

0. Optionally refresh the screenshots: run the Windows workflow by hand with "screenshots" ticked; it uploads `docs-images-<version>` (`tests/e2e/doc_screenshots.py`); copy the pictures to `docs/images/`.
1. Set the version in `pyproject.toml`, `src/lanstreamer/__init__.py` and the two workflows (`tests/test_version.py` checks they agree); in `CHANGELOG.md` turn "Unreleased" into `## [x.y.z] – <date>`. Commit, push, and run the full test on exactly that commit (`git push origin HEAD:ci`) until it is green – the changelog belongs into this commit, so the tag can use the quick run.
2. `gh release create x.y.z --draft --title "lan-streamer x.y.z" --notes-file <the changelog section>` – a draft, so nobody sees it without its files.
3. `git tag x.y.z && git push origin x.y.z` – both workflows build and test that version and attach the Windows installer, the example plugins (upload-ready zips) and the Linux packages to the release.
4. When all files are there: `gh release edit x.y.z --draft=false`.
5. Set the next version in development (e.g. `0.1.1`) in the same places.

## A display PC or TV (kiosk mode)

`http://<server>:8080/tv` shows only the stream. In a normal browser a click turns on the sound and goes full screen. A display PC can open it without anyone at it, with sound:

```
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --kiosk http://<server>:8080/tv --edge-kiosk-type=fullscreen --autoplay-policy=no-user-gesture-required --no-first-run
```

Put that in the PC's autostart (`shell:startup`). Alt+F4 ends the kiosk mode.

## Writing a plugin

See the [plugin guide](plugin-guide.md). In short: copy one of the [examples](../examples) (`web-stream`, `stream-queue`, `hello-plugin`), then `python -m lanstreamer_sdk check <folder>`, `pytest <folder>`, `python -m lanstreamer_sdk pack <folder>` and upload the zip in the admin page. The examples' tests run with the normal `pytest`; `tests/test_plugin_contract.py` pins the plugin API v1.

## Simulation mode

`python -m lanstreamer --simulate` – no GStreamer, no MediaMTX. Useful to work on the web pages and the scheduler; the player shows no video.
