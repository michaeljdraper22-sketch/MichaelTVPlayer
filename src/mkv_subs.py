# -*- coding: utf-8 -*-
"""Minimal streaming Matroska (MKV) subtitle-track reader.

Parses the byte stream as it arrives (no seeking, no whole-file buffer)
and yields (start_s, end_s, text) cues for ONE selected subtitle track —
S_TEXT/UTF8 (subrip). Video/audio payloads are counted past, never
buffered. This exists because ffmpeg's text muxers only flush subtitle
output at EOF, which is useless for live filtering.

Timestamps: MKV cluster/block timecodes are in milliseconds (the
TimecodeScale default); cue end = the next cue's start on the same track.
"""

# element IDs we care about
_EBML = 0x1A45DFA3
_SEGMENT = 0x18538067
_SEEKHEAD = 0x114D9B74
_INFO = 0x1549A966
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_CLUSTER = 0x1F43B675
_TIMECODE = 0xE7
_SIMPLE_BLOCK = 0xA3
_BLOCK_GROUP = 0xA0
_BLOCK = 0xA1
_TRACK_NUMBER = 0xD7
_TRACK_TYPE = 0x83
_CODEC_ID = 0x86
_LANGUAGE = 0x22B59C
_LANGUAGE_IETF = 0x22B59D
_NAME = 0x536E
_VOID = 0xEC

_CTX_ROOT, _CTX_SEG, _CTX_CLUSTER, _CTX_TRACKS, _CTX_TRACK, _CTX_BLOCKGROUP = range(6)

_UNKNOWN = -1


def _vint_size(buf, pos, keep_marker):
    """Parse an EBML vint at pos -> (value, length) or (None, 0)."""
    if pos >= len(buf):
        return None, 0
    first = buf[pos]
    if first == 0:
        return None, 0            # invalid (more than 8 length)
    length = 1
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
    if pos + length > len(buf):
        return None, 0
    value = first if keep_marker else (first & (mask - 1))
    for i in range(1, length):
        value = (value << 8) | buf[pos + i]
    return value, length


