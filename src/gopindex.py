# -*- coding: utf-8 -*-
"""Incremental keyframe (IDR) index over a growing MPEG-TS DVR buffer.

WHY THIS EXISTS (measured 2026-11-01 against the bundled VLC 3.0.23 and a
real provider recording): VLC's TS demuxer has no index, so every seek is a
byte estimate that starts feeding the H.264 decoder MID-GOP — on P-frames
whose reference frames were never decoded. The pixel rig (tools/
gop_pixel_probe.py) measured the three cases on the actual NFL Network
recording (2.5 s GOPs):

  * set_time(mid-GOP)   -> ~2.0 s display blackout, then clean at next IDR
                           ("pause while it loads" on every rewind)
  * :start-time reopen  -> instant, but ~1 GOP of smeared/concealed frames
                           healing toward the next IDR (the "blurry rewind";
                           at 0.125x slow-mo one damaged GOP shows for ~20
                           wall-seconds)
  * set_time(IDR-0.35)  -> ~0.4 s recovery, clean picture from the IDR

VLC 3 has no precise-seek option (that is libvlc 4), so the app builds the
keyframe index itself: this scanner reads the recorder's buffer file as it
grows and records each IDR slice together with the PTS of its access unit,
expressed on VLC's own raw axis (PTS - PTS of the file's first video PES —
VLC's get_time/set_time numbers for a local TS are exactly that axis,
verified by the rig). Chase-mode seeks then "anchor": instead of landing on
the user's exact target they land just before the previous IDR, and the
decoder starts clean.

Parsing model (deliberately cheap — the scanner runs every couple of
seconds behind the write head):

  * bulk pattern search with bytes.find (C speed) for H.264 IDR slice
    start codes (00 00 01 65/45/25) and HEVC IRAP (00 00 01 26/27/2A/2B
    01) — NOT a per-packet demux loop;
  * each hit is verified to sit inside the video PID's packets (PID learned
    from the first PES-start scan of the file);
  * the PTS is parsed by walking back at most a couple hundred packets to
    the access unit's PES-start packet — only on the (rare) IDR hits;
  * broadcast PTS renumbering (splice nights) is detected as a big PTS jump
    between consecutive IDRs; the origin is rebased so the raw axis stays
    roughly continuous and the index flags itself "torn" — accuracy
    degrades gracefully and the existing seek-verify machinery
    (player_view._verify_seek) still polices landings.

The scanner is thread-safe: one background thread calls scan(); readers
(anchor_for / idrs) take a snapshot under the same lock.
"""

import logging
import os
import threading
import time

log = logging.getLogger("mtp")
_SYNCDBG = bool(os.environ.get("MTP_GOP_DEBUG"))

_TS = 188                          # MPEG-TS packet size (the muxer's)
_PTS_PERIOD = 2 ** 33              # 33-bit PTS wrap, 1/90000 s units
_DISCONT_S = 30.0                  # IDR-to-IDR raw jump beyond this = splice
_MIN_GAP_S = 0.05                  # closer raw entries than this = duplicate
_MAX_BACKTRACK_PKTS = 256          # walk-back budget for the IDR's PES start
_SCAN_PIECE = 2 * 1024 * 1024      # one find()-granularity piece (~20 ms,
#                                  # then the GIL is yielded — a 300 ms
#                                  # hold starved the UI and the e2e's
#                                  # in-process HTTP source)
_SCAN_BUDGET = 24 * 1024 * 1024    # max new bytes per scan() pass (long
#                                  # backfills span several cadences)
_TAIL_FIRST = 16 * 1024 * 1024     # buffers bigger than this get their TAIL
_TAIL_BYTES = 64 * 1024 * 1024     # indexed first (rewinds target near the
#                                  # head; the full history fills in after)
_CARRY = 8                         # bytes kept between reads/passes

# H.264 IDR slice NAL headers (forbidden=0, nal_ref_idc 1..3, type 5);
# encoders virtually always use ref=3 (0x65) but 0x45/0x25 are legal.
_H264_IDR = (b"\x00\x00\x01\x65", b"\x00\x00\x01\x45", b"\x00\x00\x01\x25")
# HEVC IRAP NAL units: type 19/20 IDR_W_RADL/IDR_N_LP (0x26/0x27 with the
# reserved bit) and CRA (0x2A/0x2B), first payload byte 0x01.
_HEVC_IRAP = (b"\x00\x00\x01\x26\x01", b"\x00\x00\x01\x27\x01",
              b"\x00\x00\x01\x2a\x01", b"\x00\x00\x01\x2b\x01")
