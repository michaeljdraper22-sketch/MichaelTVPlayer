# -*- coding: utf-8 -*-
"""Offscreen regression test: download progress anchors JUST ABOVE the
download button (VOD/Stremio button, or the gold window button on
catch-up) instead of parking in the middle of the picture. Every other
DVR-status pill stays centered, the anchor survives the controls going
to sleep, and a pill wider than the window clamps inside the video.

Run:  .venv\\Scripts\\python.exe test_dl_pill.py   (sets QT_QPA_PLATFORM itself)
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


VOD = {"kind": "vod", "title": "Test Movie",
       "url": "http://x/movie.mp4", "fav_key": "vod:1"}
CATCHUP = {"kind": "catchup", "title": "Yestercast", "url": "http://x/c.ts",
           "stream_id": 7, "utc_start": 1700000000, "fav_key": "cu:1"}


def main():
    app = QtWidgets.QApplication(sys.argv)
    cfg = Config.load()
    cfg.data["control_buttons"] = dict(cfg.control_buttons)  # timebar True
    view = PlayerView(cfg)
    view.resize(1280, 720)
    view.show()
    app.processEvents()

    def pill():
        return view._dvr_status.geometry()

    def center_x(w):
        return w.mapTo(view.overlay, w.rect().center()).x()

    def play(kind_media, mode):
        view.current = dict(kind_media)
        view._mode = mode
        view._update_control_state()
        view._wake()
        view.hide_timer.stop()   # keep the bar awake: no 4s sleep mid-test
        view.ctl.adjustSize()
        app.processEvents()

    print("[1] VOD download pill sits just above the Download button")
    play(VOD, "vod")
    view._set_dvr_status("Downloading\u2026 12 / 34 MB")
    app.processEvents()
    p = pill()
    btn_cx = center_x(view.btn_dl)
    check("pill visible", view._dvr_status.isVisible())
    check(f"pill bottom clears the control bar by ~6px "
          f"(bottom={p.bottom()} bar top={view.ctl.y()})",
          abs(p.bottom() - (view.ctl.y() - 6)) <= 2)
    check(f"pill centered on the download button "
          f"(pill cx={p.center().x()} btn cx={btn_cx})",
          abs(p.center().x() - btn_cx) <= 2)
    check("pill is NOT mid-screen anymore",
          abs(p.center().y() - view.surface.geometry().center().y()) > 40)
    check("pill inside the video area",
          p.left() >= 0 and p.right() <= view.surface.geometry().width())

    print("[2] non-download pills stay centered on the video")
    view._set_dvr_status("DVR 5s / 20s buffered\u2026")
    app.processEvents()
    p = pill()
    g = view.surface.geometry()
    check("centered pill back at mid-screen",
          abs(p.center().y() - g.center().y()) <= 2
          and abs(p.center().x() - g.center().x()) <= 2)

    print("[3] anchor survives the controls going to sleep")
    view.ctl.hide()
    view._set_dvr_status("Downloading\u2026 13 / 34 MB")
    app.processEvents()
    p = pill()
    check("pill still just above where the bar sleeps",
          abs(p.bottom() - (view.ctl.y() - 6)) <= 2)
    check("pill horizontally still on the button",
          abs(p.center().x() - center_x(view.btn_dl)) <= 2)

    print("[4] catch-up window pill anchors above the gold window button")
    view.ctl.show()
    view.hide_timer.stop()
    play(CATCHUP, "vod")
    view._set_dvr_status(
        "Download window <  12:00 \u2013 12:30 (30m)  \u2014  drag or click")
    app.processEvents()
    p = pill()
    check("pill above the window-download button",
          abs(p.bottom() - (view.ctl.y() - 6)) <= 2
          and abs(p.center().x() - center_x(view.btn_win)) <= 2)

    print("[5] window download in flight keeps the same anchor")
    view._downloading = True   # flight state, as _start_window_download sets
    view._set_dvr_status("Downloading window\u2026")
    app.processEvents()
    p = pill()
    check("in-flight window pill above the window button",
          abs(p.bottom() - (view.ctl.y() - 6)) <= 2
          and abs(p.center().x() - center_x(view.btn_win)) <= 2)
    view._downloading = False

    print("[6] narrow window: pill clamps inside the video")
    view.current = dict(VOD)
    view._update_control_state()
    view.resize(420, 300)
    app.processEvents()
    view._set_dvr_status("Downloading\u2026 1234 / 5678 MB")
    app.processEvents()
    p = pill()
    w = view.surface.geometry().width()
    check(f"clamped pill stays inside ({p.left()}..{p.right()} of {w})",
          p.left() >= 0 and p.right() <= w)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
