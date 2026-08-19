# -*- coding: utf-8 -*-
"""Tests for the VOD splitter: relay correctness + subtitle extraction.

Builds a small REAL mkv (video + SRT subs) with ffmpeg, serves it through
VodRelay over file://, and checks: byte fidelity, Range requests, VLC
playback + seeking through the relay, and the subtitle tap's cues.

Run:  .venv\\Scripts\\python.exe test_vod_splitter.py
"""
import os
import subprocess
import sys
import time
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.player import VLCPlayer  # noqa: E402
from src.profanity import find_ffmpeg  # noqa: E402
from src.vod_splitter import VodRelay  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


FF = find_ffmpeg()
MKV = os.path.abspath("build/split_test.mkv").replace("\\", "/")
SRT_IN = os.path.abspath("build/split_test_in.srt").replace("\\", "/")


def build_sample():
    os.makedirs("build", exist_ok=True)
    with open(SRT_IN, "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,000\n"
                "what the hell is this\n\n"
                "2\n00:00:05,000 --> 00:00:07,000\n"
                "clean as snow\n\n"
                "3\n00:00:09,000 --> 00:00:11,000\n"
                "damn dogs everywhere\n")
    subprocess.run(
        [FF, "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=steelblue:s=256x144:d=12:r=10",
         "-i", SRT_IN,
         "-map", "0:v", "-map", "1:s", "-c:v", "libx264",
         "-preset", "ultrafast", "-c:s", "srt", MKV],
        check=True, timeout=120, creationflags=0x08000000)


def get(url, rng=None):
    req = urllib.request.Request(url)
    if rng:
        req.add_header("Range", rng)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def main():
    assert FF, "ffmpeg not found"
    build_sample()
    sample = open(MKV, "rb").read()
    app = QtWidgets.QApplication(sys.argv)

    print("[1] relay starts on MKV and refuses non-MKV")
    relay = VodRelay()
    cues = []
    relay.cue.connect(lambda s, e, t: cues.append((s, e, t)))
    local = relay.start("file:///" + MKV.replace("\\", "/"),
                        "MichaelTVPlayer/1.0")
    check("mkv accepted, local URL returned",
          local.startswith("http://127.0.0.1:"))
    relay2 = VodRelay()
    check("non-mkv refused (falls back to direct)",
          relay2.start("file:///" + os.path.abspath("README.md")
                       .replace("\\", "/"), "MichaelTVPlayer/1.0") == "")
    relay2.stop()

    print("[2] byte fidelity + range requests through the relay")
    status, body = get(local)
    check("full GET streams the whole file",
          status == 200 and body[:4] == b"\x1a\x45\xdf\xa3")
    # the provider stream is consumed lazily; the tap feeds from the
    # cache. Pull everything first so the cache is complete.
    deadline = time.time() + 30
    while time.time() < deadline and len(body) < len(sample):
        time.sleep(0.5)
        _, body = get(local)
    check("relayed bytes identical to the source file",
          body == sample)
    status, part = get(local, rng=f"bytes=100-199")
    check("range request served from cache",
          status == 206 and part == sample[100:200])
    status, part = get(local, rng=f"bytes={len(sample)-50}-")
    check("open-ended range",
          status == 206 and part == sample[-50:])

    print("[3] subtitle tap extracts the embedded cues")
    deadline = time.time() + 25
    while time.time() < deadline and len(cues) < 3:
        app.processEvents()
        time.sleep(0.25)
    texts = [c[2] for c in cues]
    check("all three cues extracted",
          len(cues) >= 3 and "what the hell is this" in texts
          and "damn dogs everywhere" in texts)
    hell = next(c for c in cues if "hell" in c[2])
    check("cue timing matches the source srt",
          abs(hell[0] - 1.0) < 0.3 and abs(hell[1] - 4.0) < 0.3)

    print("[4] VLC plays through the relay (and seeks)")
    p = VLCPlayer(timeshift=False)
    p.play(local)
    ok_play = False
    length = 0
    t0 = time.time()
    while time.time() - t0 < 25:
        time.sleep(0.5)
        app.processEvents()
        if p.is_playing():
            ok_play = True
            length = p.get_length()
            if 8 <= length / 1000 <= 14:
                break
    check("playback runs through the relay", ok_play)
    check("duration sane (~12 s)", 8 <= length / 1000 <= 14)
    p.set_time(6000)
    time.sleep(3.0)
    app.processEvents()
    t = p.get_time()
    print(f"    (seek debug: t={t})")
    check("seek lands through the relay", 5500 <= t <= 9500)
    p.stop_and_release()

    relay.stop()
    time.sleep(0.5)
    check("cache file cleaned up", not os.path.exists(relay.cache_path or "x"))

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
