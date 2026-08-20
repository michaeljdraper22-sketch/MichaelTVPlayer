# -*- coding: utf-8 -*-
"""VOD splitter: one provider connection, subtitle text for the filter.

Movies & series subtitle tracks are PRE-TIMED (the cue exists before its
dialogue plays), so — unlike live captions — they need no playback delay.
But VLC 3 hands the app no subtitle text, never forwards subs through its
stream output, and the account allows a single stream connection.

So the app inserts itself as a tiny local relay:

    provider ===ONE streaming GET===> cache file (temp .mkv)
                                      |            |
             local HTTP (ranges)      |            +--> streaming MKV parser
             serves VLC  <============+                  (subtitle cues)

VLC plays http://127.0.0.1:<port>/v — byte-identical to the original
(seeking works: range requests are served from the cache prefix, or by
restarting the single provider connection at the new offset — never two
connections at once). The parser (src.mkv_subs) tails the LOCAL cache and
emits subtitle cues on the file's own timeline, which is exactly VLC's
playback clock: the filter sees each cue before its words play.
"""

import logging
import os
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PyQt5 import QtCore

from .mkv_subs import MkvSubParser

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

# Seek pacing: the serving handler HOLDS the one provider stream across
# chunks (re-acquiring per chunk used to close its own live connection
# and reopen the provider every 64 KiB — seeks stalled for 40+ s), and
# overlapping requests trail the owner through the cache instead of
# replacing it. Real-provider seeks resume in ~2 s (one provider reopen,
# the rest is VLC's own re-buffer).
VOD_SPLITTER_READY = True


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
        #                            # UI reads this to learn which tracks
        #                            # are real text (S_TEXT/UTF8)
        self.parser_tracks_meta = {}   # full {number: {codec,lang,name}}
        self.parser_selected = None

    # ---- lifecycle ----
    def start(self, url: str, ua: str, prefer_language: str = "eng") -> str:
        """Open the provider, verify MKV, bring the relay up. Returns the
        local URL for VLC, or '' when the input isn't MKV (caller falls
        back to playing the original URL). ``prefer_language`` steers the
        tap's subtitle-track choice (the user's CC-menu selection)."""
        self.stop()
        self.url = url
        self.ua = ua or self.ua
        self._prefer = (prefer_language or "eng").lower() or "eng"
        self._tap_restart = False
        fd, self.cache_path = tempfile.mkstemp(suffix=".mkv",
                                               prefix="mtp_split_")
        os.close(fd)
        try:
            # SEPARATE handles: one shared pointer would let the tap
            # reader's seeks hijack the append position (that corrupted
            # the cache)
            self._cache = open(self.cache_path, "r+b")    # writer
            self._cache_r = open(self.cache_path, "rb")   # reader
            self.cache_size = 0
            # ONE 4-byte request settles MKV-or-not + the total size.
            # Non-MKV containers (mp4…) are refused HERE — before the
            # 2 MB tail prefetch, so the direct-playback fallback costs a
            # single tiny connection instead of three plus a wasted tail
            # download.
            if not self._probe_head():
                self.stop()
                self.failed.emit("not an MKV stream")
                return ""
            self._bootstrap_tail()
            st = self._acquire(0)
            if st is None:
                self.stop()
                self.failed.emit("provider open failed")
                return ""
            head = st.read(4)
            if len(head) < 4 or head != _EBML_MAGIC:
                self.stop()
                self.failed.emit("not an MKV stream")
                return ""
            # the 4 magic bytes were already appended to the cache by
            # st.read
            self._alive = True      # BEFORE the tap: it must survive the
            self._start_ffmpeg_tap()  # startup window with a near-empty cache
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
                log.info("vod splitter: %s -> 127.0.0.1:%d (total=%d)",
                         url, self._server.server_address[1], self.total)
            except Exception:
                pass
            return f"http://127.0.0.1:{self._server.server_address[1]}/v"
        except Exception as exc:  # noqa: BLE001
            # The known aborts above stop themselves; this catches the
            # UNPLANNED ones (e.g. a network error inside _bootstrap_tail)
            # so an unexpected raise can never strand the cache file.
            self.stop()
            self.failed.emit(f"splitter failed: {exc}")
            return ""

    def stop(self):
        self._alive = False
        if self._server is not None:
            try:
                self._server.shutdown()
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

    def _probe_head(self) -> bool:
        """One tiny ranged GET (bytes 0-3): the EBML magic plus the total
        size from Content-Range."""
        try:
            req = urllib.request.Request(
                self.url, headers={"User-Agent": self.ua,
                                   "Range": "bytes=0-3"})
            with urllib.request.urlopen(req, timeout=20) as r:
                head = r.read(4)
                cr = r.headers.get("Content-Range") or ""
                if "/" in cr:
                    try:
                        self.total = int(cr.split("/")[1])
                    except ValueError:
                        pass
            if head != _EBML_MAGIC:
                tlog("probe: not an MKV stream")
                return False
            tlog("probe: MKV, total=%d", self.total)
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
            tail_len = min(self.total, 2 << 20)   # cues live in the last MBs
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

    # ---- subtitle tap (streams the LOCAL cache into the MKV parser) ----
    def _start_ffmpeg_tap(self):
        self._parser = MkvSubParser(prefer_language=self._prefer)
        threading.Thread(target=self._tap_cache, daemon=True,
                         name="mtp-tap").start()

    def set_prefer_language(self, prefer: str):
        """User picked a different subtitle language in the CC menu: the
        tap re-selects its track on the next loop pass (the current cache
        window is re-parsed from its start with the new preference; cue
        times are file-absolute, so re-emitted cues dedupe downstream)."""
        prefer = (prefer or "").lower()
        if prefer and prefer != self._prefer:
            self._prefer = prefer
            self._tap_restart = True

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
                # fresh parser with the new preference. When the window
                # still starts at the file head the Tracks element is
                # re-parsed normally; after a rebase the metadata snapshot
                # (captured below) feeds the selection instead.
                at_head = (self.cache_base == 0)
                keep = dict(self.parser_tracks_meta) \
                    if not at_head and self.parser_tracks_meta else None
                parser = MkvSubParser(prefer_language=self._prefer,
                                      mid_stream=not at_head)
                if keep:
                    parser._track_meta = {n: dict(m)
                                          for n, m in keep.items()}
                    parser._select_track()
                    parser._saw_tracks = True
                self._parser = parser
                pos = 0
            if self.cache_base != base:
                base = self.cache_base
                pos = 0
                keep_sel = parser._selected
                parser = self._parser = MkvSubParser(mid_stream=True)
                if keep_sel is not None:
                    # track metadata lives at the file head — carry the
                    # selection over so the rebased parser keeps matching
                    parser._selected = keep_sel
                    parser._saw_tracks = True
            if parser._track_meta and \
                    len(parser._track_meta) != len(self.parser_tracks_meta):
                # snapshot for re-selections after a rebase + for the UI's
                # text-track check (PlayerView._cap_vod_check)
                self.parser_tracks_meta = dict(parser._track_meta)
                self.parser_tracks = {n: m["codec"]
                                      for n, m in parser._track_meta.items()}
                self.parser_selected = parser._selected
            if pos < self.cache_size:
                data = self.read_cache(self.cache_base + pos, _CHUNK)
                if data:
                    pos += len(data)
                    for cue in parser.feed(data):
                        self.cue.emit(*cue)
                    continue
            elif not self._alive:
                break
            time.sleep(0.25)
        try:
            for cue in parser.flush():
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
                self.send_header("Content-Type", "video/x-matroska")
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
                        if st is not None and (st.dead
                                               or st.gen != relay._gen):
                            why = "stale" if st.gen != relay._gen \
                                else "provider-eof"
                            break
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
                                why = "provider-fail"
                                break
                            if src != "provider":
                                tlog("serve @%d -> provider", pos)
                                src = "provider"
                        data = st.read_some(_CHUNK if b is None
                                            else min(_CHUNK, b - pos + 1))
                        if not data:
                            why = "provider-eof"
                            break
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

