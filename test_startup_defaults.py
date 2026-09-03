# -*- coding: utf-8 -*-
"""Offscreen regression test for the two startup-default fixes:

[1] MainWindow always opens MAXIMIZED — even when the saved window state is
    "normal" with a half-screen snap geometry (the old bug: the app reopened
    into the remembered half window).
[2] Every browser tab (Live TV / Movies / Series) opens on the "All"
    category — Movies/Series used to open on the FIRST category, which for
    this provider is a FIFA channel.

Run:  .venv\\Scripts\\python.exe test_startup_defaults.py   (sets QT_QPA_PLATFORM itself)
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.browsers import (  # noqa: E402
    LiveBrowser, SeriesBrowser, VodBrowser)
from src.ui.main_window import MainWindow  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def wait_until(cond, timeout=5.0):
    """Pump the event loop until cond() is true (queued AsyncRunner
    finished-signals need processing) or the timeout hits."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    return cond()


class StubClient:
    """Offline XtreamClient: FIFA is the FIRST category (as on the real
    account) so landing on index 2 would show it."""

    def __init__(self):
        self.item_calls = []   # (kind, cat_id) per fetch_items

    def live_categories(self, refresh=False):
        return [{"category_id": 1, "category_name": "FIFA"},
                {"category_id": 2, "category_name": "News"}]

    def vod_categories(self, refresh=False):
        return [{"category_id": 1, "category_name": "FIFA Movies"},
                {"category_id": 2, "category_name": "Action"}]

    def series_categories(self, refresh=False):
        return [{"category_id": 1, "category_name": "FIFA Series"},
                {"category_id": 2, "category_name": "Drama"}]

    def live_streams(self, cat_id, refresh=False):
        self.item_calls.append(("live", cat_id))
        return [{"name": "Live Chan", "stream_id": 11}]

    def vod_streams(self, cat_id, timeout=None, refresh=False):
        self.item_calls.append(("vod", cat_id))
        return [{"name": "A Movie", "stream_id": 22,
                 "container_extension": "mp4"}]

    def series(self, cat_id, timeout=None, refresh=False):
        self.item_calls.append(("series", cat_id))
        return [{"name": "A Series", "series_id": 33}]


app = QtWidgets.QApplication(sys.argv)


def fresh_cfg():
    """Isolated config (NOT Config.load()) so the user's real country
    filters can't filter the stub categories/items out."""
    return Config({}, None)


def main():
    print("[1] MainWindow._restore_state always maximizes")
    cfg = fresh_cfg()
    cfg.data["window_geometry"] = [-8, -8, 960, 540]   # half-screen snap
    cfg.data["window_state"] = "normal"                # the old-look state
    win = QtWidgets.QMainWindow()
    win.splitter = QtWidgets.QSplitter()
    win.tabs = QtWidgets.QTabWidget()
    win.splitter.addWidget(win.tabs)
    win.tabs.addTab(QtWidgets.QWidget(), "Live TV")
    win.config = cfg
    MainWindow._restore_state(win)
    app.processEvents()
    check("window is maximized despite saved normal+half-snap", win.isMaximized())

    print("[2] Live / Movies / Series all open on the \"All\" category")
    for cls, label in ((LiveBrowser, "Live TV"),
                       (VodBrowser, "Movies"),
                       (SeriesBrowser, "Series")):
        cfg2 = fresh_cfg()
        client = StubClient()
        tab = cls(cfg2, client, "live")
        ok = wait_until(lambda: tab.cat_combo.count() > 2, 5.0)
        check(f"{label}: category dropdown filled ({tab.cat_combo.count()} entries)", ok)
        idx = tab.cat_combo.currentIndex()
        data = tab.cat_combo.itemData(idx)
        check(f"{label}: selection is 'All' (idx={idx}, data={data!r})",
              idx == 1 and data == "all")
        kind = {"Live TV": "live", "Movies": "vod", "Series": "series"}[label]
        ok = wait_until(lambda: any(k == kind for k, _ in client.item_calls), 5.0)
        cat_used = next((c for k, c in client.item_calls if k == kind), "unset")
        check(f"{label}: item list loaded for 'All' (cat_id={cat_used!r})",
              ok and cat_used is None)
        check(f"{label}: list shows items",
              wait_until(lambda: tab.list.count() >= 1, 5.0))

    print("[3] Movies / Series keep the big-library loading hint")
    tab = VodBrowser(fresh_cfg(), StubClient(), "vod")
    wait_until(lambda: tab.cat_combo.count() > 2, 5.0)
    tab._populate_categories([{"category_id": 1, "category_name": "FIFA"}])
    check("VOD 'All' fetch shows the big-list hint",
          "full library" in tab.status.text())
    live = LiveBrowser(fresh_cfg(), StubClient(), "live")
    live._populate_categories([{"category_id": 1, "category_name": "News"}])
    check("Live 'All' fetch uses the plain Loading… hint",
          "full library" not in live.status.text())

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
