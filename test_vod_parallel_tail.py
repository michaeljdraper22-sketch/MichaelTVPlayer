# -*- coding: utf-8 -*-
"""Offscreen test for VodRelay(parallel_tail=True) — the Stremio-only mode.

Why it exists: on Stremio/debrid links several ranged GETs may run at once
(only LIVE TV is capped at one connection). The relay can therefore fetch
its 2.5 MB seek-index tail on its own connection while the main stream
opens and the 512 KB head parses, instead of the tail gating the first
byte. The invariants this test pins are the ones the serial design fought
for:

* the tail download NEVER goes through `_acquire` (the one-stream slot),
  so it cannot rebase/truncate the cache window;
* a VLC GET whose range starts inside the tail WAITS for the tail snapshot
  instead of acquiring the provider (no provider_opens bump, no main-body
  reset);
* the head is served while the tail is still downloading (`_ready` is set
  by the head parse, not by the tail);
* a failed tail falls through to the old acquire path instead of hanging;
* `stop()` during a slow tail returns promptly and unbinds waiters;
* resume sessions ignore the flag (serial order preserved).

Run:  .venv\\Scripts\\python.exe test_vod_parallel_tail.py
"""
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore  # noqa: E402

from src import vod_splitter  # noqa: E402
from src.vod_splitter import VodRelay  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("  [%s]" % detail) if detail else ""))


BLOB = b"\x1a\x45\xdf\xa3" + bytes((i * 31) & 0xFF
                                   for i in range(8 * 1024 * 1024 - 4))


class Srv(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, tail_delay=0.0, tail_fail=False):
        super().__init__(("127.0.0.1", 0), H)
        self.tail_delay = tail_delay
        self.tail_fail = tail_fail
        self.lock = threading.Lock()
        self.hits = []
        self.inflight = 0
        self.max_inflight = 0
        self.tail_hits = 0
        self._thread = threading.Thread(target=self.serve_forever,
                                        daemon=True)
        self._thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/movie.mkv" % self.server_address[1]


def _parse_range(rng, total):
    if not rng.startswith("bytes="):
        return 0, total - 1
    spec = rng[len("bytes="):].strip()
    try:
        a_s, b_s = spec.split("-", 1)
        if a_s == "":
            n = int(b_s)
            return max(0, total - n), total - 1
        return int(a_s), (int(b_s) if b_s else total - 1)
    except (ValueError, TypeError):
        return 0, total - 1


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        s = self.server
        total = len(BLOB)
        a, b = _parse_range(self.headers.get("Range") or "", total)
        a, b = max(0, a), min(total - 1, b)
        is_tail = a >= total - vod_splitter._TAIL_PREFETCH
        with s.lock:
            s.inflight += 1
            s.max_inflight = max(s.max_inflight, s.inflight)
            s.hits.append((a, b, self.headers.get("Range") or "", is_tail))
            if is_tail:
                s.tail_hits += 1
        try:
            # tail_fail = the FIRST tail-range GET errors (the parallel
            # prefetch); later tail requests (the fallback acquire) serve
            # normally, which is exactly what the fallback path needs.
            if is_tail and s.tail_fail and s.tail_hits == 1:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if is_tail and s.tail_delay:
                time.sleep(s.tail_delay)
            body = BLOB[a:b + 1]
            self.send_response(206)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, total))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass
        finally:
            with s.lock:
                s.inflight -= 1


