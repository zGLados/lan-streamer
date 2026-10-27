"""Web server: player page, status, live updates and the admin API (FastAPI).

Public (LAN):
    GET  /                    player page
    GET  /admin               admin page (logs in through the API below)
    GET  /api/status          status of an output (?output=<id>, default: the first) + all outputs
    WS   /api/ws              the same status, pushed on every change (?output=<id>)
    GET  /api/viewer          forms of the inputs for viewers (e.g. submit a link) + their items (?output=)
    POST /api/viewer/{input}  send such a form (a device cookie identifies the viewer)
    GET  /tv, /tv/{output}    only the stream, full window (kiosk / TV)

Admin login (one password, session cookie):
    GET  /api/admin/session   {"password_set", "logged_in"}
    POST /api/admin/setup     first start: set the password (only while none is set)
    POST /api/admin/login | /logout | /password

Admin (logged in):
    POST   /api/admin/next?output=<id>           "Next input": the input on air hands over
    GET    /api/admin/plugins                    installed plugins + settings schema
    PUT    /api/admin/plugins                    upload a plugin (.zip as the request body)
    PATCH  /api/admin/plugins/{id}               turn a plugin on or off ({"enabled": bool})
    DELETE /api/admin/plugins/{id}               remove an uploaded plugin (not in use)
    GET    /api/admin/outputs                    outputs and their inputs
    POST   /api/admin/outputs                    add an output ({"name"}; starts with a Standby input)
    PATCH  /api/admin/outputs/{id}               output settings (format/encoder: restart)
    DELETE /api/admin/outputs/{id}               remove an output and its inputs (not the last one)
    POST   /api/admin/outputs/{id}/inputs        add an input
    PUT    /api/admin/outputs/{id}/order         order of the inputs (ties in priority)
    PATCH  /api/admin/inputs/{id}                change an input (priority, override, settings, ...)
    DELETE /api/admin/inputs/{id}
    GET    /api/admin/inputs/{id}/items          an input's items (e.g. its queue)
    DELETE /api/admin/inputs/{id}/items/{item}   remove one
    GET    /api/admin/logs/{name}?lines=300      lan-streamer.log, output.log (output-<id>.log), mediamtx.log
    GET    /api/admin/update                     installed and latest version (updater.py)
    PATCH  /api/admin/update                     {"auto_check": bool}
    POST   /api/admin/update/check               look for a new release now
    POST   /api/admin/update/install             download + start the installer; lan-streamer restarts

Every configuration change is saved in SQLite and taken over by the running
output at once (OutputRuntime.apply); added and removed outputs start and stop.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from lanstreamer.core.auth import Auth
from lanstreamer.core.config import InputConfig, OutputConfig
from lanstreamer.core.store import NotFound, Store
from lanstreamer.plugins import choice, install
from lanstreamer.plugins.process import PluginError, PluginRejected, check_settings
from lanstreamer.plugins.registry import BUILTIN
from lanstreamer_sdk import Manifest

COOKIE = "lanstreamer_session"
DEVICE_COOKIE = "lanstreamer_device"  # identifies a viewer's browser for per-device limits
LOG_FILES = ("lan-streamer.log", "output.log", "mediamtx.log")
RTMP_URL = re.compile(r"^rtmps?://[^/\s]+/[^/\s]+/\S+$")
OUTPUT_LOG = re.compile(r"^output-[a-z0-9-]+\.log$")  # the workers of further outputs
RESTART_FIELDS = {"width", "height", "fps", "bitrate_kbps", "encoder"}

PAGES = resources.files("lanstreamer.web")
ICON = resources.files("lanstreamer.assets").joinpath("icon.png")


class Changes:
    """Lets WebSocket clients wait for the next status change."""

    def __init__(self) -> None:
        self.version = 0
        self._event = asyncio.Event()

    def notify(self) -> None:
        self.version += 1
        self._event.set()
        self._event = asyncio.Event()

    async def wait(self, timeout_s: float) -> None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout_s)
        except TimeoutError:
            pass


@dataclass
class Backend:
    """What the web layer needs from the app."""

    status: Callable[[str | None], dict[str, Any]]  # output id (None: the first) → its status
    skip: Callable[[str | None], None]
    store: Store
    auth: Auth
    plugins: Callable[[], dict[str, Manifest]]
    apply: Callable[[str], Awaitable[None]]  # output id → running output takes over its config
    logs: Path
    changes: Changes
    plugins_dir: Path | None = None  # where uploaded plugins go
    plugins_changed: Callable[[str], Awaitable[None]] | None = None  # plugin id → restart its inputs
    viewer: Any = None  # the running outputs: viewer_forms(output), viewer_submit, items, remove_item
    updater: Any = None  # lanstreamer.updater.Updater
    local_page: Any = None  # lanstreamer.localpage.LocalPage (None: no desktop, e.g. the Linux service)


# --- request bodies ---------------------------------------------------------------------


class Password(BaseModel):
    password: str


class PasswordChange(BaseModel):
    current: str
    new: str


class OutputPatch(BaseModel):
    name: str | None = None
    width: int | None = Field(None, ge=320, le=3840)
    height: int | None = Field(None, ge=240, le=2160)
    fps: Literal[30, 60] | None = None
    bitrate_kbps: int | None = Field(None, ge=0, le=50000)
    encoder: Literal["auto", "nvidia", "amd", "intel", "cpu"] | None = None
    overlay: Literal["switch", "always", "off"] | None = None
    overlay_s: float | None = Field(None, ge=2, le=60)
    restream_url: str | None = Field(None, max_length=500)
    debug_clock: bool | None = None

    @field_validator("restream_url")
    @classmethod
    def rtmp_url(cls, value: str | None) -> str | None:
        # rtmp(s)://host[:port]/app/stream-key – the RTMP sender needs both parts
        if value and not RTMP_URL.match(value):
            raise ValueError("must look like rtmp://server/app/stream-key (or rtmps://)")
        return value


class PluginSwitch(BaseModel):
    enabled: bool


class OutputNew(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class InputNew(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    plugin: str
    settings: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(10, ge=0, le=1000)
    fallback: bool = False
    min_duration_s: float = Field(0, ge=0)
    max_duration_s: float | None = Field(None, gt=0)


class InputPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    settings: dict[str, Any] | None = None
    priority: int | None = Field(None, ge=0, le=1000)
    fallback: bool | None = None
    min_duration_s: float | None = Field(None, ge=0)
    max_duration_s: float | None = Field(None, gt=0)
    override: Literal["none", "force", "block"] | None = None


class Order(BaseModel):
    ids: list[str]


class UpdateSettings(BaseModel):
    auto_check: bool


class LocalPageSettings(BaseModel):
    mode: str


# --- app --------------------------------------------------------------------------------


def create_app(backend: Backend) -> FastAPI:
    app = FastAPI(title="lan-streamer", docs_url="/api/docs", redoc_url=None)
    app.state.backend = backend  # for tests and tools
    schema_cache: dict[tuple[str, str], dict[str, Any]] = {}
    background: set[asyncio.Task] = set()

    def logged_in(request: Request) -> None:
        if not backend.auth.valid(request.cookies.get(COOKIE)):
            raise HTTPException(401, "login required")

    def set_cookie(response: Response, token: str) -> None:
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=7 * 24 * 3600)

    async def validated(plugin_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        manifest = backend.plugins().get(plugin_id)
        if manifest is None or manifest.path is None:
            raise HTTPException(422, f"plugin {plugin_id!r} is not installed")
        try:
            result = await check_settings(manifest.path, settings)
        except PluginError as exc:
            raise HTTPException(422, f"plugin {plugin_id!r} cannot be loaded: {exc}") from exc
        if result["errors"]:
            raise HTTPException(422, {"message": "invalid settings", "errors": result["errors"]})
        return result["settings"]

    async def changed(output_id: str) -> None:
        await backend.apply(output_id)
        backend.changes.notify()

    # --- public -------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def player() -> str:
        return PAGES.joinpath("index.html").read_text(encoding="utf-8")

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page() -> str:
        return PAGES.joinpath("admin.html").read_text(encoding="utf-8")

    @app.get("/tv", response_class=HTMLResponse)
    @app.get("/tv/{output_id}", response_class=HTMLResponse)
    def tv_page(output_id: str = "out1") -> str:
        return PAGES.joinpath("tv.html").read_text(encoding="utf-8")

    @app.get("/favicon.png")
    def favicon() -> Response:
        return Response(ICON.read_bytes(), media_type="image/png")

    def output_status(output: str | None) -> dict[str, Any]:
        try:
            return backend.status(output)
        except KeyError as exc:
            raise HTTPException(404, f"no output {output!r}") from exc

    @app.get("/api/status")
    def status(output: str | None = None) -> dict[str, Any]:
        return output_status(output)

    def viewer() -> Any:
        if backend.viewer is None:
            raise HTTPException(503, "not running yet")
        return backend.viewer

    @app.get("/api/viewer")
    async def viewer_forms(output: str | None = None) -> list[dict[str, Any]]:
        try:
            return await viewer().viewer_forms(output)
        except KeyError as exc:
            raise HTTPException(404, f"no output {output!r} running") from exc

    @app.post("/api/viewer/{input_id}")
    async def viewer_submit(input_id: str, form: dict[str, Any], request: Request, response: Response) -> dict[str, str]:
        device = request.cookies.get(DEVICE_COOKIE) or secrets.token_urlsafe(16)
        response.set_cookie(DEVICE_COOKIE, device, httponly=True, samesite="strict", max_age=365 * 24 * 3600)
        ip = request.client.host if request.client else ""
        try:
            message = await viewer().viewer_submit(input_id, form, device, ip)
        except KeyError as exc:
            raise HTTPException(404, "this input takes no submissions right now") from exc
        except PluginRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        except (PluginError, TimeoutError) as exc:
            raise HTTPException(503, f"the input could not take it: {exc}") from exc
        backend.changes.notify()
        return {"message": message}

    @app.websocket("/api/ws")
    async def live(ws: WebSocket, output: str | None = None) -> None:
        await ws.accept()
        try:
            while True:
                try:
                    status = backend.status(output)
                except KeyError:  # removed meanwhile
                    await ws.close(4404, "no such output")
                    return
                await ws.send_text(json.dumps(status))
                await backend.changes.wait(1.0)  # at least once per second (fps, uptime)
        except (WebSocketDisconnect, RuntimeError):
            pass

    # --- login --------------------------------------------------------------------------

    @app.get("/api/admin/session")
    def session(request: Request) -> dict[str, bool]:
        return {
            "password_set": backend.auth.has_password,
            "logged_in": backend.auth.valid(request.cookies.get(COOKIE)),
        }

    @app.post("/api/admin/setup", status_code=204)
    def setup(body: Password, response: Response) -> None:
        if backend.auth.has_password:
            raise HTTPException(409, "the password is already set")
        try:
            backend.auth.set_password(body.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        set_cookie(response, backend.auth.login(body.password) or "")

    @app.post("/api/admin/login", status_code=204)
    async def login(body: Password, response: Response) -> None:
        token = backend.auth.login(body.password)
        if token is None:
            await asyncio.sleep(1)  # slows down guessing
            raise HTTPException(401, "wrong password")
        set_cookie(response, token)

    @app.post("/api/admin/logout", status_code=204)
    def logout(request: Request, response: Response) -> None:
        backend.auth.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE)

    @app.post("/api/admin/password", status_code=204, dependencies=[Depends(logged_in)])
    def change_password(body: PasswordChange, response: Response) -> None:
        if backend.auth.login(body.current) is None:
            raise HTTPException(401, "wrong password")
        try:
            backend.auth.set_password(body.new)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        set_cookie(response, backend.auth.login(body.new) or "")

    # --- configuration --------------------------------------------------------------------

    @app.post("/api/admin/next", status_code=204, dependencies=[Depends(logged_in)])
    def next_input(output: str | None = None) -> None:
        try:
            backend.skip(output)
        except KeyError as exc:
            raise HTTPException(404, f"no output {output!r}") from exc

    @app.get("/api/admin/plugins", dependencies=[Depends(logged_in)])
    async def plugins() -> list[dict[str, Any]]:
        off = choice.disabled(backend.store)
        result = []
        for manifest in backend.plugins().values():
            key = (manifest.id, manifest.version)
            if key not in schema_cache and manifest.path is not None:
                try:
                    schema_cache[key] = (await check_settings(manifest.path))["schema"]
                except PluginError as exc:
                    schema_cache[key] = {"error": str(exc)}
            builtin = manifest.path is not None and manifest.path.parent == BUILTIN
            result.append({
                "id": manifest.id, "name": manifest.name, "version": manifest.version,
                "description": manifest.description, "schema": schema_cache.get(key, {}),
                "builtin": builtin, "requirements": manifest.requirements,
                "enabled": manifest.id not in off, "always_on": manifest.id in choice.ALWAYS_ON,
            })  # fmt: skip
        return result

    @app.patch("/api/admin/plugins/{plugin_id}", dependencies=[Depends(logged_in)])
    async def switch_plugin(plugin_id: str, body: PluginSwitch) -> dict[str, Any]:
        if plugin_id not in backend.plugins():
            raise HTTPException(404, f"no plugin {plugin_id!r}")
        try:
            choice.set_enabled(backend.store, plugin_id, body.enabled)
        except ValueError as exc:
            raise HTTPException(409, "Standby is always on – every output needs it") from exc
        except choice.InUse as exc:
            raise HTTPException(409, f"still used by: {exc} – delete these inputs first") from exc
        backend.changes.notify()
        return {"id": plugin_id, "enabled": body.enabled}

    @app.put("/api/admin/plugins", status_code=201, dependencies=[Depends(logged_in)])
    async def upload_plugin(request: Request) -> dict[str, Any]:
        if backend.plugins_dir is None:
            raise HTTPException(501, "plugin upload is not available")
        size = int(request.headers.get("content-length") or 0)
        if size > install.MAX_ZIP_BYTES:
            raise HTTPException(413, f"the zip is larger than {install.MAX_ZIP_BYTES // 2**20} MB")
        data = await request.body()
        builtin_ids = {m.id for m in backend.plugins().values() if m.path and m.path.parent == BUILTIN}
        try:
            manifest = await install.install_zip(data, backend.plugins_dir, builtin_ids)
        except install.InstallError as exc:
            raise HTTPException(422, str(exc)) from exc
        for key in [k for k in schema_cache if k[0] == manifest.id]:
            del schema_cache[key]
        if backend.plugins_changed:
            await backend.plugins_changed(manifest.id)
        backend.changes.notify()
        return {"id": manifest.id, "name": manifest.name, "version": manifest.version}

    @app.delete("/api/admin/plugins/{plugin_id}", status_code=204, dependencies=[Depends(logged_in)])
    async def remove_plugin(plugin_id: str) -> None:
        manifest = backend.plugins().get(plugin_id)
        if manifest is None or backend.plugins_dir is None:
            raise HTTPException(404, f"no plugin {plugin_id!r}")
        if manifest.path is not None and manifest.path.parent == BUILTIN:
            raise HTTPException(409, "built-in plugins cannot be removed")
        users = [i.name for o in backend.store.outputs() for i in o.inputs if i.plugin == plugin_id]
        if users:
            raise HTTPException(409, f"still used by: {', '.join(users)} – delete these inputs first")
        try:
            install.remove(backend.plugins_dir, plugin_id)
        except install.InstallError as exc:
            raise HTTPException(404, str(exc)) from exc
        for key in [k for k in schema_cache if k[0] == plugin_id]:
            del schema_cache[key]
        if backend.plugins_changed:
            await backend.plugins_changed(plugin_id)

    @app.get("/api/admin/outputs", dependencies=[Depends(logged_in)])
    def outputs() -> list[dict[str, Any]]:
        return [asdict(o) for o in backend.store.outputs()]

    @app.post("/api/admin/outputs", status_code=201, dependencies=[Depends(logged_in)])
    async def add_output(body: OutputNew) -> dict[str, Any]:
        first = backend.store.outputs()[0]  # a new output starts with the first one's format
        cfg = backend.store.add_output(OutputConfig(
            id="", name=body.name, width=first.width, height=first.height, fps=first.fps,
            bitrate_kbps=first.bitrate_kbps, encoder=first.encoder,
            inputs=[InputConfig("", "Standby", "standby", {"image": ""}, priority=0, fallback=True)],
        ))  # fmt: skip
        await changed(cfg.id)
        return asdict(cfg)

    @app.delete("/api/admin/outputs/{output_id}", status_code=204, dependencies=[Depends(logged_in)])
    async def delete_output(output_id: str) -> None:
        if len(backend.store.outputs()) <= 1:
            raise HTTPException(409, "the last output cannot be removed")
        try:
            backend.store.delete_output(output_id)
        except NotFound as exc:
            raise HTTPException(404, f"no output {output_id!r}") from exc
        await changed(output_id)

    @app.patch("/api/admin/outputs/{output_id}", dependencies=[Depends(logged_in)])
    async def patch_output(output_id: str, body: OutputPatch) -> dict[str, Any]:
        changes = body.model_dump(exclude_unset=True)
        try:
            out = backend.store.update_output(output_id, **changes)
        except NotFound as exc:
            raise HTTPException(404, f"no output {output_id!r}") from exc
        await changed(output_id)
        return {"output": asdict(out), "restart_required": bool(RESTART_FIELDS & set(changes))}

    @app.post("/api/admin/outputs/{output_id}/inputs", status_code=201,
              dependencies=[Depends(logged_in)])  # fmt: skip
    async def add_input(output_id: str, body: InputNew) -> dict[str, Any]:
        if body.plugin in choice.disabled(backend.store):
            raise HTTPException(422, f"the plugin {body.plugin!r} is turned off (Plugins on the admin page)")
        settings = await validated(body.plugin, body.settings)
        cfg = InputConfig(id="", **{**body.model_dump(), "settings": settings})
        try:
            cfg = backend.store.add_input(output_id, cfg)
        except NotFound as exc:
            raise HTTPException(404, f"no output {output_id!r}") from exc
        await changed(output_id)
        return asdict(cfg)

    @app.put("/api/admin/outputs/{output_id}/order", status_code=204,
             dependencies=[Depends(logged_in)])  # fmt: skip
    async def order(output_id: str, body: Order) -> None:
        current = {i.id for i in backend.store.output(output_id).inputs}
        if set(body.ids) != current:
            raise HTTPException(422, "the list must contain every input of the output once")
        backend.store.reorder_inputs(output_id, body.ids)
        await changed(output_id)

    @app.patch("/api/admin/inputs/{input_id}", dependencies=[Depends(logged_in)])
    async def patch_input(input_id: str, body: InputPatch) -> dict[str, Any]:
        changes = body.model_dump(exclude_unset=True)
        try:
            output_id = backend.store.output_of_input(input_id)
        except NotFound as exc:
            raise HTTPException(404, f"no input {input_id!r}") from exc
        if "settings" in changes:
            plugin = next(i.plugin for i in backend.store.output(output_id).inputs if i.id == input_id)
            changes["settings"] = await validated(plugin, changes["settings"] or {})
        cfg = backend.store.update_input(input_id, **changes)
        await changed(output_id)
        return asdict(cfg)

    @app.delete("/api/admin/inputs/{input_id}", status_code=204, dependencies=[Depends(logged_in)])
    async def delete_input(input_id: str) -> None:
        try:
            output_id = backend.store.output_of_input(input_id)
        except NotFound as exc:
            raise HTTPException(404, f"no input {input_id!r}") from exc
        backend.store.delete_input(input_id)
        await changed(output_id)

    @app.get("/api/admin/inputs/{input_id}/items", dependencies=[Depends(logged_in)])
    async def input_items(input_id: str) -> list[dict[str, Any]]:
        try:
            return await viewer().items(input_id)
        except KeyError as exc:
            raise HTTPException(404, f"input {input_id!r} has no items") from exc

    @app.delete("/api/admin/inputs/{input_id}/items/{item_id}", status_code=204,
                dependencies=[Depends(logged_in)])  # fmt: skip
    async def remove_input_item(input_id: str, item_id: str) -> None:
        try:
            await viewer().remove_item(input_id, item_id)
        except KeyError as exc:
            raise HTTPException(404, f"input {input_id!r} has no items") from exc
        backend.changes.notify()

    # --- updates of lan-streamer itself ---------------------------------------------------

    def updater() -> Any:
        if backend.updater is None:
            raise HTTPException(501, "updates are not available")
        return backend.updater

    @app.get("/api/admin/update", dependencies=[Depends(logged_in)])
    def update_state() -> dict[str, Any]:
        return updater().state()

    @app.patch("/api/admin/update", dependencies=[Depends(logged_in)])
    def update_settings(body: UpdateSettings) -> dict[str, Any]:
        updater().set_auto_check(body.auto_check)
        return updater().state()

    @app.post("/api/admin/update/check", dependencies=[Depends(logged_in)])
    async def update_check() -> dict[str, Any]:
        return await updater().check()

    @app.post("/api/admin/update/install", status_code=202, dependencies=[Depends(logged_in)])
    async def update_install() -> dict[str, Any]:
        u = updater()
        if u.status.state != "idle":
            raise HTTPException(409, f"busy ({u.status.state})")
        if not (u.status.available and u.status.installable):
            raise HTTPException(409, u.status.how or "no update to install – check first")
        task = asyncio.create_task(u.install())  # download, check, start the installer, quit
        background.add(task)
        task.add_done_callback(background.discard)
        return u.state()

    # --- the page lan-streamer opens on its own PC --------------------------------------

    def local_page() -> Any:
        if backend.local_page is None:
            raise HTTPException(501, "this lan-streamer has no desktop to open a page on")
        return backend.local_page

    @app.get("/api/admin/local-page", dependencies=[Depends(logged_in)])
    def local_page_state() -> dict[str, Any]:
        return local_page().state()

    @app.patch("/api/admin/local-page", dependencies=[Depends(logged_in)])
    def local_page_settings(body: LocalPageSettings) -> dict[str, Any]:
        try:
            local_page().set_mode(body.mode)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return local_page().state()

    @app.post("/api/admin/local-page/open", dependencies=[Depends(logged_in)])
    def local_page_open() -> dict[str, Any]:
        page = local_page()
        return {**page.state(), "message": page.open()}

    @app.get("/api/admin/logs/{name}", response_class=PlainTextResponse,
             dependencies=[Depends(logged_in)])  # fmt: skip
    def logs(name: str, lines: int = 300) -> str:
        if name not in LOG_FILES and not OUTPUT_LOG.match(name):
            raise HTTPException(404, f"unknown log {name!r}")
        path = backend.logs / name
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(text[-max(1, min(lines, 5000)):])

    return app
