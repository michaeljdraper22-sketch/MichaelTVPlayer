# -*- coding: utf-8 -*-
"""Offscreen test: channel-panel restore heals + autoplay button visuals.

Field report 2026-11-14:
1. "when I open the app the search panel is gone by default and the
   bring it back button does not bring it back — I had to press it and
   then drag it back from the left."  Root cause: sessions that closed
   with the panel hidden saved the splitter's 0-width report (floored to
   the 170px minimum); the next launch restored that sliver, and every
   re-show faithfully restored it again. Fixes: never CAPTURE or SAVE
   degenerate widths, and heal them at both restore points.
2. "the autoplay button stays blue highlighted when on" + "turning it
   off shows a duplicate autoplay symbol with an X over the old one."
   The button now overrides the theme's checked pill (state lives in the
   glyph: plain = ON, X-struck = OFF), and icon swaps flush the layered
   overlay so the previous glyph leaves no ghost pixels.

Run:  .venv\\Scripts\\python.exe test_panel_autoplay_fixes.py  (offscreen)
"""
import copy
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config, DEFAULTS  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402
from src.ui import icons as ic  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def main():
    app = QtWidgets.QApplication(sys.argv)
    tmp = Path(tempfile.gettempdir()) / "mtp_panel_auto_settings.json"
    cfg = Config(copy.deepcopy(DEFAULTS), tmp)
    win = MainWindow(cfg)
    app.processEvents()

    print("[1] hide/show round-trips the user's column width")
    win.splitter.setSizes([520, 900])
    app.processEvents()
    win.toggle_channels()
    check("hidden: tabs gone", win.tabs.isHidden())
    saved = list(win._splitter_saved or [])
    check("width captured on hide", len(saved) == 2 and saved[0] >= 400)
    win.toggle_channels()
    app.processEvents()
    check("shown: tabs back", not win.tabs.isHidden())
    check("width restored on show", win.splitter.sizes()[0] >= 400)

    print("[2] a degenerate capture heals on restore (the dead-button bug)")
    win.toggle_channels()               # hide (captures good widths)
    win._splitter_saved = [0, 1400]     # pre-fix poison, as field-seen
    win.toggle_channels()               # show -> must heal, not restore 0
    app.processEvents()
    check("chevron/corner show heals a 0-width capture",
          win.splitter.sizes()[0] >= 400)

    print("[3] degenerate widths are never captured")
    win._splitter_saved = [520, 900]
    win.tabs.hide()                     # splitter now reports [0, ...]
    app.processEvents()
    win._save_splitter_sizes()
    check("hidden column does not overwrite the good capture",
          list(win._splitter_saved)[0] >= 400)
    win.tabs.show()
    app.processEvents()

    print("[4] _save_state keeps remembered widths while the panel is hidden")
    win.config.splitter_sizes = [520, 900]
    win._channels_hidden = True
    win.tabs.hide()
    win._save_state()
    check("hidden close does not save the 170px floor",
          list(win.config.splitter_sizes)[0] >= 400)
    win._channels_hidden = False
    win.tabs.show()
    win._save_state()
    check("visible close saves real widths",
          list(win.config.splitter_sizes)[0] >= 170)

    print("[5] _restore_state heals poisoned settings (panel-gone-on-open)")
    win.config.splitter_sizes = [170, 1230]
    win._restore_state()
    app.processEvents()
    check("a 170px sliver in settings opens at the standard column",
          win.splitter.sizes()[0] >= 400)

    print("[6] autoplay button: no blue pill, glyph carries the state")
    view = win.player_view
    check("autoplay starts checked (ON)", view.btn_auto.isChecked())
    check("autoplay has the no-blue-checked override",
          "QToolButton:checked" in view.btn_auto.styleSheet())
    check("mute keeps the theme styling (no override)",
          view.btn_mute.styleSheet() == "")
    key_on = view.btn_auto.icon().cacheKey()
    view.btn_auto.click()
    app.processEvents()
    check("click turns autoplay off in the config",
          view.config.autoplay_next is False)
    check("icon swapped to the X-struck glyph",
          view.btn_auto.icon().cacheKey() != key_on)
    check("tooltip says OFF", "OFF" in view.btn_auto.toolTip())
    check("the two glyphs are distinct icons",
          ic.autoplay(True).cacheKey() != ic.autoplay(False).cacheKey())
    view.btn_auto.click()
    app.processEvents()
    check("click turns autoplay back on",
          view.config.autoplay_next is True and view.btn_auto.isChecked())

    win.close()
    try:
        tmp.unlink()
    except OSError:
        pass
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
