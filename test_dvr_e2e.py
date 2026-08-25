# -*- coding: utf-8 -*-
"""End-to-end DVR test: real provider + real PlayerView logic + real VLC.

Plays a live channel (always-on chase engages automatically) and verifies:
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

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

CH = {
    "kind": "live",
    "title": "US: NFL NETWORK ᴴᴰ",
    # 395713 (NFL NETWORK HD) went provider-broken on 2026-08-25: VLC
    # receives data but the TS muxer writes zero sout bytes (direct reads
    # still deliver), which starved this suite for non-app reasons.
    # 1031378 is the same network and currently muxes fine.
    "url": "http://cf.534842.xyz/live/726352471c/d809266e91/1031378.ts",
    "stream_id": 1031378,
}

app = QtWidgets.QApplication(sys.argv)
cfg = Config.load()
view = PlayerView(cfg)
view.resize(960, 540)
# never steal focus or audio (the user is watching TV)
view.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
view.showMinimized()
view.vol_slider.setValue(0)
view.btn_mute.setChecked(True)
view.vlc.set_mute(True)
view.vlc.set_volume(0)

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
    print("[1] play live channel (chase engages automatically)", flush=True)
    view.play_media(dict(CH))
    ok = wait_until(lambda: view._mode == "chase", 30, "chase mode")
    check("always-on chase engages on play", ok)
    t0 = time.time()
    ok = wait_until(lambda: view.vlc.is_playing(), 30, "chase playing")
    t_chase = time.time() - t0
    check(f"chase engages (took {t_chase:.1f}s)", ok and t_chase < 15)
    pump(4)
    check("chase playback running", view.vlc.is_playing())

    print("[3] buffer grows", flush=True)
    f1 = view._frontier_s()
    f2 = f1
    # VLC flushes the sout file in 2-4 s bursts: sample across a few
    # windows before declaring the clock frozen.
    for _ in range(4):
        pump(4)
        f2 = view._frontier_s()
        if f2 > f1 + 2.0:
            break
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
    # stage 2: the LIVE edge is the PCR-calibrated content head, which
    # sits 20-35 s of content PAST the wall-credited frontier (cold-burst
    # under-credit) — a clamped seek may land there, never beyond it
    check("and never lands past the TRUE live edge",
          p5 <= view._cap_edge_s() + 2.0)

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
