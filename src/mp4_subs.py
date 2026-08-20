# -*- coding: utf-8 -*-
"""Minimal MP4 (ISO-BMFF) mov_text (tx3g) subtitle reader for the relay.

Companion to src.mkv_subs for the MP4 half of the VOD pipeline. The two
containers demand opposite strategies, which is why this is NOT another
sequential byte-stream parser:

* MKV interleaves subtitle blocks with A/V inside clusters as the file
  streams, so mkv_subs has no choice but to walk the stream.
* MP4 keeps its sample INDEX (moov -> trak/mdia/minf/stbl: which bytes
  are text, at which file offset, when, for how long) at the file END,
  and mdat is opaque — A/V and text packets interleave in it and only
  the sample tables can tell them apart. A moov-less streaming walk
  over mdat would be guessing at sample boundaries.

STRATEGY (deliberate choice over a mkv_subs-style pure stream walk):
VodRelay._bootstrap_tail already prefetches the file end — VLC's mp4
demuxer asks for the index there anyway — so parse_tail() gets the
whole moov from those bytes BEFORE playback starts, at zero extra
connections. Text samples are then read from the streaming cache
window at their stco file offsets as the frontier passes them
(extract()), so cues still land on the file-absolute clock ahead of
playback: the same contract as the MKV tap, including re-parse dedupe
downstream (CueStore keys on (start, text)).

moov-at-head ("faststart") files leave no moov in the tail; scan_head()
walks the cache's top-level boxes instead and parses it when it lands.
If neither path finds a moov (tail prefetch failed on a moov-at-end
file, say) there are simply no cues — playback through the relay is
never affected.

Timestamps: sample start = cumulative stts deltas, duration = the
sample's own stts delta (ffmpeg's mov_text muxer pads gaps with EMPTY
tx3g samples, so deltas are true display durations and the empty
samples are skipped here). Times are trak media times in mdhd's
timescale; with the media_time=0 edit lists every writer emits, media
time == presentation time == VLC's playback clock.

tx3g sample layout: 2-byte big-endian text length, that many UTF-8
bytes, then optional styling atoms ('styl', 'hlit', 'dlay', ...) — the
length prefix already excludes those from the text we slice out.
"""

from .mkv_subs import is_text_codec, lang_matches

_FALLBACK_CUE_S = 3.0       # degenerate stts delta (0) — assume this
_MAX_SAMPLES = 1 << 21      # corrupt-table guard: real text tracks
#                           # are thousands of samples, not millions


def _u16(b, p):
    return int.from_bytes(b[p:p + 2], "big") if len(b) >= p + 2 else None


def _u32(b, p):
    return int.from_bytes(b[p:p + 4], "big") if len(b) >= p + 4 else None


def _boxes(buf, start, end):
    """Yield (fourcc, body_start, body_end) for the boxes in
    buf[start:end]; stops at the first malformed/oversized box."""
    pos = start
    while pos + 8 <= end:
        size = _u32(buf, pos)
        typ = buf[pos + 4:pos + 8]
        hdr = 8
        if size == 1:                       # 64-bit largesize follows
            if pos + 16 > end:
                return
            size = int.from_bytes(buf[pos + 8:pos + 16], "big")
            hdr = 16
        elif size == 0:                     # box runs to the slice end
            size = end - pos
        if size is None or size < hdr or pos + size > end:
            return
        yield typ, pos + hdr, pos + size
        pos += size


