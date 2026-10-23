# -*- coding: utf-8 -*-
"""Byte-integrity repro for the 2026-10-21 Stremio A/V desync report.

Field pattern (player.log 15:18-15:20, torbox/torrentio):
  - the provider cuts the streaming body mid-file REPEATEDLY (3x in 2 s),
    including bodies opened at non-zero Range offsets (a CDN age/idle
    policy, not an EOF),
  - the user PAUSES (VLC stops reading; the serve loop blocks on the
    full TCP window while the provider connection idles out and dies),
  - on unpause VLC reads again, and a second connection (the seek-index
    probe) ranges around the file.

Verifies the relay keeps the served bytes EXACT through that sequence.
Any byte shift corrupts the MKV downstream (decoder garbage -> dropped
video -> the reported persistent A/V desync).
Run:  .venv\\Scripts\\python.exe test_vod_pause_integrity.py
"""
import logging
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.vod_splitter import VodRelay  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          ("   [" + detail + "]" if detail else ""), flush=True)


class _KillHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.gets += 1
        blob = srv.blob
        rng = self.headers.get("Range") or ""
        a, b = 0, len(blob) - 1
        if rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            a_s, _, b_s = spec.partition("-")
            if a_s:
                a = int(a_s)
            if b_s:
                b = int(b_s)
        length = b - a + 1
        # the relay's STARTUP gets (head probe + tail prefetch) come
        # through unharmed — the field CDN always delivered those; every
        # later body is cut after K bytes, ANY offset (the field CDN's
        # age/idle policy)
        k = length if srv.gets <= 2 else min(srv.kill_after, length)
        self.send_response(206)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", srv.ctype)
        self.send_header("Content-Range", f"bytes {a}-{b}/{len(blob)}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        try:
            self.wfile.write(blob[a:a + k])
        except Exception:
            pass


class _KillProvider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, blob, ctype):
        super().__init__(("127.0.0.1", 0), _KillHandler)
        self.blob = blob
        self.ctype = ctype
        self.gets = 0
        self.kill_after = None


def main():
    logging.basicConfig(level=logging.INFO)
    # a synthetic but MKV-shaped blob is unnecessary: byte integrity is
    # content-agnostic; reuse any big binary (use python's own DLL)
    src = os.path.join(sys.base_prefix, "python312.dll")
    if not os.path.exists(src):
        src = os.path.join(sys.base_prefix, "python.dll")
    # content-agnostic integrity target; only the EBML magic must lead so
    # the relay's container probe accepts it
    big = bytearray(b"\x1a\x45\xdf\xa3" + b"\x42" * 12)
    big += bytearray(open(src, "rb").read() * 8)
    del big[8_000_000:]                                   # ~8 MB total

    prov = _KillProvider(bytes(big), "video/x-matroska")
    prov.kill_after = 400_000          # cut every body after 400 KB
    threading.Thread(target=prov.serve_forever, daemon=True).start()

    relay = VodRelay()
    local = relay.start(
        f"http://127.0.0.1:{prov.server_address[1]}/big.mkv",
        "MichaelTVPlayer/1.0")
    check("relay started", local.startswith("http://127.0.0.1:"))

    # wait out the async startup (head fetch + tail prefetch + acquire)
    deadline = time.time() + 15
    while time.time() < deadline and relay.cache_size == 0:
        time.sleep(0.1)

    errors = []

    def index_probe():
        """VLC's second connection: ranges the tail (seek index)."""
        try:
            n = len(big)
            req = urllib.request.Request(
                local, headers={"Range": f"bytes={n - 2_621_440}-{n - 1}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
            if body != bytes(big[n - 2_621_440:]):
                errors.append(f"index probe body mismatch ({len(body)}B)")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"index probe failed: {exc!r}")

    probe_t = threading.Thread(target=index_probe)
    probe_t.start()

    got = bytearray()
    t0 = time.time()
    with urllib.request.urlopen(local, timeout=180) as r:
        # read 2 MB, then PAUSE 4 s (client stops reading; the provider
        # connection idles and is cut mid-body), then drain the rest
        while len(got) < 2_000_000:
            chunk = r.read(2_000_000 - len(got))
            if not chunk:
                break
            got += chunk
        assert len(got) == 2_000_000, f"short pre-pause read {len(got)}"
        time.sleep(4.0)
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            got += chunk
    elapsed = time.time() - t0
    probe_t.join(timeout=60)

    check("provider bodies were cut repeatedly",
          prov.gets >= 4, f"{prov.gets} provider GETs")
    check("main body byte-exact through cuts + pause",
          bytes(got) == bytes(big),
          f"{len(got)} of {len(big)} bytes in {elapsed:.1f}s")
    check("concurrent index probe byte-exact", not errors,
          "; ".join(errors))

    relay.stop()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
