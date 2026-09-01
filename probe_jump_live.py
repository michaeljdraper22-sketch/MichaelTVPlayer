# -*- coding: utf-8 -*-
"""Real-VLC probe: does jump_to_live() land at the END of an http mkv?

Live incident (2026-09-01 18:13, player.log): during a kind=stremio
episode (torrentio resolve -> local VOD relay), pressing LIVE logged
"_jump_live mode=live" TWICE (18:13:02, 18:14:00) with ZERO effect —
no "autoplay-next: media finished" (auto=True), no next-episode switch.
So the set_time(length) seek never landed. This probe replicates the
mechanics with REAL libVLC against a range-capable HTTP server that
mimics the VOD relay's answers (206 + Accept-Ranges, 416 past EOF).

Offscreen, --no-audio; the media is a tiny ffmpeg-generated mkv.
"""
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "probe_jump_media.mkv")
DURATION = 20

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (" — " + str(detail) if detail else ""))


def make_media():
    if os.path.exists(MEDIA) and os.path.getsize(MEDIA) > 10000:
        return
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={DURATION}:size=320x240:"
                              "rate=10",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         "-shortest", MEDIA],
        check=True)


class RelayHandler(BaseHTTPRequestHandler):
    """Range answers shaped like the app's VOD relay (vod_splitter.py):
    206 + Accept-Ranges + Content-Range; 416 when start >= total."""
    total = None                 # set in main() once the media exists

    def log_message(self, *a):
        pass

    def _send(self, code, headers, body=b""):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_HEAD(self):
        self._send(200, [("Accept-Ranges", "bytes"),
                         ("Content-Type", "video/x-matroska")])

    def do_GET(self):
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                a = int(m.group(1))
                b = int(m.group(2)) if m.group(2) else self.total - 1
            else:                       # suffix range bytes=-N
                n = int(m.group(2))
                a, b = max(0, self.total - n), self.total - 1
            if a >= self.total:
                self._send(416, [("Content-Range", f"bytes */{self.total}")])
                return
            b = min(b, self.total - 1)
            with open(MEDIA, "rb") as f:
                f.seek(a)
                data = f.read(b - a + 1)
            self._send(206, [("Accept-Ranges", "bytes"),
                             ("Content-Type", "video/x-matroska"),
                             ("Content-Range", f"bytes {a}-{b}/{self.total}")],
                       data)
        else:
            with open(MEDIA, "rb") as f:
                data = f.read()
            self._send(200, [("Accept-Ranges", "bytes"),
                             ("Content-Type", "video/x-matroska")], data)


def wait_state(vlcw, states, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            st = vlcw.state_name()
        except Exception:
            st = "?"
        if st in states:
            return st
        time.sleep(0.2)
    return st


def wait_length(vlcw, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ln = vlcw.get_length()
        if ln > 0:
            return ln
        time.sleep(0.2)
    return 0


def main():
    make_media()
    RelayHandler.total = os.path.getsize(MEDIA)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/media.mkv"

    # INVISIBLE playback: with no HWND bound, libVLC spawns its own video
    # window — a testsrc box popped up on the desktop during the first
    # runs (the user closed one). sub_args rides the instance arg list
    # (the freetype slot accepts any libVLC option); the dummy vout
    # decodes but never opens a window. Audio keeps decoding at volume 0
    # so the seek/end timing stays faithful to what shipped (v1.5.12).
    # (Monkeypatching vlc.Instance does NOT work — vlc.py's lazy ctypes
    # binding treats it as a type and media_player_new explodes.)
    QUIET = {"sub_args": ["--vout=dummy"]}

    from src.player import VLCPlayer

    def jump_epsilon(eps):
        """Play, then seek to length-eps; how long until state=ended?"""
        w = VLCPlayer(volume=0, **QUIET)   # silent + invisible
        try:
            w.play(url)
            st = wait_state(w, ("playing",), 20)
            length = wait_length(w)
            if st != "playing" or length <= 0:
                return f"no start ({st}, {length})", 0.0
            time.sleep(2)
            t0 = time.monotonic()
            w.set_time(max(0, length - eps))
            st = wait_state(w, ("ended",), 20)
            dt = time.monotonic() - t0
            return (f"state={st} after {dt:.1f}s "
                    f"(t={w.get_time()}/{length})", dt)
        finally:
            try:
                w.stop()
            except Exception:
                pass

    try:
        # eps=0 documents the bug being fixed: set_time(exact length)
        # must stay SLOW (VLC clamps to the last cue and crawls without
        # ending) — if it ever ends fast, the 1500 ms epsilon can go.
        detail, dt = jump_epsilon(0)
        check("set_time(exact length) is the slow broken seek (>8 s)",
              dt >= 8, detail)
        for eps in (1500, 3000):
            detail, dt = jump_epsilon(eps)
            check(f"seek to length-{eps} ends promptly (<8 s)", 0 < dt < 8,
                  detail)

        # ---- the FIX under test: the wrapper itself --------------------
        w = VLCPlayer(volume=0, **QUIET)
        try:
            w.play(url)
            wait_state(w, ("playing",), 20)
            length = wait_length(w)
            check("wrapper: length known", length > 0, f"{length} ms")
            time.sleep(2)
            t0 = time.monotonic()
            w.jump_to_live()             # the fixed app call
            st = wait_state(w, ("ended",), 12)
            dt = time.monotonic() - t0
            check("wrapper: jump_to_live() reaches genuine ended <8 s",
                  st == "ended" and dt < 8,
                  f"state={st} after {dt:.1f}s")
        finally:
            try:
                w.stop()
            except Exception:
                pass
    finally:
        srv.shutdown()

    print(f"RESULT: {len(PASS)} pass, {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
