"""UPnP AV MediaRenderer pieces for the DLNA cast input: device and service
descriptions, SOAP requests and replies, SSDP messages, events, DIDL-Lite.

Plain functions without network access, so they can be tested alone; the
input (dlna.py) does the sockets. Follows UPnP Device Architecture 1.0 and
the AVTransport:1, RenderingControl:1 and ConnectionManager:1 templates, as
far as phone apps use them.
"""

from __future__ import annotations

import ipaddress
import platform
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape, quoteattr

DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
SSDP_GROUP, SSDP_PORT = "239.255.255.250", 1900
MAX_AGE_S = 1800
SERVER = f"{platform.system() or 'OS'}/1.0 UPnP/1.0 lan-streamer/1.0"

# What the input accepts; GStreamer plays more, but the apps choose by this list.
SINK_TYPES = (
    "video/mp4", "video/x-matroska", "video/webm", "video/quicktime", "video/x-msvideo", "video/mpeg",
    "video/mp2t", "video/x-flv", "video/3gpp", "application/vnd.apple.mpegurl", "application/x-mpegurl",
    "audio/mpeg", "audio/mp4", "audio/aac", "audio/flac", "audio/ogg", "audio/wav", "audio/x-wav",
    "image/jpeg", "image/png",
)  # fmt: skip
SINK_PROTOCOL_INFO = ",".join(f"http-get:*:{mime}:*" for mime in SINK_TYPES)


@dataclass(frozen=True)
class Service:
    name: str  # "AVTransport"
    actions: dict[str, tuple[tuple[str, str, str], ...]]  # action → (direction, argument, state variable)
    variables: tuple[tuple[str, str, bool, tuple[str, ...]], ...]  # (name, type, evented, allowed values)

    @property
    def type(self) -> str:
        return f"urn:schemas-upnp-org:service:{self.name}:1"

    @property
    def id(self) -> str:
        return f"urn:upnp-org:serviceId:{self.name}"


def _args(*items: str) -> tuple[tuple[str, str, str], ...]:
    """"in Speed TransportPlaySpeed" → ("in", "Speed", "TransportPlaySpeed")."""
    return tuple(tuple(item.split()) for item in items)  # type: ignore[misc]


_ID = "in InstanceID A_ARG_TYPE_InstanceID"
AV_TRANSPORT = Service("AVTransport", {
    "SetAVTransportURI": _args(_ID, "in CurrentURI AVTransportURI", "in CurrentURIMetaData AVTransportURIMetaData"),
    "SetNextAVTransportURI": _args(_ID, "in NextURI NextAVTransportURI", "in NextURIMetaData NextAVTransportURIMetaData"),
    "GetMediaInfo": _args(_ID, "out NrTracks NumberOfTracks", "out MediaDuration CurrentMediaDuration",
                          "out CurrentURI AVTransportURI", "out CurrentURIMetaData AVTransportURIMetaData",
                          "out NextURI NextAVTransportURI", "out NextURIMetaData NextAVTransportURIMetaData",
                          "out PlayMedium PlaybackStorageMedium", "out RecordMedium RecordStorageMedium",
                          "out WriteStatus RecordMediumWriteStatus"),
    "GetTransportInfo": _args(_ID, "out CurrentTransportState TransportState",
                              "out CurrentTransportStatus TransportStatus", "out CurrentSpeed TransportPlaySpeed"),
    "GetPositionInfo": _args(_ID, "out Track CurrentTrack", "out TrackDuration CurrentTrackDuration",
                             "out TrackMetaData CurrentTrackMetaData", "out TrackURI CurrentTrackURI",
                             "out RelTime RelativeTimePosition", "out AbsTime AbsoluteTimePosition",
                             "out RelCount RelativeCounterPosition", "out AbsCount AbsoluteCounterPosition"),
    "GetDeviceCapabilities": _args(_ID, "out PlayMedia PossiblePlaybackStorageMedia",
                                   "out RecMedia PossibleRecordStorageMedia",
                                   "out RecQualityModes PossibleRecordQualityModes"),
    "GetTransportSettings": _args(_ID, "out PlayMode CurrentPlayMode", "out RecQualityMode CurrentRecordQualityMode"),
    "GetCurrentTransportActions": _args(_ID, "out Actions CurrentTransportActions"),
    "Play": _args(_ID, "in Speed TransportPlaySpeed"),
    "Pause": _args(_ID),
    "Stop": _args(_ID),
    "Seek": _args(_ID, "in Unit A_ARG_TYPE_SeekMode", "in Target A_ARG_TYPE_SeekTarget"),
    "Next": _args(_ID),
    "Previous": _args(_ID),
}, (
    ("TransportState", "string", False, ("STOPPED", "PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING", "NO_MEDIA_PRESENT")),
    ("TransportStatus", "string", False, ("OK", "ERROR_OCCURRED")),
    ("TransportPlaySpeed", "string", False, ("1",)),
    ("PlaybackStorageMedium", "string", False, ("NONE", "NETWORK")),
    ("RecordStorageMedium", "string", False, ("NOT_IMPLEMENTED",)),
    ("PossiblePlaybackStorageMedia", "string", False, ()),
    ("PossibleRecordStorageMedia", "string", False, ()),
    ("PossibleRecordQualityModes", "string", False, ()),
    ("RecordMediumWriteStatus", "string", False, ("NOT_IMPLEMENTED",)),
    ("CurrentPlayMode", "string", False, ("NORMAL",)),
    ("CurrentRecordQualityMode", "string", False, ("NOT_IMPLEMENTED",)),
    ("NumberOfTracks", "ui4", False, ()),
    ("CurrentTrack", "ui4", False, ()),
    ("CurrentTrackDuration", "string", False, ()),
    ("CurrentMediaDuration", "string", False, ()),
    ("CurrentTrackMetaData", "string", False, ()),
    ("CurrentTrackURI", "string", False, ()),
    ("AVTransportURI", "string", False, ()),
    ("AVTransportURIMetaData", "string", False, ()),
    ("NextAVTransportURI", "string", False, ()),
    ("NextAVTransportURIMetaData", "string", False, ()),
    ("RelativeTimePosition", "string", False, ()),
    ("AbsoluteTimePosition", "string", False, ()),
    ("RelativeCounterPosition", "i4", False, ()),
    ("AbsoluteCounterPosition", "i4", False, ()),
    ("CurrentTransportActions", "string", False, ()),
    ("LastChange", "string", True, ()),
    ("A_ARG_TYPE_SeekMode", "string", False, ("REL_TIME", "ABS_TIME", "TRACK_NR")),
    ("A_ARG_TYPE_SeekTarget", "string", False, ()),
    ("A_ARG_TYPE_InstanceID", "ui4", False, ()),
))  # fmt: skip

