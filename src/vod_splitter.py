# -*- coding: utf-8 -*-
"""VOD splitter: one provider connection, subtitle text for the filter.

Movies & series subtitle tracks are PRE-TIMED (the cue exists before its
dialogue plays), so — unlike live captions — they need no playback delay.
But VLC 3 hands the app no subtitle text, never forwards subs through its
stream output, and the account allows a single stream connection.

So the app inserts itself as a tiny local relay:

    provider ===ONE streaming GET===> cache file (temp .mkv/.mp4)
                                      |            |
             local HTTP (ranges)      |            +--> streaming MKV parser
             serves VLC  <============+                  (subtitle cues)
                                      +--> MP4 sample-table tap
                                           (src.mp4_subs)

VLC plays http://127.0.0.1:<port>/v — byte-identical to the original
(seeking works: range requests are served from the cache prefix, or by
restarting the single provider connection at the new offset — never two
connections at once). The parser (src.mkv_subs) tails the LOCAL cache and
emits subtitle cues on the file's own timeline, which is exactly VLC's
playback clock: the filter sees each cue before its words play. MP4
inputs (ftyp box) run the same relay with the moov-index tap from
src.mp4_subs instead.
"""

import logging
import os
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PyQt5 import QtCore

from .mkv_subs import MkvSubParser, _vint_size
from .mp4_subs import Mp4SubParser

log = logging.getLogger("mtp")

# MTP_SPLIT_TRACE=1 prints a relay timeline (requests, provider reopens,
# rebases, slow reads) for seek-pacing diagnosis. Shared time base so
# tools can print markers onto the same timeline.
TRACE = bool(os.environ.get("MTP_SPLIT_TRACE"))
TRACE_T0 = time.monotonic()


def tlog(fmt, *args):
    if TRACE:
        try:
            print("[split %+9.3f] %s" % (time.monotonic() - TRACE_T0,
                                         fmt % args if args else fmt),
                  flush=True)
        except Exception:  # noqa: BLE001
            pass


_CHUNK = 1 << 16
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"          # MKV header magic
_FTYP = b"ftyp"                            # MP4: brand box at offset 4

# VLC's MKV demuxer opens a second connection for the file's seek index
# asking for the last ~2.3 MB (Cues + duration + track entries at the
# tail; measured on this provider: a 1.9 GB rip asked 1.3 MB + 11 KB).
# The prefetch must COVER that whole request: one byte short and the
# handler acquires the provider for the remainder — killing VLC's
# main connection (ConnectionReset) and rebasing the cache window away
# from the head, which starves the subtitle tap (see _tap_cache).
# 2.5 MB = 2.3 MB + margin; every MB cut here is ~0.5 s off EVERY
# subtitle-carrying movie start (the prefetch sits on the critical path
# before VLC's first byte).
_TAIL_PREFETCH = 2_621_440

# Head pre-parse budget: the Tracks element (subtitle track metadata)
# lives in the first KBs; half an MB covers every rip seen in practice
# while costing ~0.2 s of sequential read on the single connection.
_HEAD_PARSE_BYTES = 1 << 19

# Language-switch re-anchor distance: the tap's fresh parser restarts
# this far behind the write frontier so the cues around the CURRENT
# playback position (which trails the frontier by VLC's read-ahead) are
# re-emitted immediately; the rest of the window is backfilled from its
# start on a side thread (dupes dedupe downstream).
_TAP_RESTART_BACK_BYTES = 24 << 20

# Seek pacing: the serving handler HOLDS the one provider stream across
# chunks (re-acquiring per chunk used to close its own live connection
# and reopen the provider every 64 KiB — seeks stalled for 40+ s), and
# overlapping requests trail the owner through the cache instead of
# replacing it. Real-provider seeks resume in ~2 s (one provider reopen,
# the rest is VLC's own re-buffer).
VOD_SPLITTER_READY = True

# Provider ride-through: a CDN that caps connection AGE (the 2026-09-01
# Incredibles kills at ~600 s) or reaps idle ones (a long VLC pause —
# the 2026-09-02 Paw Patrol freeze) cuts the ONE streaming body
# mid-file. The serve loop reopens at the exact byte position so VLC
# never sees a truncated body. Reopens after a body that DELIVERED are
# unbounded (a 2 h movie on a 10-min age cap rides a dozen of them);
# only failures are budgeted — an open error or a zero-byte reopen
# burns one unit, any served byte refills the budget, and an exhausted
# budget cuts the body with a WARN (a loud, reportable failure instead
# of the silent freeze the pre-fix relay produced).
_REOPEN_FAIL_BUDGET = 3
_REOPEN_BACKOFF_S = 0.5


def _snap_cluster(buf: bytes) -> int:
    """Offset of the first plausibly-real Cluster header in ``buf``:
    the magic plus a size vint that fits the buffer, followed by the
    next cluster header (or the buffer end). A mid-file prefetch
    boundary starts MID-ELEMENT — the parser's size decode of that
    garbage can exceed 1 MB and its stream-skip then swallows the whole
    region without ever resyncing, so the harvest must anchor on a
    validated header instead of feeding from offset 0. Falls back to
    the first magic whose size merely fits, else -1."""
    magic = b"\x1f\x43\xb6\x75"
    first_fit = -1
    i = buf.find(magic)
    while i >= 0:
        size, slen = _vint_size(buf, i + 4, keep_marker=False)
        if size is not None and size > 0 and i + 4 + slen + size <= len(buf):
            if first_fit < 0:
                first_fit = i
            j = i + 4 + slen + size
            if j == len(buf) or buf[j:j + 4] == magic:
                return i
        i = buf.find(magic, i + 1)
    return first_fit


