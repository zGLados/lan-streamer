# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- A fresh setup has only Standby and Short-video submissions; the two test inputs are gone (also from existing installs). The name is optional when submitting a video.
- Short-video submissions: when the input goes on air, an "Up next" card (title, who submitted it) shows first; once it is on screen the first video starts after a moment (setting `card_s`, default 2 s), cut in without crossfade so its start is not lost. The next videos in the queue follow directly.

### Added

- Windows firewall rules for the LAN (private and domain networks), added by the installer for all users and removed on uninstall – OBS and viewers on other PCs no longer depend on the Windows prompt.
- Restream: send an output to an RTMP(S) server (YouTube, Twitch, …) as well – per output on the admin page, restarts by itself if the connection drops.
- Several outputs, each with its own inputs and ranking: add and remove them on the admin page while running; the player page picks the output (`/?output=<id>`), the TV view shows `/tv/<id>`.
- YouTube (built-in input): a channel's live stream while it is live, or the videos of a playlist, channel or a single video one after another (in order or shuffled), in up to 1080p.
- Plugin SDK: `UriSource(audio_uri=…)` takes picture and sound from two separate URLs.
- RTMP/SRT ingest (built-in input): send from OBS, a camera or another PC; on air while the stream comes in, the admin page shows the addresses and the stream key.
- Overlay: a lower third with the title, the input, the channel and who submitted it – after each switch for a few seconds, always, or off (per output on the admin page, no restart needed).
- The installer asks which built-in inputs to turn on (short-video submissions, playlist, test pattern); plugins can be turned on and off on the admin page; Standby is always on.
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
- Short-video submissions (built-in input): viewers submit TikTok/Shorts/Reels links on the player page, with a queue ("Up next"), max. length and a limit per device; yt-dlp downloads its newest version on start in the background (used from the next start); the admin sees and can delete queue entries.
- Viewer forms: plugins can put a form on the player page; the page shows title, channel and who submitted what is on air.
- TV view `/tv`: only the stream in the full window (kiosk mode for display PCs).
- Plugin upload in the admin page (.zip; manifest and API version check; requirements installed with pip into the plugin's own folder; replace and remove); example plugin `examples/hello-plugin`.
- Standby fallback: every output shows the lan-streamer slate (or your own picture) when no input is active.
- Playlist: folders, pictures (shown for a set time) and the next item without a gap.
- Local tests without GStreamer: `--simulate` and `tests/e2e/run_local.py` (see `docs/development.md`).
- Admin page at `/admin`: set the password on first start, manage the inputs (add with forms generated from the plugin settings, edit, delete, priority, fallback, force/block, order by drag and drop), live status with "Next input", output settings, logs, change password. "Next input" is admin-only now.
- Web server on FastAPI: live status over a WebSocket, admin login (password set on first start), admin API for plugins (with settings schema), outputs, inputs (add, change, reorder, delete, force/block) and logs; API docs at `/api/docs`.

### Fixed

- Short-video submissions on Windows: a finished video that the output still held open could not be deleted, which broke the hand-over to the next video; such files are now deleted a little later.
- A reconfigured input could leave its old plugin process running (it was restarted as if it had crashed).
- At start, a less important input could go on air for a moment before the right one.

- Black picture in the browser on fast machines: mpegtsmux's stale stream header made MediaMTX drop the video track.
- GPU encoders on a fresh Windows: the Visual C++ runtime is now bundled.
- Output to MediaMTX broke after ~20 s with RTSP on Windows; now SRT.
