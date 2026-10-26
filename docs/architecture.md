# Architecture

lan-streamer pulls media from pluggable **inputs**, lets a **scheduler** pick the most important active one, and plays it on one or more **outputs**: re-encoded 1080p live streams that a browser on the local network watches with sub-second latency.

Status: M1 (prototype), M2 (core), M3 (submissions, viewer forms, TV page) and M4 (overlays, RTMP/SRT ingest, YouTube, several outputs, restream via RTMP) done. M5 in progress: Windows firewall rules and the Linux packages are done; next: optional Windows service, Docker. The Twitch top stream is parked at the end of the roadmap. Details in [roadmap.md](roadmap.md).

## At a glance

```mermaid
flowchart LR
    subgraph plugins["Plugin hosts (one process per input instance)"]
        TW[Twitch top stream]
        TT[TikTok queue]
        YT[YouTube]
        IN[RTMP/SRT ingest]
        FB[Fallback playlist]
    end

    subgraph core["Core process (Python, asyncio)"]
        API[FastAPI: admin + viewer API, WebSocket status]
        SCH[Scheduler]
        DB[(SQLite)]
    end

    subgraph outputs["Output workers (one GStreamer process per output)"]
        O1[Output 1: compositor → H.264 + Opus]
        O2[Output 2]
    end

    MTX[MediaMTX]
    WEB[Browser: player page + admin page]
    OBS[OBS] -- RTMP/SRT --> MTX

    plugins -- "activity, metadata (JSON-RPC over stdio)" --> SCH
    SCH -- "play source X" --> outputs
    outputs -- "SRT (MPEG-TS)" --> MTX
    MTX -- "WebRTC (WHEP) / LL-HLS" --> WEB
    WEB -- HTTP/WS --> API
    API -- "proxied plugin UIs" --> plugins
    MTX -. "ingest path" .-> O1
```

## Processes

| Process | Count | Does | Why separate |
| --- | --- | --- | --- |
| Core | 1 | Web API, scheduler, config in SQLite, supervises the other processes | Single source of truth |
| Plugin host | 1 per input instance | Runs one plugin: activity detection, source resolution, its own web routes | A broken plugin only takes itself down (F-IN-07, F-PLG-05) |
| Output worker | 1 per output | Runs one GStreamer pipeline with a permanently running encoder | A crashed pipeline is restarted without touching other outputs |
| MediaMTX | 1 | Serves outputs via WebRTC and LL-HLS, accepts RTMP/SRT ingest, optional restream | Proven media server, no need to write one |

Core talks to plugin hosts and output workers over JSON-RPC on the child's stdin/stdout. This works the same on Windows and Linux. The core restarts a dead child with backoff and shows it as `error` in the admin UI.

### Outputs and input instances

Every input instance belongs to exactly one output. Two outputs that both show TikTok submissions have two instances, each with its own settings, queue and submit form. This keeps the outputs independent and the scheduler simple.

At runtime `app.App` holds one `Output` per stored output: its own worker process (own encoder session, MediaMTX path = output id, log `output.log` for the first, `output-<id>.log` for the others), its own `OutputRuntime` (plugins + scheduler) and status. MediaMTX is shared. Outputs added or removed on the admin page start (in the background – choosing the encoder takes a while) or stop while the others keep running. The web API takes `?output=<id>` for status, WebSocket, viewer forms and "Next input" (default: the first output); the player page shows an output picker when there are several, `/tv/<id>` shows one output.

## Media pipeline (per output)

The hard requirement is F-SCH-03: switching inputs must never interrupt the output stream. The pipeline is therefore built so the encoder never sees a gap:

```
background layer (still image / black + silence, always running) ──┐
input slot A: fallbacksrc → decode → scale/rate 1920×1080@60 (I420) ─────┼─► compositor + audiomixer ─► H.264 encoder ─► MPEG-TS ─► SRT ─► MediaMTX
input slot B: (next input, prerolling)                                   ─┘                         Opus encoder ─┘
```