class _ProviderStream:
    """One sequential HTTP read from the provider at a byte offset."""

    def __init__(self, relay, offset: int):
        self.relay = relay
        self.offset = offset
        self.gen = relay.bump_gen()
        self.dead = False
        self.owner = None       # the one handler allowed to read it
        # a stream that starts exactly at the cache frontier extends it;
        # one opened at a seek target past a gap stays passthrough
        self.appendable = True   # the window rebases on jumps
        req = urllib.request.Request(
            relay.url, headers={"User-Agent": relay.ua,
                                "Range": f"bytes={offset}-"})
        t0 = time.monotonic()
        self.resp = urllib.request.urlopen(req, timeout=30)
        status = getattr(self.resp, "status", None)
        if status is not None and status not in (200, 206):
            # 403 (expired signed URL) / 5xx (provider down): fail the
            # OPEN, not the body — the serve loop's bounded-retry path
            # then decides between another attempt and cutting. A None
            # status is a non-HTTP scheme (file://) — always accepted.
            self.resp.close()
            raise OSError(f"provider HTTP {status}")
        tlog("provider open @%d: %.3fs", offset, time.monotonic() - t0)

    def read(self, n: int) -> bytes:
        return self._read(n, partial=False)

    def read_some(self, n: int) -> bytes:
        """Like read(), but returns as soon as any data is available —
        the first bytes reach VLC sooner after a provider restart."""
        return self._read(n, partial=True)

    def _read(self, n: int, partial: bool) -> bytes:
        if self.dead:
            return b""
        at = self.offset
        t0 = time.monotonic()
        try:
            if partial:
                r1 = getattr(self.resp, "read1", None)
                data = r1(n) if r1 is not None else self.resp.read(n)
            else:
                data = self.resp.read(n)
        except Exception as exc:  # noqa: BLE001
            tlog("provider read @%d: died (%r)", at, exc)
            self.dead = True
            return b""
        dt = time.monotonic() - t0
        if not data:
            tlog("provider read @%d: EOF after %.3fs", at, dt)
            self.dead = True
            return b""
        if dt > 0.2:
            tlog("provider read @%d: %d bytes stalled %.3fs",
                 at, len(data), dt)
        self.offset += len(data)
        if self.appendable:
            self.relay.append_cache(data)
        return data

    def close(self):
        self.dead = True
        try:
            self.resp.close()
        except Exception:  # noqa: BLE001
            pass


