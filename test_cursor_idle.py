# -*- coding: utf-8 -*-
"""Offscreen test: the idle cursor must blank over the video in EVERY mode.

The 2026-11-15 report: two MichaelTV windows tiled left/right, cursor left
sitting on one stream, and it never disappeared. _sleep() only set
BlankCursor inside `if self._immersive:` — plain windowed mode kept the
arrow forever. The blank now applies in all modes; the windowed-mode rails
(corner buttons stay visible, a cursor parked ON a visible control holds
the sleep, movement wakes the arrow back) are pinned here too.

Run:  .venv\\Scripts\\python.exe test_cursor_idle.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

BLANK = QtCore.Qt.BlankCursor
ARROW = QtCore.Qt.ArrowCursor

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def blanked(view):
    return (view.cursor().shape() == BLANK
            and view.overlay.cursor().shape() == BLANK)


def main():
    app = QtWidgets.QApplication(sys.argv)
    view = PlayerView(Config.load())
    view.resize(1280, 720)
    view.show()
    app.processEvents()

    print("[1] windowed mode: 4 s idle blanks the cursor over the video")
    view._wake()
    app.processEvents()
    check("awake: arrow on view and overlay",
          view.cursor().shape() == ARROW
          and view.overlay.cursor().shape() == ARROW)
    view._sleep()
    app.processEvents()
    check("asleep: cursor blanks on view AND overlay", blanked(view))
    check("windowed contract: corner buttons stay visible",
          view._btn_panel.isVisible() and view._btn_ovfs.isVisible()
          and view._btn_newwin.isVisible() and view._btn_reload.isVisible())
    check("control bar hidden by the sleep", not view.ctl.isVisible())

    print("[2] movement wakes the arrow back (the _poll_cursor path)")
    seq = [QtCore.QPoint(5, 5), QtCore.QPoint(9, 9)]
    QtGui.QCursor.pos = staticmethod(lambda: seq[0])
    view.mapFromGlobal = lambda p: p           # view sits at the origin
    view._poll_cursor()                        # seed _last_cursor
    seq[0] = QtCore.QPoint(9, 9)               # a real move inside the view
    view._poll_cursor()
    app.processEvents()
    check("cursor movement restores the arrow",
          view.cursor().shape() == ARROW
          and view.overlay.cursor().shape() == ARROW)
    check("movement re-arms the 4 s hide timer", view.hide_timer.isActive())

    print("[3] cursor parked on a visible corner button holds the sleep")
    view._wake()
    app.processEvents()
    view._btn_reload.underMouse = lambda: True
    view._sleep()
    app.processEvents()
    check("hovered button: sleep re-armed, no blank, bar stays",
          view.hide_timer.isActive() and not blanked(view)
          and view.ctl.isVisible())
    view._btn_reload.underMouse = lambda: False

    print("[4] zen and fullscreen: blank still applies, buttons hide too")
    view.set_zen(True)
    app.processEvents()
    check("zen: blank + corner buttons hidden",
          blanked(view) and not view._btn_panel.isVisible())
    view.set_zen(False)
    view.set_fullscreen_mode(True)
    app.processEvents()
    check("fullscreen: blank + corner buttons hidden",
          blanked(view) and not view._btn_panel.isVisible())
    view.set_fullscreen_mode(False)
    app.processEvents()
    check("leaving immersive wakes the arrow",
          view.cursor().shape() == ARROW)

    print("[5] two views (multi-window): each blanks on its own sleep")
    view2 = PlayerView(Config.load())
    view2.resize(640, 360)
    view2.show()
    app.processEvents()
    view._wake()
    view2._sleep()
    app.processEvents()
    check("sleeping view blanks, awake neighbor keeps its arrow",
          blanked(view2) and not blanked(view))
    view2.stop()

    view.stop()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
