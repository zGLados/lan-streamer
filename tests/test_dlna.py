"""The built-in DLNA cast input: the UPnP pieces alone, then a real plugin process
driven like a phone's cast app (BubbleUPnP & co.) over HTTP and SSDP."""

import asyncio
import http.server
import socket
import sys
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import ClassVar
from xml.sax.saxutils import escape

import pytest

from lanstreamer.plugins.process import PluginProcess
from lanstreamer.plugins.registry import BUILTIN

PLUGIN = BUILTIN / "dlna"
sys.path.insert(0, str(PLUGIN))
import upnp  # the plugin's own module

DIDL = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/"'
    ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"><item id="1" parentID="0" restricted="1">'
    "<dc:title>Holiday 2026</dc:title><upnp:artist>Anna</upnp:artist>"
    "<upnp:class>object.item.videoItem</upnp:class>"
    '<res protocolInfo="http-get:*:video/mp4:*" duration="0:03:25.000">http://192.0.2.10:8080/v.mp4</res>'
    "</item></DIDL-Lite>"
)


# --- The UPnP pieces -------------------------------------------------------------------


def test_the_cast_apps_metadata():
    item = upnp.parse_didl(DIDL, "http://192.0.2.10:8080/v.mp4")
    assert (item.title, item.artist, item.duration_s, item.mime, item.image) == ("Holiday 2026", "Anna", 205.0, "video/mp4", False)
    # many apps send no metadata: the name from the link
    assert upnp.parse_didl("", "http://192.0.2.10/media/My%20Song.mp3").title == "My Song.mp3"
    assert upnp.parse_didl("<broken", "http://192.0.2.10/IMG_1.JPG").image is True
    assert upnp.parse_didl("", "http://192.0.2.10/").title == "Cast"


@pytest.mark.parametrize("uri", ["file:///C:/Windows/win.ini", "rtsp://192.0.2.1/x", "http://127.0.0.1:9997/v3/paths/list",
                                 "http://localhost/x", "http:///x"])  # fmt: skip
def test_links_that_cannot_be_cast(uri):
    with pytest.raises(upnp.SoapError) as exc:
        upnp.check_media_uri(uri)
    assert exc.value.code == 716


def test_the_descriptions_are_complete():
    root = ET.fromstring(upnp.device_xml("Living <room>", "uuid:x"))
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    assert root.find("d:device/d:friendlyName", ns).text == "Living <room>"
    assert len(root.findall("d:device/d:serviceList/d:service", ns)) == 3
    for service in upnp.SERVICES.values():
        ET.fromstring(upnp.service_xml(service))
        names = {v[0] for v in service.variables}
        for args in service.actions.values():
            assert {variable for _d, _a, variable in args} <= names


def test_searches_get_an_answer_only_for_us():
    asked = {"MAN": '"ssdp:discover"', "ST": "ssdp:all", "MX": "2"}
    assert len(upnp.search_replies(asked, "uuid:x", "http://h/d.xml")) == 6
    renderer = upnp.search_replies({**asked, "ST": upnp.DEVICE_TYPE}, "uuid:x", "http://h/d.xml")
    assert len(renderer) == 1 and b"LOCATION: http://h/d.xml" in renderer[0]
    assert upnp.search_replies({**asked, "ST": "urn:schemas-upnp-org:device:MediaServer:1"}, "uuid:x", "") == []
    assert upnp.search_replies({"ST": "ssdp:all"}, "uuid:x", "") == []


def test_times():
    assert upnp.parse_time("1:02:03.5") == 3723.5 and upnp.parse_time("NOT_IMPLEMENTED") is None
    assert upnp.format_time(3723.9) == "1:02:03" and upnp.format_time(None) == "NOT_IMPLEMENTED"


# --- A real plugin process, driven like a cast app -------------------------------------


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(request):
    with urllib.request.urlopen(request, timeout=5) as reply:
        return reply.headers, reply.read()


