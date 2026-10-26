# Roadmap

The milestones follow the requirements specification. Each one ends with something that runs.

## M1 – Prototype: seamless switching

Goal: prove the riskiest part first. A 1080p stream plays in the browser over WebRTC and switches between two sources without a dropout.

- [ ] Dev setup on Windows first (primary platform), then Linux: GStreamer 1.26 (incl. gst-plugins-rs), MediaMTX, Python
- [x] PyGObject with the official GStreamer binaries on Windows (own build for Python 3.13, see architecture)
- [x] Windows test installer built and install-tested in CI (self-test + 45 s streaming smoke test)
- [x] Output worker: background layer + input slots → compositor → H.264/Opus → SRT → MediaMTX (1080p60, CPU encoder ~60 fps in CI)
- [x] Switch between inputs by command with a 1 s crossfade (two test inputs + files/URLs via `--source`)
- [x] Minimal web page with a WHEP player, local clock for latency comparison (since 0.1.1 a debug option per output), WebRTC stats
- [x] Measure the latency in the browser: ~220 ms encoder → screen with NVENC at 1080p60 (test PC, 2026-10-20)
- [x] Encoders for all vendors (NVIDIA, AMD, Intel, CPU) with auto-detection and a manual override, each probe in its own process
- [x] Test NVENC on the NVIDIA test PC (`nvd3d11h264enc` auto-selected, 2026-10-20)
- [ ] Measure the CPU/GPU load of one and of two outputs on the target machine

## M2 – Core (done, confirmed on the test PC 2026-10-21)

- [x] Plugin SDK v1 (`lanstreamer_sdk`: `InputPlugin`, sources, context, manifest)
- [x] Plugin host process + JSON lines over stdio
- [x] Core runtime (`core/runtime.py`): plugin processes + scheduler + output worker, playback events back to the plugins
- [x] Supervisor with restart and backoff (plugin processes)
- [x] Scheduler as a pure function, fully unit-tested
- [x] Activity events + polling, debounce, min/max duration, overrides (force/block)
- [x] SQLite configuration store (`core/store.py`, versioned schema) and live reconfiguration (`OutputRuntime.apply`)
- [x] FastAPI admin API with login, live status over WebSocket (`web/api.py`)
- [x] Admin page `/admin` (confirmed on the test PC, 2026-10-21): login, inputs (add/edit/delete, priority, fallback, force/block, order by drag and drop), generated settings forms, live status, output settings, logs
- [x] Plugin upload (.zip, manifest and API version check, requirements via pip into the plugin's own folder, replace/remove; example plugin `examples/hello-plugin`)
- [x] Fallback: built-in "Standby" picture on every output; playlist with folders, pictures and automatic next item

## M3 – Reference inputs (without Twitch)

- [x] Short-video submissions (built-in plugin): TikTok and every other link yt-dlp supports; queue, max length, limit per device with display name, yt-dlp updates itself on start
- [x] Input forms on the viewer page, described by the plugin (F-IN-04, F-WEB-03)
- [x] Viewer page shows what is playing (title, channel, submitted by)
- [x] Fullscreen-only page `/tv`: just the stream (no text, no buttons), click for sound and fullscreen, Edge kiosk command for display PCs

## M4 – Expansion

- [x] YouTube (live and playlist)
- [x] RTMP/SRT ingest via MediaMTX
- [x] Overlays (source, title, submitter)
- [x] Several outputs, each with its own ranking
- [x] Optional: restream via RTMP

## M5 – Installers

- [x] Windows: Inno Setup `.exe` with Python runtime, GStreamer, MediaMTX and firewall rules
- [ ] Windows: optional Windows service
- [x] Linux: `.deb` and `.tar.gz` + `install.sh`, systemd service
- [x] Both built and smoke-tested in GitHub Actions on every push
- [ ] Optional: Docker Compose for Linux servers

## M6 – Opening up

- [x] Plugin developer guide (`docs/plugin-guide.md`) + example plugins (`examples/web-stream`, `examples/stream-queue`, with tests), all interfaces in `docs/interfaces.md`, `python -m lanstreamer_sdk check|pack`, test context `lanstreamer_sdk.testing`
- [x] Stable plugin API v1, contract tests in CI (`tests/test_plugin_contract.py`)
- [ ] Release for third-party developers

## Later – to be reviewed

- [x] Updater for lan-streamer itself (0.1.1): checks the GitHub releases, shows them on the admin page, installs on Windows with one click (installer checked by SHA-256, restarts by itself) – requested 2026-10-21
- [ ] Updater on Linux (packages; needs root – e.g. a small helper or apt)
- [x] Screenshots in the docs (player page with submit form, TV view, admin page, "Add input") – taken by the Windows CI (`tests/e2e/doc_screenshots.py`), done for 0.1.0; the installer pages are still missing
- [ ] Twitch top stream (built-in plugin; Helix API for the ranking, streamlink for playback) – moved here 2026-10-21, the owner looks at it again before it is built