def get(local, rng, timeout=20):
    req = urllib.request.Request(local, headers={"Range": rng,
                                                 "User-Agent": "VLC/3.0.24"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        status = r.status
    return status, data, time.monotonic() - t0


def main():
    app = QtCore.QCoreApplication([])

    # ---- [1] parallel: head served while the tail is still downloading --
    print("[1] parallel tail: first byte no longer waits for the tail")
    srv = Srv(tail_delay=1.5)
    relay = VodRelay(parallel_tail=True)
    local = relay.start(srv.url, "MichaelTVPlayer/1.0", "eng", 0)
    check("relay started", bool(local), local)
    status, head, dt = get(local, "bytes=0-65535")
    check("head range served byte-exact", status == 206 and head == BLOB[:65536])
    check("head served BEFORE the tail finished (%.2fs < 1.5s)" % dt, dt < 1.2,
          "%.2fs" % dt)
    check("_ready is set without the tail", relay._ready.is_set()
          and not relay._tail_ready.is_set())
    check("two provider GETs overlapped during startup",
          srv.max_inflight >= 2, "max=%d" % srv.max_inflight)
    opens_before = relay.provider_opens

    # the seek-index GET (VLC's second connection) must wait it out, then
    # be served from the prefetched tail — never acquire the provider
    status, idx, dt_idx = get(local, "bytes=%d-" % (len(BLOB) - 1000))
    check("index GET served byte-exact after waiting (%.2fs)" % dt_idx,
          status == 206 and idx == BLOB[-1000:] and 1.0 <= dt_idx < 8.0)
    check("index GET did NOT open a provider stream",
          relay.provider_opens == opens_before,
          "opens %d -> %d" % (opens_before, relay.provider_opens))
    check("tail prefetched exactly once", srv.tail_hits == 1,
          "tail_hits=%d" % srv.tail_hits)
    check("tail snapshot published (base + bytes)",
          relay._tail_base == len(BLOB) - vod_splitter._TAIL_PREFETCH
          and len(relay._tail) == vod_splitter._TAIL_PREFETCH)
    relay.stop()
    srv.shutdown()
    srv.server_close()

    # ---- [2] serial (IPTV) keeps the old order: tail gates the head -----
    print("[2] serial mode unchanged: the tail still gates the first byte")
    srv = Srv(tail_delay=0.8)
    relay = VodRelay()          # default: parallel_tail=False
    local = relay.start(srv.url, "MichaelTVPlayer/1.0", "eng", 0)
    status, head, dt = get(local, "bytes=0-65535")
    check("serial: head waits for the tail (%.2fs >= 0.8s)" % dt, dt >= 0.55,
          "%.2fs" % dt)
    check("serial: head still byte-exact",
          status == 206 and head == BLOB[:65536])
    check("serial: tail present at ready",
          relay._tail_ready.is_set() and relay._tail_base >= 0)
    relay.stop()
    srv.shutdown()
    srv.server_close()

    # ---- [3] resume sessions ignore the flag ----------------------------
    print("[3] resume sessions stay serial even with the flag on")
    srv = Srv(tail_delay=0.8)
    relay = VodRelay(parallel_tail=True)
    local = relay.start(srv.url, "MichaelTVPlayer/1.0", "eng", 1)
    status, head, dt = get(local, "bytes=0-65535")
    check("resume: first serve still waits for the tail (%.2fs)" % dt,
          dt >= 0.55, "%.2fs" % dt)
    check("resume: no head buffer is seeded", relay._head == b"")
    relay.stop()
    srv.shutdown()
    srv.server_close()

    # ---- [4] a failed tail falls back, never hangs ----------------------
    print("[4] failed tail: fall back to the acquire path, no hang")
    srv = Srv(tail_fail=True)
    relay = VodRelay(parallel_tail=True)
    local = relay.start(srv.url, "MichaelTVPlayer/1.0", "eng", 0)
    status, head, _dt = get(local, "bytes=0-65535")
    check("head served despite the tail 500", status == 206
          and head == BLOB[:65536])
    status, idx, dt = get(local, "bytes=%d-" % (len(BLOB) - 1000))
    check("index GET falls back and stays byte-exact",
          status == 206 and idx == BLOB[-1000:])
    check("fallback acquired the provider (rebase path)",
          relay.provider_opens >= 2, "opens=%d" % relay.provider_opens)
    relay.stop()
    srv.shutdown()
    srv.server_close()

    # ---- [5] stop() during a slow tail is prompt ------------------------
    print("[5] stop() during a slow tail")
    srv = Srv(tail_delay=6.0)
    relay = VodRelay(parallel_tail=True)
    local = relay.start(srv.url, "MichaelTVPlayer/1.0", "eng", 0)
    get(local, "bytes=0-65535")
    t0 = time.monotonic()
    relay.stop()
    dt = time.monotonic() - t0
    check("stop returns promptly (%.2fs < 3s)" % dt, dt < 3.0)
    check("stop unblocks the tail waiter", relay._tail_ready.is_set())
    srv.shutdown()
    srv.server_close()

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("  FAILED:", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
