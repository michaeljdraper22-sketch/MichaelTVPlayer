# -*- coding: utf-8 -*-
"""Minimal streaming Matroska (MKV) subtitle-track reader.

Parses the byte stream as it arrives (no seeking, no whole-file buffer)
and yields (start_s, end_s, text) cues for ONE selected subtitle track —
any text codec: S_TEXT/UTF8 (subrip), S_TEXT/ASS / S_TEXT/SSA (flattened
to plain text — the app overlay draws ONE style for every source),
WEBVTT. Video/audio payloads are counted past, never buffered. This
exists because ffmpeg's text muxers only flush subtitle output at EOF,
which is useless for live filtering.

Timestamps: MKV cluster/block timecodes are in milliseconds (the
TimecodeScale default). Cue end: the BlockGroup's BlockDuration when the
writer supplied one (subrip/ASS writers do), else start + 3 s —
SimpleBlock carries no duration field at all.
"""

import re

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
_BLOCK_DURATION = 0x9B
_TRACK_NUMBER = 0xD7
_TRACK_TYPE = 0x83
_CODEC_ID = 0x86
_LANGUAGE = 0x22B59C
_LANGUAGE_IETF = 0x22B59D
_NAME = 0x536E
_VOID = 0xEC

_CTX_ROOT, _CTX_SEG, _CTX_CLUSTER, _CTX_TRACKS, _CTX_TRACK, _CTX_BLOCKGROUP = range(6)

_UNKNOWN = -1

_FALLBACK_CUE_S = 3.0      # SimpleBlocks carry no duration — assume this

# ---- text flattening (subrip markup + ASS/SSA payloads -> plain text) ----
_DRAW_RE = re.compile(r"\{\\p[0-9]+\}.*?(?:\{\\p0\}|$)", re.S)
_TAG_RE = re.compile(r"<[^>]+>")            # <i>, <b>, <font ...>
_OVERRIDE_RE = re.compile(r"\{[^}]*\}")     # {\an8}, {\k20}, {\pos(...)}
# ffmpeg's matroska muxer stores the Dialogue line's fixed fields
# (Layer/Style/Name/Margins/Effect) with the "Dialogue:" prefix stripped;
# mkvmerge stores the text alone and never matches this rigid pattern
_FIELD_PREFIX_RE = re.compile(
    r"^\d+,[^,]*,[^,]*,[^,]*,\d+,\d+,\d+,[^,]*,")


# ---- language matching (CC-menu words vs MKV Language elements) ----
# VLC names a track "English (United States) - [English]" while the MKV
# TrackEntry carries the ISO 639 code "eng" (or "en", or occasionally the
# full word). Matching by raw substring fails across those spellings and
# the selection used to fall through to the FIRST text track — Arabic on
# this provider's multi-language rips — so captions "never appeared" for
# English picks. Every spelling maps to one canonical token instead.
_LANG_EQUIV = [
    ("eng", "en", "english"), ("ara", "ar", "arabic"),
    ("ger", "de", "deu", "german", "deutsch"),
    ("fre", "fra", "fr", "french", "francais"),
    ("spa", "es", "spanish", "espanol"),
    ("por", "pt", "portuguese", "portugues"),
    ("dut", "nld", "nl", "dutch", "nederlands"),
    ("ita", "it", "italian"), ("rus", "ru", "russian"),
    ("tur", "tr", "turkish"), ("hin", "hi", "hindi"),
    ("jpn", "ja", "japanese"), ("kor", "ko", "korean"),
    ("chi", "zho", "zh", "chinese"), ("tha", "th", "thai"),
    ("vie", "vi", "vietnamese"), ("pol", "pl", "polish"),
    ("cze", "ces", "cs", "czech"), ("dan", "da", "danish"),
    ("fin", "fi", "finnish"), ("nor", "no", "norwegian"),
    ("swe", "sv", "swedish"), ("heb", "he", "hebrew"),
    ("ind", "id", "indonesian"), ("may", "msa", "ms", "malay"),
    ("rum", "ron", "ro", "romanian"), ("hun", "hu", "hungarian"),
    ("gre", "ell", "el", "greek"), ("ukr", "uk", "ukrainian"),
    ("ben", "bn", "bengali"), ("tam", "ta", "tamil"),
    ("tel", "te", "telugu"), ("mar", "mr", "marathi"),
    ("pan", "pa", "punjabi"), ("urd", "ur", "urdu"),
    ("fas", "fa", "persian"), ("cat", "ca", "catalan"),
    ("baq", "eus", "eu", "basque"), ("gal", "gl", "galician"),
    ("hrv", "hr", "croatian"), ("srp", "sr", "serbian"),
    ("slv", "sl", "slovenian"), ("bul", "bg", "bulgarian"),
    ("slo", "slk", "sk", "slovak"),
]
_LANG_ALIAS = {tok: group[0] for group in _LANG_EQUIV for tok in group}
_LANG_CANON = set(_LANG_ALIAS.values())


