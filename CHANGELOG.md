# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Architecture concept and roadmap.
- Windows installer (Inno Setup, built and install-tested in CI): all users to `C:\Program Files\lan-streamer` or only for the current user, listed under Settings → Apps, complete uninstall; bundles Python 3.13, an own PyGObject build, the GStreamer runtime (whitelisted plugins only, 74 MB installer) and MediaMTX.
- Start menu entries for 1080p60 and 1080p30 that run windowless with a tray icon (open player, on air, open log / log folder, quit); app icon.
- Self-test `python -m lanstreamer.doctor`: GStreamer, required elements, plugins that fail to load, H.264 encoders; shows the build version.
- Encoder auto-detection for NVIDIA, AMD, Intel and CPU with a manual override; each probe runs in its own process.
- Output worker: 1080p60 (or 30) with seamless crossfade switching, H.264 constrained baseline + Opus, published to MediaMTX via SRT.
- Player page: WebRTC (WHEP), on-air input, encoder, H.264 profile, tracks sent to MediaMTX, WebRTC statistics, latency clock, input list with state and priority, "Next input".
- Scheduler (priority, immediate interruption, equal-priority hand-over, min/max duration, force/block, fallback, debounce).
- Plugin SDK v1 (`lanstreamer_sdk`), one process per plugin instance, built-in plugins "test-pattern" and "playlist".
- Core runtime: plugins + scheduler + output worker; the output reports when a source ended or broke.
- Configuration in SQLite with live changes while running.

### Fixed

- Black picture in the browser on fast machines: mpegtsmux's stale stream header made MediaMTX drop the video track.
- GPU encoders on a fresh Windows: the Visual C++ runtime is now bundled.
- Output to MediaMTX broke after ~20 s with RTSP on Windows; now SRT.
