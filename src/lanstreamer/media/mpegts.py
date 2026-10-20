"""Reads the stream table (PAT/PMT) from MPEG-TS packets, for diagnostics.

MediaMTX only accepts the tracks listed in the first PMT it receives, so the
output worker reports what it actually sends.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PACKET = 188

STREAM_TYPES = {
    0x02: "MPEG-2 video",
    0x03: "MP3",
    0x04: "MP3",
    0x06: "private (Opus/other)",
    0x0F: "AAC",
    0x11: "AAC-LATM",
    0x15: "metadata",
    0x1B: "H.264",
    0x24: "H.265",
    0x81: "AC-3",
}


@dataclass
class Pmt:
    version: int
    pcr_pid: int
    streams: list[tuple[int, int]] = field(default_factory=list)  # (pid, stream_type)

    def describe(self) -> str:
        tracks = ", ".join(
            f"PID {pid} {STREAM_TYPES.get(kind, 'unknown')} (0x{kind:02x})"
            for pid, kind in self.streams
        )
        return f"PMT v{self.version}: {tracks or 'no tracks'} · PCR PID {self.pcr_pid}"


class TableReader:
    """Feed it TS data; it returns a Pmt whenever a new PMT version appears."""

    def __init__(self) -> None:
        self.pmt_pid: int | None = None
        self.last: Pmt | None = None

    def feed(self, data: bytes) -> list[Pmt]:
        found = []
        for offset in range(0, len(data) - PACKET + 1, PACKET):
            packet = data[offset : offset + PACKET]
            if packet[0] != 0x47 or not packet[1] & 0x40:  # sync byte, payload start
                continue
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            if pid != 0 and pid != self.pmt_pid:
                continue
            section = _section(packet)
            if section is None:
                continue
            if pid == 0 and section[0] == 0x00:
                self.pmt_pid = _pat_pmt_pid(section)
            elif section[0] == 0x02:
                pmt = _parse_pmt(section)
                if pmt and (self.last is None or pmt != self.last):
                    self.last = pmt
                    found.append(pmt)
        return found


def _section(packet: bytes) -> bytes | None:
    start = 4
    adaptation = (packet[3] >> 4) & 0x3
    if adaptation in (2, 3):
        start += 1 + packet[4]
    if adaptation == 2 or start >= PACKET:
        return None
    start += 1 + packet[start]  # pointer field
    if start + 3 > PACKET:
        return None
    length = ((packet[start + 1] & 0x0F) << 8) | packet[start + 2]
    return packet[start : start + 3 + length]


def _pat_pmt_pid(section: bytes) -> int | None:
    # 8 bytes header, entries of 4 bytes, 4 bytes CRC
    for pos in range(8, len(section) - 4, 4):
        program = (section[pos] << 8) | section[pos + 1]
        if program != 0:
            return ((section[pos + 2] & 0x1F) << 8) | section[pos + 3]
    return None


def _parse_pmt(section: bytes) -> Pmt | None:
    if len(section) < 16:
        return None
    version = (section[5] >> 1) & 0x1F
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    info_len = ((section[10] & 0x0F) << 8) | section[11]
    pos, end = 12 + info_len, len(section) - 4
    pmt = Pmt(version, pcr_pid)
    while pos + 5 <= end:
        kind = section[pos]
        pid = ((section[pos + 1] & 0x1F) << 8) | section[pos + 2]
        es_len = ((section[pos + 3] & 0x0F) << 8) | section[pos + 4]
        pmt.streams.append((pid, kind))
        pos += 5 + es_len
    return pmt
