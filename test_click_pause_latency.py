# -*- coding: utf-8 -*-
"""Offscreen regression: a click on the video pauses IMMEDIATELY.

The 2026-11-17 report: clicking the picture took noticeably longer to
pause than clicking the pause button. The click handlers waited out the
OS double-click interval (500 ms+ on a default Windows box) before
firing the single-click action; the button never did.

The single click now acts on the PRESS. A following double-click — its
second press arrives as Qt's MouseButtonDblClick, which REPLACES the
press — emits single_cancelled so the toggle is undone, then requests
fullscreen. Net result of a double-click: no pause, fullscreen.

Run:  .venv\\Scripts\\python.exe test_click_pause_latency.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def click(widget):
    """One physical left click: press + release through the widget."""
    QTest.mouseClick(widget, QtCore.Qt.LeftButton,
                     pos=widget.rect().center())


def double_click(widget):
    """A PHYSICAL double-click: the first press goes through mousePress
    (Qt sends it normally), the second arrives as MouseButtonDblClick —
    exactly the sequence Windows/libvlc deliver. QTest.mouseDClick alone
    only sends the dblclick event, which skips the immediate action."""
    click(widget)
    pos = widget.rect().center()
    ev = QtGui.QMouseEvent(
        QtCore.QEvent.MouseButtonDblClick,
        QtCore.QPointF(pos), QtCore.QPointF(widget.mapToGlobal(pos)),
        QtCore.Qt.LeftButton, QtCore.Qt.LeftButton,
        QtCore.Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(widget, ev)


def let_click_window_close():
    """Past the OS double-click window: a pending single is final."""
    QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 60)


def main():
    app = QtWidgets.QApplication(sys.argv)
    view = PlayerView(Config.load())
    view.resize(1280, 720)
    view.show()
    # the mouse layer + its popup only show with a rendering video in the
    # real app; offscreen there is no vout, so show the layer explicitly
    view.overlay.show()
    app.processEvents()
    QTest.qWait(200)
    # a loaded, playing live stream (the real VLC player is idle: the
    # toggle is a no-op there, the STATE flags are what these checks read)
    view.current = {"title": "Probe", "kind": "live"}
    view._mode = "live"
    view._live_paused = False
    app.processEvents()

    surf = view.surface
    ov = view.overlay

    print("[1] surface click pauses synchronously (no event loop in between)")
    click(surf)
    check("state flips during the click itself", view._live_paused is True)
    let_click_window_close()
    check("an ordinary click stays paused (no undo fires)",
          view._live_paused is True)

    print("[2] the next click resumes just as fast")
    click(surf)
    check("state flips during the click itself", view._live_paused is False)
    let_click_window_close()
    check("stays playing", view._live_paused is False)

    print("[3] physical double-click: immediate pause is undone, "
          "fullscreen runs")
    fs = []
    view.request_fullscreen.connect(lambda: fs.append(1))
    double_click(surf)
    check("first press of the pair acted at once (paused, then undone)",
          view._live_paused is False)
    check("double-click requested fullscreen once", len(fs) == 1)
    let_click_window_close()
    check("still playing after the double-click", view._live_paused is False)

    print("[4] the overlay hit plate (the REAL mouse layer) behaves the same")
    view._live_paused = False
    click(ov)
    check("overlay click pauses synchronously", view._live_paused is True)
    let_click_window_close()
    check("overlay click stays paused", view._live_paused is True)
    fs.clear()
    double_click(ov)
    check("overlay double-click undoes the pause (stays paused)",
          view._live_paused is True)
    check("overlay double-click requested fullscreen once", len(fs) == 1)

    print("[5] click-away on an open control popup never toggles or undoes")
    view._live_paused = False
    view._ctl_panel.show()
    app.processEvents()
    check("popup actually open", view._ctl_panel.isVisible())
    click(ov)
    check("popup closed by the click", not view._ctl_panel.isVisible())
    check("playback untouched", view._live_paused is False)
    let_click_window_close()

    print("[6] pre-stream click is still a quiet no-op")
    view._live_paused = False
    view.current = None
    click(surf)
    double_click(surf)
    check("nothing loaded -> nothing toggled", view._live_paused is False)
    let_click_window_close()

    view.stop()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
