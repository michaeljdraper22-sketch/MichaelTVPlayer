# -*- coding: utf-8 -*-
"""E2E: anchored vs unanchored seeks on a GROWING DVR buffer (headless).

Simulates chase mode: a writer thread appends the recording to a temp
buffer while a vmem player watches it; seeks are issued mid-GOP (exact
target) and GOP-anchored (IDR - 0.35), and the display-callback timeline
shows the blackout/healing difference. No window, no audio.

Run: .venv\\Scripts\\python.exe tools\\gop_growing_e2e.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

src = open(os.path.join(HERE, "gop_pixel_probe.py")).read()
src = src.replace('if __name__ == "__main__":', 'if False:')
gpp = type(sys)("gpp")
gpp.__file__ = os.path.abspath(__file__)
exec(compile(src, "gpp", "exec"), gpp.__dict__)

from src.gopindex import GopIndex  # noqa: E402

REC = os.path.join(ROOT, "TV Recordings",
                   "US_NFL_NETWORK_HD_20260827_191626.ts")
IDR = 30.463

tmp = tempfile.mkdtemp(prefix="mtp_grow_")
buf = os.path.join(tmp, "buffer.ts")
full = open(REC, "rb").read()
PRE = 60 * 1024 * 1024 // 8        # ~60 s at this bitrate... use bytes: 60s
PRE = int(60 / 99.918 * len(full))
with open(buf, "wb") as f:
    f.write(full[:PRE])

stop = threading.Event()


def writer():
    """Append the rest of the recording at ~4x wall speed (a live writer
    that outpaces the viewer, like the recorder does)."""
    pos = PRE
    with open(buf, "ab") as f:
        while not stop.is_set() and pos < len(full):
            f.write(full[pos:pos + 1024 * 1024])
            f.flush()
            pos += 1024 * 1024
            time.sleep(0.2)


gop = GopIndex(buf)
gop.scan()
print("index before play: %d IDRs (%.1fs..%.1fs)"
      % (len(gop.idrs()), gop.idrs()[0][0], gop.idrs()[-1][0]), flush=True)

t = threading.Thread(target=writer, daemon=True)
t.start()

g = gpp.Grab(["--ignore-config", "--no-audio", "--vout=vmem",
              "--file-caching=1000"])
gpp.lib.libvlc_media_player_set_time(
    gpp.ctypes.c_void_p(g.mp), 20_000)
g.play()
time.sleep(4.0)


def seek_case(label, target):
    gop.scan()
    n0 = len(g.frames)
    t0 = time.monotonic()
    gpp.lib.libvlc_media_player_set_time(
        gpp.ctypes.c_void_p(g.mp), int(target * 1000))
    # wait until a NEW frame displays, then a bit more
    lat = None
    while time.monotonic() - t0 < 6.0:
        time.sleep(0.02)
        with g._lk:
            if len(g.frames) > n0 + 1:
                lat = time.monotonic() - t0
                break
    time.sleep(2.0)
    with g._lk:
        got = list(g.frames[n0:])
    print("%-28s target=%6.2f  first-frame latency: %s   (%d frames in 6s)"
          % (label, target, "%.2fs" % lat if lat else "NONE",
             len(got)), flush=True)


anchored = gop.anchor_for(31.0) - 0.35
seek_case("UNANCHORED mid-GOP", 31.0)
seek_case("GOP-ANCHORED (IDR-0.35)", anchored)
seek_case("UNANCHORED mid-GOP again", 61.0)
a2 = gop.anchor_for(61.0) - 0.35
seek_case("GOP-ANCHORED again", a2)

stop.set()
t.join(timeout=3)
g.close()
shutil.rmtree(tmp, ignore_errors=True)
print("DONE", flush=True)
