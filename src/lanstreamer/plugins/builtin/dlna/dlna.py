"""Built-in input: a DLNA / UPnP media renderer – phones cast to it like to a TV.

The input shows up in the network as a "TV" (UPnP MediaRenderer). Cast apps
on a phone (BubbleUPnP, Web Video Caster, VLC, the gallery of many Android
phones, "Cast to TV" apps on iOS) find it and send it the link of a video, a
song or a picture; lan-streamer plays it and the phone controls it: play,
stop and the next item of its queue. Pause takes it off air; play then starts
the item over (lan-streamer cannot continue at a position at the moment).
The input is active while something is cast.

A real Chromecast or AirPlay receiver is not possible: phones only cast to
devices with a certificate from Google, and iOS apps encrypt AirPlay video.
Mirroring the phone's screen does not work either, only media with a link.

Only devices in the LAN may cast (private addresses, or the networks in the
settings). Links must be http(s) and must not point to this PC.

Ports: UDP 1900 (SSDP, how the phones find it) and one TCP port for the
UPnP requests (setting; 0 = chosen once and kept).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import random
import socket
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import upnp
from pydantic import BaseModel, Field, field_validator

from lanstreamer_sdk import ImageSource, InputPlugin, Metadata, PlaybackEvent, UriSource

MAX_HEAD, MAX_BODY = 16 * 1024, 1024 * 1024  # bytes of an UPnP request
IMAGE_MAX = 30 * 1024 * 1024  # bytes of a cast picture
SUBSCRIPTION_S = 1800  # longest event subscription
ALIVE_EVERY_S = 600  # SSDP announcements (max-age 1800)


def parse_networks(text: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(part.strip(), strict=False) for part in text.split(",") if part.strip()]


class Settings(BaseModel):
    name: str = Field("lan-streamer", title="Name on the phone", min_length=1, max_length=60,
                      description="How the cast apps list it")  # fmt: skip
    port: int = Field(0, title="Port", ge=0, le=65535,
                      description="TCP port for the phones' requests; 0 = chosen once and kept")  # fmt: skip
    networks: str = Field("", title="Allowed networks", max_length=500,
                          description="Who may cast, e.g. 192.168.1.0/24; empty = any device in the LAN (private addresses)")  # fmt: skip

    @field_validator("networks")
    @classmethod
    def _networks(cls, value: str) -> str:
        parse_networks(value)  # ValueError: shown on the admin page
        return value.strip()


def lan_address() -> str:
    """This PC's address in the LAN (the one with the default route)."""
    return route_address("192.0.2.1") or "127.0.0.1"


