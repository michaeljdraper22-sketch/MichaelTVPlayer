# -*- coding: utf-8 -*-
"""Offscreen regression test: playing a Stremio stream hides the channel
list automatically, so the user doesn't have to hide it manually on every
handoff. A normal live-channel play must leave the panel alone, bringing
the list back (Ctrl+L / corner button) must restore the saved splitter
widths, and a Stremio play while the list is ALREADY hidden must not
clobber the sizes the user's own hide saved.

Run:  .venv\\Scripts\\python.exe test_stremio_panel.py   (sets QT_QPA_PLATFORM itself)
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else " FAIL ") + name)


app = QtWidgets.QApplication(sys.argv)


class FakePlayerView(QtWidgets.QWidget):
    """Just enough of PlayerView for MainWindow.play(): records the media
    and remembers the panel-hidden flag _apply_channels reports."""

    def __init__(self):
        super().__init__()
        self.played = []
        self.panel_hidden = None

    def play_media(self, playable, start_at=0.0):
        self.played.append((playable, start_at))

    def set_panel_hidden(self, on):
        self.panel_hidden = bool(on)

    def setFocus(self, *a, **k):
        pass


class Win(QtWidgets.QMainWindow):
    """A bare QMainWindow carrying the few attributes MainWindow.play()
    and _apply_channels() touch — the same trick test_startup_defaults
    uses for _restore_state. isHidden() (not isVisible()) is asserted
    below because the window is never shown offscreen."""

    # the REAL panel applier, unbound — the code under test
    _apply_channels = MainWindow._apply_channels

    def __init__(self):
        super().__init__()
        self.config = Config({}, None)
        self.player_view = FakePlayerView()
        self.splitter = QtWidgets.QSplitter()
        self.tabs = QtWidgets.QTabWidget()
        self.splitter.addWidget(self.tabs)
        self.splitter.addWidget(QtWidgets.QWidget())   # the "video" side
        self.setCentralWidget(self.splitter)
        self.splitter.setSizes([300, 500])
        self._channels_hidden = False
        self._zen = False
        self.btn_hide_channels = QtWidgets.QToolButton()
        self.act_chan = QtWidgets.QAction("Hide channel list", self)


STREMIO = {"kind": "stremio", "title": "Stremio stream",
           "url": "http://127.0.0.1:11470/abc/0",
           "fav_key": "stremio:abc123:0"}
LIVE = {"kind": "live", "title": "Live Chan",
        "url": "http://x/11.ts", "fav_key": "live:11"}


def main():
    print("[1] Stremio play hides the channel list")
    win = Win()
    before_sizes = list(win.splitter.sizes())
    MainWindow.play(win, dict(STREMIO), 0.0)
    check("panel flag set", win._channels_hidden is True)
    check("tabs widget hidden", win.tabs.isHidden() is True)
    check("player told panel hidden (floating chevron mode)",
          win.player_view.panel_hidden is True)
    check("media actually started", len(win.player_view.played) == 1)
    check("splitter sizes saved before hiding",
          list(win._splitter_saved) == before_sizes)
    check("menu action now reads Show", win.act_chan.text() == "Show channel list")

    print("[2] toggle_channels brings it back with the saved sizes")
    MainWindow.toggle_channels(win)
    app.processEvents()
    check("panel flag cleared", win._channels_hidden is False)
    check("tabs widget shown again", win.tabs.isHidden() is False)
    check("menu action reads Hide again",
          win.act_chan.text() == "Hide channel list")
    check("splitter widths restored", list(win.splitter.sizes()) == before_sizes)

    print("[3] Live-channel play leaves the panel alone")
    win2 = Win()
    MainWindow.play(win2, dict(LIVE), 0.0)
    check("panel flag untouched", win2._channels_hidden is False)
    check("tabs widget still shown", win2.tabs.isHidden() is False)
    check("player never flagged hidden", win2.player_view.panel_hidden is not True)
    check("media actually started", len(win2.player_view.played) == 1)

    print("[4] Stremio replay while already hidden keeps the user's sizes")
    win3 = Win()
    MainWindow.play(win3, dict(STREMIO), 0.0)
    win3._splitter_saved = [42, 99]   # sizes the user's OWN hide saved
    MainWindow.play(win3, dict(STREMIO), 0.0)
    check("saved sizes not clobbered", win3._splitter_saved == [42, 99])
    check("panel stays hidden", win3._channels_hidden is True)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