RENDERING_CONTROL = Service("RenderingControl", {
    "GetVolume": _args(_ID, "in Channel A_ARG_TYPE_Channel", "out CurrentVolume Volume"),
    "SetVolume": _args(_ID, "in Channel A_ARG_TYPE_Channel", "in DesiredVolume Volume"),
    "GetMute": _args(_ID, "in Channel A_ARG_TYPE_Channel", "out CurrentMute Mute"),
    "SetMute": _args(_ID, "in Channel A_ARG_TYPE_Channel", "in DesiredMute Mute"),
}, (
    ("Volume", "ui2", False, ()),
    ("Mute", "boolean", False, ()),
    ("LastChange", "string", True, ()),
    ("A_ARG_TYPE_Channel", "string", False, ("Master",)),
    ("A_ARG_TYPE_InstanceID", "ui4", False, ()),
))  # fmt: skip

CONNECTION_MANAGER = Service("ConnectionManager", {
    "GetProtocolInfo": _args("out Source SourceProtocolInfo", "out Sink SinkProtocolInfo"),
    "GetCurrentConnectionIDs": _args("out ConnectionIDs CurrentConnectionIDs"),
    "GetCurrentConnectionInfo": _args("in ConnectionID A_ARG_TYPE_ConnectionID", "out RcsID A_ARG_TYPE_RcsID",
                                      "out AVTransportID A_ARG_TYPE_AVTransportID",
                                      "out ProtocolInfo A_ARG_TYPE_ProtocolInfo",
                                      "out PeerConnectionManager A_ARG_TYPE_ConnectionManager",
                                      "out PeerConnectionID A_ARG_TYPE_ConnectionID",
                                      "out Direction A_ARG_TYPE_Direction", "out Status A_ARG_TYPE_ConnectionStatus"),
}, (
    ("SourceProtocolInfo", "string", True, ()),
    ("SinkProtocolInfo", "string", True, ()),
    ("CurrentConnectionIDs", "string", True, ()),
    ("A_ARG_TYPE_ConnectionStatus", "string", False, ("OK", "ContentFormatMismatch", "InsufficientBandwidth",
                                                      "UnreliableChannel", "Unknown")),
    ("A_ARG_TYPE_ConnectionManager", "string", False, ()),
    ("A_ARG_TYPE_Direction", "string", False, ("Input", "Output")),
    ("A_ARG_TYPE_ProtocolInfo", "string", False, ()),
    ("A_ARG_TYPE_ConnectionID", "i4", False, ()),
    ("A_ARG_TYPE_AVTransportID", "i4", False, ()),
    ("A_ARG_TYPE_RcsID", "i4", False, ()),
))  # fmt: skip

