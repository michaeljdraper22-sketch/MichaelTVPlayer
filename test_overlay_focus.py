# -*- coding: utf-8 -*-
"""Offscreen test: on-video controls must NEVER surface above other apps.

The overlay window is ToolTip-style (Windows paints it above everything),
so while the app is backgrounded (_overlay_suppressed, set by
_on_focus_changed) no wake path may show it — cursor over the app's
exposed area behind Chrome, DVR pill ticks, or info banners.

Run:  .venv\\Scripts\\python.exe test_overlay_focus.py
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


def main():
    app = QtWidgets.QApplication(sys.argv)
    view = PlayerView(Config.load())
    view.resize(1280, 720)
    view.show()
    app.processEvents()

    print("[1] backgrounded: cursor wake does not surface the overlay")
    # model a genuinely backgrounded app: the flag AND another app front
    view._app_foreground = lambda: False
    view._overlay_suppressed = True     # set by _on_focus_changed on focus loss
    view.overlay.hide()
    view.ctl.hide()
    view._wake()
    app.processEvents()
    check("overlay stays hidden on _wake", not view.overlay.isVisible())
    check("control bar stays hidden", not view.ctl.isVisible())

    print("[2] backgrounded: DVR status pill must not surface it either")
    view._set_dvr_status("DVR 12s / 20s buffered\u2026")
    app.processEvents()
    check("overlay stays hidden on pill update",
          not view.overlay.isVisible())

    print("[3] backgrounded: info banner doesn't surface it")
    view.show_info("Test Channel")
    app.processEvents()
    check("overlay stays hidden on show_info", not view.overlay.isVisible())

    print("[4] foreground: wake works normally again")
    del view._app_foreground            # back to the real method
    view._overlay_suppressed = False
    view._overlay_was_visible = False
    view._wake()
    app.processEvents()
    check("overlay shows when app is foreground", view.overlay.isVisible())
    check("control bar shows", view.ctl.isVisible())

    print("[4b] latched suppression self-heals when the app is foreground")
    # the real-world failure: a native color picker swallowed the focus
    # events, latching _overlay_suppressed with nothing to clear it
    view._overlay_suppressed = True
    view.overlay.hide()
    view.ctl.hide()
    view._app_foreground = lambda: True   # our process owns the foreground
    view._poll_cursor()
    check("poll self-heals a latched suppression",
          not view._overlay_suppressed)
    view._overlay_suppressed = True        # latch again, heal via _wake
    view._wake()
    check("wake works when app is truly foreground",
          view.overlay.isVisible() and view.ctl.isVisible())
    check("suppression cleared by wake", not view._overlay_suppressed)
    # genuinely backgrounded (another app owns the foreground): stays off
    view._overlay_suppressed = True
    view.overlay.hide()
    view.ctl.hide()
    view._app_foreground = lambda: False
    view._wake()
    check("still suppressed while another app is front",
          not view.overlay.isVisible() and view._overlay_suppressed)
    del view._app_foreground               # restore the real method

    print("[5] focus loss hides; focus regain restores")
    view._overlay_suppressed = True
    view._overlay_was_visible = True
    view.overlay.hide()
    view._on_focus_changed(None, view)     # app focused again
    app.processEvents()
    check("focus regain restores the overlay", view.overlay.isVisible())

    print("[6] pill works in the foreground (no regression)")
    view._set_dvr_status("DVR 12s / 20s buffered\u2026")
    check("pill visible with app foreground",
          view._dvr_status.isVisible() or not view._dvr_status.isHidden())
    view._set_dvr_status("")

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