_PES_VIDEO_SIDS = range(0xE0, 0xF0)   # ITU-T H.222.0 video stream ids


def _packet_pid(p: bytes) -> int:
    return ((p[1] & 0x1F) << 8) | p[2]


def _packet_pusi(p: bytes) -> bool:
    return bool(p[1] & 0x40)


def _payload_offset(p: bytes) -> int:
    """Byte offset of the packet's payload (4-byte header + adaptation
    field when present); -1 when the packet carries no payload."""
    afc = (p[3] >> 4) & 0x3
    if afc == 0 or afc == 2:
        return -1
    off = 4
    if afc == 3:
        off += 1 + p[4]
        if off >= _TS:
            return -1
    return off


def _packet_pcr(p: bytes):
    """(pid, pcr_90k) of a TS packet carrying a PCR in its adaptation
    field, else None. pcr in 1/90000 s units (extension folded in)."""
    if len(p) < _TS or p[0] != 0x47:
        return None
    if (p[3] >> 4) & 0x3 < 2:
        return None
    afl = p[4]
    if afl < 7 or afl > 183:
        return None
    if not p[5] & 0x10:
        return None
    q = 6
    base = ((p[q] << 25) | (p[q + 1] << 17) | (p[q + 2] << 9)
            | (p[q + 3] << 1) | (p[q + 4] >> 7))
    ext = ((p[q + 4] & 0x01) << 8) | p[q + 5]
    return _packet_pid(p), base + ext / 300.0   # 27MHz ext -> 90k units


def _pes_pts(payload: bytes):
    """33-bit PTS of a PES payload starting at its 00 00 01 <sid> code, or
    None. (PES: 3 start + 1 sid + 2 len + 2 flag bytes + 1 header length,
    then the 5-byte PTS when flags2 bit 7 is set.)"""
    if len(payload) < 14 or payload[:3] != b"\x00\x00\x01":
        return None
    if not payload[7] & 0x80:
        return None
    b = payload[9:14]
    return (((b[0] >> 1) & 0x07) << 30 | b[1] << 22
            | (b[2] >> 1) << 15 | b[3] << 7 | (b[4] >> 1))