def route_address(peer: str) -> str:
    """This PC's address on the way to `peer` ("" if there is none)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((peer, 9))  # no packet is sent
            return probe.getsockname()[0]
    except OSError:
        return ""


@dataclass
class Subscription:
    sid: str
    service: str
    callback: str
    expires: float
    seq: int = 0
    waiting: bytes | None = None  # the newest event not sent yet
    sending: bool = False


class Plugin(InputPlugin[Settings]):
    Settings = Settings

    async def start(self) -> None:
        self.udn = self.ctx.store.get("udn") or f"uuid:{uuid.uuid4()}"
        self.ctx.store.set("udn", self.udn)
        self.networks = parse_networks(self.settings.networks)
        self.refused: set[str] = set()  # addresses already logged
        self.tasks: set[asyncio.Task] = set()
        self.subscriptions: dict[str, Subscription] = {}
        self.wake = asyncio.Event()
        # what is cast
        self.uri = self.didl = self.next_uri = self.next_didl = ""
        self.item = upnp.parse_didl("", "")
        self.sender = ""
        self.state, self.status, self.problem = "NO_MEDIA_PRESENT", "OK", ""
        self.active = False
        self.on_air = False  # the output plays the current item ("started")
        self.played, self.since = 0.0, None  # seconds before `since`; monotonic time it plays from
        self.image: Path | None = None
        self.volume, self.mute = 100, False
        self.starting: asyncio.Task | None = None

        self.http = await self._listen()
        self.port = self.http.sockets[0].getsockname()[1] if self.http else 0
        self.ssdp, self.ssdp_error = None, ""
        if self.http:
            try:
                self.ssdp = await self._ssdp()
            except OSError as exc:
                self.ssdp_error = str(exc)
                self.ctx.log.warning("phones cannot find it: UDP %d not usable (%s)", upnp.SSDP_PORT, exc)
            if self.ssdp:
                self._spawn(self._announce())
        self._report()

    async def stop(self) -> None:
        if self.ssdp:
            self._send_multicast(upnp.notifications(self.udn, self._location(""), alive=False))
            self.ssdp.close()
        if self.http:
            self.http.close()
        for task in list(self.tasks):
            task.cancel()

    # --- Activity and source -----------------------------------------------------------

    async def watch_active(self):
        last = None
        while True:
            if self.active != last:
                last = self.active
                yield last
            self.wake.clear()
            await self.wake.wait()

    async def is_active(self) -> bool:
        return self.active

    async def get_source(self):
        if self.image:
            return ImageSource(str(self.image), size="auto")
        return UriSource(self.uri)

    async def can_release(self) -> bool:
        return self.state != "PLAYING"  # what a phone casts stays on air until it stops

    async def on_playback(self, event: PlaybackEvent) -> None:
        if event.kind == "started":
            if not self.on_air and self.state == "PLAYING":
                self.on_air, self.since = True, time.monotonic()
                self._changed()
            return
        if event.kind == "failed":
            self.ctx.log.warning("could not play %s: %s", self.uri, event.detail)
            self._stopped(f"could not play it ({event.detail})" if event.detail else "could not play it")
            return
        if not self.on_air:  # "ended" of an item replaced meanwhile
            return
        self.on_air, self.since, self.played = False, None, 0.0
        if self.next_uri:  # the phone's queue: SetNextAVTransportURI
            self._take(self.next_uri, self.next_didl, self.sender)
            self.next_uri = self.next_didl = ""
            self._begin(switch=True)
        else:
            self._stopped()

    # --- Transport -------------------------------------------------------------------

    def _position(self) -> float:
        return self.played + (time.monotonic() - self.since if self.since is not None else 0.0)

    def _take(self, uri: str, didl: str, sender: str) -> None:
        if uri != self.uri:
            self.played, self.since, self.on_air = 0.0, None, False
        self.uri, self.didl, self.sender = uri, didl, sender
        self.item = upnp.parse_didl(didl, uri)
        thumbnail = self.item.thumbnail if self.item.thumbnail.startswith(("http://", "https://")) else ""
        self.ctx.set_metadata(Metadata(title=self.item.title, channel=self.item.artist, thumbnail=thumbnail,
                                       duration_s=self.item.duration_s, submitter=sender))  # fmt: skip

    def _begin(self, switch: bool) -> None:
        """Puts the current item on air: at once, or after a picture is fetched."""
        if self.starting:
            self.starting.cancel()
        self.state, self.status, self.problem = "PLAYING", "OK", ""
        self.starting = self._spawn(self._start_item(switch))
        self._changed()

    async def _start_item(self, switch: bool) -> None:
        self.image = None
        if self.item.image:
            try:
                self.image = await asyncio.to_thread(self._fetch_image, self.uri)
            except (OSError, ValueError) as exc:
                self.ctx.log.warning("could not fetch the picture %s: %s", self.uri, exc)
                self._stopped(f"could not fetch the picture ({exc})")
                return
        if switch and self.active:
            self.ctx.next_source()  # on air already: the new item replaces the old one
        self.active = True
        self._changed()

    def _stopped(self, problem: str = "") -> None:
        self.state = "STOPPED" if self.uri else "NO_MEDIA_PRESENT"
        self.status, self.problem = ("ERROR_OCCURRED" if problem else "OK"), problem
        self.active = self.on_air = False
        self.since = None
        self._changed()

    def _fetch_image(self, uri: str) -> Path:
        """Pictures are shown from a file (ImageSource): PNG and JPEG."""
        with urllib.request.urlopen(uri, timeout=15) as reply:
            data = reply.read(IMAGE_MAX + 1)
        if len(data) > IMAGE_MAX:
            raise ValueError("larger than 30 MB")
        if data.startswith(b"\x89PNG"):
            suffix = ".png"
        elif data.startswith(b"\xff\xd8"):
            suffix = ".jpg"
        else:
            raise ValueError("only PNG and JPEG pictures can be shown")
        folder = self.ctx.data_dir / "cast"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (hashlib.sha256(data).hexdigest()[:16] + suffix)
        for old in folder.iterdir():
            if old != path and old != self.image:
                try:
                    old.unlink()
                except OSError:
                    pass  # Windows: still open in the output worker; goes next time
        path.write_bytes(data)
        return path

    def _actions(self) -> str:
        if not self.uri:
            return ""
        return {"PLAYING": "Pause,Stop", "PAUSED_PLAYBACK": "Play,Stop"}.get(self.state, "Play")

    def _control(self, service: upnp.Service, action: str, args: dict[str, str], peer: str) -> dict[str, str]:
        if action not in service.actions:
            raise upnp.SoapError(401, "Invalid Action")
        if service is upnp.AV_TRANSPORT:
            return self._av_transport(action, args, peer)
        if service is upnp.RENDERING_CONTROL:
            if action == "SetVolume":
                self.volume = max(0, min(100, _number(args.get("DesiredVolume"))))
                self._changed()
            elif action == "SetMute":
                self.mute = args.get("DesiredMute", "").strip().lower() in ("1", "true", "yes")
                self._changed()
            return {"CurrentVolume": str(self.volume), "CurrentMute": "1" if self.mute else "0"}
        return {"Source": "", "Sink": upnp.SINK_PROTOCOL_INFO, "ConnectionIDs": "0", "RcsID": "0",
                "AVTransportID": "0", "ProtocolInfo": "", "PeerConnectionManager": "", "PeerConnectionID": "-1",
                "Direction": "Input", "Status": "OK"}  # fmt: skip

    def _av_transport(self, action: str, args: dict[str, str], peer: str) -> dict[str, str]:
        if action == "SetAVTransportURI":
            uri = args.get("CurrentURI", "").strip()
            upnp.check_media_uri(uri)
            same = uri == self.uri
            self._take(uri, args.get("CurrentURIMetaData", ""), peer)
            if self.state == "PLAYING":
                if not same:
                    self._begin(switch=True)
            else:
                self._stopped()
            self.ctx.log.info("cast from %s: %s", peer, uri)
        elif action == "SetNextAVTransportURI":
            uri = args.get("NextURI", "").strip()
            if uri:
                upnp.check_media_uri(uri)
            self.next_uri, self.next_didl = uri, args.get("NextURIMetaData", "")
            self._changed()
        elif action == "Play":
            if not self.uri:
                raise upnp.SoapError(701, "nothing to play")
            if self.state != "PLAYING":
                self._begin(switch=False)
        elif action == "Pause":
            if self.state != "PLAYING":
                raise upnp.SoapError(701, "not playing")
            self.played, self.since = 0.0, None  # Play starts it over
            self.state, self.active, self.on_air = "PAUSED_PLAYBACK", False, False
            self._changed()
        elif action == "Stop":
            self.played = 0.0
            self.next_uri = self.next_didl = ""
            self._stopped()
        elif action == "Seek":
            raise upnp.SoapError(710, "seeking is not supported")
        elif action in ("Next", "Previous"):
            raise upnp.SoapError(701, "no other item")
        return self._av_values()

    def _av_values(self) -> dict[str, str]:
        duration = upnp.format_time(self.item.duration_s) if self.item.duration_s else "0:00:00"
        position = upnp.format_time(self._position())
        return {
            "CurrentTransportState": self.state, "CurrentTransportStatus": self.status, "CurrentSpeed": "1",
            "NrTracks": "1" if self.uri else "0", "MediaDuration": duration,
            "CurrentURI": self.uri, "CurrentURIMetaData": self.didl,
            "NextURI": self.next_uri, "NextURIMetaData": self.next_didl,
            "PlayMedium": "NETWORK" if self.uri else "NONE", "RecordMedium": "NOT_IMPLEMENTED",
            "WriteStatus": "NOT_IMPLEMENTED",
            "Track": "1" if self.uri else "0", "TrackDuration": duration, "TrackMetaData": self.didl,
            "TrackURI": self.uri, "RelTime": position, "AbsTime": position,
            "RelCount": "2147483647", "AbsCount": "2147483647",
            "PlayMedia": "NETWORK", "RecMedia": "NOT_IMPLEMENTED", "RecQualityModes": "NOT_IMPLEMENTED",
            "PlayMode": "NORMAL", "RecQualityMode": "NOT_IMPLEMENTED", "Actions": self._actions(),
        }  # fmt: skip

    def _changed(self) -> None:
        """The state changed: the core, the phones' event subscriptions, the admin page."""
        self.wake.set()
        for name in ("AVTransport", "RenderingControl"):
            self._notify(name)
        self._report()

    def _report(self) -> None:
        if not self.http:
            text = f"port {self.settings.port} is in use – choose another one"
        elif self.state in ("PLAYING", "PAUSED_PLAYBACK"):
            verb = "casting" if self.state == "PLAYING" else "paused"
            text = f'{verb} "{self.item.title}" from {self.sender}'
        else:
            text = f'"{self.settings.name}" waiting for a phone'
            if self.problem:
                text += f" · {self.problem}"
        if self.http:
            text += f" · {lan_address()}:{self.port}"
        if self.ssdp_error:
            text += f" · phones cannot find it: UDP {upnp.SSDP_PORT} not usable ({self.ssdp_error})"
        self.ctx.set_info(text)

    # --- Who may cast ----------------------------------------------------------------

    def allowed(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        ok = any(ip in net for net in self.networks) if self.networks else ip.is_private
        if not ok and address not in self.refused:
            self.refused.add(address)
            self.ctx.log.warning("refused %s: not in the allowed networks", address)
        return ok

    # --- HTTP: descriptions, control, events -----------------------------------------

    async def _listen(self) -> asyncio.Server | None:
        fixed = self.settings.port
        ports = [fixed] if fixed else [p for p in (self.ctx.store.get("port"), 0) if p is not None]
        for port in ports:
            try:
                server = await asyncio.start_server(self._serve, "0.0.0.0", port, limit=MAX_HEAD)
            except OSError as exc:
                self.ctx.log.warning("cannot listen on port %s: %s", port, exc)
                continue
            if not fixed:
                self.ctx.store.set("port", server.sockets[0].getsockname()[1])
            return server
        return None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("",))[0]
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _version = (lines[0].split(" ") + ["", ""])[:3]
            headers = {}
            for line in lines[1:]:
                name, sep, value = line.partition(":")
                if sep:
                    headers[name.strip().upper()] = value.strip()
            if not self.allowed(peer):
                status, extra, body = 403, {}, b""
            else:
                length = _number(headers.get("CONTENT-LENGTH"))
                if length > MAX_BODY:
                    status, extra, body = 413, {}, b""
                else:
                    data = await asyncio.wait_for(reader.readexactly(length), 10) if length > 0 else b""
                    status, extra, body = self._handle(method, urlsplit(target).path, headers, data, peer)
            reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
                      412: "Precondition Failed", 413: "Payload Too Large", 500: "Internal Server Error"}  # fmt: skip
            if body:
                extra.setdefault("Content-Type", 'text/xml; charset="utf-8"')
            fields = {"Server": upnp.SERVER, "Content-Length": str(len(body)), "Connection": "close", **extra}
            text = f"HTTP/1.1 {status} {reason.get(status, 'Error')}\r\n"
            text += "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
            writer.write(text.encode() + (b"" if method == "HEAD" else body))
            await writer.drain()
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            writer.close()

    def _handle(self, method: str, path: str, headers: dict[str, str], data: bytes, peer: str):
        name, _, rest = path.strip("/").partition("/")
        if method in ("GET", "HEAD"):
            if path == "/description.xml":
                return 200, {}, upnp.device_xml(self.settings.name, self.udn).encode()
            service = upnp.SERVICES.get(name.removesuffix(".xml")) if name.endswith(".xml") and not rest else None
            return (200, {}, upnp.service_xml(service).encode()) if service else (404, {}, b"")
        service = upnp.SERVICES.get(name)
        if service is None or rest not in ("control", "event"):
            return 404, {}, b""
        if method == "POST" and rest == "control":
            try:
                action, args = upnp.parse_soap(data)
                return 200, {"EXT": ""}, upnp.soap_reply(service, action, self._control(service, action, args, peer))
            except upnp.SoapError as error:
                return 500, {}, upnp.soap_fault(error)
        if method == "SUBSCRIBE" and rest == "event":
            return self._subscribe(service.name, headers, peer)
        if method == "UNSUBSCRIBE" and rest == "event":
            found = self.subscriptions.pop(headers.get("SID", ""), None)
            return (200, {}, b"") if found else (412, {}, b"")
        return 405, {}, b""

    def _subscribe(self, service: str, headers: dict[str, str], peer: str):
        timeout = headers.get("TIMEOUT", "").lower().removeprefix("second-")
        seconds = min(_number(timeout) or SUBSCRIPTION_S, SUBSCRIPTION_S)
        sid = headers.get("SID", "")
        if sid:  # renewal
            sub = self.subscriptions.get(sid)
            if sub is None or sub.service != service:
                return 412, {}, b""
            sub.expires = time.monotonic() + seconds
        else:
            callback = headers.get("CALLBACK", "").split(">", 1)[0].lstrip("<").strip()
            parts = urlsplit(callback)
            # events only go back to the device that asked: no requests to other machines
            if headers.get("NT") != "upnp:event" or parts.scheme != "http" or parts.hostname != peer:
                return 412, {}, b""
            sid = f"uuid:{uuid.uuid4()}"
            sub = self.subscriptions[sid] = Subscription(sid, service, callback, time.monotonic() + seconds)
            self._spawn(self._first_event(sub))
        return 200, {"SID": sid, "TIMEOUT": f"Second-{seconds}"}, b""

    async def _first_event(self, sub: Subscription) -> None:
        await asyncio.sleep(0.2)  # after the answer to SUBSCRIBE
        self._queue_event(sub)

    def _event_body(self, service: str) -> bytes:
        if service == "AVTransport":
            v = self._av_values()
            values = {
                "TransportState": v["CurrentTransportState"], "TransportStatus": v["CurrentTransportStatus"],
                "TransportPlaySpeed": "1", "CurrentPlayMode": "NORMAL", "NumberOfTracks": v["NrTracks"],
                "CurrentTrack": v["Track"], "CurrentTrackDuration": v["TrackDuration"],
                "CurrentMediaDuration": v["MediaDuration"], "AVTransportURI": self.uri,
                "CurrentTrackURI": self.uri, "AVTransportURIMetaData": self.didl,
                "CurrentTrackMetaData": self.didl, "NextAVTransportURI": self.next_uri,
                "NextAVTransportURIMetaData": self.next_didl, "CurrentTransportActions": v["Actions"],
                "PlaybackStorageMedium": v["PlayMedium"],
            }  # fmt: skip
            return upnp.property_set({"LastChange": upnp.last_change(upnp.AV_TRANSPORT, values)})
        if service == "RenderingControl":
            values = {"Volume": str(self.volume), "Mute": "1" if self.mute else "0"}
            return upnp.property_set({"LastChange": upnp.last_change(upnp.RENDERING_CONTROL, values)})
        return upnp.property_set({"SourceProtocolInfo": "", "SinkProtocolInfo": upnp.SINK_PROTOCOL_INFO,
                                  "CurrentConnectionIDs": "0"})  # fmt: skip

    def _notify(self, service: str) -> None:
        now = time.monotonic()
        for sid, sub in list(self.subscriptions.items()):
            if sub.expires < now:
                del self.subscriptions[sid]
            elif sub.service == service:
                self._queue_event(sub)

    def _queue_event(self, sub: Subscription) -> None:
        """Events go one after another (SEQ in order); a change while one is
        on its way replaces the waiting one, as each holds the whole state."""
        sub.waiting = self._event_body(sub.service)
        if not sub.sending:
            sub.sending = True
            self._spawn(self._send_events(sub))

    async def _send_events(self, sub: Subscription) -> None:
        try:
            while sub.waiting is not None and sub.sid in self.subscriptions:
                body, sub.waiting = sub.waiting, None
                await self._send_event(sub, body)
        finally:
            sub.sending = False

    async def _send_event(self, sub: Subscription, body: bytes) -> None:
        sid = sub.sid
        seq, sub.seq = sub.seq, (sub.seq + 1) % 4294967296 or 1
        parts = urlsplit(sub.callback)
        request = (f"NOTIFY {parts.path or '/'}{'?' + parts.query if parts.query else ''} HTTP/1.1\r\n"
                   f"HOST: {parts.netloc}\r\nCONTENT-TYPE: text/xml; charset=\"utf-8\"\r\nNT: upnp:event\r\n"
                   f"NTS: upnp:propchange\r\nSID: {sid}\r\nSEQ: {seq}\r\nContent-Length: {len(body)}\r\n"
                   "Connection: close\r\n\r\n").encode() + body  # fmt: skip
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(parts.hostname, parts.port or 80), 5)
            try:
                writer.write(request)
                await writer.drain()
                await asyncio.wait_for(reader.readline(), 5)
            finally:
                writer.close()
        except (OSError, TimeoutError):
            pass  # the app is gone or asleep; the subscription runs out

    # --- SSDP: how the phones find it ------------------------------------------------

    def _location(self, peer: str) -> str:
        host = (route_address(peer) if peer else "") or lan_address()
        return f"http://{host}:{self.port}/description.xml"

    async def _ssdp(self) -> asyncio.DatagramTransport:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Windows' own SSDP service uses it too
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", upnp.SSDP_PORT))
            lan = lan_address()
            joined = 0
            for address in {lan, "0.0.0.0"}:  # the LAN adapter; any, for a PC without default route
                try:
                    mreq = socket.inet_aton(upnp.SSDP_GROUP) + socket.inet_aton(address)
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                    joined += 1
                except OSError:
                    pass  # already joined on that adapter
            if not joined:
                raise OSError("cannot join the SSDP multicast group")
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lan))
            except OSError:
                pass
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: _Ssdp(self), sock=sock)
        return transport

    def _send_multicast(self, messages: list[bytes]) -> None:
        for message in messages:
            try:
                self.ssdp.sendto(message, (upnp.SSDP_GROUP, upnp.SSDP_PORT))
            except OSError:
                pass

    async def _announce(self) -> None:
        while True:
            for _ in range(2):  # UDP: twice, as the spec suggests
                self._send_multicast(upnp.notifications(self.udn, self._location(""), alive=True))
                await asyncio.sleep(0.5)
            await asyncio.sleep(ALIVE_EVERY_S)

    def search(self, data: bytes, address: tuple[str, int]) -> None:
        start, headers = upnp.parse_ssdp(data)
        if not start.upper().startswith("M-SEARCH") or not self.allowed(address[0]):
            return
        replies = upnp.search_replies(headers, self.udn, self._location(address[0]))
        if not replies:
            return
        delay = random.uniform(0, min(max(_number(headers.get("MX")), 1), 3) / 3)  # spread out, within MX

        def send() -> None:
            for reply in replies:
                try:
                    self.ssdp.sendto(reply, address)
                except OSError:
                    pass

        asyncio.get_running_loop().call_later(delay, send)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


class _Ssdp(asyncio.DatagramProtocol):
    def __init__(self, plugin: Plugin):
        self.plugin = plugin

    def datagram_received(self, data: bytes, addr) -> None:
        self.plugin.search(data, addr)

    def error_received(self, exc: Exception) -> None:
        pass  # e.g. an ICMP "unreachable" for a reply


def _number(text: str | None) -> int:
    try:
        return int(str(text).strip())
    except ValueError:
        return 0