class VodRelay(QtCore.QObject):
    """Owns the single provider connection + the local relay server."""

    cue = QtCore.pyqtSignal(float, float, str)      # start_s, end_s, text
    failed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.url = ""
        self.ua = "MichaelTVPlayer/1.0"
        self.total = 0                 # Content-Length (0 = unknown)
        self._container = ""           # "mkv" | "mp4" (probe sets it)
        self._content_type = "video/x-matroska"
        self.cache_base = 0            # file offset of the cache window
        self.cache_size = 0            # bytes in the window
        self.cache_gen = 0
        self._tail = b""               # prefetched file tail (cues index)
        self._tail_base = -1           # file offset of the tail
        self.cache_path = None
        self._cache = None             # open file handle (append+read)
        self._cache_lock = threading.Lock()
        self._gen = 0
        self._lock = threading.Lock()
        self._stream = None            # the ONE provider stream
        self.provider_opens = 0        # provider connections opened (diag/
        #                               # regression: seek pacing must not
        #                               # reopen per chunk)
        self._server = None
        self._parser = None
        self._alive = False
        self._prefer = "eng"        # preferred subtitle language (tap)
        self._tap_restart = False   # re-select the tap's track on the
        #                            # next loop pass (set_prefer_language)
        self.parser_tracks = {}     # {mkv track number: codec id} — the
        #                               # UI reads this to learn which tracks
        #                               # are text the parser can flatten
        self.parser_tracks_meta = {}   # full {number: {codec,lang,name}}
        self.parser_selected = None
        self.parser_scale_ns = 1_000_000   # Info TimecodeScale seen by the
        #                                      # tap (seeds mid-stream parsers)
        self._tail_harvested = False  # one-shot tail-region parse ran
        # startup/off the UI thread: start() returns after the probe; the
        # head fetch + tail prefetch + main acquire happen on a thread and
        # handlers wait for _ready (bounded) so playback simply buffers
        self._ready = threading.Event()
        self._startup_failed = False
        self._head = b""            # prefetched file head (metadata + VLC probes)
        self._start_offset = 0      # where the main stream opens (resume/switch)

    # ---- lifecycle ----
    def start(self, url: str, ua: str, prefer_language: str = "eng",
              start_offset: int = 0) -> str:
        """Open the provider, verify MKV or MP4, bring the relay up.
        Returns the local URL for VLC, or '' when the input is neither
        (caller falls back to playing the original URL).

        ``prefer_language`` steers the tap's subtitle-track choice (the
        user's CC-menu selection). ``start_offset`` is a RESUME MARKER
        (any truthy value): playback resumes mid-file, so the relay
        prefetches only the tail and VLC's own opening walk drives the
        provider (VLC seeks by TIME — the resume byte cannot be known
        in advance; see _startup).

        Only the tiny probe runs synchronously — start() returns as soon
        as the local server listens, and the head fetch + tail prefetch +
        main acquire complete on a background thread (one provider
        connection at a time, strictly ordered). Handlers wait for that
        startup (bounded), so VLC just buffers through it instead of the
        UI thread freezing for the 4 MB tail download.
        """
        self.stop()
        self.url = url
        self.ua = ua or self.ua
        self._prefer = (prefer_language or "eng").lower() or "eng"
        self._tap_restart = False
        self._tail_harvested = False
        self.parser_scale_ns = 1_000_000
        self._start_offset = max(0, int(start_offset))
        try:
            # ONE tiny request settles the container + the total size.
            # Anything but MKV/MP4 is refused HERE — before any temp
            # file or the 2 MB tail prefetch, so the direct-playback
            # fallback costs a single tiny connection instead of three
            # plus a wasted tail download.
            if not self._probe_head():
                self.failed.emit("not an MKV or MP4 stream")
                return ""
            if self.total and self._start_offset >= self.total:
                self._start_offset = 0
            fd, self.cache_path = tempfile.mkstemp(
                suffix=".mp4" if self._container == "mp4" else ".mkv",
                prefix="mtp_split_")
            os.close(fd)
            # SEPARATE handles: one shared pointer would let the tap
            # reader's seeks hijack the append position (that corrupted
            # the cache)
            self._cache = open(self.cache_path, "r+b")    # writer
            self._cache_r = open(self.cache_path, "rb")   # reader
            self.cache_size = 0
            self._ready.clear()
            self._startup_failed = False
            self._head = b""
            self._alive = True      # BEFORE the startup thread + tap
            threading.Thread(target=self._startup, daemon=True,
                             name="mtp-relay-start").start()
            try:
                self._server = ThreadingHTTPServer(
                    ("127.0.0.1", 0), self._make_handler())
                self._server.daemon_threads = True

                def _quiet_error(*_a, **_k):
                    pass         # VLC drops probe connections routinely
                self._server.handle_error = _quiet_error
                threading.Thread(target=self._server.serve_forever,
                                 daemon=True, name="mtp-relay").start()
            except Exception as exc:  # noqa: BLE001
                self.stop()
                self.failed.emit(f"relay failed: {exc}")
                return ""
            try:
                log.info("vod splitter: %s -> 127.0.0.1:%d (total=%d "
                         "offset=%d, startup async)",
                         url, self._server.server_address[1], self.total,
                         self._start_offset)
            except Exception:
                pass
            return f"http://127.0.0.1:{self._server.server_address[1]}/v"
        except Exception as exc:  # noqa: BLE001
            # The known aborts above stop themselves; this catches the
            # UNPLANNED ones (e.g. a network error inside the probe) so an
            # unexpected raise can never strand the cache file.
            self.stop()
            self.failed.emit(f"splitter failed: {exc}")
            return ""

    def _startup(self):
        """Background: bring the relay up, strictly one provider
        connection at a time. Offset-0 sessions ride the head pre-parse
        on the MAIN stream (its bytes land in the cache, so VLC's opening
        walk continues on the same stream — zero rebases, one fewer
        connection). Resume / mid-movie-engage sessions let VLC's own
        opening walk drive everything after the tail prefetch: its
        bytes=0- walk GET opens the provider at 0 (consumed bytes land
        in the cache — the tap parses the Tracks element from them),
        the seek index is served from the tail prefetch, and the seek
        GET lands the main provider stream exactly where playback
        continues."""
        try:
            self._bootstrap_tail()
            if not self._alive:
                return
            if self._start_offset:
                # VLC-driven resume engage (see docstring). The previous
                # design pre-fetched the head AND pre-opened a stream at
                # start_offset: measured on a real provider, VLC's walk
                # REPLACED that stream before it served a playback byte —
                # a wasted provider open plus ~5 MB streamed into a
                # window VLC immediately rebased away from (~2 s of dead
                # download on the "turn subtitles on mid-movie" path).
                self._start_ffmpeg_tap(mid_stream=self.cache_base > 0)
                self._ready.set()
                tlog("startup ready (vlc-driven): head=%d tail=%d base=%d",
                     len(self._head), len(self._tail), self.cache_base)
                return
            st = None
            for attempt in range(3):
                if not self._alive:
                    return
                st = self._acquire(self._start_offset)
                if st is not None:
                    break
                tlog("startup: main acquire attempt %d failed", attempt + 1)
                time.sleep(1.0)
            if st is None:
                self._startup_failed = True
                self.failed.emit("provider open failed")
                return
            self._ride_head(st)
            if not self._alive:
                return
            # the tap now (metadata from the head parse is in hand, the
            # window base is final) — before _ready so no byte is ever
            # served without the tap watching for it
            self._start_ffmpeg_tap(mid_stream=self.cache_base > 0)
            self._ready.set()
            tlog("startup ready: head=%d tail=%d base=%d",
                 len(self._head), len(self._tail), self.cache_base)
        except Exception as exc:  # noqa: BLE001
            tlog("startup failed (%r)", exc)
            self._startup_failed = True
            try:
                self.failed.emit(f"splitter startup failed: {exc}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            # never leave waiters blocked, whatever happened
            self._ready.set()

    def _ride_head(self, st) -> None:
        """Offset-0 head phase: read the head budget off the MAIN stream
        (append_cache runs inside read — the cache and the stream stay in
        lockstep, so VLC's later walk continues on this very stream with
        no rebase and no extra provider connection) while feeding the
        MKV track parser."""
        parser = MkvSubParser(prefer_language=self._prefer) \
            if self._container == "mkv" else None
        buf = bytearray()
        while len(buf) < _HEAD_PARSE_BYTES and not st.dead and self._alive:
            data = st.read_some(min(1 << 16, _HEAD_PARSE_BYTES - len(buf)))
            if not data:
                break
            buf += data
            if parser is not None:
                parser.feed(bytes(data))
        self._head = bytes(buf)
        if parser is not None and parser._track_meta:
            self.parser_tracks_meta = dict(parser._track_meta)
            self.parser_tracks = {num: m["codec"]
                                  for num, m in parser._track_meta.items()}
            self.parser_selected = parser._selected
            self.parser_scale_ns = parser._tc_scale_ns
        tlog("head ride: %d bytes, %d tracks, selected=%r",
             len(buf), len(self.parser_tracks), self.parser_selected)

    def stop(self):
        self._alive = False
        self._ready.set()        # un-block any handler still waiting
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._server.server_close()   # free the listening socket
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        self._kill_ffmpeg()
        with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        if self._cache is not None:
            try:
                self._cache.close()
            except Exception:  # noqa: BLE001
                pass
            self._cache = None
        if getattr(self, "_cache_r", None) is not None:
            try:
                self._cache_r.close()
            except Exception:  # noqa: BLE001
                pass
            self._cache_r = None
        if self.cache_path:
            path = self.cache_path
            for _ in range(3):
                try:
                    os.remove(path)
                    break
                except OSError:
                    time.sleep(0.3)
            else:
                try:
                    log.warning("vod splitter: cache %s still locked "
                                "after 3 tries — left for the startup "
                                "sweep", path)
                except Exception:
                    pass
            self.cache_path = None
        self.cache_size = 0
        self._head = b""

    def _probe_head(self) -> bool:
        """One tiny ranged GET (bytes 0-11): the EBML magic (MKV) or an
        ftyp box at offset 4 (MP4) plus the total size from
        Content-Range. Sets ``self._container``/``_content_type``."""
        try:
            req = urllib.request.Request(
                self.url, headers={"User-Agent": self.ua,
                                   "Range": "bytes=0-11"})
            # 10 s for a 12-byte ranged GET is already generous; this runs
            # synchronously on the UI thread inside VodRelay.start, so a
            # hanging (not fast-erroring) debrid URL used to freeze the
            # open path for the old 20 s before playback even began
            with urllib.request.urlopen(req, timeout=10) as r:
                head = r.read(12)
                cr = r.headers.get("Content-Range") or ""
                if "/" in cr:
                    try:
                        self.total = int(cr.split("/")[1])
                    except ValueError:
                        pass
            if head[:4] == _EBML_MAGIC:
                self._container = "mkv"
                self._content_type = "video/x-matroska"
            elif len(head) >= 8 and head[4:8] == _FTYP:
                self._container = "mp4"
                self._content_type = "video/mp4"
            else:
                tlog("probe: not an MKV or MP4 stream")
                return False
            tlog("probe: %s, total=%d", self._container, self.total)
            return True
        except Exception as exc:  # noqa: BLE001
            tlog("probe failed (%r)", exc)
            return False

    def _bootstrap_tail(self):
        """VLC's MKV demuxer opens a SECOND connection asking for the
        file END (the seek-index). Serving that from the one live
        connection would rebase away the main stream and kill playback —
        so prefetch the tail first, sequentially (probe -> tail -> main;
        never two connections at once)."""
        if not self.total:
            return
        try:
            tail_len = min(self.total, _TAIL_PREFETCH)   # cues live in the
            #                                          # last MBs; 4 MB so
            #                                          # VLC's whole index
            #                                          # GET is served from
            #                                          # the prefetch
            req = urllib.request.Request(
                self.url, headers={
                    "User-Agent": self.ua,
                    "Range": f"bytes={self.total - tail_len}-"})
            with urllib.request.urlopen(req, timeout=30) as r:
                self._tail = r.read()
            self._tail_base = self.total - len(self._tail)
            try:
                log.info("vod splitter: tail prefetched %d bytes at %d",
                         len(self._tail), self._tail_base)
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            try:
                log.debug("vod splitter: tail prefetch skipped (%r)", exc)
            except Exception:
                pass

    # ---- provider slot ----
    def bump_gen(self) -> int:
        self._gen += 1
        return self._gen

    def _acquire(self, offset: int, owner=None):
        """The single provider slot: reuse a matching live stream, else
        close it and open one at ``offset`` (never two at once). A stream
        already being read by another handler is closed and replaced:
        two threads reading one response interleave bytes and corrupt
        the cache."""
        if not self._alive:
            return None       # a stopped relay opens no provider streams
        with self._lock:
            st = self._stream
            if st is not None and not st.dead and st.offset == offset \
                    and st.owner is None:
                tlog("acquire @%d: reuse", offset)
                st.owner = owner
                return st
            if st is not None:
                tlog("acquire @%d: replace (old @%d dead=%s owned=%s)",
                     offset, st.offset, st.dead, st.owner is not None)
                st.close()
            if st is None or st.offset != offset:
                self._rebase(offset)
            try:
                st = _ProviderStream(self, offset)
                self.provider_opens += 1
                status = getattr(st.resp, "status", 206)
                if offset > 0 and status == 200:
                    # provider ignored our Range — reading it would count
                    # from 0 while we claim offset N: refuse, never corrupt
                    st.close()
                    return None
                # remember the total for Range end math
                cl = st.resp.headers.get("Content-Length")
                cr = st.resp.headers.get("Content-Range")
                if cr and "-" in cr and "/" in cr:
                    try:
                        self.total = int(cr.split("/")[1])
                    except ValueError:
                        pass
                elif cl and offset == 0:
                    try:
                        self.total = int(cl)
                    except ValueError:
                        pass
            except Exception as exc:  # noqa: BLE001
                try:
                    log.warning("vod splitter: provider open at %d: %r",
                                offset, exc)
                except Exception:
                    pass
                return None
            if st is not None:
                st.owner = owner
            self._stream = st
            return st

    def _rebase(self, offset: int):
        """Drop the cached prefix; the window restarts at ``offset``
        (forward jumps must not starve the subtitle tap)."""
        tlog("rebase: window [%d +\u00d7%d) -> base %d",
             self.cache_base, self.cache_size, offset)
        with self._cache_lock:
            try:
                self._cache.seek(0)
                self._cache.truncate()
            except Exception:  # noqa: BLE001
                pass
            self.cache_base = offset
            self.cache_size = 0
        self.cache_gen += 1

    def wait_cache(self, pos: int, timeout: float) -> bool:
        """A sibling handler is fetching the bytes at ``pos`` on the one
        provider stream. Wait for them to land in the cache (or for the
        slot to free up) instead of killing its connection. True -> retry
        the serve loop; False -> take the slot over the old way."""
        deadline = time.monotonic() + timeout
        while True:
            if self.cache_base + self.cache_size > pos:
                return True
            cur = self._stream
            if cur is None or cur.dead or cur.owner is None:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _release(self, st, owner):
        """Handler teardown: drop stream ownership under the slot lock, so
        a concurrent _acquire reuses the live stream instead of paying a
        provider reopen."""
        if st is None:
            return
        with self._lock:
            if st.owner == owner:
                st.owner = None

    def append_cache(self, data: bytes):
        if self._cache is None:
            return
        try:
            with self._cache_lock:
                self._cache.seek(0, os.SEEK_END)
                self._cache.write(data)
                self._cache.flush()
                self.cache_size += len(data)
        except Exception:  # noqa: BLE001
            pass

    def read_cache(self, offset: int, n: int) -> bytes:
        if self._cache_r is None:
            return b""
        if not (self.cache_base <= offset
                < self.cache_base + self.cache_size):
            return b""
        try:
            with self._cache_lock:
                self._cache_r.seek(offset - self.cache_base)
                return self._cache_r.read(n)
        except Exception:  # noqa: BLE001
            return b""

    def _tap_read(self, offset: int, n: int) -> bytes:
        """The MP4 tap's sample reader: cache first, then the prefetched
        head and tail. Bytes in the tail region are served to VLC from
        _tail (never streamed through the cache), so without this
        fallback the tap would never see text samples that live there —
        ALL of them when a small file fits inside the tail prefetch. The
        head region likewise: a window starting past 0 (resume /
        mid-movie engage) never re-caches bytes 0..len(_head). Regions
        are stitched only where contiguous in file space (a sample may
        straddle the cache->tail seam); a read that starts in, or runs
        into, an uncached HOLE returns just the readable prefix — never
        bytes from the wrong file position."""
        out = bytearray()
        while len(out) < n:
            if self.cache_base <= offset \
                    < self.cache_base + self.cache_size:
                take = self.read_cache(offset, n - len(out))
            elif self._head and offset < len(self._head):
                take = self._head[offset:offset + (n - len(out))]
            elif self._tail_base >= 0 and self._tail_base <= offset \
                    < self._tail_base + len(self._tail):
                i = offset - self._tail_base
                take = self._tail[i:i + (n - len(out))]
            else:
                break
            if not take:
                break
            out += take
            offset += len(take)
        return bytes(out)

    # ---- subtitle tap (streams the LOCAL cache into the parsers) ----
    def _start_ffmpeg_tap(self, mid_stream: bool = False):
        """Launch the tap thread. ``mid_stream`` starts the MKV parser in
        cluster-resync mode with the head-fetched track metadata injected —
        used when the cache window begins past byte 0 (resume / mid-movie
        subtitle engage), where no Tracks element will ever pass by."""
        if self._container == "mp4":
            self._parser = Mp4SubParser(prefer_language=self._prefer)
            threading.Thread(target=self._tap_cache_mp4, daemon=True,
                             name="mtp-tap").start()
            return
        parser = MkvSubParser(prefer_language=self._prefer,
                              mid_stream=mid_stream,
                              timecode_scale_ns=self.parser_scale_ns)
        if mid_stream and self.parser_tracks_meta:
            parser._track_meta = {num: dict(m) for num, m
                                  in self.parser_tracks_meta.items()}
            parser._select_track()
            parser._saw_tracks = True
        self._parser = parser
        threading.Thread(target=self._tap_cache, daemon=True,
                         name="mtp-tap").start()

    def set_prefer_language(self, prefer: str) -> bool:
        """User picked a different subtitle language in the CC menu: the
        tap re-anchors a fresh parser a bounded distance behind the write
        frontier on the next loop pass (instant cues at the playback
        position) and a backfill thread re-parses the window from its
        start so rewinds keep the new language. Returns True when the
        preference actually changed (the caller drops the old language's
        cues then)."""
        prefer = (prefer or "").lower()
        if prefer and prefer != self._prefer:
            self._prefer = prefer
            self._tap_restart = True
            return True
        return False

    def _tap_cache(self):
        """Thread: tail the cache from byte 0, feeding the streaming MKV
        parser; emit each completed cue on the file's own timeline."""
        import time
        pos = 0
        base = self.cache_base
        parser = self._parser
        while self._alive or pos < self.cache_size:
            if self._tap_restart:
                self._tap_restart = False
                self._tail_harvested = False   # re-harvest in the new
                #                                   # language below
                tlog("tap restart: prefer=%r cache_base=%d cache_size=%d",
                     self._prefer, self.cache_base, self.cache_size)
                # fresh parser with the new preference, re-anchored a
                # bounded distance behind the frontier so cues around the
                # CURRENT playback position (which trails the frontier by
                # VLC's read-ahead) re-appear immediately. The head
                # metadata (from the startup head fetch, or parsed before
                # a rebase) feeds the selection — the window itself no
                # longer contains the Tracks element at offset > 0.
                keep = dict(self.parser_tracks_meta) \
                    if self.parser_tracks_meta else \
                    (dict(parser._track_meta) if parser._track_meta
                     else None)
                self.parser_scale_ns = getattr(parser, "_tc_scale_ns",
                                               self.parser_scale_ns)
                pos = max(0, self.cache_size - _TAP_RESTART_BACK_BYTES)
                parser = self._parser = MkvSubParser(
                    prefer_language=self._prefer,
                    mid_stream=(self.cache_base > 0 or pos > 0),
                    timecode_scale_ns=self.parser_scale_ns)
                if keep:
                    parser._track_meta = {n: dict(m)
                                          for n, m in keep.items()}
                    parser._select_track()
                    parser._saw_tracks = True
                # rewind coverage: re-parse the whole window from its
                # start on a side thread (re-emitted cues dedupe
                # downstream on (start, text))
                threading.Thread(target=self._tap_backfill,
                                 args=(self._prefer, keep, self.cache_base),
                                 daemon=True,
                                 name="mtp-tap-backfill").start()
            if self.cache_base != base:
                base = self.cache_base
                pos = 0
                tlog("tap rebase -> %d (meta kept: %d tracks)",
                     self.cache_base, len(self.parser_tracks_meta))
                keep_sel = parser._selected
                keep_meta = dict(self.parser_tracks_meta) \
                    if self.parser_tracks_meta else \
                    (dict(parser._track_meta) if parser._track_meta
                     else None)
                self.parser_scale_ns = getattr(parser, "_tc_scale_ns",
                                               self.parser_scale_ns)
                parser = self._parser = MkvSubParser(
                    prefer_language=self._prefer, mid_stream=True,
                    timecode_scale_ns=self.parser_scale_ns)
                if keep_meta:
                    parser._track_meta = {n: dict(m)
                                          for n, m in keep_meta.items()}
                    parser._select_track()
                    parser._saw_tracks = True
                    if keep_sel is not None:
                        parser._selected = keep_sel
            if parser._track_meta and (
                    len(parser._track_meta) != len(self.parser_tracks_meta)
                    or self.parser_selected != parser._selected):
                # snapshot for re-selections after a rebase + for the UI's
                # text-track check (PlayerView._cap_vod_check)
                self.parser_tracks_meta = dict(parser._track_meta)
                self.parser_tracks = {n: m["codec"]
                                      for n, m in parser._track_meta.items()}
                self.parser_selected = parser._selected
            if parser._track_meta:
                self.parser_scale_ns = parser._tc_scale_ns
            if self._tail and self._tail_base >= 0 \
                    and not self._tail_harvested \
                    and (self.parser_tracks_meta or parser._track_meta):
                # One-shot harvest of the prefetched tail: VLC's reads of
                # the tail region are served straight from _tail and NEVER
                # enter the cache, so the sequential parse above can never
                # reach those clusters — captions stopped for the last
                # _TAIL_PREFETCH bytes of every MKV, and a seek landing
                # inside the region never rebased the window (tail bytes
                # cost no provider stream), leaving the tap anchored
                # elsewhere with its parser starved. Cluster timecodes are
                # absolute, so a mid-stream parser resynced inside the
                # tail emits cues on the file's own clock; re-emitted cues
                # dedupe downstream on (start, text).
                self._tail_harvested = True
                meta = {n: dict(m) for n, m in
                        (self.parser_tracks_meta or parser._track_meta)
                        .items()}
                threading.Thread(
                    target=self._tap_tail_harvest,
                    args=(self._prefer, meta, self.parser_scale_ns),
                    daemon=True, name="mtp-tap-tail").start()
            if pos < self.cache_size:
                data = self.read_cache(self.cache_base + pos, _CHUNK)
                if data:
                    pos += len(data)
                    try:
                        made = parser.feed(data)
                    except Exception as exc:  # noqa: BLE001
                        # a dead tap loses captions, never playback —
                        # the UI falls back to VLC's own rendering
                        self.failed.emit(f"tap parser crashed: {exc!r}")
                        break
                    for cue in made:
                        tlog("tap cue @%.1f-%.1f %r", cue[0], cue[1],
                             cue[2][:30])
                        self.cue.emit(*cue)
                    continue
            elif not self._alive:
                break
            time.sleep(0.25)
        tlog("tap thread exit: alive=%r pos=%d cache_size=%d "
             "selected=%r track_meta=%d", self._alive, pos,
             self.cache_size, getattr(parser, "_selected", None),
             len(getattr(parser, "_track_meta", {}) or {}))

    def _tap_backfill(self, prefer: str, meta, base: int):
        """Language-switch backfill: re-parse the cached window from its
        START with the new preference so rewinds find the new track's
        cues (the live tap only re-anchored near the frontier). Aborts if
        the window rebases (a seek moved it) — the live tap owns the new
        window then. Window-relative reads only; no provider traffic."""
        import time
        parser = MkvSubParser(prefer_language=prefer, mid_stream=base > 0,
                              timecode_scale_ns=self.parser_scale_ns)
        if meta:
            parser._track_meta = {n: dict(m) for n, m in meta.items()}
            parser._select_track()
            parser._saw_tracks = True
        pos = 0
        gen = self.cache_gen
        while self._alive and pos < self.cache_size:
            if self.cache_base != base or self.cache_gen != gen:
                tlog("tap backfill aborted: window rebased")
                return
            data = self.read_cache(base + pos, _CHUNK)
            if not data:
                time.sleep(0.2)
                continue
            pos += len(data)
            try:
                made = parser.feed(data)
            except Exception:  # noqa: BLE001
                return
            for cue in made:
                self.cue.emit(*cue)
        tlog("tap backfill done: %d bytes @%d", pos, base)

    def _tap_tail_harvest(self, prefer: str, meta, scale_ns: int):
        """One-shot side parse of the prefetched tail region (spawned by
        _tap_cache once track metadata exists; re-run on a language
        switch). Window-independent — _tail is a prefetch snapshot that
        never rebases — and costs no provider traffic."""
        import time
        parser = MkvSubParser(prefer_language=prefer, mid_stream=True,
                              timecode_scale_ns=scale_ns)
        if meta:
            parser._track_meta = {n: dict(m) for n, m in meta.items()}
            parser._select_track()
            parser._saw_tracks = True
        pos = 0
        tail = self._tail
        start = _snap_cluster(tail)
        if start < 0:
            tlog("tail harvest: no cluster header found in %d bytes",
                 len(tail))
            return
        tlog("tail harvest: snapped to cluster @%d (region %d bytes)",
             start, len(tail))
        while self._alive and start + pos < len(tail):
            data = tail[start + pos:start + pos + _CHUNK]
            pos += len(data)
            try:
                made = parser.feed(data)
            except Exception:  # noqa: BLE001
                return
            for cue in made:
                tlog("tail cue @%.1f-%.1f %r", cue[0], cue[1],
                     cue[2][:30])
                self.cue.emit(*cue)
        tlog("tail harvest done: %d bytes @%d selected=%r", pos,
             self._tail_base + start, parser._selected)

    def _tap_cache_mp4(self):
        """Thread: the MP4 tap. The moov index comes from the prefetched
        tail (streaming-layout files keep it at the end), the prefetched
        head (faststart files), or the cache head as it streams; text
        samples are then read from the cache window at their stco file
        offsets as the frontier passes them (see src.mp4_subs for why
        this beats a moov-less walk over mdat). Cue times are
        file-absolute media times = VLC's playback clock; after a rebase
        the cursor rewinds and re-emitted cues dedupe downstream — the
        same contract as the MKV tap."""
        import time
        parser = self._parser
        if self._tail:
            parser.parse_tail(self._tail)
        if not parser.have_index and self._head:
            parser.parse_head_bytes(self._head)
        base = self.cache_base
        tail_tried = bool(self._tail)
        head_tried = True
        while self._alive:
            if self._tap_restart:
                self._tap_restart = False
                parser.reselect(self._prefer)
            if self.cache_base != base:
                base = self.cache_base
                parser.rewind(base)   # index + selection carried over
            if not parser.have_index:
                # the startup thread may still be prefetching the tail —
                # retry both static sources as they land
                if not tail_tried and self._tail:
                    tail_tried = True
                    parser.parse_tail(self._tail)
                if not parser.have_index and self._head:
                    parser.parse_head_bytes(self._head)
                if not parser.have_index and self.cache_base == 0:
                    parser.scan_head(self.read_cache, self.cache_base,
                                     self.cache_size)
            if parser._track_meta and (
                    len(parser._track_meta) != len(self.parser_tracks_meta)
                    or self.parser_selected != parser._selected):
                # snapshot for the UI's text-track check
                # (PlayerView._cap_vod_check) and CC-menu re-selections
                self.parser_tracks_meta = dict(parser._track_meta)
                self.parser_tracks = {n: m["codec"]
                                      for n, m in parser._track_meta.items()}
                self.parser_selected = parser._selected
            try:
                made = parser.extract(self._tap_read, self.cache_base,
                                      self.cache_size, self._tail_base,
                                      len(self._tail))
            except Exception as exc:  # noqa: BLE001
                # a dead tap loses captions, never playback — the UI
                # falls back to VLC's own rendering
                self.failed.emit(f"tap parser crashed: {exc!r}")
                break
            for cue in made:
                self.cue.emit(*cue)
            time.sleep(0.25)
        # death drain: pull whatever the final window holds
        if parser.have_index:
            try:
                for cue in parser.extract(self._tap_read, self.cache_base,
                                          self.cache_size,
                                          self._tail_base,
                                          len(self._tail)):
                    self.cue.emit(*cue)
            except Exception:  # noqa: BLE001
                pass

    def _kill_ffmpeg(self):
        self._parser = None

    # ---- HTTP relay ----
    def _make_handler(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):      # silence
                pass

            def _headers(self, code, length, rng=None):
                self.send_response(code)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", relay._content_type)
                if rng is not None:
                    self.send_header("Content-Range", rng)
                if length is not None:
                    self.send_header("Content-Length", str(length))
                else:
                    self.close_connection = True   # no length -> no keepalive
                self.end_headers()

            def do_HEAD(self):
                tlog("HEAD")
                total = relay.total or relay.cache_size
                if not total:
                    self._headers(200, None)
                    return
                self._headers(200, total)

            def do_GET(self):
                tlog("GET %s (ua=%s)",
                     self.headers.get("Range") or "-",
                     (self.headers.get("User-Agent") or "")[:24])
                # startup (head fetch -> tail prefetch -> main acquire)
                # runs off the UI thread; VLC simply buffers while the
                # first bytes of the tail download land
                if not relay._ready.is_set():
                    deadline = time.monotonic() + 30.0
                    while (not relay._ready.is_set() and relay._alive
                           and time.monotonic() < deadline):
                        time.sleep(0.1)
                    if (not relay._ready.is_set() or relay._startup_failed
                            or not relay._alive):
                        self._headers(503, 0)
                        return
                a, b = self._range()
                total = relay.total
                if a is None:
                    a = 0
                    if b is None:
                        self._headers(200, total or None)
                        self._stream_out(a, None)
                        return
                if b is None:
                    b = total - 1 if total else None
                if total and (a >= total or (b is not None and b >= total)):
                    b = min(b, total - 1) if b is not None else total - 1
                    if a >= total:
                        self._headers(416, 0,
                                      f"bytes */{total}")
                        return
                if b is not None:
                    length = b - a + 1
                    rng = (f"bytes {a}-{b}/{total}" if total
                           else f"bytes {a}-{b}/*")
                    self._headers(206, length, rng)
                    self._stream_out(a, b)
                else:
                    self._headers(206, None, f"bytes {a}-/*")
                    self._stream_out(a, None)

            def _range(self):
                r = self.headers.get("Range")
                if not r or not r.startswith("bytes="):
                    return None, None
                spec = r[len("bytes="):].strip()
                if "-" not in spec:
                    return None, None
                a_s, b_s = spec.split("-", 1)
                try:
                    if a_s == "":
                        if not relay.total:
                            return None, None
                        n = int(b_s)
                        return max(0, relay.total - n), relay.total - 1
                    return int(a_s), (int(b_s) if b_s else None)
                except ValueError:
                    return None, None

            def _stream_out(self, a, b):
                pos = a
                me = id(self)
                st = None            # held across chunks: re-acquiring
                #                     # per chunk closed our own healthy
                #                     # stream and reopened the provider
                src = ""             # last source served from (trace only)
                why = "range-end"
                fails = 0            # consecutive provider failures that
                #                     # served no bytes (open error or a
                #                     # zero-byte body); any byte refills
                served = False       # the current stream delivered bytes
                reopens = 0
                try:
                    while b is None or pos <= b:
                        if relay.cache_base <= pos \
                                < relay.cache_base + relay.cache_size:
                            data = relay.read_cache(
                                pos, _CHUNK if b is None
                                else min(_CHUNK, b - pos + 1))
                            if not data:
                                why = "cache-miss"
                                break
                            if src != "cache":
                                tlog("serve @%d -> cache [%d +%d)",
                                     pos, relay.cache_base, relay.cache_size)
                                src = "cache"
                            self.wfile.write(data)
                            pos += len(data)
                            continue
                        if relay._head and pos < relay.cache_base \
                                and pos < len(relay._head):
                            # prefetched head bytes (offset-0 sessions,
                            # via _ride_head): once a seek rebases the
                            # window PAST the head, VLC's re-probes of the
                            # EBML header / tracks must not rebase the
                            # window back to 0. Resume sessions carry no
                            # _head — their walk GET caches the head
                            # region for real instead.
                            n = (len(relay._head) - pos) if b is None \
                                else min(len(relay._head) - pos,
                                         b - pos + 1)
                            if src != "head":
                                tlog("serve @%d -> head +%d", pos,
                                     len(relay._head))
                                src = "head"
                            self.wfile.write(
                                relay._head[pos:pos + n])
                            pos += n
                            continue
                        if relay._tail_base >= 0 \
                                and pos >= relay._tail_base:
                            # tail (seek-index) region: prefetched bytes
                            off = pos - relay._tail_base
                            n = (len(relay._tail) - off) if b is None \
                                else min(len(relay._tail) - off,
                                         b - pos + 1)
                            if n <= 0:
                                why = "tail-end"
                                break
                            if src != "tail":
                                tlog("serve @%d -> tail @%d +%d",
                                     pos, relay._tail_base,
                                     len(relay._tail))
                                src = "tail"
                            self.wfile.write(relay._tail[off:off + n])
                            pos += n
                            continue
                        if st is not None and st.gen != relay._gen:
                            why = "stale"   # another handler owns the slot
                            break           # now — our bytes come via the
                                           # cache
                        if st is not None and st.dead:
                            # the provider cut the body mid-serve (CDN
                            # connection-age cap, idle reap during a long
                            # VLC pause) — or it ended naturally at the
                            # file's end
                            if relay.total and pos >= relay.total:
                                why = "provider-eof"
                                break
                            if served:
                                fails = 0   # a body that delivered is a
                                            # healthy ride, not a failure
                            else:
                                fails += 1
                            if fails >= _REOPEN_FAIL_BUDGET:
                                why = "provider-dead"
                                log.warning(
                                    "vod splitter: provider dead at %d — "
                                    "cutting after %d failed reopens",
                                    pos, fails)
                                break
                            reopens += 1
                            tot = (" of %d" % relay.total) \
                                if relay.total else ""
                            log.info(
                                "vod splitter: provider body died at %d%s"
                                " — reopening (%d)", pos, tot, reopens)
                            st = None
                            src = ""
                            served = False
                            time.sleep(_REOPEN_BACKOFF_S)
                            continue
                        if st is None:
                            cur = relay._stream
                            if cur is not None and not cur.dead \
                                    and cur.owner is not None \
                                    and cur.offset >= pos >= relay.cache_base \
                                    and relay.wait_cache(pos, 1.5):
                                # a sibling handler is fetching these very
                                # bytes — trail it via the cache instead of
                                # replacing its connection
                                continue
                            st = relay._acquire(pos, owner=me)
                            if st is None:
                                fails += 1
                                if fails >= _REOPEN_FAIL_BUDGET \
                                        or not relay._alive:
                                    why = "provider-fail"
                                    log.warning(
                                        "vod splitter: provider open at %d "
                                        "failed %dx — cutting", pos, fails)
                                    break
                                time.sleep(_REOPEN_BACKOFF_S)
                                continue
                            served = False
                            if src != "provider":
                                tlog("serve @%d -> provider", pos)
                                src = "provider"
                        data = st.read_some(_CHUNK if b is None
                                            else min(_CHUNK, b - pos + 1))
                        if not data:
                            continue    # the dead-branch above decides:
                                        # natural EOF, reopen, or cut
                        served = True
                        self.wfile.write(data)
                        pos += len(data)
                except Exception as exc:  # noqa: BLE001
                    why = f"exc {exc!r}"
                finally:
                    relay._release(st, me)
                tlog("serve done at %d (%s)", pos, why)
                try:
                    self.wfile.flush()
                except Exception:  # noqa: BLE001
                    pass

        return Handler

