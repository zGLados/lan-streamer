# Roadmap

The milestones follow the requirements specification. Each one ends with something that runs.

## M1 – Prototype: seamless switching

Goal: prove the riskiest part first. A 1080p stream plays in the browser over WebRTC and switches between two sources without a dropout.

- [ ] Dev setup on Windows first (primary platform), then Linux: GStreamer 1.26 (incl. gst-plugins-rs), MediaMTX, Python
- [x] PyGObject with the official GStreamer binaries on Windows (own build for Python 3.13, see architecture)
- [x] Windows test installer built and install-tested in CI (self-test + 45 s streaming smoke test)
- [x] Output worker: background layer + input slots → compositor → H.264/Opus → SRT → MediaMTX (1080p60, CPU encoder ~60 fps in CI)
- [x] Switch between inputs by command with a 1 s crossfade (two test inputs + files/URLs via `--source`)
- [x] Minimal web page with a WHEP player, local clock for latency comparison, WebRTC stats
- [x] Measure the latency in the browser: ~220 ms encoder → screen with NVENC at 1080p60 (test PC, 2026-10-20)
- [x] Encoders for all vendors (NVIDIA, AMD, Intel, CPU) with auto-detection and a manual override, each probe in its own process
- [x] Test NVENC on the NVIDIA test PC (`nvd3d11h264enc` auto-selected, 2026-10-20)
- [ ] Measure the CPU/GPU load of one and of two outputs on the target machine

## M2 – Core

- [x] Plugin SDK v1 (`lanstreamer_sdk`: `InputPlugin`, sources, context, manifest)
- [x] Plugin host process + JSON lines over stdio
- [x] Core runtime (`core/runtime.py`): plugin processes + scheduler + output worker, playback events back to the plugins
- [x] Supervisor with restart and backoff (plugin processes)
- [x] Scheduler as a pure function, fully unit-tested
- [x] Activity events + polling, debounce, min/max duration, overrides (in the runtime; admin UI follows)
- [x] SQLite configuration store (`core/store.py`, versioned schema) and live reconfiguration (`OutputRuntime.apply`)
- [x] FastAPI admin API with login, live status over WebSocket (`web/api.py`)
- [x] Admin page `/admin` (confirmed on the test PC, 2026-10-21): login, inputs (add/edit/delete, priority, fallback, force/block, order by drag and drop), generated settings forms, live status, output settings, logs
- [x] Plugin upload (.zip, manifest and API version check, requirements via pip into the plugin's own folder, replace/remove; example plugin `examples/hello-plugin`)
- [x] Fallback: built-in "Standby" picture on every output; playlist with folders, pictures and automatic next item

## M3 – Reference inputs

- [ ] Twitch top stream (Helix API for the ranking, streamlink for playback)
- [ ] TikTok submissions (queue, per-user limit, max length, submit form on the viewer page)
- [ ] Viewer page shows what is playing

## M4 – Expansion

- [ ] YouTube (live and playlist)
- [ ] RTMP/SRT ingest via MediaMTX
- [ ] Overlays (source, title, submitter)
- [ ] Several outputs, each with its own ranking
- [ ] Optional: restream via RTMP

## M5 – Installers

- [ ] Windows: Inno Setup `.exe` with Python runtime, GStreamer, MediaMTX, Windows service and firewall rules
- [ ] Linux: `.deb` and `.tar.gz` + `install.sh`, systemd service
- [ ] Both built and smoke-tested in GitHub Actions on every release
- [ ] Optional: Docker Compose for Linux servers

## M6 – Opening up

- [ ] Plugin developer guide + example plugin template
- [ ] Stable plugin API v1, contract tests in CI
- [ ] Release for third-party developers
