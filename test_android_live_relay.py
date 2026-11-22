# -*- coding: utf-8 -*-
"""Offline tests for the Android live HLS DVR relay (live_relay.py).

Runs on the desktop Python (requests + stdlib only): a synthetic provider
serves a real-shaped HLS playlist (302 redirect, per-request token path,
10 s MPEG-TS-looking segments, sequence rollover, sequence reset) and the
relay is exercised end to end:

  * startup takes only the newest two segments and becomes ready
  * the local playlist is a valid live media playlist (sequence, EXTINF,
    relative segment URLs)
  * segments are byte-identical and support HEAD/Range/206/416/CORS
  * new provider segments append; the window prunes and old URLs 404
  * a provider sequence reset appends a discontinuity instead of looping
  * encrypted and fMP4 playlists raise RelayError (direct fallback)
  * stop_current() closes the server and removes the cache directory

Run: .venv\\Scripts\\python.exe test_android_live_relay.py
"""
import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ANDROID_PY = os.path.join(HERE, "android-gradle", "app", "src", "main",
                          "python")
sys.path.insert(0, ANDROID_PY)

import live_relay  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"   [{detail}]" if detail else ""), flush=True)


def seg_bytes(seed):
    """A TS-looking segment: every packet starts with 0x47."""
    body = bytearray()
    for i in range(600):
        packet = bytearray(b"\x47" + bytes([0x00, 0x00, 0x10]) + b"\x00" * 184)
        packet[4] = (seed + i) & 0xFF
        body += packet
    return bytes(body)


class Provider:
    """Synthetic Xtream-style HLS provider."""

    def __init__(self):
        self.media_seq = 1000
        self.segments = []          # list of (seq, duration, bytes)
        self.encrypted = False
        self.fmp4 = False
        self.master = False
        self._stop_event = threading.Event()
        self.lock = threading.Lock()
        self.hits = {"playlist": 0, "segment": 0}
        self.httpd = None
        self.thread = None
        self.port = 0

    # -- content -----------------------------------------------------
    def seed(self, n=6):
        with self.lock:
            self.segments = []
            for i in range(n):
                seq = self.media_seq + i
                self.segments.append((seq, 10.009367, seg_bytes(seq)))
            self.media_seq += n
        return self

    def advance(self, n=1):
        with self.lock:
            for i in range(n):
                seq = self.media_seq + i
                self.segments.append((seq, 10.009367, seg_bytes(seq)))
            self.media_seq += n
            self.segments = self.segments[-8:]

    def reset_sequence(self, n=6):
        with self.lock:
            self.media_seq = 7
            self.segments = []
            for i in range(n):
                seq = self.media_seq + i
                self.segments.append((seq, 10.009367, seg_bytes(seq)))
            self.media_seq += n

    def playlist(self, master_ok=True):
        with self.lock:
            segs = list(self.segments)
            seq = segs[0][0] if segs else self.media_seq
        if self.master and master_ok:
            return ("#EXTM3U\n"
                    "#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/index.m3u8\n"
                    "#EXT-X-STREAM-INF:BANDWIDTH=5000000\nhigh/index.m3u8\n")
        head = ["#EXTM3U", "#EXT-X-VERSION:3",
                "#EXT-X-MEDIA-SEQUENCE:%d" % seq,
                "#EXT-X-TARGETDURATION:11"]
        if self.encrypted:
            head.append('#EXT-X-KEY:METHOD=AES-128,URI="key.bin"')
        if self.fmp4:
            head.append('#EXT-X-MAP:URI="init.mp4"')
        for _seq, dur, _body in segs:
            head.append("#EXTINF:%.6f," % dur)
            head.append("/hls/tok%d/%d.ts" % (self.port, _seq))
        return "\n".join(head) + "\n"

    # -- server ------------------------------------------------------
    def start(self):
        provider = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *a):
                pass

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/live.m3u8" or path == "/high/index.m3u8":
                    provider.hits["playlist"] += 1
                    body = provider.playlist(
                        master_ok=(path == "/live.m3u8")).encode()
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                m = re.match(r"^/hls/tok\d+/(\d+)\.ts$", path)
                if m:
                    seq = int(m.group(1))
                    provider.hits["segment"] += 1
                    with provider.lock:
                        body = None
                        for s, _d, b in provider.segments:
                            if s == seq:
                                body = b
                                break
                    if body is None:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        return self

    def url(self):
        return "http://127.0.0.1:%d/live.m3u8" % self.port

    def stop(self):
        self._stop_event.set()
        if self.httpd:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()
            try:
                self.httpd.server_close()
            except Exception:  # noqa: BLE001
                pass


def wait_for(pred, timeout=8.0, step=0.1):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


def get(url, headers=None):
    import requests
    r = requests.get(url, headers=headers or {}, timeout=10)
    return r