SERVICES = {s.name: s for s in (AV_TRANSPORT, RENDERING_CONTROL, CONNECTION_MANAGER)}


# --- Descriptions ---------------------------------------------------------------------


def device_xml(name: str, udn: str) -> str:
    services = "".join(
        f"<service><serviceType>{s.type}</serviceType><serviceId>{s.id}</serviceId>"
        f"<SCPDURL>/{s.name}.xml</SCPDURL><controlURL>/{s.name}/control</controlURL>"
        f"<eventSubURL>/{s.name}/event</eventSubURL></service>"
        for s in SERVICES.values()
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion><device>"
        f"<deviceType>{DEVICE_TYPE}</deviceType><friendlyName>{escape(name)}</friendlyName>"
        "<manufacturer>lan-streamer</manufacturer><manufacturerURL>https://github.com/zGLados/lan-streamer</manufacturerURL>"
        "<modelName>lan-streamer</modelName><modelDescription>Plays what a phone casts on the lan-streamer outputs</modelDescription>"
        f"<modelNumber>1</modelNumber><UDN>{udn}</UDN><dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>"
        f"<serviceList>{services}</serviceList></device></root>"
    )


def service_xml(service: Service) -> str:
    actions = []
    for action, args in service.actions.items():
        listed = "".join(
            f"<argument><name>{arg}</name><direction>{direction}</direction>"
            f"<relatedStateVariable>{variable}</relatedStateVariable></argument>"
            for direction, arg, variable in args
        )
        actions.append(f"<action><name>{action}</name><argumentList>{listed}</argumentList></action>")
    variables = []
    for name, kind, evented, allowed in service.variables:
        values = "".join(f"<allowedValue>{v}</allowedValue>" for v in allowed)
        values = f"<allowedValueList>{values}</allowedValueList>" if values else ""
        if name == "Volume":
            values = "<allowedValueRange><minimum>0</minimum><maximum>100</maximum><step>1</step></allowedValueRange>"
        variables.append(f'<stateVariable sendEvents="{"yes" if evented else "no"}"><name>{name}</name>'
                         f"<dataType>{kind}</dataType>{values}</stateVariable>")  # fmt: skip
    return (
        '<?xml version="1.0" encoding="utf-8"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion>"
        f"<actionList>{''.join(actions)}</actionList><serviceStateTable>{''.join(variables)}</serviceStateTable></scpd>"
    )


# --- SOAP -----------------------------------------------------------------------------


class SoapError(Exception):
    """An answer with a UPnP error code (UPnP AV: 701 transition not available, ...)."""

    def __init__(self, code: int, text: str):
        super().__init__(text)
        self.code = code


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_soap(body: bytes) -> tuple[str, dict[str, str]]:
    """The action name and its arguments from a SOAP request."""
    try:
        envelope = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SoapError(401, f"not a SOAP request: {exc}") from None
    soap_body = next((e for e in envelope if _local(e.tag) == "Body"), None)
    action = next(iter(soap_body), None) if soap_body is not None else None
    if action is None:
        raise SoapError(401, "no action in the SOAP body")
    return _local(action.tag), {_local(arg.tag): arg.text or "" for arg in action}


def soap_reply(service: Service, action: str, values: dict[str, str]) -> bytes:
    outs = [arg for direction, arg, _variable in service.actions[action] if direction == "out"]
    args = "".join(f"<{arg}>{escape(str(values.get(arg, '')))}</{arg}>" for arg in outs)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action}Response xmlns:u="{service.type}">{args}</u:{action}Response></s:Body></s:Envelope>'
    ).encode()


def soap_fault(error: SoapError) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
        "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
        '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{error.code}</errorCode><errorDescription>{escape(str(error))}</errorDescription>"
        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
    ).encode()


# --- Events (GENA) --------------------------------------------------------------------


def last_change(service: Service, values: dict[str, str]) -> str:
    """The LastChange value: every state variable of instance 0."""
    short = "AVT" if service is AV_TRANSPORT else "RCS"
    channel = ' channel="Master"' if service is RENDERING_CONTROL else ""
    inner = "".join(f"<{name}{channel} val={quoteattr(value)}/>" for name, value in values.items())
    return f'<Event xmlns="urn:schemas-upnp-org:metadata-1-0/{short}/"><InstanceID val="0">{inner}</InstanceID></Event>'


