# -*- coding: utf-8 -*-
"""End-to-end auto-caption test: real provider + real VLC + real vosk.

Verifies the caption pipeline redesign:
  - auto-captions can be enabled in PLAIN LIVE mode (wav fork grows)
  - enabling auto-captions in DVR mode does NOT jump playback to live and
    does NOT lose the buffer
  - the caption wav keeps growing while playback runs (it logs displayed
    audio), and a rewind re-tails the feed without killing it
Run:  .venv\\Scripts\\python.exe -X utf8 test_captions_e2e.py
(a small real window will flash while the test runs)
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src import captions as capmod  # noqa: E402
from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

CH = {
    "kind": "live",
    "title": "US: NFL NETWORK HD",
    "url": "http://cf.534842.xyz/live/726352471c/d809266e91/395713.ts",
    "stream_id": 395713,
}

app = QtWidgets.QApplication(sys.argv)
cfg = Config.load()
view = PlayerView(cfg)
view.resize(960, 540)
view.show()

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name, flush=True)


def pump(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.03)


def wait_until(pred, timeout, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    print(f"  (timeout waiting for {what} after {timeout}s)", flush=True)
    return False


def wav_size():
    try:
        return os.path.getsize(view._cap_wav())
    except OSError:
        return 0


if not (capmod.vosk_importable() and capmod.model_ready()):
    print("vosk or the speech model is missing — run the app once and "
          "enable auto-captions to install them, then re-run.")
    os._exit(1)

try:
    print("[1] play live channel", flush=True)
    view.play_media(dict(CH))
    ok = wait_until(lambda: view.vlc.is_playing(), 25, "live playing")
    check("live playback starts", ok)
    pump(2)

    print("[2] enable auto-captions in PLAIN LIVE mode", flush=True)
    t0 = time.time()
    view._set_caption("auto")
    ok = wait_until(lambda: view._cap_mode == "auto", 60, "cap mode")
    check("caption mode engaged", ok)
    ok = wait_until(lambda: wav_size() > 50000, 40, "wav fork writing")
    check(f"caption wav grows in live mode ({wav_size()} bytes)",
          ok and wav_size() > 50000)
    check("playback still running with the fork",
          view.vlc.is_playing())

    print("[3] switch DVR on with captions active", flush=True)
    view.btn_dvr.setChecked(True)
    ok = wait_until(lambda: view._mode == "chase", 30, "chase mode")
    check("chase engages with captions on", ok)
    ok = wait_until(lambda: view.vlc.is_playing(), 30, "chase playing")
    pump(4)
    check("chase playback running", view.vlc.is_playing())
    ok = wait_until(lambda: wav_size() > 100000, 30, "wav keeps growing")
    check("caption wav keeps growing in chase mode", ok)

    print("[4] rewind far behind live, then verify captions re-tail", flush=True)
    ok = wait_until(lambda: view._frontier_s() >= 60, 60, "buffer >= 60s")
    check("buffer built up", ok)
    view._jump_live()
    pump(2)
    fr = view._frontier_s()
    p_live = view._vid_s
    view._seek_ms(-40000)
    pump(2.5)
    p_rw = view._vid_s
    check(f"rewind moved the playhead back "
          f"({p_live:.1f}s -> {p_rw:.1f}s, frontier {fr:.1f}s)",
          p_rw < p_live - 20.0)

    print("[5] TOGGLE captions off/on MID-DVR: position must survive", flush=True)
    pos_before = view._vid_s
    frontier_before = view._frontier_s()
    view._set_caption("off")
    pump(1.5)
    view._set_caption("auto")
    pump(1.5)
    ok = wait_until(lambda: wav_size() > 50000, 40, "wav restarted")
    check("caption wav restarted", ok)
    still_chase = view._mode == "chase"
    pos_after = view._vid_s
    check(f"still in chase mode after caption toggle", still_chase)
    check(f"playback position preserved, not jumped to live "
          f"(was {pos_before:.1f}s, now {pos_after:.1f}s, "
          f"frontier {frontier_before:.1f}s)",
          abs(pos_after - pos_before) < 15.0)
    check("buffer NOT lost (frontier kept growing)",
          view._frontier_s() >= frontier_before - 1.0)
    pump(4)
    check("playback alive after the toggle", view.vlc.is_playing())
    ok = wait_until(lambda: wav_size() > 150000, 30, "wav still growing")
    check("captions keep flowing after the toggle", ok)

    print("[6] zombie guard: captioner survives a quick restart", flush=True)
    gen = view.captioner._gen
    view.captioner.stop()
    check("stop() invalidates the old worker", view.captioner._gen > gen)
    view._set_caption("off")   # clean state
finally:
    view.stop()
    pump(1)

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed", flush=True)
for f in FAIL:
    print("  FAILED:", f, flush=True)
os._exit(1 if FAIL else 0)
