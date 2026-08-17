# -*- coding: utf-8 -*-
"""Offscreen tests for the Chrome-style tab shrinking and the resize
debounce / no-op guards that stop the "shadow window" trails.

Run:  .venv\\Scripts\\python.exe test_tab_resize.py   (sets QT_QPA_PLATFORM itself)
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def wait(ms):
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def main():
    app = QtWidgets.QApplication(sys.argv)

    print("[1] Chrome-style tab shrinking (ChromeTabBar)")
    from src.ui.main_window import ChromeTabBar

    tabs = QtWidgets.QTabWidget()
    bar = ChromeTabBar()
    tabs.setTabBar(bar)
    for name in ("Live TV", "Movies", "Series", "★ Favorites", "➕ Custom"):
        tabs.addTab(QtWidgets.QWidget(), name)
    tabs.resize(700, 300)
    tabs.show()
    app.processEvents()

    widths = [bar.tabRect(i).width() for i in range(bar.count())]
    check("wide: all 5 tabs laid out", all(w > 0 for w in widths))
    check("wide: tabs share the width equally", max(widths) - min(widths) <= 1)
    wide_w = max(widths)
    check(f"wide: tabs wider than the minimum ({wide_w}px > "
          f"{bar.MIN_TAB_WIDTH}px)", wide_w > bar.MIN_TAB_WIDTH)

    tabs.resize(560, 300)          # medium: still fits, tabs must shrink
    app.processEvents()
    widths = [bar.tabRect(i).width() for i in range(bar.count())]
    check("medium: tabs shrink with the width", max(widths) < wide_w)
    check("medium: tabs stay equal", max(widths) - min(widths) <= 1)

    tabs.resize(300, 300)          # narrow: tabs clamp at MIN, arrows on
    app.processEvents()
    widths = [bar.tabRect(i).width() for i in range(bar.count())]
    check("narrow: tabs clamp at the minimum width",
          all(w == bar.MIN_TAB_WIDTH for w in widths))
    cur = bar.tabRect(bar.currentIndex())
    check("narrow: current tab fully visible",
          cur.left() >= 0 and cur.right() < bar.width())

    tabs.resize(176, 300)          # tiny: exactly one tab fits at a time
    app.processEvents()
    cur = bar.tabRect(bar.currentIndex())
    check("tiny: current tab still fully visible",
          cur.left() >= 0 and cur.right() < bar.width())

    print("[2] resize no longer calls libvlc per event (debounced)")
    from src.config import Config
    from src.ui.player_view import PlayerView

    class FakeVlc:
        def __init__(self):
            self.calls = 0

        def set_scale_mode(self, mode):
            self.mode = mode

        def apply_scale(self, w, h):
            self.calls += 1
            self.wh = (w, h)

        # no-ops for the timers that keep running in the view
        def set_window(self, *a, **k):
            pass

        def is_playing(self):
            return False

        def is_mute(self):
            return False

        def get_time(self):
            return 0

        def get_length(self):
            return 0

        def is_seekable(self):
            return False

    cfg = Config.load()
    view = PlayerView(cfg)
    real_vlc = view.vlc
    view.vlc = FakeVlc()
    view.show()                     # resize events are only delivered when
    app.processEvents()             # the widget is visible
    view.vlc.calls = 0
    for w in (900, 850, 800, 760, 700, 660, 620):
        view.resize(w, 500)
        app.processEvents()
    check("no libvlc scale call while resizing", view.vlc.calls == 0)
    wait(400)
    check("exactly one call after resizing settles", view.vlc.calls == 1)
    view.resize(880, 500)
    app.processEvents()
    wait(400)
    check("a later resize applies again", view.vlc.calls == 2)
    view.vlc = real_vlc

    print("[3] _apply_scale_to skips no-op VLC round-trips")
    from src.player import VLCPlayer

    class FakePlayer:
        def __init__(self):
            self.calls = []

        def video_set_aspect_ratio(self, v):
            self.calls.append(("ar", v))

        def video_set_crop_geometry(self, v):
            self.calls.append(("cg", v))

        def video_set_crop_ratio(self, w, h):
            self.calls.append(("cr", w, h))

        def audio_set_volume(self, v):
            pass

        def audio_set_mute(self, v):
            pass

        def event_manager(self):
            class _EM:
                def attach(self, *a, **k):
                    pass
            return _EM()

    p = VLCPlayer(volume=50)
    fp = FakePlayer()
    p._apply_scale_to(fp, 640, 360)
    n1 = len(fp.calls)
    check("first apply reaches the player", n1 >= 2)
    p._apply_scale_to(fp, 640, 360)
    check("identical repeat is skipped", len(fp.calls) == n1)
    p._apply_scale_to(fp, 800, 600)
    check("changed size is applied", len(fp.calls) > n1)
    p.apply_scale(800, 600)        # records _scale_wh (apply itself skipped)
    fp2 = FakePlayer()
    p._setup_player(fp2)           # NEW player -> cache reset + re-apply
    check("a fresh player gets the scale re-applied", len(fp2.calls) >= 2)
    p.set_scale_mode("stretch")
    p._apply_scale_to(fp, 640, 360)
    check("stretch mode sends the aspect ratio",
          ("ar", "16:9") in fp.calls)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
