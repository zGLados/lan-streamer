# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Architecture concept and roadmap.
- Windows test installer (Inno Setup, built in CI) with bundled Python 3.13, GStreamer runtime and MediaMTX.
- Self-test `python -m lanstreamer.doctor`: checks GStreamer, the required elements and the H.264 encoders.
- Encoder auto-detection for NVIDIA, AMD, Intel and CPU, with a manual override.
