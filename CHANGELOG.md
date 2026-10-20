# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Architecture concept and roadmap.
- Windows test installer (Inno Setup, built in CI) with bundled Python 3.13, GStreamer runtime and MediaMTX.
- Self-test `python -m lanstreamer.doctor`: checks GStreamer, the required elements and the H.264 encoders.
- Encoder auto-detection for NVIDIA, AMD, Intel and CPU, with a manual override; each probe runs in its own process.
- M1 prototype (`python -m lanstreamer`, start menu entries for 1080p60 and 1080p30): output with seamless crossfade switching, published to MediaMTX via SRT, player page with WebRTC (WHEP), status and latency clock.
