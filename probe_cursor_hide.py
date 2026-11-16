# -*- coding: utf-8 -*-
"""Offline repro: does the idle cursor blank over the video?

User report (2026-11-15): two MichaelTV windows tiled left/right, cursor
left sitting on one stream, and it NEVER disappeared. _sleep() only sets
BlankCursor inside `if self._immersive:` — plain windowed mode keeps the
arrow forever. This probe pins the current behavior in all three modes so
the fix has a before/after.

Run:  .venv\\Scripts\\python.exe probe_cursor_hide.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

BLANK = QtCore.Qt.BlankCursor


def cursor_state(view):
    return (view.cursor().shape(), view.overlay.cursor().shape())


def main():
    app = QtWidgets.QApplication(sys.argv)
    view = PlayerView(Config.load())
    view.resize(1280, 720)
    view.show()
    app.processEvents()

    print("[1] plain windowed mode (the reported scenario)")
    view._wake()
    app.processEvents()
    print("   after wake:      view=%s overlay=%s" % cursor_state(view))
    view._sleep()
    app.processEvents()
    v, o = cursor_state(view)
    print("   after sleep:     view=%s overlay=%s" % (v, o))
    print("   -> cursor %s over the video" %
          ("BLANKS" if BLANK in (v, o) else "STAYS VISIBLE  <-- the bug"))

    print("[2] zen mode")
    view.set_zen(True)
    app.processEvents()
    v, o = cursor_state(view)
    print("   after sleep:     view=%s overlay=%s" % (v, o))
    print("   -> cursor %s over the video" %
          ("blanks" if BLANK in (v, o) else "STAYS VISIBLE (regression!)"))
    view.set_zen(False)
    app.processEvents()

    print("[3] fullscreen mode")
    view.set_fullscreen_mode(True)
    app.processEvents()
    v, o = cursor_state(view)
    print("   after sleep:     view=%s overlay=%s" % (v, o))
    print("   -> cursor %s over the video" %
          ("blanks" if BLANK in (v, o) else "STAYS VISIBLE (regression!)"))
    view.set_fullscreen_mode(False)
    app.processEvents()

    print("[4] wake restores the arrow (post-fix contract)")
    view._wake()
    app.processEvents()
    v, o = cursor_state(view)
    print("   after wake:      view=%s overlay=%s" % (v, o))

    print("[5] cursor parked on a visible corner button holds the sleep")
    # windowed mode keeps the corner buttons up; _cursor_on_controls must
    # re-arm the timer instead of sleeping (no blank under a hovered button)
    view._wake()
    app.processEvents()
    view._btn_reload.underMouse = lambda: True
    view._sleep()
    app.processEvents()
    print("   hide_timer re-armed: %s  ctl visible: %s"
          % (view.hide_timer.isActive(), view.ctl.isVisible()))
    view._btn_reload.underMouse = lambda: False

    view.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
