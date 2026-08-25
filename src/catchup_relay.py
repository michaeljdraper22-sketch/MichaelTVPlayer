"""Local range-proxy relay for catch-up (timeshift) streams.

The provider's timeshift endpoint serves valid MPEG-TS and honors HTTP
Range requests, but its responses carry a malformed ``Accept-Ranges``
header (``0-<size>`` instead of the literal ``bytes`` token), so libVLC
marks the stream NON-SEEKABLE: no duration, no scrub bar, dead seeks
(measured 2026-08-25 — get_length()=0, set_time() no-ops, on three
channels).

This relay terminates VLC on localhost with fully standards-correct
headers (``Accept-Ranges: bytes``, Content-Length, 206 +
Content-Range) and forwards each client request — range or whole-file —
to a fresh provider connection.  VLC then scrubs normally.

Deliberately dumb: no parsing, no caching, no subtitle peeling (the
VOD splitter in vod_splitter.py owns that job for MKV/MP4).  Playback
must never depend on it — on any startup failure the caller falls back
to the provider URL directly (plays fine, just without the scrub bar).
"""

import logging
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("mtp")

_COPY_CHUNK = 1 << 18          # 256 KB relay chunks
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
# This provider's timeshift backend REAPS sibling connections: while a
# second timeshift URL is open for the same account (e.g. a window
# download), one of the two gets killed mid-body ~15-30 s in — measured
# 2026-08-25, full-speed AND throttled downloads alike, 4 kills in 6
# trials; a lone playback connection ran 200 MB uninterrupted. When that
# happens to the playback side, the EOF lands short of Content-Length —
# so the relay re-dials the provider from the exact next byte and keeps
# filling the SAME client response. VLC never notices.
_RESUME_BACKOFF_S = 0.75       # pause between reconnect attempts
_RESUME_STALLS = 4             # consecutive ZERO-byte dials before giving
#                              # up (provider truly gone — VLC's watchdog
#                              # then reopens the media)
_RESUME_CREEP_N = 6            # ...and this many attempts must deliver at
_RESUME_CREEP_BYTES = 1 << 16  # least this many bytes in total, else the
#                              # re-dials are starving the client anyway