def decode_tx3g(raw: bytes) -> str:
    """One tx3g / QuickTime-text sample -> plain text. The 2-byte length
    prefix bounds the text, so trailing styling atoms simply never enter
    the slice; corrupt lengths are clamped, never trusted."""
    if len(raw) < 2:
        return ""
    n = min(_u16(raw, 0), len(raw) - 2)
    text = raw[2:2 + n].decode("utf-8", "replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    # 3GPP text carries CR line breaks; the overlay wants \n
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _unpack_lang(packed):
    """mdhd's 3x5-bit packed ISO-639-2/T language ('und' when zero)."""
    if not packed:
        return "und"
    return "".join(chr(0x60 + ((packed >> s) & 0x1F)) for s in (10, 5, 0))


class _Trak:
    """One text-handler trak: metadata plus its parsed sample table."""

    def __init__(self):
        self.meta = {"codec": "", "lang": "und", "name": ""}
        self.timescale = 1
        self.sizes = []       # stsz, per sample
        self.deltas = []      # stts, per sample (media timescale ticks)
        self.chunk_offs = []  # stco/co64, file-absolute
        self.chunk_runs = []  # stsc, [(first_chunk, last_chunk, per)]


class Mp4SubParser:
    """Give it the moov index (parse_tail / scan_head), then pull cues
    out of the relay cache (extract); cues land in ``self.cues`` (also
    returned by extract)."""

    def __init__(self, prefer_language: str = "eng"):
        self.prefer_language = (prefer_language or "").lower()
        self.cues = []
        self._track_meta = {}   # track id -> {codec, lang, name} (the
        #                          # UI reads this via relay.parser_tracks)
        self._traks = {}        # track id -> _Trak (text-handler traks)
        self._selected = None
        self._samples = []      # (file_off, size, start_s, end_s)
        self._cursor = 0        # next sample to extract, file order
        self._saw_tracks = False    # a moov was parsed (index in hand)
        self._moov = None       # parsed moov body, kept for reselect()
        self._head_pos = 0      # scan_head's top-level box cursor

    # ---- public ----
    @property
    def have_index(self) -> bool:
        """True once a moov has been parsed (even if it held no text)."""
        return self._saw_tracks

    def parse_tail(self, tail: bytes) -> bool:
        """Locate moov inside the relay's prefetched file-tail slice and
        parse it. The slice starts at an arbitrary byte, so candidates
        are 'moov' fourccs whose preceding size chains cleanly to the
        slice end — and whose first child box is mvhd (moov's mandatory
        opener), which rejects 'moov' bytes occurring inside mdat
        payload. The LAST valid candidate wins: moov sits at the end of
        the file whenever it is in the tail at all."""
        if self._saw_tracks:
            return True
        end = len(tail)
        idx = end
        while True:
            idx = tail.rfind(b"moov", 0, idx)
            if idx < 4:
                return False
            start = idx - 4
            size = _u32(tail, start)
            if (size is None or size < 8 or start + size > end
                    or tail[idx + 8:idx + 12] != b"mvhd"):
                continue
            if not self._chains(tail, start + size, end):
                continue
            self._parse_moov(tail[start + 8:start + size])
            return self._saw_tracks

    def scan_head(self, read, cache_base: int, cache_size: int) -> bool:
        """No moov in the tail (faststart files keep it at the head; the
        file:// test provider prefetches no tail at all): walk the
        cache's top-level boxes as the window streams and parse moov
        when the whole box has landed. Only meaningful while the window
        still starts at file offset 0. ``read`` is the relay's
        random-access cache reader (file-absolute offsets)."""
        if self._saw_tracks or cache_base != 0:
            return self._saw_tracks
        while self._head_pos + 8 <= cache_size:
            hdr = read(self._head_pos, 8)
            if len(hdr) < 8:
                break                       # frontier regressed; retry later
            size = int.from_bytes(hdr[:4], "big")
            typ = hdr[4:8]
            hdr_len = 8
            if size == 1:                   # 64-bit largesize follows
                ext = read(self._head_pos + 8, 8)
                if len(ext) < 8:
                    break
                size = int.from_bytes(ext, "big")
                hdr_len = 16
            elif size == 0:
                return False                # runs to EOF: unbounded, stop
            if size < hdr_len:
                return False                # garbage: stop hunting
            if typ == b"moov":
                if self._head_pos + size > cache_size:
                    break                   # wait for the rest of the box
                body = read(self._head_pos + hdr_len, size - hdr_len)
                if len(body) == size - hdr_len:
                    self._parse_moov(body)
                    return True
                break
            self._head_pos += size          # skip ftyp/free/mdat & friends
        return self._saw_tracks

    def parse_head_bytes(self, buf: bytes) -> bool:
        """Faststart moov out of the relay's prefetched head bytes — the
        fixed-buffer twin of scan_head, for sessions whose cache window
        starts past 0 (resume / mid-movie subtitle engage) where the
        streaming walk never sees offset 0."""
        if self._saw_tracks or not buf:
            return self._saw_tracks
        pos = 0
        while pos + 8 <= len(buf):
            size = _u32(buf, pos)
            typ = buf[pos + 4:pos + 8]
            hdr_len = 8
            if size == 1:                   # 64-bit largesize follows
                if pos + 16 > len(buf):
                    return False
                size = int.from_bytes(buf[pos + 8:pos + 16], "big")
                hdr_len = 16
            elif size == 0:
                return False                # runs to EOF: unbounded
            if size is None or size < hdr_len:
                return False                # garbage: stop hunting
            if typ == b"moov":
                if pos + size > len(buf):
                    return False            # moov bigger than the prefetch
                self._parse_moov(buf[pos + hdr_len:pos + size])
                return True
            pos += size                     # skip ftyp/free/mdat & friends
        return self._saw_tracks

    def extract(self, read, cache_base: int, cache_size: int,
                tail_base: int = -1, tail_len: int = 0) -> list:
        """Pull every text sample that is fully readable NOW: inside the
        cache window [cache_base, cache_base+cache_size), inside the
        prefetched tail [tail_base, tail_base+tail_len), or straddling
        the two when they are contiguous (``read`` must serve both — the
        relay's _tap_read does). Returns the cues completed by this call
        (also appended to self.cues)."""
        out = []
        if self._selected is None:
            return out
        cache_end = cache_base + cache_size
        tail_end = tail_base + tail_len if tail_base >= 0 else -1
        samples = self._samples
        while self._cursor < len(samples):
            off, size, start_s, end_s = samples[self._cursor]
            if off < cache_base:
                self._cursor += 1           # left behind by a rebase
                continue
            in_cache = off + size <= cache_end
            in_tail = tail_base >= 0 and off >= tail_base \
                and off + size <= tail_end
            straddle = tail_base >= 0 and cache_end >= tail_base \
                and off + size <= tail_end
            if not (in_cache or in_tail or straddle):
                break                       # frontier hasn't reached it
            raw = read(off, size)
            if len(raw) < size:
                break                       # not really there; retry later
            self._cursor += 1
            text = decode_tx3g(raw)
            if text:                        # gap-padding samples are empty
                out.append(self._finish(start_s, end_s, text))
        return out

    def rewind(self, cache_base: int):
        """Seek rebase: extraction cursor back to the first sample at or
        after the new window base (tables and selection are kept — the
        index is file-absolute and never expires). Samples re-read after
        a backwards rebase re-emit their cues; downstream CueStore
        dedupes on (start, text)."""
        i = 0
        samples = self._samples
        while i < len(samples) and samples[i][0] < cache_base:
            i += 1
        self._cursor = i

    def reselect(self, prefer: str):
        """CC-menu language change: re-run track selection against the
        kept moov bytes with the new preference, then rewind the cursor
        (re-emitted cues dedupe downstream)."""
        prefer = (prefer or "").lower()
        if not prefer or prefer == self.prefer_language:
            return
        self.prefer_language = prefer
        self._selected = None
        self._samples = []
        self._cursor = 0
        if self._moov is not None:
            self._parse_moov(self._moov)

    # ---- internals ----
    @staticmethod
    def _chains(buf, start, end):
        """buf[start:end] parses as one exact run of boxes?"""
        p = start
        while p + 8 <= end:
            size = _u32(buf, p)
            if size is None or size < 8 or p + size > end:
                return False
            p += size
        return p == end

    def _finish(self, start, end, text):
        cue = (max(0.0, start), max(start, end), text)
        self.cues.append(cue)
        return cue

    def _parse_moov(self, body):
        """moov body -> track metadata + sample tables. The body is kept
        whole so reselect() can re-parse it with a new preference."""
        self._moov = body
        self._traks = {}
        self._track_meta = {}
        for typ, s, e in _boxes(body, 0, len(body)):
            if typ == b"trak":
                self._parse_trak(body[s:e])
        self._saw_tracks = True
        self._select_track()

    def _parse_trak(self, body):
        tid = None
        mdia = None
        for typ, s, e in _boxes(body, 0, len(body)):
            if typ == b"tkhd" and tid is None:
                tid = _u32(body, s + (12 if body[s] == 0 else 20))
            elif typ == b"mdia" and mdia is None:
                mdia = body[s:e]
        if tid is None or mdia is None:
            return
        trak = _Trak()
        handler = b""
        for typ, s, e in _boxes(mdia, 0, len(mdia)):
            if typ == b"mdhd":
                off = 12 if mdia[s] == 0 else 20
                ts = _u32(mdia, s + off)
                if ts:
                    trak.timescale = ts
                packed = _u16(mdia, s + off + 8)
                if packed is not None:
                    trak.meta["lang"] = _unpack_lang(packed)
            elif typ == b"hdlr":
                handler = mdia[s + 8:s + 12]
                trak.meta["name"] = mdia[s + 24:e].split(b"\x00")[0] \
                    .decode("utf-8", "replace")
            elif typ == b"minf":
                for t2, s2, e2 in _boxes(mdia, s, e):
                    if t2 == b"stbl":
                        self._parse_stbl(trak, mdia[s2:e2])
        # sbtl/text are the text handlers; 'subt' tags along (same tables).
        # Non-text traks (vide/soun) never reach _track_meta. A text-handler
        # trak whose codec is NOT text (wvtt-in-sbtl say) still registers,
        # so the UI's bitmap-style check can hand rendering back to VLC.
        if handler not in (b"sbtl", b"text", b"subt"):
            return
        if not trak.meta["codec"]:
            trak.meta["codec"] = handler.decode("ascii", "replace").upper()
        self._traks[tid] = trak
        self._track_meta[tid] = trak.meta

    def _parse_stbl(self, trak, body):
        """stbl body -> the four sample tables, eagerly materialized
        (subtitle tables are tiny; a video trak never gets here)."""
        stsc_ents = []
        for typ, s, e in _boxes(body, 0, len(body)):
            if typ == b"stsd" and not trak.meta["codec"]:
                # first entry's format fourcc: ver/flags(4) + count(4)
                # + size(4) + fourcc
                trak.meta["codec"] = body[s + 12:s + 16] \
                    .decode("ascii", "replace").upper()
            elif typ == b"stts":
                n = min(_u32(body, s + 4) or 0, (e - s - 8) // 8)
                for i in range(n):
                    cnt = _u32(body, s + 8 + 8 * i) or 0
                    delta = _u32(body, s + 12 + 8 * i) or 0
                    if len(trak.deltas) >= _MAX_SAMPLES:
                        break
                    trak.deltas.extend(
                        [delta] * min(cnt, _MAX_SAMPLES - len(trak.deltas)))
            elif typ == b"stsz":
                fixed = _u32(body, s + 4) or 0
                n = _u32(body, s + 8) or 0
                if fixed:
                    trak.sizes = [fixed] * min(n, _MAX_SAMPLES)
                else:
                    n = min(n, (e - s - 12) // 4, _MAX_SAMPLES)
                    trak.sizes = [_u32(body, s + 12 + 4 * i) or 0
                                  for i in range(n)]
            elif typ == b"stsc":
                n = min(_u32(body, s + 4) or 0, (e - s - 8) // 12)
                for i in range(n):
                    first = _u32(body, s + 8 + 12 * i) or 0
                    per = _u32(body, s + 12 + 12 * i) or 0
                    if first >= 1:
                        stsc_ents.append((first, per))
            elif typ in (b"stco", b"co64"):
                wide = 8 if typ == b"co64" else 4
                n = min(_u32(body, s + 4) or 0, (e - s - 8) // wide)
                trak.chunk_offs = [
                    int.from_bytes(body[s + 8 + wide * i:s + 8 + wide * (i + 1)],
                                   "big") for i in range(n)]
        # stsc entry i covers chunks first..(next first)-1, the last entry
        # runs to however many chunks stco listed (stco typically follows
        # stsc in the box order, so runs are built only now)
        nchunks = len(trak.chunk_offs)
        for i, (first, per) in enumerate(stsc_ents):
            last = stsc_ents[i + 1][0] - 1 if i + 1 < len(stsc_ents) \
                else nchunks
            if first <= min(last, nchunks):
                trak.chunk_runs.append(
                    (first, min(last, nchunks), max(1, per)))

    def _select_track(self):
        """Pick ONE text track: preferred language/name match, else the
        lowest track id — the same policy as MkvSubParser."""
        if self._selected is not None or not self._track_meta:
            return
        text = {tid: m for tid, m in self._track_meta.items()
                if is_text_codec(m["codec"])}
        if not text:
            # only non-text codecs (or nothing parsed): emit nothing —
            # the UI leaves those files to VLC's own renderer
            return
        if self.prefer_language:
            for tid in sorted(text):
                m = text[tid]
                if lang_matches(self.prefer_language, m["lang"],
                                m["name"]):
                    self._selected = tid
                    break
        if self._selected is None:
            self._selected = min(text)
        self._samples = self._expand(self._traks[self._selected])
        self._cursor = 0

    @staticmethod
    def _expand(trak):
        """Sample tables -> [(file_off, size, start_s, end_s)] in file
        order: walk stsc's chunk runs, carving per-sample offsets out of
        each chunk, and accumulate stts deltas for starts/durations."""
        samples = []
        si = 0
        t = 0
        nsizes = len(trak.sizes)
        for first, last, per in trak.chunk_runs:
            for chunk in range(first, last + 1):
                off = trak.chunk_offs[chunk - 1]
                for _ in range(per):
                    if si >= nsizes:
                        break
                    dur = trak.deltas[si] if si < len(trak.deltas) else 0
                    start_s = t / trak.timescale
                    t += dur
                    end_s = start_s + (dur / trak.timescale
                                       if dur > 0 else _FALLBACK_CUE_S)
                    samples.append((off, trak.sizes[si], start_s, end_s))
                    off += trak.sizes[si]
                    si += 1
            if si >= nsizes:
                break
        samples.sort(key=lambda s: s[0])    # extraction follows file order
        return samples
