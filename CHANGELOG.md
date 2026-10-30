# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Plugin authors no longer need a copy of lan-streamer: the SDK installs on its own as `lanstreamer-sdk` (see the plugin guide's quick start).
- `python -m lanstreamer_sdk new <folder>` creates a working plugin with tests, ready to check, pack and upload.
- New SDK versions are published to PyPI automatically with a release.
- SDK: `FileSource(audio_path=…)` plays the sound from a second file.

### Changed

- `python -m lanstreamer_sdk check` and `pack` refuse the id of a built-in plugin at once instead of at the upload.

### Fixed

- Short-video submissions and YouTube no longer fail to start when a downloaded yt-dlp update cannot replace the old one (`WinError 183`, a file in use on Windows): the version in use stays and the switch is tried again at the next start. An update cut off by closing lan-streamer is not used any more.
- Short-video submissions from YouTube failed with "Requested format is not available", because YouTube often no longer offers a file with both picture and sound. Picture and sound are now downloaded separately and played together, in up to 1080p.

## [0.1.5] – 2026-10-29

Full screen on the streaming PC, set from the admin page, and an output that repairs itself when its video process hangs.

### Added

- Player page: a "Full screen" button (or a double-click on the video) shows only the stream in full screen with sound, like `/tv`; Esc or a double-click leaves it.
- Admin page → "Page on this PC": what lan-streamer opens on the PC it runs on when it starts – the player page (as before), only the stream in full screen with sound (Microsoft Edge in kiosk mode, no click needed; Alt+F4 ends it) or nothing. "Open now" applies it at once.

### Changed

- After an update lan-streamer starts again without opening the browser. (Takes effect from the update after this version: the restart is done by the version that was running.)
- An output whose video process hangs (no answer for 5 s) or ends is restarted on its own and plays what should be on air again; the other outputs keep running. Before, lan-streamer stopped completely (or that output stood still). The player and admin pages show "worker restarted N×", and the output log holds where the process hung.
- A switch the video process never answers counts as failed after 30 s instead of blocking the output for ever.

## [0.1.4] – 2026-10-27

A small release, mainly the first update that installs without an admin prompt (coming from 0.1.3).

### Changed

- Windows: the self-test window opened from the tray is titled "lan-streamer self-test" instead of the path of python.exe.
- Linux: when the output's MPEG-TS muxer fails while starting (seen once: "Could not create handler for stream"), the output log now records what each of its inputs had and GStreamer's reason; the service starts again by itself after 5 s as before.

## [0.1.3] – 2026-10-26

Updates without an admin prompt. Coming from 0.1.2: this update still asks once (the old updater); from 0.1.3 on, "Install update" needs no rights.

### Changed

- Updates no longer ask for admin rights (install for all users): the installer sets up the task "lan-streamer Update" (runs as SYSTEM, every user may start it), which downloads the release, checks its SHA-256 and installs it; lan-streamer starts again by itself. A failed update is shown on the admin page. Comes into effect after installing this version once.
- One start menu entry "lan-streamer" instead of "lan-streamer (1080p60)", "(1080p30)" and "self-test": the frame rate is set per output on the admin page, the self-test is in the tray menu now. An update removes the old entries.

### Fixed

- After an update, lan-streamer could stay off when its relauncher did not start; the relauncher now writes `logs\relaunch.log`.

## [0.1.2] – 2026-10-26

No functional changes: the first release to update to from the admin page (0.1.1 → 0.1.2), to try the updater on a real installation.

## [0.1.1] – 2026-10-26

Updates from the admin page. Coming from 0.1.0: install 0.1.1 once by hand (it has no updater yet); from 0.1.1 on, "Install update" on the admin page does it.

### Added

- Updates of lan-streamer itself: the admin page shows the installed and the latest release (checked on start and twice a day, can be turned off) and installs a new version on Windows with one click – the installer is checked by its SHA-256, runs silently and starts lan-streamer again. Linux: the new version is shown with its link.

### Changed

- The clock in the top right of the picture (input name and time in ms, for measuring the delay) is off by default; it is a debug option per output now (admin page → Output → Debug clock, no restart needed). The player page shows its local clock only while it is on.

### Fixed

- Quitting lan-streamer (tray "Quit", the end of a smoke test) could hang on Windows when a browser connection had been reset: the web server now gets 5 s to close its connections, then it is stopped anyway.
- Windows: a hanging output worker is ended instead of left running when it does not start; its start is logged in `output.log`.

## [0.1.0] – 2026-10-26

First release: Windows installer, Linux packages (.deb, .tar.gz), plugin API 1 with a guide and example plugins.

### Changed

- A fresh setup has only Standby and Short-video submissions; the two test inputs are gone (also from existing installs). The name is optional when submitting a video.
- Short-video submissions: when the input goes on air, an "Up next" card (title, who submitted it) shows first; once it is on screen the first video starts after a moment (setting `card_s`, default 2 s), cut in without crossfade so its start is not lost. The next videos in the queue follow directly.

### Added

- Guide for plugin authors (`docs/plugin-guide.md`) and example plugins to copy, with their own tests: `examples/web-stream` (an external live stream while it is online) and `examples/stream-queue` (a link queue for viewers).
- Screenshots in the README and the plugin guide, taken by the Windows CI from a real installation (`tests/e2e/doc_screenshots.py`).
- `docs/interfaces.md`: every interface in one place – HTTP API and WebSocket with examples, the plugin and output worker protocols, ports and stream paths, files and folders.
- Plugin SDK: `python -m lanstreamer_sdk check <folder>` (load a plugin as lan-streamer does, show its forms) and `pack <folder>` (zip for the upload); `lanstreamer_sdk.testing.make_context()` to test a plugin without lan-streamer running.
- Plugin API v1 is pinned by contract tests in CI: within API 1 only additions.
- Linux packages: `.deb` and `.tar.gz` with `install.sh` (Ubuntu 24.04 / Debian 13+), systemd service, built and install-tested in GitHub Actions with the same end-to-end checks as Windows.
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

[Unreleased]: https://github.com/zGLados/lan-streamer/compare/0.1.5...HEAD
[0.1.5]: https://github.com/zGLados/lan-streamer/releases/tag/0.1.5
[0.1.4]: https://github.com/zGLados/lan-streamer/releases/tag/0.1.4
[0.1.3]: https://github.com/zGLados/lan-streamer/releases/tag/0.1.3
[0.1.2]: https://github.com/zGLados/lan-streamer/releases/tag/0.1.2
[0.1.1]: https://github.com/zGLados/lan-streamer/releases/tag/0.1.1
[0.1.0]: https://github.com/zGLados/lan-streamer/releases/tag/0.1.0