class CatchupRelay:
    """Owns the localhost HTTP server proxying one timeshift URL."""

    def __init__(self):
        self.url = ""
        self.ua = "MichaelTVPlayer/1.0"
        self.total = 0                 # provider Content-Length (bytes)
        self.provider_opens = 0        # diagnostics: provider GETs served
        self._server = None
        self._alive = False
        self._streams = set()          # open provider responses
        self._streams_lock = threading.Lock()

    # ---- lifecycle ----
    def start(self, url: str, ua: str = "MichaelTVPlayer/1.0") -> str:
        """Probe the stream size, bring the relay up.  Returns the local
        URL for VLC, or '' on any failure (caller plays the original)."""
        self.stop()
        self.url = url
        self.ua = ua or self.ua
        try:
            if not self._probe_size():
                return ""
            self._alive = True
            self._server = ThreadingHTTPServer(
                ("127.0.0.1", 0), self._make_handler())
            self._server.daemon_threads = True

            def _quiet_error(*_a, **_k):
                pass    # VLC drops probe connections routinely
            self._server.handle_error = _quiet_error
            threading.Thread(target=self._server.serve_forever,
                             daemon=True, name="mtp-catchup-relay").start()
        except Exception as exc:  # noqa: BLE001
            self.stop()
            try:
                log.warning("catchup relay: start failed (%r)", exc)
            except Exception:
                pass
            return ""
        try:
            log.info("catchup relay: %s -> 127.0.0.1:%d (total=%d)",
                     url, self._server.server_address[1], self.total)
        except Exception:
            pass
        return f"http://127.0.0.1:{self._server.server_address[1]}/c.ts"

    def stop(self):
        self._alive = False
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        with self._streams_lock:
            streams = list(self._streams)
            self._streams.clear()
        for st in streams:
            try:
                st.close()
            except Exception:  # noqa: BLE001
                pass

    def _probe_size(self) -> bool:
        """One tiny ranged GET settles the total size (Content-Range's
        ``bytes 0-0/TOTAL``) without downloading the stream."""
        try:
            req = urllib.request.Request(
                self.url, headers={"User-Agent": self.ua,
                                   "Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                cr = r.headers.get("Content-Range") or ""
                m = re.match(r"bytes\s+\d+-\d+/(\d+)", cr)
                if m:
                    self.total = int(m.group(1))
                else:
                    self.total = int(r.headers.get("Content-Length") or 0)
                r.read(1)
            if not self.total:
                try:
                    log.warning("catchup relay: no size probe on %s "
                                "(headers lacked ranges) — refusing",
                                self.url)
                except Exception:
                    pass
                return False
            self.provider_opens += 1
            return True
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("catchup relay: size probe failed (%r)", exc)
            except Exception:
                pass
            return False

    # ---- serving ----
    def _open_provider(self, start: int, end: int):
        """Open a provider connection for [start, end] (end=0 -> to EOF)."""
        headers = {"User-Agent": self.ua}
        if start or end:
            headers["Range"] = (f"bytes={start}-{end}" if end
                                else f"bytes={start}-")
        req = urllib.request.Request(self.url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=60)
        with self._streams_lock:
            if not self._alive:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
                return None
            self._streams.add(resp)
        self.provider_opens += 1
        return resp

    def _close_provider(self, resp):
        if resp is None:
            return
        with self._streams_lock:
            self._streams.discard(resp)
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass

    def _make_handler(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def do_GET(self):
                if not relay._alive:
                    self.close_connection = True
                    return
                start, end = 0, 0
                rng = self.headers.get("Range")
                if rng:
                    m = _RANGE_RE.match(rng.strip())
                    if m and m.group(1):
                        start = int(m.group(1))
                        if m.group(2):
                            end = int(m.group(2))
                if start >= relay.total:
                    self.send_response(416)
                    self.send_header(
                        "Content-Range", f"bytes */{relay.total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not end or end >= relay.total:
                    end = relay.total - 1
                length = end - start + 1
                try:
                    resp = relay._open_provider(start, end)
                except Exception:  # noqa: BLE001
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if resp is None:
                    self.close_connection = True
                    return
                try:
                    self.send_response(206 if rng else 200)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(length))
                    if rng:
                        self.send_header(
                            "Content-Range",
                            f"bytes {start}-{end}/{relay.total}")
                    self.end_headers()
                    # The body may span SEVERAL provider connections: the
                    # CDN kills timeshift connections mid-body (see the
                    # _RESUME_* constants above), and an EOF short of the
                    # promised length is bridged with a byte-exact Range
                    # re-dial — the client response is one continuous
                    # stream. A WRITE failure (client went away: seek,
                    # stop) is never retried.
                    sent = 0
                    dial_bytes = []          # bytes delivered per attempt
                    first = True
                    while True:
                        if not first:
                            time.sleep(_RESUME_BACKOFF_S)
                            try:
                                resp = relay._open_provider(start + sent,
                                                            end)
                            except Exception:  # noqa: BLE001
                                resp = None   # counts as a zero-delivery
                                               # attempt; the stall checks
                                               # below gate the retries
                        first = False
                        before = sent
                        if resp is not None:
                            try:
                                while sent < length:
                                    try:
                                        chunk = resp.read(
                                            min(_COPY_CHUNK, length - sent))
                                    except Exception:  # noqa: BLE001
                                        break   # killed mid-body
                                                # (IncompleteRead, reset)
                                    if not chunk:
                                        break   # clean EOF short of the
                                                # length (the CDN reaping)
                                    self.wfile.write(chunk)
                                    sent += len(chunk)
                            except Exception:  # noqa: BLE001
                                return       # client went away
                            relay._close_provider(resp)
                            resp = None
                            if dial_bytes and sent > before:
                                try:
                                    log.info(
                                        "catchup relay: provider cut the "
                                        "body at %d/%d — re-dial %d "
                                        "delivered +%d bytes",
                                        start + before, start + length,
                                        len(dial_bytes), sent - before)
                                except Exception:
                                    pass
                        if sent >= length or not relay._alive:
                            break
                        dial_bytes.append(sent - before)
                        # keep re-dialing while bytes keep flowing; the
                        # CDN's sibling-kill just makes this a moving
                        # window of ~15 s connections. Only truly dead
                        # providers (or a starved trickle) give up.
                        stalls = dial_bytes[-_RESUME_STALLS:]
                        if (len(stalls) >= _RESUME_STALLS
                                and not any(stalls)) \
                                or (len(dial_bytes) >= _RESUME_CREEP_N
                                    and sum(dial_bytes[-_RESUME_CREEP_N:])
                                    < _RESUME_CREEP_BYTES):
                            break
                    if sent < length:
                        # could not complete: the client must not wait on
                        # this keep-alive connection for the missing bytes
                        self.close_connection = True
                except Exception:  # noqa: BLE001 - client went away (seek)
                    pass
                finally:
                    relay._close_provider(resp)

        return Handler