- **Make before break:** the next input starts in a free slot. Only when its first frame has arrived does the compositor fade it in (this gives F-SCH-05 transitions almost for free) and release the old slot.
- **Background layer:** if an input stalls, the compositor keeps producing frames from the background, so the stream stays up (NF-01) until the scheduler switches.
- **Robust sources:** `fallbacksrc` / `fallbackswitch` from gst-plugins-rs handle timeouts and reconnects of live sources.
- **Uniform output (NF-03):** every slot is scaled to the output format, 1080p60 by default (1080p30 selectable), in I420. EBU R128 loudness normalisation (`audioloudnorm`) follows later.
- **Encoder:** H.264, no B-frames, one keyframe per second, tuned for low latency, profile constrained-baseline/baseline/main (what every browser decodes over WebRTC); see [Encoder selection](#encoder-selection).
- **Output → MediaMTX via SRT** (MPEG-TS, 20 ms latency on localhost). `rtspclientsink` over TCP broke after some seconds on Windows. MediaMTX only reads the first stream table (PMT), so the muxer output is held back until video and audio both have data.
- **Audio is Opus**, because browsers only accept Opus over WebRTC; MediaMTX also serves Opus over fMP4 LL-HLS. A restream to Twitch/YouTube (F-OUT-03) adds an AAC branch only while it is enabled.
- **Overlays (F-OUT-04):** a lower third (bottom left) with the title and below it input · channel · "submitted by …", drawn by GStreamer `textoverlay` in the output worker. Per output: after each switch for `overlay_s` seconds (default 8), always, or off – changed live on the admin page. Not shown for the fallback (Standby) and for announcement cards; shown again when the title of what is on air changes. HTML overlays are a later option.

### Encoder selection

By default (`encoder = auto`) the available GStreamer encoders are probed at startup and the first that works is used. Each probe encodes one second of 1080p test frames **in its own process**, because an element can exist without a matching GPU or driver, and a crashing driver must not take the app down:

| Vendor | Windows | Linux |
| --- | --- | --- |
| NVIDIA | `nvh264enc` (NVENC) | `nvh264enc` (NVENC) |
| AMD | `amfh264enc` (AMF) | `vah264enc` (VA-API, Mesa) |
| Intel | `qsvh264enc` (Quick Sync) | `vah264enc` / `qsvh264enc` |
| CPU | `x264enc` | `x264enc` |

The admin can override this in the settings, globally and per output: `auto`, `nvidia`, `amd`, `intel` or `cpu`. If the chosen encoder fails, the output falls back to `cpu` and the admin UI shows a warning. The admin UI also shows which encoder each output actually uses.

### Media sources

A plugin resolves *what* to play; the output worker does the decoding. The plugin returns one of:

| Source | Example | Played via |
| --- | --- | --- |
| `UriSource(url, audio_uri="")` | Direct HLS/MP4 URL from yt-dlp; YouTube HD with picture and sound as two URLs | `fallbacksrc` (two of them – video only / audio only – when `audio_uri` is set) |
| `PipeSource(argv)` | `streamlink --stdout twitch.tv/x best` (handles token refresh and ads) | Process stdout → `fdsrc` |
| `FileSource(path)` | Fallback playlist, downloaded TikTok clip | `filesrc` |
| `UriSource("rtspt://127.0.0.1:8554/ingest/<key>")` | OBS stream arriving at MediaMTX (built-in `ingest`) | RTSP over TCP from MediaMTX |

**RTMP/SRT ingest (built-in `ingest`):** MediaMTX receives on `ingest/<stream key>` (RTMP `rtmp://<pc>:1935/ingest` + key, or SRT `srt://<pc>:8890?streamid=publish:ingest/<key>`). The app starts MediaMTX with its API on `127.0.0.1:9997` only (env `MTX_API`, `MTX_APIADDRESS`; the shipped `mediamtx.yml` stays untouched); the plugin polls `/v3/paths/get/ingest/<key>` every 2 s and is active while the path is ready. On equal priority it holds the output while the stream is sent. The admin page shows the addresses and the received tracks. OBS on another PC reaches it through the installer's firewall rule for `mediamtx.exe` (private/domain networks).

**YouTube (built-in `youtube`):** yt-dlp (shared helper `lanstreamer/plugins/ytdlp.py`, also used by submissions; updates itself per instance) resolves the link right before playing, because the URLs expire. A channel's `/live` page or a live video: active while live (checked every `live_check_s`), holds the output on equal priority. A playlist, channel or single video: always active, plays in order (or shuffled), starts over, may hand over after each video. Format: H.264 + AAC up to `max_height`, picture and sound as separate streams (no ffmpeg needed). Note: yt-dlp warns that YouTube without a JavaScript runtime (Deno, QuickJS, …) is deprecated and formats may go missing – if that happens, a JS runtime has to be bundled.

**Restream (per output, `restream_url`):** a separate process per output (`media/restream.py`, managed by `core/restreamer.py`) reads the finished output back from MediaMTX over SRT, keeps the H.264 as it is, turns Opus into AAC and sends FLV with `rtmp2sink` (rtmp:// and rtmps://). Separate so a refused or dropped connection never touches the output; it restarts after 5/10/30/60 s. The admin page shows the state (off, connecting, live, retrying …); the stream key never appears in the public status. Changing the URL takes effect at once.

## Scheduler

The scheduler is a pure, deterministic function, so it can be fully unit-tested with a fake clock (NF-08):

```
decide(output_config, input_states, current, now) -> Decision(keep | switch_to(instance) | fallback)
```

Rules, in order:

1. Admin overrides win: a forced input plays, a blocked input is ignored (F-AKT-05).
2. Among active inputs, the highest priority in this output's ranking wins (F-SCH-01).
3. A higher-priority input interrupts the current one immediately, even if the current one has not reached its minimum duration (F-SCH-04).
4. On equal priority, the current input keeps playing until its minimum duration is over and its plugin's `can_release()` returns true, or until its maximum duration is reached (F-SCH-04, F-AKT-04).
5. No active input: the fallback plays (F-SCH-02). The fallback input is always active with the lowest priority.

Plugins push activity changes as events. The core also polls every few seconds (F-AKT-02) and debounces flapping activity before acting on it.

## Configuration

Outputs and their input instances live in SQLite (`lanstreamer.db` in the data folder, `core/store.py`). The schema is versioned (`PRAGMA user_version`, one migration per version). The first start creates one output with the "Standby" fallback and – if that plugin is on – a "Short-video submissions" input (`plugins/choice.py`); older installs lose the two test inputs of the first seed (migration 4). Changes made while running are taken over by `OutputRuntime.apply()`: new inputs start, removed ones stop, inputs with changed plugin or settings restart, everything else changes in place. Output format and encoder take effect after a restart.

## Plugin system

A plugin is a `.zip` with a manifest, uploaded in the admin UI (F-PLG-02):

```toml
# plugin.toml
id = "tiktok-queue"
name = "TikTok submissions"
version = "1.0.0"
api_version = 1                  # major version of the plugin API
entry = "tiktok_queue:Plugin"
requirements = ["yt-dlp>=2026.1"]
```

Upload (admin page → Plugins, `PUT /api/admin/plugins`, `plugins/install.py`): the zip is checked (size, no paths outside the plugin folder; `plugin.toml` at the top or in one folder) and unpacked into a staging folder; the manifest is checked including `api_version` – incompatible plugins are rejected with a clear message (F-PLG-04); the `requirements` are installed with pip into the plugin's own `.deps` folder (internet needed only then; the installer ships pip), so plugins cannot break each other's dependencies; the plugin is imported once in a plugin host process; only then it replaces an older version, and running inputs that use it restart with the new code. Uploaded plugins live in `<data>/plugins/<id>`, their instance data in `<data>/plugin-data/<instance>`. Built-in plugin ids cannot be taken; a plugin still used by an input cannot be removed. Templates are in `examples/` (`hello-plugin`, `web-stream`, `stream-queue`); the guide for plugin authors is [plugin-guide.md](plugin-guide.md), all interfaces (HTTP API, protocols, ports, files) are in [interfaces.md](interfaces.md).

Plugin authors depend only on the small SDK package `lanstreamer_sdk` (`src/lanstreamer_sdk/`, shipped with the app). It holds the base class, the source types, the context object and the manifest check, plus tools for authors: `python -m lanstreamer_sdk check|pack` (`tools.py`; the plugin host loads plugins with the same `load_plugin`) and a test context (`testing.py`). The API is pinned by contract tests (`tests/test_plugin_contract.py`): within API 1 only additions are allowed. The plugin API, version 1: The plugin API, version 1:

```python
class InputPlugin[S: BaseModel]:
    Settings: type[S]                       # Pydantic model → admin form (F-IN-03)
    poll_interval_s: float = 5.0            # for the default watch_active()

    def __init__(self, ctx: PluginContext, settings: S): ...

    async def start(self) -> None: ...                          # once, before anything else
    async def stop(self) -> None: ...
    async def is_active(self) -> bool: ...                      # F-AKT-01
    async def watch_active(self) -> AsyncIterator[bool]: ...    # default polls is_active (F-AKT-02)
    async def get_source(self) -> MediaSource: ...              # F-IN-01: TestSource, UriSource, FileSource
    async def can_release(self) -> bool: ...                    # equal-priority handover
    async def on_playback(self, event: PlaybackEvent) -> None: ...  # started / ended / failed
    def routes(self) -> APIRouter | None: ...                   # own UI (F-IN-04) – planned
```

Sources: `TestSource`, `UriSource`, `FileSource` (`cut=True`: switched to without crossfade), `ImageSource` (PNG/JPEG with silence), `CardSource` (dark card with heading, title and subtitle – e.g. "Up next"). `ctx.next_source()` asks for `get_source()` again while the input stays on air (a playlist's next item).

Viewer forms (F-IN-04, F-WEB-03): a plugin can declare a `ViewerForm` (Pydantic model) and a `viewer_title`; the player page builds the form from its JSON Schema and sends it to `on_viewer_submit(form, viewer)`, which returns a message or raises `Rejected("…")` (shown to the viewer as it is). `viewer_items()` lists lines under the form (e.g. the queue), `remove_item()` lets the admin delete one, `ctx.changed()` tells the pages to reload them, and `ctx.set_info("…")` sets the status line in the admin. A viewer is identified per device: a long-lived browser cookie plus the LAN address. Custom HTML pages for plugins (`routes()`) are not needed for now.

Plugins can be turned off (`plugins/choice.py`, settings key `disabled_plugins`): a plugin that is off is not offered for new inputs and cannot be turned off while inputs use it; Standby is always on. The installer asks which built-in inputs to turn on (submissions and playlist on, test pattern off by default) and writes `setup.ini` in the program folder; the app takes the answers over on the first start, and again when a reinstall changes them. Afterwards the admin page (Plugins → "On") decides.

Built-in plugins (`src/lanstreamer/plugins/builtin/`):
- **submissions** – "Short-video submissions": viewers submit links on the player page (TikTok, YouTube Shorts, Instagram Reels, plain video URLs – anything yt-dlp reads), with an optional name. Checked (max. length, videos per device, queue size, already queued), downloaded with yt-dlp into the instance's data folder, played in order, deleted afterwards; active while a video is ready; the queue survives a restart. On start a newer yt-dlp is downloaded in the background (pip into the data folder) and used from the next start on – submissions never wait for it, and a running process never swaps the library; offline the bundled version keeps working. Without ffmpeg only files with picture and sound in one file are used – fine for TikTok, YouTube Shorts then come in 360p (proper YouTube support: M4).
- **standby** – a still picture (the lan-streamer slate or your own), always active. Every output gets a "Standby" input as fallback (priority 0) from the start (F-SCH-02).
- **playlist** – files, folders (their media files sorted by name) or URLs one after another; pictures are shown for `image_duration_s`; after an item it hands over to an input of the same priority, or plays its next item itself.
- **test-pattern** – a generated test picture with a tone, always active. Off after installing; for tests.

- `PluginContext` gives each instance a persistent key-value store, a logger, its own data folder and a way to publish metadata such as title, channel and thumbnail (F-IN-06, F-PLG-06). An HTTP client follows with the first network plugins.
- `on_playback` closes the loop for queue-style inputs: the TikTok plugin learns that a clip has ended, pops its queue and can release.
- `routes()` keeps the sketch's FastAPI router, but it runs inside the plugin host on a random loopback port. The core reverse-proxies `/p/<instance>/…` to it, and the viewer page embeds it in an iframe (F-WEB-03).

**Security note:** isolation means fault isolation, not a sandbox. An uploaded plugin runs arbitrary code. Only an admin may install plugins, and the whole system is meant for the local network or a VPN (NF-06).

## Web UI

- **Web server:** FastAPI on uvicorn, inside the app's asyncio loop (`web/api.py`). The app binds port 8080 itself, so a port in use gives a clear message instead of ending the app. Interactive API docs: `/api/docs`.
- **Pages:** plain HTML and JavaScript without a build step (owner's decision: fast builds, no Node.js toolchain).
- **Viewer page** `/` (no login, LAN only): player (WebRTC via WHEP), what is on air (title · channel · submitted by), the forms of the inputs (e.g. "Submit a video") with their lists ("Up next"), the inputs with state and priority, links to the admin page and the TV view.
- **TV view** `/tv` (or `/tv/<output>`): only the video, black, no text; the pointer hides after 2 s; it reconnects by itself. A click or key turns the sound on and goes full screen; a display PC can open it in Edge kiosk mode with sound and no click (`docs/development.md`). "Next input" shows only for a logged-in admin. Live status comes over the WebSocket `/api/ws` (pushed on every change, at least once a second), polling `/api/status` is the fallback. Planned: hls.js fallback, embedded plugin UIs.
- **Admin login:** one password, set on first start (`POST /api/admin/setup`, only while none is set), stored as a scrypt hash in the `settings` table; session cookie (HttpOnly, SameSite=Strict, 7 days, in memory – a restart logs out); a wrong password costs one second.
- **Admin API** `/api/admin/…`: plugins with their settings schema, outputs, add/change/delete/reorder inputs (priority, override force/block, durations, settings), logs. Settings are validated by the plugin itself in a short-lived plugin host process (`host.py --check`), so the core never imports plugin code. Every change is stored and taken over at once by `OutputRuntime.apply()`; output format and encoder need a restart (the API says so).
- **Admin page** `/admin` (`web/admin.html`, on top of the API): first start sets the password, then login; live "on air" status and "Next input"; the inputs as a table with live state, priority, fallback and override (force/block) editable in place, order by drag and drop, edit and delete; "Add input" with the settings form generated from the plugin's JSON Schema (enums → select, numbers with limits, booleans, lists of strings, anything else as JSON); output settings (restart note for format/encoder); plugins (list, upload as .zip, remove); logs; change password.

## Installation and packaging

lan-streamer is meant to run natively on Windows and Linux. **Windows is the primary platform:** new features are built and tested there first, Linux follows. GitHub Actions builds and install-tests the Windows installer and the Linux packages on every push. The Windows installer brings everything with it (Python runtime, GStreamer, MediaMTX). The Linux packages use the distribution's Python 3.12+ and GStreamer 1.24+ (Ubuntu 24.04 / Debian 13 or newer) and bring the app in its own venv (all Python packages as wheels, no internet needed), MediaMTX and the two gst-plugins-rs plugins the distributions do not package (fallbacksrc, audioloudnorm – built in CI against Ubuntu's GStreamer).

| System | Format | Runs as |
| --- | --- | --- |
| Windows 10/11 x64 | Setup `.exe` (Inno Setup) with the official GStreamer MSVC runtime | Today: start menu entry → runs windowless with a tray icon; firewall rules for the LAN (MediaMTX, python/pythonw; private + domain networks, all-users install only, removed on uninstall). Planned: optional Windows service |
| Linux x64 | `.deb` (Debian/Ubuntu, `apt install ./lan-streamer_….deb`) and a `.tar.gz` with `install.sh` (apt-get for the dependencies) | systemd service `lan-streamer` as user `lanstreamer` (groups video/render for GPU encoding), program in `/opt/lan-streamer`, data in `/var/lib/lan-streamer`; `lan-streamer selftest` checks GStreamer and the encoders |
| Linux server (optional) | Docker Compose with GPU passthrough | Container |

After starting, the player page opens at `http://<host>:8080`. The admin page `http://<host>:8080/admin` asks for the admin password on its first visit.

**PyGObject on Windows:** the Python bindings inside the official GStreamer Windows build only work with Python 3.9, which is end of life. CI therefore builds PyGObject for Python 3.13 against the official GStreamer MSVC build, and the installer ships that build.

### Windows test installer (now)

`.github/workflows/windows.yml` builds `lan-streamer-setup-<version>.exe` on every push to `main`, using `packaging/windows/build.ps1` and Inno Setup. CI then installs it on a clean runner, runs the self-test and uninstalls it again. The installer:

- installs to `C:\Program Files\lan-streamer` for all users (Windows asks for admin rights), or with "only for me" to `%LOCALAPPDATA%\Programs\lan-streamer` without admin rights. It needs no internet and is listed under Settings → Apps; its uninstaller also removes `%LOCALAPPDATA%\lan-streamer`;
- ships an embeddable CPython 3.13 with lan-streamer and the PyGObject build, a private copy of the GStreamer runtime (`gstreamer\`, with its own plugin registry so other GStreamer installs do not interfere) and MediaMTX;
- adds start menu entries "lan-streamer (1080p60)", "lan-streamer (1080p30)" and "lan-streamer self-test". The first two run `pythonw.exe -m lanstreamer --tray`: no console window, a tray icon next to the clock (open player, what is on air, open log / log folder, quit). The self-test runs `python -m lanstreamer.doctor` in a console;
- uses one icon everywhere (start menu, Apps list, setup, tray, browser tab), drawn by `packaging/windows/make_icon.py`.

The installer ships only the GStreamer plugins listed in `packaging/windows/gstreamer-plugins.txt` and the DLLs they import (found with dumpbin). **Every new input plugin or feature adds the plugins it needs there and its elements to the self-test** (`REQUIRED_ELEMENTS` in `doctor.py`); CI plays a real MP4 file and the same file over HTTP to cover the decoder path.

CI caches the GStreamer build install and the PyGObject wheel (keyed by their versions), compresses test installers with `lzma2/fast` (release tags: `max`) and skips the build for documentation-only changes.

## Repository layout

```
src/lanstreamer/
  app.py             starts everything: encoder choice, MediaMTX, output worker, runtime, web, tray
  scheduler.py       decide() + Debounce – pure, unit-tested
  core/              config.py, store.py (SQLite), auth.py (admin login), runtime.py (plugins +
                     scheduler + worker), worker.py (output worker handle), simworker.py (--simulate)
  media/             gst.py (GStreamer setup), encoders.py, output.py (output worker), mpegts.py
  plugins/           host.py (plugin process), process.py (core side), registry.py, install.py
                     (upload), builtin/ (standby, playlist, test-pattern)
  web/               api.py (FastAPI), index.html (player), admin.html (admin page)
  doctor.py, tray.py, paths.py, assets/ (icon, standby slate)
src/lanstreamer_sdk/ plugin SDK: InputPlugin, sources, context, manifest; tools.py (check, pack), testing.py
examples/            hello-plugin, web-stream, stream-queue (with tests) – templates for plugin authors
packaging/windows/   build.ps1, lan-streamer.iss, gstreamer-plugins.txt, make_icon.py, *.cmd
tests/               unit tests; e2e/ (check_api, check_admin, check_player, run_local, edge.py)
docs/                architecture, roadmap, development, plugin-guide, interfaces
```

## Tech stack

| Part | Choice | Reason |
| --- | --- | --- |
| Core, scheduler, plugin API | Python 3.12+, asyncio | streamlink and yt-dlp are Python libraries; easiest entry for plugin authors |
| Web API | FastAPI + Pydantic | Async, and settings models produce the forms |
| Media | GStreamer 1.24+ via PyGObject, gst-plugins-rs | Dynamic pipelines, compositor, robust live sources |
| Media server | MediaMTX | WebRTC, LL-HLS, RTMP/SRT ingest in one binary |
| Frontend | Plain HTML/JS | No build step; enough for forms, lists and live status |
| Storage | SQLite | One machine, no extra service |
| Operations | Native installers for Windows and Linux, built in GitHub Actions; Docker optional | Works the same on both systems |
| Quality | pytest, ruff, mypy, GitHub Actions | NF-08 |
