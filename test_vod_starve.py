# -*- coding: utf-8 -*-
"""VOD stall watchdog's video-starvation trigger (2026-09-03 Elite Force
diagnosis follow-up).

The frozen-CLOCK and frozen-DEMUX tests are structurally blind to the
trickle shape a collapsing CDN produces: the provider cuts the body every
~260 KB, each reopen delivers a few hundred KB, and that ~300 KB/s keeps
VLC 'playing', its clock advancing (audio survives) and demux bytes
creeping — while the video output holds the last picture. The user sees
'video stuck, time bar rolling' (or a garbled slideshow) and has to
reload by hand. The third trigger measures the average DISPLAYED-PICTURE
rate over a 15 s window (time-based, immune to VBR quiet scenes) and
rescues through the existing reload path with the existing 2-rescue cap.

Also checks the per-media read-ahead plumbing exists (the :network-caching
option path in Player.play_at / the _VOD_READAHEAD_MS constant).

Run:  .venv\\Scripts\\python.exe test_vod_starve.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def make_view():
    app = QtWidgets.QApplication.instance() \
        or QtWidgets.QApplication(sys.argv)
    view = PlayerView(Config.load())
    view.current = {"kind": "series", "title": "t", "url": "http://x/v.mkv"}
    view.rescues = []
    view.play_media = lambda p, start_at=0.0: view.rescues.append(
        (dict(p), start_at))
    view._closing = False
    view._live_paused = False
    view._seeking = False
    view._vid_s = 100.0
    return view


def reset(view, t):
    view._vod_rescues = 0
    view._vod_raw_wall = t
    view._vod_demux_wall = t
    view._vod_pic_win_t = t
    view._vod_demux_last = None
    view._vod_pic_win_b = None


def tick_s(view, t, playing, raw_moved, demux, pics, dur=3000.0):
    """One watchdog call; returns True if a rescue fired this tick."""
    before = view._vod_rescues
    view._vod_stall_watchdog(t, playing, t - 1000.0 + 100.0, dur,
                             raw_moved, demux, pics)
    return view._vod_rescues != before


def main():
    view = make_view()
    t = 1000.0
    pics = 0
    demux = 0

    print("[1] healthy playback (24 fps, clock+demux moving) never fires")
    reset(view, t)
    fired = False
    for i in range(60):
        t += 1.0
        pics += 24
        demux += 4_000_000
        if tick_s(view, t, True, True, demux, pics):
            fired = True
    check("60 s of healthy playback fires nothing", not fired)

    print("[2] the trickle: 0 new pictures, clock+demux still creeping")
    reset(view, t)
    fired_at = None
    for i in range(30):
        t += 1.0
        demux += 300_000          # ~2.4 Mbps trickle keeps demux_moved True
        if tick_s(view, t, True, True, demux, pics):
            if fired_at is None:
                fired_at = i + 1
    check("video-starved rescue fires", fired_at is not None)
    check("fires within the 15 s window + one tick (16 s)",
          fired_at is not None and fired_at <= 17)
    check("rescue reopens a little before the stall (pos-3 s)",
          view.rescues and abs(view.rescues[-1][1] - 97.0) < 0.01)

    print("[3] garbled slideshow (3 fps) also counts as starved")
    reset(view, t)
    fired_at = None
    for i in range(30):
        t += 1.0
        pics += 3
        demux += 1_000_000
        if tick_s(view, t, True, True, demux, pics):
            if fired_at is None:
                fired_at = i + 1
    check("slideshow rescue fires", fired_at is not None)

    print("[4] disarm paths")
    reset(view, t)
    for i in range(20):           # paused: not playing
        t += 1.0
        tick_s(view, t, False, False, demux, pics)
    check("paused never rescues", view._vod_rescues == 0)
    view._seeking = True
    for i in range(20):           # mid-seek
        t += 1.0
        tick_s(view, t, True, False, demux, pics)
    view._seeking = False
    check("mid-seek never rescues", view._vod_rescues == 0)

    print("[5] the pre-existing frozen-clock trigger still works "
          "(no stats signal at all)")
    reset(view, t)
    fired = False
    for i in range(35):
        t += 1.0
        if tick_s(view, t, True, False, -1, -1):
            fired = True
    check("clock-frozen rescue still fires", fired)

    print("[6] rescue cap + near-end guard")
    reset(view, t)
    calls = len(view.rescues)
    for i in range(90):
        t += 1.0
        tick_s(view, t, True, True, demux, pics)
    check("cap holds at 2 rescues", len(view.rescues) - calls == 2)
    view._vid_s = 2990.0         # inside the 15 s end margin
    reset(view, t)
    for i in range(30):
        t += 1.0
        tick_s(view, t, True, True, demux, pics)
    check("near-end never rescues", view._vod_rescues == 0)
    view._vid_s = 100.0

    print("[7] read-ahead plumbing")
    check("_VOD_READAHEAD_MS constant is 15000",
          pv_mod._VOD_READAHEAD_MS == 15000)
    import inspect
    src = inspect.getsource(pv_mod.PlayerView.play_media)
    check("play_media passes network_caching_ms for VOD",
          "network_caching_ms=nc" in src.replace(" ", ""))
    import src.player as player_mod
    psrc = inspect.getsource(player_mod.VLCPlayer.play_at)
    check("play_at applies the per-media :network-caching option",
          ":network-caching=" in psrc)
    check("player exposes displayed_pictures()",
          hasattr(player_mod.VLCPlayer, "displayed_pictures"))

    print()
    print(f"vod-starve: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name in FAIL:
            print("  FAILED:", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
