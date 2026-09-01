# -*- coding: utf-8 -*-
"""Offscreen probe for the four requested player-input behaviors.

[1] autoplay button default state (fresh config)
[2] Space key -> toggle_pause, with focus in: nothing / channel list /
    search box / player view / video surface
[3] single left click on the video -> pause
[4] double click on the video -> fullscreen request

Run:  .venv\\Scripts\\python.exe probe_inputs.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

from src.config import Config  # noqa: E402
import src.ui.main_window as mw  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402

PAUSE_CALLS = []
FS_REQUESTS = []


class StubClient:
    """Offline XtreamClient."""

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

    def live_url(self, sid):
        return "http://example/live"

    def vod_streams(self, cat_id, timeout=None):
        return [{"name": "A Movie", "stream_id": 22,
                 "container_extension": "mp4"}]

    def series(self, cat_id, timeout=None):
        return [{"name": "A Series", "series_id": 33}]


def pump(seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def space_probe(label, focus_widget):
    focus_widget.setFocus()
    app.processEvents()
    n0 = len(PAUSE_CALLS)
    QTest.keyClick(focus_widget, QtCore.Qt.Key_Space)
    app.processEvents()
    n = len(PAUSE_CALLS) - n0
    print(f"  Space with focus on {label:<14} -> toggle_pause x{n}"
          + ("   <-- DEAD" if n == 0 else ""))
    return n


def main():
    mw.XtreamClient = StubClient          # offline client
    cfg = Config({}, None)                # isolated config
    win = MainWindow(cfg)
    pv = win.player_view
    pv._toggle_pause = lambda: PAUSE_CALLS.append(1)
    pv.request_fullscreen.connect(lambda: FS_REQUESTS.append(1))
    win.show()
    pump(1.0)

    print("[1] autoplay button default state")
    print(f"  config.autoplay_next = {cfg.autoplay_next}")
    print(f"  btn_auto.isChecked() = {pv.btn_auto.isChecked()}")
    print(f"  btn_auto.toolTip()   = {pv.btn_auto.toolTip()!r}")

    print("[2] Space key -> toggle_pause (nothing loaded, patched counter)")
    space_probe("main window", win)
    space_probe("channel list", win.live_tab.list)
    space_probe("search box", win.live_tab.search)
    space_probe("player view", pv)
    space_probe("video surface", pv.surface)

    print("[3] single left click on the video surface")
    surf = pv.surface
    center = QtCore.QPoint(surf.width() // 2, surf.height() // 2)
    n0 = len(PAUSE_CALLS)
    QTest.mouseClick(surf, QtCore.Qt.LeftButton, pos=center)
    pump(0.8)     # long enough for any single/double-click timer
    n = len(PAUSE_CALLS) - n0
    print(f"  click -> toggle_pause x{n}" + ("   <-- NOT IMPLEMENTED" if n == 0 else ""))

    print("[3b] click while the control popup is open -> click-away only")
    pv._ctl_panel.show()
    pump(0.1)
    QTest.mouseClick(surf, QtCore.Qt.LeftButton, pos=center)
    pump(0.8)
    print(f"  popup visible after click: {pv._ctl_panel.isVisible()}")

    print("[4] double click on the video surface")
    n0 = len(FS_REQUESTS)
    p0 = len(PAUSE_CALLS)
    QTest.mouseDClick(surf, QtCore.Qt.LeftButton, pos=center)
    pump(0.8)
    n = len(FS_REQUESTS) - n0
    p = len(PAUSE_CALLS) - p0
    print(f"  double-click -> fullscreen requests x{n}"
          + ("   <-- DEAD" if n == 0 else ""))
    print(f"  double-click -> accidental pauses x{p}"
          + ("   <-- BAD" if p else ""))

    print("[5] Space after playback hands focus to the player")
    win.live_tab.search.setFocus()
    app.processEvents()
    n0 = len(PAUSE_CALLS)
    QTest.keyClick(win.live_tab.search, QtCore.Qt.Key_Space)
    app.processEvents()
    typed = len(PAUSE_CALLS) - n0
    pv.setFocus(QtCore.Qt.OtherFocusReason)   # what MainWindow.play() now does
    app.processEvents()
    n0 = len(PAUSE_CALLS)
    QTest.keyClick(pv, QtCore.Qt.Key_Space)
    app.processEvents()
    focused = len(PAUSE_CALLS) - n0
    print(f"  Space in search box (typing) x{typed}, Space once player "
          f"focused x{focused}")

    print("[6] Enter in the search box activates the first row")
    win.live_tab.media_activated.disconnect(win.play)
    acts = []
    win.live_tab.media_activated.connect(lambda p: acts.append(p))
    win.live_tab.search.clear()          # flush the space typed in [5]
    pump(0.4)
    win.live_tab.list.clear()
    win.live_tab.list.addItem("Chan A")
    win.live_tab.list.item(0).setData(
        QtCore.Qt.UserRole, {"name": "Chan A", "stream_id": 11})
    QTest.keyClick(win.live_tab.search, QtCore.Qt.Key_Return)
    pump(0.4)
    print(f"  Enter -> media_activated x{len(acts)}"
          + (f" ({acts[0].get('title')!r})" if acts else "   <-- DEAD"))

    win.close()


app = QtWidgets.QApplication(sys.argv)
main()