class GopIndex:
    """IDR index of one growing TS file. Not Qt-aware: the owner starts a
    plain thread that calls scan() periodically and stops it via close()."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._pos = 0              # file bytes consumed (packet-aligned)
        self._idrs = []            # [(raw_s, file_offset)] ascending
        self._hit_offs = set()     # indexed hit offsets (dedupe across the
        #                          # tail-first pass + the forward fill)
        self._origin_pts = None    # 33-bit PTS that maps to raw 0.0
        self._video_pid = None
        self._last_pts_u = None    # last UNWRAPPED IDR PTS (1/90000 units)
        self._last_raw = None      # raw_s of the last indexed IDR
        self._last_wall = 0.0      # wall clock when the last IDR was indexed
        self._tail_done = False    # initial tail pass ran (or was N/A)
        self.torn = False          # a PTS renumber was rebased (approximate)
        self.closed = False

    # ---- reader API (any thread) ----

    def anchor_for(self, raw_s: float, max_back_s: float = 8.0):
        """Raw-axis time of the IDR at/below ``raw_s`` (or None when no IDR
        sits within max_back_s — early buffer, scan lag, torn index)."""
        with self._lock:
            idrs = list(self._idrs)
        best = None
        for raw, _off in reversed(idrs):
            if raw <= raw_s:
                best = raw
                break
        if best is None or raw_s - best > max_back_s:
            return None
        return best

    def idrs(self):
        with self._lock:
            return list(self._idrs)

    def close(self):
        self.closed = True

    # ---- scanner (owner thread) ----

    def reset(self):
        """New file at the same path (or a truncate): drop everything."""
        with self._lock:
            self._pos = 0
            self._idrs = []
            self._hit_offs = set()
            self._origin_pts = None
            self._video_pid = None
            self._last_pts_u = None
            self._last_raw = None
            self._last_wall = 0.0
            self._tail_done = False
            self.torn = False

    def scan(self) -> int:
        """Consume newly written bytes (bounded by _SCAN_BUDGET per pass);
        returns the number of new IDRs indexed (0 also when the file
        vanished or a read fails — the next pass retries)."""
        if self.closed:
            return 0
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return 0
        if size < self._pos:
            # the buffer was replaced under us (fresh engage on same path)
            self.reset()
            return 0
        new = 0
        try:
            with open(self.path, "rb") as f:
                # Long-buffer fast path: index the newest _TAIL_BYTES first
                # (rewinds target near the head) before the forward fill
                # from byte 0. The tail pass needs the origin, which lives
                # at the file head — a 256 KB head read discovers it.
                # _tail_done only latches on SUCCESS, so a head that is not
                # parseable yet retries next pass.
                if not self._tail_done and size > _TAIL_FIRST:
                    head = f.read(262144)
                    if len(head) >= _TS * 3 and self._discover(head):
                        tail_pos = max(0, size - _TAIL_BYTES)
                        tail_pos -= tail_pos % _TS
                        new += self._scan_region(
                            f, tail_pos, size, backfill=True)
                        self._tail_done = True
                        if _SYNCDBG:
                            log.info("gopindex: tail-first pass: %d IDRs",
                                     new)
                # forward fill from the head, several pieces per pass
                pos = self._pos
                budget = _SCAN_BUDGET
                while pos < size and budget > 0 and not self.closed:
                    new += self._scan_region(
                        f, pos, min(size, pos + budget), backfill=False)
                    step = min(budget, size - pos)
                    step -= step % _TS
                    if step <= 0:
                        break
                    pos += step
                    budget -= step
                if pos > self._pos:
                    self._pos = pos
        except OSError as exc:
            try:
                log.debug("gopindex: read failed: %r", exc)
            except Exception:
                pass
            return new
        return new

    def _scan_region(self, f, start: int, end: int, backfill: bool) -> int:
        """Index IDR hits in [start, end) of the open file ``f`` in
        _SCAN_PIECE-sized pieces (each piece is one find() sweep, so the
        GIL is yielded between pieces). Each read extends _CARRY bytes
        past the piece so patterns straddling a boundary are caught (the
        offset dedupe absorbs the overlap). Returns new IDRs found."""
        new = 0
        pos = start
        while pos < end and not self.closed:
            f.seek(pos)
            piece = min(_SCAN_PIECE, end - pos)
            data = f.read(piece + _CARRY)
            if len(data) <= _CARRY:
                break
            new += self._scan_chunk(data, pos, backfill=backfill)
            if piece <= 0:
                break
            pos += piece
            time.sleep(0.001)           # yield the GIL between pieces
        return new

    # ---- internals (scanner thread) ----

    def _scan_chunk(self, data: bytes, base: int, backfill: bool) -> int:
        """Index IDR hits inside ``data`` (the file's bytes at ``base``,
        which is packet-aligned); returns how many were recorded.
        ``backfill`` marks out-of-order regions (the tail-first pass) whose
        entries skip the splice-gap heuristics."""
        if self._video_pid is None or self._origin_pts is None:
            if not self._discover(data):
                return 0
        new = 0
        for pat in _H264_IDR + _HEVC_IRAP:
            start = 0
            while True:
                i = data.find(pat, start)
                if i < 0:
                    break
                start = i + 1
                if self._maybe_idr(data, base, i, backfill=backfill):
                    new += 1
        if new:
            with self._lock:
                self._idrs.sort()
        return new

    def _discover(self, data: bytes) -> bool:
        """Learn the video PID and the PTS origin from the file's first
        packets. VLC normalizes a local TS so its raw axis (get_time /
        set_time numbers) is PTS minus the file's FIRST PCR — measured on
        the NFL Network recording: first PCR 0.0008, first audio PTS
        0.415, first IDR 0.433, and VLC raw zero sits at the PCR (probed:
        set_time(31.0) displays ffprobe-31.0 content). Using the audio PES
        instead shifted every anchor by -0.415 s. Returns True when both
        the PID and the origin are known."""
        pid = None
        pcr0 = None
        pts_min = None
        for i in range(0, min(len(data) - _TS + 1, _TS * 4000), _TS):
            if data[i] != 0x47:
                continue
            pkt = data[i:i + _TS]
            if pcr0 is None:
                r = _packet_pcr(pkt)
                if r is not None:
                    pcr0 = r[1]
            if not _packet_pusi(pkt):
                continue
            off = _payload_offset(pkt)
            if off < 0 or pkt[off:off + 3] != b"\x00\x00\x01":
                continue
            sid = pkt[off + 3]
            if sid in _PES_VIDEO_SIDS and pid is None:
                pts = _pes_pts(pkt[off:])
                if pts is not None:
                    pid = _packet_pid(pkt)
            elif pts_min is None:
                pts = _pes_pts(pkt[off:])
                if pts is not None:
                    pts_min = pts
            if pid is not None and pcr0 is not None:
                break
        if pid is None:
            return False
        origin = pcr0 if pcr0 is not None else pts_min
        if origin is None:
            return False
        self._video_pid = pid
        self._origin_pts = origin
        self._last_pts_u = origin
        try:
            log.info("gopindex: video pid %d, pts origin %.3f s%s",
                     pid, origin / 90000.0,
                     "" if pcr0 is not None else " (PES fallback)")
        except Exception:
            pass
        return True

    def _maybe_idr(self, data: bytes, base: int, i: int,
                   backfill: bool) -> bool:
        """A start-code hit at ``i`` inside ``data`` (file offset
        ``base + i``): verify the containing packet's PID and index the
        access unit's PTS. Returns True when a new IDR was recorded."""
        off_abs = base + i
        if off_abs in self._hit_offs:
            return False                      # tail-first/forward overlap
        grid = off_abs % _TS
        pkt_start = i - grid
        if pkt_start < 0:
            return False                      # packet started before chunk
        pkt = data[pkt_start:pkt_start + _TS]
        if len(pkt) < _TS or pkt[0] != 0x47:
            return False
        if _packet_pid(pkt) != self._video_pid:
            return False
        pts = self._pes_pts_backtrack(data, pkt_start)
        if pts is None:
            return False
        pts_u = self._unwrap(pts)
        if pts_u is None:
            return False
        raw_s = (pts_u - self._origin_pts) / 90000.0
        # The gap tracker (duplicate + splice detection) only applies to
        # entries that EXTEND the newest indexed raw. Tail-first backfill
        # entries and forward-fill entries still below the tail's newest
        # are historical data in known order — rebasing on them would be
        # a false torn flag, and their PTS continuity is already implied
        # by the entries around them.
        extends = self._last_raw is None or raw_s > self._last_raw
        if extends and self._last_raw is not None:
            gap = raw_s - self._last_raw
            if abs(gap) < _MIN_GAP_S:
                return False                  # duplicate AU hit
            if gap < -0.5 or gap > _DISCONT_S:
                self._rebase(pts_u)
                raw_s = self._last_raw + 1.0
        with self._lock:
            self._idrs.append((raw_s, off_abs))
            self._hit_offs.add(off_abs)
        if extends:
            self._last_pts_u = pts_u
            self._last_raw = raw_s
            self._last_wall = time.time()
        return True

    def _rebase(self, pts_u: int) -> None:
        """Broadcast PTS renumbering (splice): shift the origin so the raw
        axis stays ~continuous. The wall-time estimate caps the error on
        burst-gap nights; entries before the jump stay only approximately
        right (torn) until the buffer is replaced."""
        est = (self._last_raw or 0.0) + min(
            max(0.0, time.time() - (self._last_wall or time.time())), 5.0)
        self._origin_pts = pts_u - est * 90000
        self.torn = True
        try:
            log.warning("gopindex: PTS discontinuity — index rebased, "
                        "raw %.1fs onward approximate (torn)", est)
        except Exception:
            pass

    def _pes_pts_backtrack(self, data: bytes, pkt_start: int):
        """PTS of the access unit containing packet ``pkt_start``: walk
        back packet by packet to the nearest PES-start of the video PID."""
        p = pkt_start
        for _ in range(_MAX_BACKTRACK_PKTS):
            p -= _TS
            if p < 0:
                return None
            pkt = data[p:p + _TS]
            if len(pkt) < _TS or pkt[0] != 0x47:
                return None
            if _packet_pid(pkt) != self._video_pid or not _packet_pusi(pkt):
                continue
            off = _payload_offset(pkt)
            if off < 0:
                continue
            return _pes_pts(pkt[off:])
        return None

    def _unwrap(self, pts: int):
        """Unwrap a 33-bit PTS against the last seen IDR (returns the
        unwrapped value; never fails — callers check state)."""
        last = self._last_pts_u
        if last is None:
            return pts
        last = int(last)        # the origin can carry the PCR float tail
        delta = pts - (last & (_PTS_PERIOD - 1))
        if delta < -(_PTS_PERIOD // 2):
            delta += _PTS_PERIOD
        elif delta > _PTS_PERIOD // 2:
            delta -= _PTS_PERIOD
        return last + delta