def property_set(values: dict[str, str]) -> bytes:
    props = "".join(f"<e:property><{name}>{escape(value)}</{name}></e:property>" for name, value in values.items())
    return f'<?xml version="1.0" encoding="utf-8"?><e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">{props}</e:propertyset>'.encode()


# --- DIDL-Lite (what the phone says about the media) ----------------------------------


@dataclass(frozen=True)
class Item:
    title: str = ""
    artist: str = ""
    thumbnail: str = ""
    duration_s: float | None = None
    mime: str = ""
    image: bool = False


def parse_didl(metadata: str, uri: str) -> Item:
    """Title, artist, cover, duration and type of a cast item; empty or broken
    metadata (many apps send none) gives what the URI tells."""
    title = artist = thumbnail = mime = kind = ""
    duration = None
    if metadata.strip():
        try:
            root = ET.fromstring(metadata)
        except ET.ParseError:
            root = None
        item = next(iter(root), None) if root is not None else None
        for e in item if item is not None else ():
            name, text = _local(e.tag), (e.text or "").strip()
            if name == "title":
                title = text
            elif name in ("artist", "creator") and not artist:
                artist = text
            elif name == "albumArtURI":
                thumbnail = text
            elif name == "class":
                kind = text
            elif name == "res":
                parts = (e.get("protocolInfo") or "").split(":")
                mime = mime or (parts[2] if len(parts) > 2 else "")
                duration = duration if duration is not None else parse_time(e.get("duration") or "")
    path = urlsplit(uri).path
    image = ("imageItem" in kind or mime.startswith("image/")
             or path.lower().endswith((".jpg", ".jpeg", ".png")))  # fmt: skip
    if not title:
        title = unquote(path.rsplit("/", 1)[-1]) or "Cast"
    return Item(title, artist, thumbnail, duration, mime, image)


def parse_time(text: str) -> float | None:
    """"0:03:25.500" → 205.5; None for nothing or NOT_IMPLEMENTED."""
    parts = text.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None


def format_time(seconds: float | None) -> str:
    if seconds is None:
        return "NOT_IMPLEMENTED"
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def check_media_uri(uri: str) -> None:
    """Only http(s) from somewhere else: no files of this PC, no services on it."""
    parts = urlsplit(uri)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SoapError(716, "only http and https links can be cast")
    host = parts.hostname.lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost" or host.endswith(".localhost")
    if loopback:
        raise SoapError(716, "links to this PC itself cannot be cast")


# --- SSDP -----------------------------------------------------------------------------


def targets(udn: str) -> list[tuple[str, str]]:
    """(NT/ST, USN) of everything the device announces."""
    found = [("upnp:rootdevice", f"{udn}::upnp:rootdevice"), (udn, udn), (DEVICE_TYPE, f"{udn}::{DEVICE_TYPE}")]
    return found + [(s.type, f"{udn}::{s.type}") for s in SERVICES.values()]


def parse_ssdp(data: bytes) -> tuple[str, dict[str, str]]:
    """The start line and the headers (names in upper case) of an SSDP datagram."""
    lines = data.decode("utf-8", "replace").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().upper()] = value.strip()
    return lines[0].strip(), headers


def search_replies(headers: dict[str, str], udn: str, location: str) -> list[bytes]:
    """The answers to an M-SEARCH, none when it does not ask for us."""
    if headers.get("MAN", "").strip('"') != "ssdp:discover":
        return []
    wanted = headers.get("ST", "")
    found = [t for t in targets(udn) if wanted in ("ssdp:all", t[0])]
    return [
        (f"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age={MAX_AGE_S}\r\nEXT:\r\nLOCATION: {location}\r\n"
         f"SERVER: {SERVER}\r\nST: {st}\r\nUSN: {usn}\r\n\r\n").encode()
        for st, usn in found
    ]  # fmt: skip


def notifications(udn: str, location: str, alive: bool) -> list[bytes]:
    host = f"HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n"
    if alive:
        return [
            (f"NOTIFY * HTTP/1.1\r\n{host}CACHE-CONTROL: max-age={MAX_AGE_S}\r\nLOCATION: {location}\r\n"
             f"NT: {nt}\r\nNTS: ssdp:alive\r\nSERVER: {SERVER}\r\nUSN: {usn}\r\n\r\n").encode()
            for nt, usn in targets(udn)
        ]  # fmt: skip
    return [f"NOTIFY * HTTP/1.1\r\n{host}NT: {nt}\r\nNTS: ssdp:byebye\r\nUSN: {usn}\r\n\r\n".encode() for nt, usn in targets(udn)]
