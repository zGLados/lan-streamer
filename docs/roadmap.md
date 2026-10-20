# Roadmap

The milestones follow the requirements specification. Each one ends with something that runs.

## M1 – Prototype: seamless switching

Goal: prove the riskiest part first. A 1080p stream plays in the browser over WebRTC and switches between two sources without a dropout.

- [ ] Dev setup on Windows and Linux: GStreamer 1.26 (incl. gst-plugins-rs), MediaMTX, Python
- [ ] Check PyGObject with the official GStreamer binaries on Windows; if it does not work, write the output worker in Rust (see architecture)
- [ ] Output worker: background layer + two input slots → compositor → H.264/Opus → MediaMTX
- [ ] Switch between a local file and a second source (file or URL) by command, with a crossfade
- [ ] Minimal web page with a WHEP player; measure the latency
- [ ] Encoder auto-detection (NVIDIA, AMD, Intel, CPU) with a manual override
- [ ] Measure the CPU/GPU load of one and of two outputs on the target machine

## M2 – Core

- [ ] Plugin SDK v1 (`InputPlugin`, sources, context, test harness)
- [ ] Plugin host process + JSON-RPC, supervisor with restart and backoff
- [ ] Scheduler as a pure function, fully unit-tested
- [ ] Activity events + polling, debounce, min/max duration, admin overrides
- [ ] SQLite store, FastAPI admin API with login
- [ ] Admin SPA: outputs, input instances, rankings, generated settings forms, live status
- [ ] Plugin upload (.zip, manifest, version check, per-plugin virtualenv)
- [ ] Fallback playlist as the first real plugin

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
