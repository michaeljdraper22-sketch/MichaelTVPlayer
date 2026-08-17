# -*- coding: utf-8 -*-
"""End-to-end DVR test: real provider + real PlayerView logic + real VLC.

Plays a live channel, switches DVR on, and verifies:
  - chase mode engages quickly (fast entry)
  - the buffer grows (frontier advances)
  - REWIND (-30s) actually moves playback backwards
  - JUMP LIVE returns near the live edge
  - FAST-FORWARD (+10s) works inside the buffer
Run:  .venv\\Scripts\\python.exe -X utf8 test_dvr_e2e.py
(a small real window will flash while the test runs)
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

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


try:
    print("[1] play live channel", flush=True)
    view.play_media(dict(CH))
    ok = wait_until(lambda: view.vlc.is_playing(), 25, "live playing")
    if not ok:
        # one retry: the provider throttles rapid re-connections
        print("  (retrying live open once)", flush=True)
        view.play_media(dict(CH))
        ok = wait_until(lambda: view.vlc.is_playing(), 30, "live playing 2")
    check("live playback starts", ok)
    pump(2)

    print("[2] switch DVR on", flush=True)
    t0 = time.time()
    view.btn_dvr.setChecked(True)
    ok = wait_until(lambda: view._mode == "chase", 30, "chase mode")
    t_chase = time.time() - t0
    check(f"chase engages (took {t_chase:.1f}s)", ok and t_chase < 15)
    ok = wait_until(lambda: view.vlc.is_playing(), 30, "chase playing")
    pump(4)
    check("chase playback running", view.vlc.is_playing())

    print("[3] buffer grows", flush=True)
    f1 = view._frontier_s()
    pump(5)
    f2 = view._frontier_s()
    check(f"frontier advances ({f1:.1f}s -> {f2:.1f}s)", f2 > f1 + 2.0)

    print("[4] rewind -30s (after the buffer has room)", flush=True)
    ok = wait_until(lambda: view._frontier_s() >= 45, 45,
                    "buffer >= 45s")
    check("buffer built up for rewinding", ok)
    p1 = view.vlc.get_time() / 1000.0
    expect = max(0.0, p1 - 30.0)
    view._seek_ms(-30000)
    pump(2.5)
    p2 = view.vlc.get_time() / 1000.0
    check(f"playhead rewound ({p1:.1f}s -> {p2:.1f}s, "
          f"expected ~{expect:.1f}s)",
          p2 <= expect + 6.0 and p2 < p1 - 5.0)

    print("[5] jump to LIVE", flush=True)
    view._jump_live()
    pump(2.5)
    p3 = view.vlc.get_time() / 1000.0
    fr = view._frontier_s()
    check(f"playhead near frontier ({p3:.1f}s vs {fr:.1f}s)",
          p3 > p2 + 5.0 and fr - p3 < 12.0)

    print("[6] fast-forward +10s (from behind the live edge)", flush=True)
    view._seek_ms(-30000)          # get safely behind the edge first —
    pump(2.0)                      # at the edge a +10s seek MUST clamp
    p4 = view.vlc.get_time() / 1000.0
    view._seek_ms(10000)
    pump(2.0)
    p5 = view.vlc.get_time() / 1000.0
    check(f"playhead moved forward ({p4:.1f}s -> {p5:.1f}s)",
          p5 > p4 + 4.0)
    check("and never lands past the live edge",
          p5 <= view._frontier_s() + 2.0)

    print("[6b] jump to BEGINNING", flush=True)
    view._jump_begin()
    pump(2.5)
    p6 = view.vlc.get_time() / 1000.0
    check(f"playhead back near 0 ({p6:.1f}s)", p6 < 5.0)
    view._jump_live()
    pump(2.5)
    p7 = view.vlc.get_time() / 1000.0
    fr2 = view._frontier_s()
    check(f"and LIVE returns to the edge ({p7:.1f}s vs {fr2:.1f}s)",
          fr2 - p7 < 15.0)

    print("[7] still playing after all seeks", flush=True)
    ok = wait_until(lambda: view.vlc.is_playing(), 15, "playing after seeks")
    check("playback alive", ok)
finally:
    view.stop()
    pump(1)

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed", flush=True)
for f in FAIL:
    print("  FAILED:", f)
os._exit(1 if FAIL else 0)
