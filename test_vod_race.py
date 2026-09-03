# -*- coding: utf-8 -*-
"""VOD splitter byte fidelity under provider-death + sibling-acquire
churn, and the append_cache poison race (2026-09-03 Elite Force
follow-up).

The bug: _ProviderStream._read appended its returned bytes into the
cache window with no identity re-check; a sibling handler's _acquire
(close old stream -> _rebase the window -> open a new stream) could land
between a read returning and its append, writing OLD-offset bytes into
the NEW window base — a byte-shifted cache. The probe that triggered the
rebase then reads its own bytes back from that poisoned window: garbage
to VLC's demuxer (the 'weirdly garbled' picture). The fix clears the
provider slot under the cache lock in _acquire and makes append_cache
check stream identity + write atomically under that same lock.

This test builds a pattern provider (every 8-byte word encodes its own
file offset) over a Range-capable local HTTP server that CUTS every
connection after a small random budget (the collapsing-CDN shape), then
hammers the relay with a sequential consumer plus head/tail/mid probes
(rebases), including a phase that sleeps inside append_cache to widen
the race window to a guaranteed hit. Asserts:

  - the sequential consumer's bytes always match the pattern,
  - every probe response matches the pattern (the poisoned-cache read
    path — a stale append lands in the window the probe just rebased),
  - the cache window, via read_cache after a churn-free growth phase,
    matches the pattern byte-for-byte, and
  - a detached stream can never append into the cache.

Run:  .venv\\Scripts\\python.exe test_vod_race.py
"""
import os
import random
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src import vod_splitter as vs  # noqa: E402

PASS = []
FAIL = []
TOTAL = 12 << 20          # 12 MB pattern file
HEAD = 65536              # the "prefetched head" region the relay serves
                              # head probes from (no rebase on probe)


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


# the pattern table: every 8-byte word encodes its own word index, so a
# single shifted byte anywhere in a served range is instantly detectable
PAT = b"".join(b"%08d" % i for i in range(TOTAL // 8 + 2))


def pattern(a: int, n: int) -> bytes:
    return PAT[a:a + n]


class PatternProvider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    kill_after = (96 << 10, 320 << 10)   # per-connection body budget

    def log_message(self, *args):
        pass

    def do_GET(self):
        rng = self.headers.get("Range") or "bytes=0-"
        spec = rng[len("bytes="):]
        a_s, _, b_s = spec.partition("-")
        a = int(a_s or 0)
        b = int(b_s) if b_s else TOTAL - 1
        b = min(b, TOTAL - 1)
        n = b - a + 1
        served = 0
        budget = random.randint(*self.kill_after)
        self.send_response(206)
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Content-Range", f"bytes {a}-{b}/{TOTAL}")
        self.send_header("Content-Length", str(n))
        self.end_headers()
        pos = a
        try:
            while pos <= b:
                chunk = min(64 << 10, b - pos + 1)
                self.wfile.write(pattern(pos, chunk))
                served += chunk
                pos += chunk
                if served >= budget and pos <= b:
                    # cut the body mid-stream, like the collapsing CDN
                    self.close_connection = True
                    break
                self.wfile.flush()
        except Exception:
            pass


def start_provider():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), PatternProvider)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v.mkv"


def bring_up_relay(url):
    """start() minus probe/tail/tap: the serve-loop + cache machinery
    only (this test exercises the relay path, not the subtitle taps)."""
    app = QtWidgets.QApplication.instance() \
        or QtWidgets.QApplication(sys.argv)
    relay = vs.VodRelay()
    relay.url = url
    relay.ua = "test"
    relay.total = TOTAL
    relay._container = "mkv"
    fd, relay.cache_path = tempfile.mkstemp(suffix=".mkv",
                                            prefix="mtp_race_")
    os.close(fd)
    relay._cache = open(relay.cache_path, "r+b")
    relay._cache_r = open(relay.cache_path, "rb")
    relay._head = pattern(0, HEAD)
    relay._alive = True
    relay._ready.set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), relay._make_handler())
    server.daemon_threads = True

    def _quiet(*_a, **_k):
        pass
    server.handle_error = _quiet
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return relay, server, f"http://127.0.0.1:{server.server_address[1]}/v"