class MkvSubParser:
    """Feed bytes; collect cues in ``self.cues`` (also returned by feed)."""

    def __init__(self, prefer_language: str = "eng",
                 mid_stream: bool = False):
        self.prefer_language = (prefer_language or "").lower()
        self.cues = []
        self.buf = bytearray()
        self._mid_stream = mid_stream
        self._skip = 0             # bytes to swallow without buffering
        self._stack = [_CTX_ROOT]
        self._track_meta = {}      # number -> {codec, lang, name}
        self._selected = None
        self._cluster_tc = 0       # ms
        self._pending = None       # last cue on the selected track
        self._saw_tracks = False
        if mid_stream:
            # joining mid-file: wait for the next Cluster header (cluster
            # Timecodes are absolute, so cue times stay on file time)
            self._stack = [_CTX_ROOT, _CTX_SEG, _CTX_CLUSTER]

    # ---- public ----
    def feed(self, data: bytes) -> list:
        """Consume bytes; returns cues completed by this call."""
        if not data:
            return []
        if self._skip:
            take = min(self._skip, len(data))
            self._skip -= take
            data = data[take:]
            if not data:
                return []
        self.buf += data
        return self._parse()

    def flush(self) -> list:
        """End of stream: close the dangling cue."""
        out = []
        if self._pending is not None:
            s, _, text = self._pending
            out.append(self._finish(s, s + 4.0, text))
        return out

    # ---- internals ----
    def _finish(self, start, end, text):
        self._pending = None
        cue = (max(0.0, start), max(start, end), text)
        self.cues.append(cue)
        return cue

    def _parse(self):
        """Cursor-based walk: exactly ONE place consumes buffer bytes, so
        no branch can mis-delete on chunk-boundary re-entry (the previous
        del-per-branch design dropped bytes that way and misaligned)."""
        out = []
        while True:
            eid, ilen = _vint_size(self.buf, 0, keep_marker=True)
            if eid is None:
                if self.buf and self.buf[0] == 0                         and self._stack[-1] == _CTX_CLUSTER:
                    self._resync()
                    continue
                break
            size, slen = _vint_size(self.buf, ilen, keep_marker=False)
            if size is None:
                break
            header = ilen + slen
            avail = len(self.buf) - header
            all_ones = slen > 0 and size == (1 << (7 * slen)) - 1
            ctx = self._stack[-1]
            absurd = size > (1 << 30)

            # ---- context transitions (header-only consumption) ----
            if ctx == _CTX_ROOT and eid == _SEGMENT:
                self._stack.append(_CTX_SEG)
                del self.buf[:header]
                continue
            if eid == _CLUSTER and ctx in (_CTX_SEG, _CTX_CLUSTER):
                # descend (or sibling cluster): reset the clock
                if ctx == _CTX_SEG:
                    self._stack.append(_CTX_CLUSTER)
                self._cluster_tc = 0
                del self.buf[:header]
                continue
            if absurd or (all_ones and eid not in (_SEGMENT, _CLUSTER)):
                self._resync()
                continue

            # ---- payload elements ----
            if ctx == _CTX_SEG and eid == _TRACKS:
                if avail < size:
                    break                       # wait, fully buffered
                self._parse_children(
                    _CTX_TRACKS, bytes(self.buf[header:header + size]))
                self._saw_tracks = True
                self._select_track()
                del self.buf[:header + size]
                continue
            if ctx == _CTX_CLUSTER and eid == _TIMECODE and size <= 8:
                if avail < size:
                    break
                self._cluster_tc = int.from_bytes(
                    bytes(self.buf[header:header + size]), "big")
                del self.buf[:header + size]
                continue
            if ctx == _CTX_CLUSTER and eid in (_SIMPLE_BLOCK, _BLOCK):
                made = self._parse_block(header, size)
                if made is None:
                    break                       # incomplete: wait
                out += made
                continue
            if ctx == _CTX_CLUSTER and eid == _BLOCK_GROUP:
                if avail < size:
                    break
                blob = bytes(self.buf[header:header + size])
                del self.buf[:header + size]
                out += self._parse_block_group(blob)
                continue

            # ---- generic skip ----
            if avail < size:
                if size > (1 << 20):
                    # stream-skip without buffering the payload
                    self._skip = size - max(0, avail)
                    del self.buf[:]
                break
            del self.buf[:header + size]
        return out

    def _resync(self):
        """Self-heal after garbage: jump to the next Cluster header."""
        i = self.buf.find(bytes([0x1F, 0x43, 0xB6, 0x75]), 1)
        if i < 0:
            del self.buf[:max(0, len(self.buf) - 4)]
        else:
            del self.buf[:i]
        if self._stack[-1] != _CTX_CLUSTER:
            self._stack.append(_CTX_CLUSTER)

    def _parse_children(self, ctx, blob):
        """Walk a fully-buffered container (Tracks) for track metadata."""
        pos = 0
        while pos < len(blob):
            eid, ilen = _vint_size(blob, pos, keep_marker=True)
            if eid is None:
                break
            size, slen = _vint_size(blob, pos + ilen, keep_marker=False)
            if size is None:
                break
            header = ilen + slen
            body = blob[pos + header:pos + header + size]
            if ctx == _CTX_TRACKS and eid == _TRACK_ENTRY:
                self._parse_track_entry(body)
            pos += header + size

    def _parse_track_entry(self, body):
        meta = {"codec": "", "lang": "eng", "name": ""}
        number = None
        pos = 0
        while pos < len(body):
            eid, ilen = _vint_size(body, pos, keep_marker=True)
            if eid is None:
                break
            size, slen = _vint_size(body, pos + ilen, keep_marker=False)
            if size is None:
                break
            header = ilen + slen
            chunk = body[pos + header:pos + header + size]
            if eid == _TRACK_NUMBER:
                number = int.from_bytes(chunk, "big")
            elif eid == _TRACK_TYPE:
                meta["type"] = int.from_bytes(chunk, "big")
            elif eid == _CODEC_ID:
                meta["codec"] = chunk.decode("ascii", "replace")
            elif eid in (_LANGUAGE, _LANGUAGE_IETF):
                meta["lang"] = chunk.decode("ascii", "replace").lower()
            elif eid == _NAME:
                meta["name"] = chunk.decode("utf-8", "replace")
            pos += header + size
        if number is not None and meta.get("type") == 0x11:
            self._track_meta[number] = meta

    def _select_track(self):
        if self._selected is not None or not self._track_meta:
            return
        text_tracks = {n: m for n, m in self._track_meta.items()
                       if m["codec"].startswith("S_TEXT/UTF8")
                       or m["codec"] in ("S_TEXT/UTF8", "S_UTF8",
                                         "S_TEXT/WEBVTT")}
        if not text_tracks:
            # only ASS/PGS tracks (or none parsed): emit nothing — parsing
            # their payloads as text would be garbage. The UI falls back
            # to VLC's own rendering for those codecs.
            return
        if self.prefer_language:
            for n, m in text_tracks.items():
                if self.prefer_language in f"{m['lang']} {m['name']}" \
                        .lower():
                    self._selected = n
                    return
        self._selected = min(text_tracks)

    def _parse_block(self, header, size):
        """Block at buf[0] (SimpleBlock or Block). Returns a cue list
        (possibly empty) when CONSUMED, or None while incomplete. The
        ONLY consumer of buffer bytes for blocks."""
        buf = self.buf
        track, tlen = _vint_size(buf, header, keep_marker=False)
        if track is None:
            return None
        if size < tlen + 3:
            del buf[:header + size]
            return []
        if len(buf) < header + tlen + 3:
            return None
        flags = buf[header + tlen + 2]
        rel_tc = int.from_bytes(
            bytes(buf[header + tlen:header + tlen + 2]), "big",
            signed=True)
        if len(buf) < header + size:
            return None                     # wait for the whole payload
        payload_start = header + tlen + 3
        payload_len = size - (tlen + 3)
        out = []
        if not (flags & 0x06) and track == self._selected                 and payload_len > 0:
            text = bytes(buf[payload_start:payload_start + payload_len])                 .decode("utf-8", "replace").strip()
            if text:
                start_s = (self._cluster_tc + rel_tc) / 1000.0
                out.append(self._finish(start_s, start_s + 3.0, text))
        del buf[:header + size]
        return out

    def _parse_block_group(self, blob):
        """BlockGroup body (children only). Returns cue list."""
        pos = 0
        while pos < len(blob):
            eid, ilen = _vint_size(blob, pos, keep_marker=True)
            if eid is None:
                break
            size, slen = _vint_size(blob, pos + ilen, keep_marker=False)
            if size is None:
                break
            header = ilen + slen
            if eid == _BLOCK:
                scratch = bytearray(blob[pos:pos + header + size])
                save = self.buf
                self.buf = scratch
                made = self._parse_block(header, size)
                self.buf = save
                return made or []
            pos += header + size
        return []
