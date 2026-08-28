# -*- coding: utf-8 -*-
"""Live probe: pre-stream button states + popup close-on-outside-click.

No stream, no audio; window lowered (never activated).
"""
import ctypes
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def wait(app, ms):
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec_()
    app.processEvents()


def click_qt(app, widget, local):
    gp = widget.mapToGlobal(local)
    ev = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, local, gp,
                           QtCore.Qt.LeftButton, QtCore.Qt.LeftButton,
                           QtCore.Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(widget, ev)
    app.processEvents()


def main():
    from src.config import Config
    from src.ui.main_window import MainWindow
    from src.ui.theme import apply_theme

    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    MainWindow._restore_state = lambda self: None
    win = MainWindow(Config.load())
    win._save_state = lambda: None
    win.resize(1180, 760)
    win.show()
    win.lower()
    app.processEvents()
    wait(app, 900)

    pv = win.player_view
    pv._wake()
    app.processEvents()

    print("[1] pre-stream button states")
    for name, want in (("btn_play", False), ("btn_back60", False),
                       ("btn_back10", False), ("btn_fwd10", False),
                       ("btn_rec", False), ("btn_audio", False),
                       ("btn_speed", True), ("btn_cc", True),
                       ("btn_scale", True), ("btn_mute", True),
                       ("btn_auto", True), ("btn_next", False)):
        b = getattr(pv, name)
        check(f"{name} enabled={want}", b.isEnabled() == want
              and b.isVisibleTo(win) or b.isEnabled() == want)

    print("[2] popup opens from scale, closes on native press on the video")
    # instrument: did the closer path even run?
    calls = []
    orig = pv._ctl_panel._closer.maybe_close_at
    def _spy(gp, _orig=orig):
        calls.append((gp.x(), gp.y()))
        return _orig(gp)
    pv._ctl_panel._closer.maybe_close_at = _spy
    orig_filter = pv._ctl_panel._closer.eventFilter
    pv._scale_menu()
    app.processEvents()
    wait(app, 150)
    check("scale card visible", pv._ctl_panel.isVisible())
    # native WM_LBUTTONDOWN posted to the video surface HWND (client pt)
    user32 = ctypes.windll.user32
    hwnd = int(pv.surface.winId())
    # pick a point on the surface far from the panel (top-left area)
    pt = QtCore.QPoint(30, 30)
    lparam = wintypes.MAKELPARAM(pt.x(), pt.y()) \
        if hasattr(wintypes, "MAKELPARAM") else (pt.y() << 16 | pt.x())
    user32.PostMessageW(hwnd, 0x0201, 1, lparam)   # WM_LBUTTONDOWN
    wait(app, 300)
    print("   closer calls seen:", calls)
    check("card closed by native press on video",
          not pv._ctl_panel.isVisible())

    print("[3] popup from cc button, closes on Qt press on channel list")
    pv._wake()
    app.processEvents()
    pv._subs_menu()
    app.processEvents()
    wait(app, 150)
    check("subs card visible", pv._ctl_panel.isVisible())
    list_w = win.live_tab.list
    click_qt(app, list_w, QtCore.QPoint(30, 60))
    wait(app, 200)
    print("   closer calls after Qt press:", calls)
    qt_closed = not pv._ctl_panel.isVisible()
    if not qt_closed:
        # separate plumbing from decision: feed the same global point in
        gp = list_w.mapToGlobal(QtCore.QPoint(30, 60))
        pv._ctl_panel._closer.maybe_close_at(gp)
        app.processEvents()
        print("   direct maybe_close_at at", (gp.x(), gp.y()),
              "-> closed:", not pv._ctl_panel.isVisible())
    check("card closed by Qt press on the channel list", qt_closed)

    print("[4] popup toggle: same button closes, other button swaps")
    pv._wake(); app.processEvents()
    pv._scale_menu(); app.processEvents(); wait(app, 100)
    check("scale card open again", pv._ctl_panel.isVisible())
    pv._scale_menu(); app.processEvents(); wait(app, 100)
    check("same button closes (toggle)", not pv._ctl_panel.isVisible())

    win.close()
    app.processEvents()
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
