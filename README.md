<p align="center">
  <img src="src/lanstreamer/assets/icon.png" alt="lan-streamer logo" width="128">
</p>

<h1 align="center">lan-streamer</h1>

<p align="center">Your own TV channel for the LAN – inputs as plugins, automatic switching, 1080p in the browser.</p>

A self-hosted, modular media streaming platform for your local network. lan-streamer collects media from pluggable sources, such as the top Twitch stream of a category, TikTok links submitted by viewers, YouTube, your own OBS stream or a fallback playlist. It decides automatically what to show and plays it as a continuous, re-encoded 1080p live stream in the browser, with sub-second latency over WebRTC.

> **Status:** early development. Streaming, switching, the core (scheduler, plugins incl. upload, configuration), the admin page, short-video submissions by viewers and a TV view work. Also YouTube, sending from OBS (RTMP/SRT), an overlay with title and submitter, and several outputs. Installers for Windows and Linux, and a guide for plugin authors. Next: opening up for third-party plugins. See the [roadmap](docs/roadmap.md).

## Try the prototype (Windows)

1. Download the latest installer from [Actions → Windows](https://github.com/zGLados/lan-streamer/actions/workflows/windows.yml) (artifact `lan-streamer-setup-…`) and run it. It installs to `C:\Program Files\lan-streamer` (or only for you, without admin rights) and appears under Settings → Apps.
2. Start **lan-streamer (1080p60)** from the start menu. It runs in the background with a tray icon next to the clock (right-click: open player, log, quit), and the player page opens at `http://localhost:8080`.
3. It shows the standby picture until a viewer submits a video. The installer asks which built-in inputs to turn on (short-video submissions and playlist by default; the test pattern is for tests); this can be changed later on the admin page. For a quick test with your own file or URL: `"C:\Program Files\lan-streamer\start.cmd" --source C:\path\video.mp4`.

**Linux (Ubuntu 24.04 / Debian 13 or newer):** download `lan-streamer-linux-…` from [Actions → Linux](https://github.com/zGLados/lan-streamer/actions/workflows/linux.yml), then `sudo apt install ./lan-streamer_….deb` – or unpack the `.tar.gz` and run `sudo ./install.sh`. It runs as the systemd service `lan-streamer` (player page on port 8080); `lan-streamer selftest` shows which encoders work. Remove it with `sudo apt purge lan-streamer` (or `sudo ./install.sh --uninstall --purge`).

**Submit a video:** on the player page viewers paste a TikTok (or YouTube Shorts, Reels, …) link, optionally with their name.

**Restream:** on the admin page enter an RTMP URL with your stream key (YouTube, Twitch, …) under "Restream to" – the output is sent there as well.

**YouTube:** add an input "YouTube" with a link: a channel's `/live` page (on air while it is live), a playlist, a channel or a single video (played one after another).

**Send from OBS:** add an input "RTMP/SRT ingest" on the admin page; it shows the server address and stream key to enter in OBS (RTMP or SRT). The installer (all users) allows lan-streamer through the Windows firewall on private and domain networks – if other PCs cannot connect, set the network to "Private" in the Windows settings.

**TV view:** `http://localhost:8080/tv` – only the stream in the full window, e.g. for a TV or a display PC (kiosk mode: see [docs/development.md](docs/development.md)).

**Admin page:** `http://localhost:8080/admin` – on first start you choose the admin password, then you manage the outputs (add more, format, overlay), their inputs (add, priority, force/block, order) and the logs there.

**lan-streamer self-test** shows which video encoders work on your PC.

## How it works

- **Inputs** are plugins. Each one reports by itself whether it has something to show right now.
- **The scheduler** puts the highest-priority active input on air. A higher priority interrupts at once; on equal priority the running input decides when to hand over (for example when its video ends). With nothing active, a fallback plays.
- **Outputs** are permanently running encoders. They switch between inputs without the stream ever dropping, and [MediaMTX](https://github.com/bluenviron/mediamtx) serves them via WebRTC and LL-HLS.
- **Everything is configured in the web UI**, including installing new input plugins as `.zip` uploads.

Details: [docs/architecture.md](docs/architecture.md). Developing and testing: [docs/development.md](docs/development.md). Writing a plugin: [docs/plugin-guide.md](docs/plugin-guide.md).

## Stack

Python 3.13 (asyncio, FastAPI) · GStreamer · MediaMTX · plain HTML/JS · SQLite

Windows first (installer today), Linux packages follow (M5). Hardware encoding is picked automatically (NVIDIA, AMD, Intel, or CPU as a fallback) and can be overridden in the settings.

## Scope

lan-streamer is meant for private use on your own network or behind a VPN. It is not a public streaming service. It has no moderation, monetisation or recording features. Make sure you have the right to show the content you route through it.

## License

[MIT](LICENSE)