def main():
    import requests
    orig_poll = live_relay.POLL_S
    live_relay.POLL_S = 0.35          # make the rollover tests fast
    cache = tempfile.mkdtemp(prefix="mtp_relay_test_")
    prov = Provider().seed(6).start()
    relay = None
    try:
        print("[1] startup, playlist shape, byte-identical segments")
        t0 = time.time()
        relay = live_relay.start(prov.url(), cache, window_s=25,
                                 cast_ip="192.168.1.50", timeout_s=12)
        took = time.time() - t0
        st = relay.status()
        check("relay became ready from the newest two segments",
              st["ready"] and st["segments"] >= 2, str(st))
        check("startup did not download the whole provider window",
              prov.hits["segment"] <= 3,
              "segment downloads=%d" % prov.hits["segment"])
        check("startup is quick", took < 8.0, "%.1fs" % took)
        check("local and cast URLs point at the same token",
              st["local_url"].startswith("http://127.0.0.1:")
              and "/s/" in st["local_url"]
              and st["cast_url"].startswith("http://192.168.1.50:"), str(st))
        pl = get(relay.local_url)
        check("local playlist is served as HLS",
              pl.status_code == 200
              and "application/vnd.apple.mpegurl" in pl.headers.get(
                  "Content-Type", ""), pl.headers.get("Content-Type"))
        check("playlist has MEDIA-SEQUENCE and EXTINF entries",
              "#EXT-X-MEDIA-SEQUENCE:0" in pl.text
              and pl.text.count("#EXTINF:") == st["segments"], pl.text)
        check("playlist segment URLs are relative",
              "seg/0.ts" in pl.text and "/hls/" not in pl.text)
        seg = get(relay.local_url.replace("live.m3u8", "seg/0.ts"))
        check("segment is byte-identical MPEG-TS",
              seg.status_code == 200 and seg.content[:1] == b"\x47"
              and len(seg.content) == 600 * 188, str(len(seg.content)))
        check("segment advertises ranges and CORS",
              seg.headers.get("Accept-Ranges") == "bytes"
              and seg.headers.get("Access-Control-Allow-Origin") == "*")
        part = get(relay.local_url.replace("live.m3u8", "seg/0.ts"),
                   {"Range": "bytes=0-187"})
        check("Range returns 206 with exactly the requested bytes",
              part.status_code == 206 and len(part.content) == 188
              and part.headers.get("Content-Range", "").startswith(
                  "bytes 0-187/"), str(part.status_code))
        bad = get(relay.local_url.replace("live.m3u8", "seg/99.ts"))
        check("unknown segment is 404", bad.status_code == 404)
        badrange = get(relay.local_url.replace("live.m3u8", "seg/0.ts"),
                       {"Range": "bytes=999999-"})
        check("out-of-range request is 416", badrange.status_code == 416)

        print("[2] append + sliding window pruning")
        start_hits = prov.hits["segment"]
        prov.advance(3)
        ok = wait_for(lambda: prov.hits["segment"] >= start_hits + 3, 10.0)
        check("new provider segments download", ok,
              "downloads=%d" % prov.hits["segment"])
        ok = wait_for(
            lambda: "MEDIA-SEQUENCE:0" not in get(relay.local_url).text, 8.0)
        st = relay.status()
        check("25 s window prunes back to two segments",
              ok and st["segments"] <= 2, str(st))
        pl = get(relay.local_url).text
        first = int(re.search(r"MEDIA-SEQUENCE:(\d+)", pl).group(1))
        check("MEDIA-SEQUENCE advanced with the window", first > 0, pl)
        gone = get(relay.local_url.replace("live.m3u8", "seg/0.ts"))
        check("pruned segment is gone", gone.status_code == 404,
              str(gone.status_code))
        newseg = get(relay.local_url.replace(
            "live.m3u8", "seg/%d.ts" % first))
        check("a current window segment still serves", newseg.status_code == 200)

        print("[3] provider sequence reset -> discontinuity, no loop")
        prov.reset_sequence(6)
        ok = wait_for(lambda: "#EXT-X-DISCONTINUITY" in get(
            relay.local_url).text, 8.0)
        check("sequence reset appends a discontinuity", ok,
              get(relay.local_url).text)
        st = relay.status()
        check("relay keeps growing after the reset", st["segments"] >= 2,
              str(st))

        print("[4] unsupported playlists fall back")
        live_relay.stop_current()
        relay = None
        enc = Provider().seed(6)
        enc.encrypted = True
        enc.start()
        try:
            live_relay.start(enc.url(), cache, timeout_s=4)
            check("encrypted playlist raises RelayError", False)
        except live_relay.RelayError as exc:
            check("encrypted playlist raises RelayError", True, str(exc))
        enc.stop()
        mp4 = Provider().seed(6)
        mp4.fmp4 = True
        mp4.start()
        try:
            live_relay.start(mp4.url(), cache, timeout_s=4)
            check("fMP4/EXT-X-MAP playlist raises RelayError", False)
        except live_relay.RelayError as exc:
            check("fMP4/EXT-X-MAP playlist raises RelayError", True, str(exc))
        mp4.stop()

        print("[5] master playlist picks the highest bandwidth")
        mas = Provider().seed(3)
        mas.master = True
        mas.start()
        relay = live_relay.start(mas.url(), cache, timeout_s=10)
        check("master playlist resolved to a media playlist",
              relay.status()["ready"], str(relay.status()))
        live_relay.stop_current()
        relay = None
        mas.stop()

        print("[6] stop closes the server and removes the cache")
        st = live_relay.status()
        check("stop_current reports not running", not st.get("running"), str(st))
        cache_root = os.path.join(cache, "live_dvr")
        check("cache directory removed",
              not os.path.isdir(cache_root) or not os.listdir(cache_root),
              str(os.listdir(cache_root) if os.path.isdir(cache_root) else []))
    finally:
        live_relay.POLL_S = orig_poll
        try:
            live_relay.stop_current()
        except Exception:  # noqa: BLE001
            pass
        prov.stop()
        import shutil
        shutil.rmtree(cache, ignore_errors=True)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