def lang_token(word: str) -> str:
    """Canonical spelling of a language word ('english'/'en' -> 'eng')."""
    w = (word or "").strip().lower()
    return _LANG_ALIAS.get(w, w)


def is_language_name(word: str) -> bool:
    """True when ``word`` names a KNOWN language (guards the UI's
    wanted-language check from junk hints like 'track' or 'closed')."""
    return lang_token(word) in _LANG_CANON


def lang_matches(hint: str, lang: str, name: str = "") -> bool:
    """Does the CC-menu hint (a language word) refer to this track?
    Full words substring-match the track's lang+name; every spelling
    (full word, ISO 639-2, two-letter) equal-matches through the alias
    table, against the language element AND the track name (rippers
    often leave one of them empty). Short ISO codes never
    substring-match ('en' inside 'french' would be nonsense)."""
    hint = (hint or "").strip().lower()
    if not hint:
        return False
    words = f"{lang or ''} {name or ''}".lower().replace(",", " ").split()
    if len(hint) >= 4 and hint in " ".join(words):
        return True
    h = _LANG_ALIAS.get(hint)
    if h is None:
        return False
    return any(_LANG_ALIAS.get(tok) == h for tok in words)


def is_text_codec(codec: str) -> bool:
    """True when a subtitle-track codec carries flatten-able text. Covers
    BOTH parsers: MKV CodecIDs (subrip/ASS/SSA/WebVTT and their legacy
    spellings) and the MP4 tap's stsd fourccs (tx3g / QuickTime text /
    the mov_text alias — see src.mp4_subs). Bitmap subtitle codecs (PGS,
    VOBSUB) carry images, not text — the UI leaves those to VLC's own
    renderer."""
    c = (codec or "").upper()
    return c.startswith(("S_TEXT/UTF8", "S_TEXT/ASS", "S_TEXT/SSA",
                         "S_TEXT/WEBVTT")) or c in ("S_UTF8", "S_ASS",
                                                    "S_SSA", "TX3G",
                                                    "MOV_TEXT", "TEXT")


# bidi embedding/isolate controls (U+202A-202E, U+2066-2069). Middle-East
# rip houses wrap even their ENGLISH SRT lines in these plus NBSP padding;
# on Latin lines they only garble centering, so they are stripped there.
# Lines that actually carry Arabic-script characters keep every mark.
_BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069]")
_ARABIC_RE = re.compile("[\u0600-\u06ff\u0750-\u077f]")


def strip_bidi_noise(text: str) -> str:
    """Drop bidi controls / NBSP padding from non-Arabic lines."""
    if _ARABIC_RE.search(text):
        return text
    return _BIDI_RE.sub("", text).replace("\xa0", " ")


