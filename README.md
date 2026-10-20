# lan-streamer

A self-hosted, modular media streaming platform for your local network. lan-streamer collects media from pluggable sources, such as the top Twitch stream of a category, TikTok links submitted by viewers, YouTube, your own OBS stream or a fallback playlist. It decides automatically what to show and plays it as a continuous, re-encoded 1080p live stream in the browser, with sub-second latency over WebRTC.

> **Status:** early development. A Windows test installer exists, but so far it only runs a self-test (GStreamer, encoders). See the [roadmap](docs/roadmap.md).

## How it works

- **Inputs** are plugins. Each one reports by itself whether it has something to show right now.
- **The scheduler** puts the highest-priority active input on air. A higher priority interrupts at once; on equal priority the running input decides when to hand over (for example when its video ends). With nothing active, a fallback plays.
- **Outputs** are permanently running encoders. They switch between inputs without the stream ever dropping, and [MediaMTX](https://github.com/bluenviron/mediamtx) serves them via WebRTC and LL-HLS.
- **Everything is configured in the web UI**, including installing new input plugins as `.zip` uploads.

Details: [docs/architecture.md](docs/architecture.md).

## Planned stack

Python 3.12 (asyncio, FastAPI) · GStreamer · MediaMTX · SvelteKit · SQLite

Runs natively on **Windows and Linux**; each release will ship an installer for both. Hardware encoding is picked automatically (NVIDIA, AMD, Intel, or CPU as a fallback) and can be overridden in the settings.

## Scope

lan-streamer is meant for private use on your own network or behind a VPN. It is not a public streaming service. It has no moderation, monetisation or recording features. Make sure you have the right to show the content you route through it.

## License

[MIT](LICENSE)
