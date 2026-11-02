# -*- coding: utf-8 -*-
"""Offline repro for the resume-mode subtitle-tap blindness (2026-11-02).

Field shape (player.log 03:33-03:36, Parks S01E01 Rus.Eng MKV): a resume
open (start-at-position) routes through VodRelay with the resume flag;
VLC picks the embedded English SDH track; the app's caption overlay
engages — but the relay tap NEVER produces track metadata or cues, so
nothing renders ("captions: relay tap produced no track metadata" in a
local app-level repro, zero cue lines in the field session).

Reproduced app-level with a 2.5 MB local MKV; this test drives the SAME
relay code paths headlessly against a Range-capable local provider:

[1] SMALL file (whole file inside the 2.6 MB tail prefetch): the tap
    can only parse track metadata from CACHE bytes, but VLC's opening
    walk is served from the TAIL, so the cache never fills -> the tap
    stays blind (and the tail harvest requires metadata that can only
    come from the tap — a precondition deadlock).
[2] LARGE file (tail beyond byte 0): VLC's opening walk fills the cache
    from the provider; the tap should parse the head and emit cues.

Run:  .venv\\Scripts\\python.exe test_relay_resume_tap.py
"""
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.profanity import find_ffmpeg  # noqa: E402
from src.vod_splitter import VodRelay  # noqa: E402

PASS = []
FAIL = []
FF = find_ffmpeg()
SMALL = os.path.abspath("build/resume_tap_small.mkv").replace("\\", "/")
SMALL_SRT = os.path.abspath("build/resume_tap_small.srt")
LARGE = os.path.abspath("build/resume_tap_large.mkv").replace("\\", "/")
LARGE_SRT = os.path.abspath("build/resume_tap_large.srt")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          ("   [%s]" % detail if detail else ""), flush=True)


def _srt(path, n, gap, prefix):
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            s, e = i * gap, i * gap + gap * 0.9
            f.write("%d\n%02d:%02d:%06.3f --> %02d:%02d:%06.3f\n%s %d\n\n"
                    % (i + 1, s // 60, s % 60, s % 1, e // 60, e % 60,
                       e % 1, prefix, i + 1))


def build_media():
    os.makedirs("build", exist_ok=True)
    if not os.path.isfile(SMALL):
        _srt(SMALL_SRT, 30, 2.0, "SMALL CUE")
        subprocess.run(
            [FF, "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=steelblue:s=256x144:d=60:r=10",
             "-i", SMALL_SRT, "-map", "0:v", "-map", "1:s",
             "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "60k",
             "-c:s", "srt", "-metadata:s:s:0", "language=eng",
             SMALL], check=True, timeout=120, creationflags=0x08000000)
    if not os.path.isfile(LARGE):
        _srt(LARGE_SRT, 60, 5.0, "LARGE CUE")
        subprocess.run(
            [FF, "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=darkred:s=256x144:d=300:r=10",
             "-i", LARGE_SRT, "-map", "0:v", "-map", "1:s",
             "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "120k",
             "-minrate", "120k", "-maxrate", "120k",
             "-c:s", "srt", "-metadata:s:s:0", "language=eng",
             LARGE], check=True, timeout=600, creationflags=0x08000000)


class _Provider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    root = "."

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = os.path.join(self.root, self.path.lstrip("/").split("?")[0])
        if not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        rng = self.headers.get("Range", "")
        start, end = 0, size - 1
        if rng.startswith("bytes="):
            spec = rng[6:]
            if "-" in spec:
                a, _, b = spec.partition("-")
                start = int(a) if a else 0
                end = min(int(b), size - 1) if b else size - 1
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(end - start + 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        if rng:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        self.wfile.write(data)


def start_provider(root):
    _Provider.root = root
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run_case(app, name, media_url, expect_cues):
    """Resume-mode relay + a simulated VLC opening walk (GET bytes 0-…)
    + a playback read at a resume position; verdict on track metadata
    and cues."""
    cues = []
    relay = VodRelay()
    relay.cue.connect(lambda s, e, t: cues.append((s, t)))
    local = relay.start(media_url, "VLC/3.0.21 LibVLC/3.0.21",
                        prefer_language="eng", start_offset=1)
    if not local:
        check("%s: relay started" % name, False, "start() failed")
        return
    deadline = time.monotonic() + 25.0
    walk_done = False
    while time.monotonic() < deadline:
        app.processEvents()
        if not walk_done:
            try:
                # VLC's opening walk: the head bytes 0-1MB
                urllib.request.urlopen(
                    urllib.request.Request(
                        local, headers={"Range": "bytes=0-1048575"}),
                    timeout=10).read()
            except Exception:
                pass
            walk_done = True
        if relay.parser_tracks and len(cues) >= 1:
            break
        time.sleep(0.2)
    app.processEvents()
    check("%s: tap found track metadata (resume mode)" % name,
          bool(relay.parser_tracks),
          "tracks=%r" % (relay.parser_tracks,))
    check("%s: cues emitted (resume mode)" % name,
          len(cues) >= (1 if expect_cues else 0),
          "%d cues" % len(cues))
    try:
        relay.stop()
    except Exception:  # noqa: BLE001
        pass


def main():
    build_media()
    app = QtWidgets.QApplication(sys.argv)
    srv = start_provider(os.path.abspath("build"))
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    small_size = os.path.getsize(SMALL)
    large_size = os.path.getsize(LARGE)
    print("small=%.2f MB (tail covers byte 0: %s)"
          % (small_size / 1e6, small_size <= 2_621_440))
    print("large=%.2f MB (tail covers byte 0: %s)"
          % (large_size / 1e6, large_size <= 2_621_440))
    run_case(app, "[1] small MKV", url + os.path.basename(SMALL), True)
    run_case(app, "[2] large MKV", url + os.path.basename(LARGE), True)
    srv.shutdown()
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