def flatten_ass_text(raw: str) -> str:
    r"""ASS/SSA payload (or plain subrip text) -> clean text. Drawing
    payloads (vector commands between {\p1}..{\p0}) and override blocks
    are dropped, \N/\n become line breaks, \h a space. mkvmerge stores
    only a Dialogue line's Text field; ffmpeg stores the line with its
    fixed fields (with or without the "Dialogue:" prefix) — all three
    shapes are handled (a stored Comment: event is skipped)."""
    low = raw[:9].lower()
    if low.startswith("dialogue:") or low.startswith("comment:"):
        parts = raw.split(",", 9)
        if len(parts) < 10 or low.startswith("comment:"):
            return ""
        raw = parts[9]
    elif _FIELD_PREFIX_RE.match(raw):
        raw = raw.split(",", 8)[-1]
    raw = _DRAW_RE.sub("", raw)
    raw = _OVERRIDE_RE.sub("", raw)
    raw = _TAG_RE.sub("", raw)
    raw = raw.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    return strip_bidi_noise(raw).strip()


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

    # ---- internals ----
    def _finish(self, start, end, text):
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
                    if not self._resync():
                        break       # nothing to discard: wait for data
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
                if not self._resync():
                    break           # garbage with no magic: wait for data
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

    def _resync(self) -> bool:
        """Self-heal after garbage: jump to the next Cluster header.
        Returns False when no progress is possible — nothing left to
        discard, the parse must WAIT for more data. Without that, an
        all-zero or magic-less stretch shrank the buffer to 4 bytes and
        _parse spun on it forever (a core at 100% and the tap's cues
        dead for the rest of the session)."""
        i = self.buf.find(bytes([0x1F, 0x43, 0xB6, 0x75]), 1)
        if i < 0:
            # keep the tail: the magic can straddle the chunk edge
            keep = min(4, len(self.buf))
            del self.buf[:len(self.buf) - keep]
            if keep <= 4:
                if self._stack[-1] != _CTX_CLUSTER:
                    self._stack.append(_CTX_CLUSTER)
                return False
        else:
            del self.buf[:i]
        if self._stack[-1] != _CTX_CLUSTER:
            self._stack.append(_CTX_CLUSTER)
        return True

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
                       if is_text_codec(m["codec"])}
        if not text_tracks:
            # only bitmap tracks (PGS/VOBSUB) or nothing parsed: emit
            # nothing — there is no text to peel. The UI leaves those
            # files to VLC's own renderer.
            return
        if self.prefer_language:
            for n in sorted(text_tracks):
                m = text_tracks[n]
                if lang_matches(self.prefer_language, m["lang"], m["name"]):
                    self._selected = n
                    return
        self._selected = min(text_tracks)

    def _parse_block(self, header, size, duration_s=None):
        """Block at buf[0] (SimpleBlock or Block). Returns a cue list
        (possibly empty) when CONSUMED, or None while incomplete. The
        ONLY consumer of buffer bytes for blocks. ``duration_s`` comes
        from the enclosing BlockGroup's BlockDuration, when there is
        one."""
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
            text = flatten_ass_text(
                bytes(buf[payload_start:payload_start + payload_len])
                .decode("utf-8", "replace"))
            if text:
                start_s = (self._cluster_tc + rel_tc) / 1000.0
                end_s = start_s + (duration_s if duration_s
                                   else _FALLBACK_CUE_S)
                out.append(self._finish(start_s, end_s, text))
        del buf[:header + size]
        return out

    def _parse_block_group(self, blob):
        """BlockGroup body: find the Block plus its BlockDuration (the
        real cue length, written by subrip/ASS muxers; SimpleBlock has
        nowhere to put one). Returns the block's cue list."""
        pos = 0
        duration_s = None
        block_at = None
        while pos < len(blob):
            eid, ilen = _vint_size(blob, pos, keep_marker=True)
            if eid is None:
                break
            size, slen = _vint_size(blob, pos + ilen, keep_marker=False)
            if size is None:
                break
            header = ilen + slen
            if eid == _BLOCK and block_at is None:
                block_at = (pos, header, size)
            elif eid == _BLOCK_DURATION and 0 < size <= 8:
                ms = int.from_bytes(blob[pos + header:pos + header + size],
                                    "big")
                duration_s = ms / 1000.0
            pos += header + size
        if block_at is None:
            return []
        bpos, bheader, bsize = block_at
        scratch = bytearray(blob[bpos:bpos + bheader + bsize])
        save = self.buf
        self.buf = scratch
        made = self._parse_block(bheader, bsize, duration_s)
        self.buf = save
        return made or []
