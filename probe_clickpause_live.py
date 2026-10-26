# -*- coding: utf-8 -*-
"""Real-desktop probe: WHERE do clicks land on the playing video?

The offscreen probe (probe_inputs.py) proves the WIDGET logic —
VideoSurface.mousePressEvent -> single_clicked -> _toggle_pause — by
injecting Qt events straight into the widget. It cannot see the NATIVE
routing a physical click takes: layered overlay window (per-pixel alpha)
over a native video HWND that libvlc renders into through its own child
window ("VLC video output"). If any of those swallows the press, the
user clicks the video and nothing pauses.

This probe runs the REAL windowed app (never activated, VLC muted),
plays the local test mkv, then WindowFromPoint — the same routing real
mouse clicks use — across a grid of the video surface. Each point is
identified as one of: the overlay window (controls / hit plates), the
VideoSurface HWND, VLC's vout child, or something else.

Run:  .venv\\Scripts\\python.exe probe_clickpause_live.py
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.localplay import make_file_playable  # noqa: E402
import src.ui.main_window as mw  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class StubClient:
    def __init__(self, *a, **k):
        pass

    def authenticate(self):
        raise RuntimeError("offline probe")

    def live_categories(self):
        return [{"category_id": 1, "category_name": "News"}]

    def vod_categories(self):
        return [{"category_id": 1, "category_name": "Action"}]

    def series_categories(self):
        return [{"category_id": 1, "category_name": "Drama"}]

    def live_streams(self, cat_id):
        return [{"name": "Live Chan", "stream_id": 11}]

    def vod_streams(self, cat_id, timeout=None):
        return [{"name": "A Movie", "stream_id": 22,
                 "container_extension": "mp4"}]

    def series(self, cat_id, timeout=None):
        return [{"name": "A Series", "series_id": 33}]


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def enum_children(hwnd):
    """Direct child HWNDs of ``hwnd`` (class names)."""
    out = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                  wintypes.LPARAM)
    user32.EnumChildWindows.argtypes = [wintypes.HWND, EnumProc,
                                        wintypes.LPARAM]

    def cb(child, _l):
        out.append((child, class_name(child)))
        return True
    user32.EnumChildWindows(hwnd, EnumProc(cb), 0)
    return out


def window_from_point(x, y):
    pt = wintypes.POINT(x, y)
    return user32.WindowFromPoint(pt)


def identify(hwnd, known):
    for label, h in known.items():
        if hwnd == h:
            return label
    return "%s(%x)" % (class_name(hwnd), hwnd or 0)


def main():
    app = QtWidgets.QApplication(sys.argv)
    mw.XtreamClient = StubClient
    cfg = Config({}, None)                # isolated config
    win = MainWindow(cfg)
    pv = win.player_view

    # never activate: the user may be watching TV on this desktop
    win.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    win.move(40, 40)
    win.resize(960, 600)
    win.show()
    pump(app, 1.0)

    media = make_file_playable(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "probe_jump_media.mkv"))
    pv.play_media(media, start_at=0.0)

    # wait for the vout to exist (local file: fast), max ~12 s; mute the
    # moment playback is actually rolling (silent, per testing rules)
    surf_hwnd = None
    vout_children = []
    muted = [False]

    def _mute():
        try:
            pv.vlc.player.audio_set_volume(0)
            pv.vlc.player.audio_set_mute(True)
            muted[0] = True
        except Exception:
            pass

    playing_seen = False
    for _ in range(240):
        pump(app, 0.05)
        if not playing_seen and pv.vlc.state_name() == "playing":
            playing_seen = True
            _mute()
            QtCore.QTimer.singleShot(2000, _mute)
        surf_hwnd = int(pv.surface.winId())
        kids = enum_children(surf_hwnd)
        if any("VLC" in c or "video" in c.lower() for _h, c in kids):
            vout_children = kids
            break
    else:
        vout_children = enum_children(surf_hwnd)
    state = pv.vlc.state_name()
    print("vlc state: %s (playing seen: %s, muted: %s)   "
          "surface hwnd: %x" % (state, playing_seen, muted[0], surf_hwnd))
    if state != "playing":
        # give it one more moment and report; probe anyway
        pump(app, 3.0)
        print("vlc state (later): %s" % pv.vlc.state_name())
        _mute()
    for h, c in vout_children:
        print("  surface child: %s (%x)" % (c, h))

    known = {
        "overlay(window)": int(pv.overlay.winId()),
        "VideoSurface": surf_hwnd,
        "main-window": int(win.winId()),
    }
    for h, c in vout_children:
        if h not in known:
            known["vlc-vout-child"] = h

    g = pv.surface.geometry()
    top_left = pv.surface.mapToGlobal(QtCore.QPoint(0, 0))
    gx, gy = top_left.x(), top_left.y()

    def probe_grid(label, cols=7, rows=5):
        print("[%s] WindowFromPoint over the video "
              "(%dx%d px at %d,%d)" % (label, g.width(), g.height(), gx, gy))
        hits = {}
        dead = []
        for r in range(rows):
            line = []
            for c in range(cols):
                x = gx + g.width() * (2 * c + 1) // (2 * cols)
                y = gy + g.height() * (2 * r + 1) // (2 * rows)
                hwnd = window_from_point(x, y)
                who = identify(hwnd, known)
                hits[who] = hits.get(who, 0) + 1
                if who.startswith("unknown") or who.startswith("vlc"):
                    dead.append((x, y, who))
                line.append("%-16s" % who)
            print("   " + " ".join(line))
        print("  tally: %r" % hits)

    # controls asleep is the steady state a viewer clicks in; give the
    # wake timer time to sleep first, then also probe awake (bar shown)
    pump(app, 4.0)
    try:
        pv._sleep(force=True)
    except Exception:
        pass
    pump(app, 0.2)
    probe_grid("controls asleep")

    pv._wake()
    pump(app, 0.3)
    probe_grid("controls awake")

    # END-TO-END: a single click on the bare video must actually pause
    # the playing media, and the next click resume it (events injected
    # into the overlay widget — the window real clicks now land on)
    print("[round trip] click -> pause -> click -> resume")
    from PyQt5.QtTest import QTest
    oc = QtCore.QPoint(pv.overlay.width() // 2, pv.overlay.height() // 2)
    QTest.mouseClick(pv.overlay, QtCore.Qt.LeftButton, pos=oc)
    pump(app, 0.2)
    pump(app, 0.8)     # single/double-click disambiguation interval
    st1 = pv.vlc.state_name()
    QTest.mouseClick(pv.overlay, QtCore.Qt.LeftButton, pos=oc)
    pump(app, 0.2)
    pump(app, 0.8)
    st2 = pv.vlc.state_name()
    print("  after 1st click: %s   after 2nd click: %s" % (st1, st2))
    print("  ROUND TRIP: %s"
          % ("OK" if st1 == "paused" and st2 == "playing" else "BROKEN"))

    verdict_surf = pv.surface.isVisible()
    print("surface visible: %s" % verdict_surf)

    win.close()
    pump(app, 0.5)
    app.processEvents()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