def soap(port, service, action, **args):
    body = "".join(f"<{k}>{v}</{k}>" for k, v in {"InstanceID": "0", **args}.items())
    envelope = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">{body}</u:{action}></s:Body></s:Envelope>')  # fmt: skip
    request = urllib.request.Request(f"http://127.0.0.1:{port}/{service}/control", envelope.encode(), headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"urn:schemas-upnp-org:service:{service}:1#{action}"'})  # fmt: skip
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            root = ET.fromstring(reply.read())
    except urllib.error.HTTPError as exc:
        code = ET.fromstring(exc.read()).find(".//{urn:schemas-upnp-org:control-1-0}errorCode").text
        return {"error": int(code)}
    answer = root[0][0]
    return {e.tag: e.text or "" for e in answer}


class Events(http.server.BaseHTTPRequestHandler):
    """A cast app's event callback: records the NOTIFYs it gets."""

    got: ClassVar[list] = []

    def do_NOTIFY(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.got.append((self.headers["SID"], self.headers["SEQ"], body.decode()))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass


def run_plugin(tmp_path, settings, steps):
    async def main():
        events = []
        proc = PluginProcess("cast", PLUGIN, tmp_path / "data", settings, lambda _i, e: events.append(e))
        await proc.start()
        try:
            await steps(proc, events)
        finally:
            await proc.stop()

    asyncio.run(main())


def last(events, kind):
    found = [e for e in events if e.get("event") == kind]
    return found[-1]["value"] if found else None


async def until(check):
    for _ in range(50):
        if check():
            return True
        await asyncio.sleep(0.1)
    return False


def test_a_phone_casts_pauses_and_stops(tmp_path):
    port = free_port()
    Events.got = []
    callbacks = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Events)
    threading.Thread(target=callbacks.serve_forever, daemon=True).start()

    async def steps(proc, events):
        ask = lambda *a, **k: asyncio.to_thread(soap, port, *a, **k)
        description = await asyncio.to_thread(fetch, f"http://127.0.0.1:{port}/description.xml")
        assert b"<friendlyName>Test TV</friendlyName>" in description[1]
        assert await until(lambda: last(events, "active") is False)
        assert '"Test TV" waiting for a phone' in last(events, "info")

        subscribe = urllib.request.Request(f"http://127.0.0.1:{port}/AVTransport/event", method="SUBSCRIBE", headers={
            "CALLBACK": f"<http://127.0.0.1:{callbacks.server_address[1]}/ev>", "NT": "upnp:event", "TIMEOUT": "Second-300"})  # fmt: skip
        headers, _body = await asyncio.to_thread(fetch, subscribe)
        sid = headers["SID"]
        assert sid.startswith("uuid:") and headers["TIMEOUT"] == "Second-300"
        assert await until(lambda: Events.got)
        assert Events.got[0][:2] == (sid, "0") and "NO_MEDIA_PRESENT" in Events.got[0][2]

        assert (await ask("AVTransport", "Play"))["error"] == 701  # nothing set yet
        assert (await ask("AVTransport", "SetAVTransportURI", CurrentURI="file:///etc/passwd", CurrentURIMetaData=""))["error"] == 716
        uri = "http://192.0.2.10:8080/v.mp4"
        await ask("AVTransport", "SetAVTransportURI", CurrentURI=uri, CurrentURIMetaData=escape(DIDL))
        assert (await ask("AVTransport", "GetTransportInfo"))["CurrentTransportState"] == "STOPPED"
        await ask("AVTransport", "Play", Speed="1")
        assert await until(lambda: last(events, "active") is True)
        assert await proc.get_source() == {"kind": "uri", "uri": uri}
        assert await proc.can_release() is False  # the cast stays on air
        meta = last(events, "metadata")
        assert (meta["title"], meta["channel"], meta["duration_s"], meta["submitter"]) == ("Holiday 2026", "Anna", 205.0, "127.0.0.1")
        assert last(events, "info").startswith('casting "Holiday 2026" from 127.0.0.1')
        assert await until(lambda: any("PLAYING" in body for _s, _q, body in Events.got))

        await proc.playback("started")
        await asyncio.sleep(1.1)
        info = await ask("AVTransport", "GetPositionInfo")
        assert info["TrackURI"] == uri and info["TrackDuration"] == "0:03:25" and info["RelTime"] == "0:00:01"
        assert (await ask("AVTransport", "Seek", Unit="REL_TIME", Target="0:01:00"))["error"] == 710

        await ask("AVTransport", "Pause")
        assert await until(lambda: last(events, "active") is False)
        assert (await ask("AVTransport", "GetTransportInfo"))["CurrentTransportState"] == "PAUSED_PLAYBACK"
        assert (await ask("AVTransport", "GetCurrentTransportActions"))["Actions"] == "Play,Stop"
        assert (await ask("AVTransport", "GetPositionInfo"))["RelTime"] == "0:00:00"  # Play starts it over
        await ask("AVTransport", "Play")
        assert await until(lambda: last(events, "active") is True)

        await ask("AVTransport", "Stop")
        assert await until(lambda: last(events, "active") is False)
        assert await proc.can_release() is True
        assert (await ask("RenderingControl", "SetVolume", Channel="Master", DesiredVolume="30")) == {}
        assert (await ask("RenderingControl", "GetVolume", Channel="Master"))["CurrentVolume"] == "30"
        assert "http-get:*:video/mp4:*" in (await ask("ConnectionManager", "GetProtocolInfo"))["Sink"]

        seqs = [int(q) for s, q, _b in Events.got if s == sid]
        assert seqs == list(range(len(seqs))) and len(seqs) >= 4

    try:
        run_plugin(tmp_path, {"port": port, "name": "Test TV"}, steps)
    finally:
        callbacks.shutdown()
        callbacks.server_close()


def test_the_queue_plays_on_and_the_end_stops(tmp_path):
    port = free_port()

    async def steps(proc, events):
        ask = lambda *a, **k: asyncio.to_thread(soap, port, *a, **k)
        await ask("AVTransport", "SetAVTransportURI", CurrentURI="http://192.0.2.10/1.mp4", CurrentURIMetaData="")
        await ask("AVTransport", "Play")
        await ask("AVTransport", "SetNextAVTransportURI", NextURI="http://192.0.2.10/2.mp4", NextURIMetaData="")
        assert await until(lambda: last(events, "active") is True)
        await proc.playback("ended")  # before "started": an old item's end, ignored
        assert (await ask("AVTransport", "GetMediaInfo"))["CurrentURI"] == "http://192.0.2.10/1.mp4"
        await proc.playback("started")
        await proc.playback("ended")
        assert await until(lambda: any(e.get("event") == "next_source" for e in events))
        assert await proc.get_source() == {"kind": "uri", "uri": "http://192.0.2.10/2.mp4"}
        assert last(events, "metadata")["title"] == "2.mp4" and last(events, "active") is True
        await proc.playback("started")
        await proc.playback("ended")
        assert await until(lambda: last(events, "active") is False)
        assert (await ask("AVTransport", "GetTransportInfo"))["CurrentTransportState"] == "STOPPED"

    run_plugin(tmp_path, {"port": port}, steps)


def test_only_the_allowed_networks_may_cast(tmp_path):
    port = free_port()

    async def steps(_proc, _events):
        with pytest.raises(urllib.error.HTTPError) as exc:
            await asyncio.to_thread(urllib.request.urlopen, f"http://127.0.0.1:{port}/description.xml", timeout=5)
        assert exc.value.code == 403

    run_plugin(tmp_path, {"port": port, "networks": "10.0.0.0/8"}, steps)


def test_a_phone_finds_it(tmp_path):
    port = free_port()

    async def steps(_proc, events):
        if "not usable" in last(events, "info"):
            pytest.skip(f"UDP {upnp.SSDP_PORT} is not usable here: {last(events, 'info')}")
        search = (f"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 1\r\n"
                  f"ST: {upnp.DEVICE_TYPE}\r\n\r\n").encode()  # fmt: skip

        def ask():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(3)
                s.sendto(search, ("127.0.0.1", upnp.SSDP_PORT))
                return s.recv(2048).decode()

        reply = await asyncio.to_thread(ask)
        assert reply.startswith("HTTP/1.1 200 OK") and f"ST: {upnp.DEVICE_TYPE}" in reply
        assert f"LOCATION: http://127.0.0.1:{port}/description.xml" in reply

    run_plugin(tmp_path, {"port": port}, steps)


def test_a_picture_is_fetched_and_shown(tmp_path):
    host = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        host.connect(("192.0.2.1", 9))  # this PC's LAN address: links to 127.0.0.1 are refused
        lan = host.getsockname()[0]
    except OSError:
        pytest.skip("no LAN address")
    finally:
        host.close()
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64

    class Picture(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)

        def log_message(self, *_args):
            pass

    pictures = http.server.ThreadingHTTPServer((lan, 0), Picture)
    threading.Thread(target=pictures.serve_forever, daemon=True).start()
    port = free_port()

    async def steps(proc, events):
        uri = f"http://{lan}:{pictures.server_address[1]}/DCIM/IMG_0042.PNG"
        await asyncio.to_thread(soap, port, "AVTransport", "SetAVTransportURI", CurrentURI=uri, CurrentURIMetaData="")
        await asyncio.to_thread(soap, port, "AVTransport", "Play")
        assert await until(lambda: last(events, "active") is True)
        source = await proc.get_source()
        assert source["kind"] == "image" and source["size"] == "auto"
        assert await asyncio.to_thread(Path(source["path"]).read_bytes) == png

    try:
        run_plugin(tmp_path, {"port": port}, steps)
    finally:
        pictures.shutdown()
        pictures.server_close()