def run_consumers(local, run_s, probes=True, min_a=0):
    """Consumer A: sequential read from 0 (the VLC main connection).
    Consumer B: head/tail/mid probes (the rebase churn), each response
    verified against the pattern — a poisoned cache window serves the
    probe that just rebased it WRONG bytes. Returns (a_read, all_ok)."""
    stop = threading.Event()
    result = {"a": 0, "ok": True}

    def fail():
        result["ok"] = False

    def consume_a():
        pos = 0
        try:
            while not stop.is_set():
                try:
                    req = urllib.request.Request(
                        local, headers={"Range": f"bytes={pos}-"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        while not stop.is_set():
                            data = r.read(64 << 10)
                            if not data:
                                break   # cut body — reconnect like VLC
                            if data != pattern(pos, len(data)):
                                fail()
                                return
                            pos += len(data)
                            if pos >= (8 << 20):
                                stop.set()
                except Exception:
                    if stop.is_set():
                        break
                    time.sleep(0.05)    # transient (churn) — reconnect
            result["a"] = pos
        except Exception:
            pass

    def probe_b():
        targets = [0, TOTAL - 4096, 5 << 20]
        i = 0
        while not stop.is_set():
            off = targets[i % len(targets)]
            i += 1
            try:
                req = urllib.request.Request(
                    local,
                    headers={"Range": f"bytes={off}-{off + 4095}"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    data = r.read()
                if data and data != pattern(off, len(data)):
                    fail()          # the poisoned-cache read path
                    return
            except Exception:
                pass
            time.sleep(0.03)

    ta = threading.Thread(target=consume_a, daemon=True)
    threads = [ta]
    if probes:
        tb = threading.Thread(target=probe_b, daemon=True)
        threads.append(tb)
        tb.start()
    ta.start()
    time.sleep(run_s)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    return result["a"], result["ok"]


def verify_cache_window(relay):
    """read_cache over the relay's CURRENT window must match the
    pattern byte-for-byte."""
    base, size = relay.cache_base, relay.cache_size
    if size <= 0:
        return False, "empty cache window"
    got = bytearray()
    pos = base
    while pos < base + size:
        n = min(1 << 20, base + size - pos)
        data = relay.read_cache(pos, n)
        if not data:
            break
        got += data
        pos += len(data)
    if bytes(got) != pattern(base, len(got)):
        return False, f"MISMATCH ({len(got)} B at base {base})"
    return True, f"window [{base}, +{size}) verified"


def main():
    print("[0] pattern self-check")
    check("pattern regenerates identically",
          pattern(123456, 4096) == pattern(123456, 4096))
    check("pattern is position-unique",
          pattern(0, 64) != pattern(8, 64))

    psrv, purl = start_provider()
    relay, rserver, local = bring_up_relay(purl)
    try:
        print("[1] churn with the real (unwidened) race window")
        a_bytes, ok = run_consumers(local, 5.0, probes=True)
        check(f"all bytes exact through {a_bytes >> 10} KiB of "
              f"cut/reopen/probe churn", ok and a_bytes > (1 << 20))

        print("[2] race WIDENED: 20 ms sleep inside append_cache "
              "(the old interleaving, guaranteed)")
        orig_append = vs.VodRelay.append_cache

        def slow_append(self, st, data):
            time.sleep(0.02)      # a sibling _acquire lands here
            orig_append(self, st, data)

        vs.VodRelay.append_cache = slow_append
        try:
            a_bytes, ok = run_consumers(local, 5.0, probes=True)
            check(f"all bytes exact under the widened race "
                  f"({a_bytes >> 10} KiB)", ok and a_bytes > (200 << 10))
        finally:
            vs.VodRelay.append_cache = orig_append

        print("[3] churn-free growth: the whole window verifies")
        a_bytes, ok = run_consumers(local, 2.0, probes=False)
        check("solo sequential growth is byte-exact", ok)
        vok, why = verify_cache_window(relay)
        check(f"cache window byte-exact ({why})",
              vok and relay.cache_size > (512 << 10))

        print("[4] the identity gate: a detached stream cannot append")
        st = vs._ProviderStream.__new__(vs._ProviderStream)
        st.relay = relay
        st.offset = 0
        st.appendable = True
        before = relay.cache_size
        relay._stream = None       # detached (as _acquire does first)
        relay.append_cache(st, b"x" * 1024)
        check("detached stream's bytes never enter the cache",
              relay.cache_size == before)
    finally:
        relay.stop()
        rserver.shutdown()
        rserver.server_close()
        psrv.shutdown()
        psrv.server_close()

    print()
    print(f"vod-race: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name in FAIL:
            print("  FAILED:", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
