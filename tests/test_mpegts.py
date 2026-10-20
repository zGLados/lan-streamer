from lanstreamer.media.mpegts import TableReader


def packet(pid: int, section: bytes) -> bytes:
    header = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10, 0x00])  # payload start, pointer 0
    return (header + section).ljust(188, b"\xff")


def pat(pmt_pid: int) -> bytes:
    body = bytes([0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01, 0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF])
    return bytes([0x00, 0xB0, len(body) + 4]) + body + b"\x00\x00\x00\x00"


def pmt(version: int, streams: list[tuple[int, int]], pcr: int = 65) -> bytes:
    es = b"".join(bytes([kind, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0, 0x00]) for pid, kind in streams)
    body = bytes([0x00, 0x01, 0xC1 | (version << 1), 0x00, 0x00, 0xE0 | (pcr >> 8), pcr & 0xFF, 0xF0, 0x00]) + es
    return bytes([0x02, 0xB0, len(body) + 4]) + body + b"\x00\x00\x00\x00"


def test_reads_tracks_from_pat_and_pmt():
    reader = TableReader()
    found = reader.feed(packet(0, pat(4096)) + packet(4096, pmt(0, [(65, 0x1B), (66, 0x06)])))
    assert len(found) == 1
    assert found[0].streams == [(65, 0x1B), (66, 0x06)]
    assert found[0].describe() == (
        "PMT v0: PID 65 H.264 (0x1b), PID 66 private (Opus/other) (0x06) · PCR PID 65"
    )


def test_reports_only_changes():
    reader = TableReader()
    data = packet(0, pat(4096)) + packet(4096, pmt(0, [(66, 0x06)]))
    assert len(reader.feed(data)) == 1
    assert reader.feed(data) == []
    changed = reader.feed(packet(4096, pmt(1, [(65, 0x1B), (66, 0x06)])))
    assert changed[0].version == 1 and len(changed[0].streams) == 2


def test_ignores_other_pids_and_garbage():
    reader = TableReader()
    assert reader.feed(packet(65, b"\x00" * 20) + b"\x00" * 188) == []
