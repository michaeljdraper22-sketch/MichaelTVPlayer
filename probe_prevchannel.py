# -*- coding: utf-8 -*-
"""Headless probe: live TV "Play previous" — the channel ABOVE in the list.

Offscreen, VLC never starts (play_media is stubbed to a recorder): the
⏮ button on a live channel must emit request_prev_channel and step to the
channel above the current one in the Live tab's visible list, wrapping to
the bottom at the top — the exact twin of play-next.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

app = QtWidgets.QApplication(sys.argv)
fails = [0]


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else extra))
    if not cond:
        fails[0] += 1


from src.config import DEFAULTS, Config  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402

MainWindow._restore_state = lambda self: None
cfg = Config(dict(DEFAULTS, server_url="http://dummy", username="u",
                  password="p", parent_dir=None), None)
win = MainWindow(cfg)
win._save_state = lambda: None

pv = win.player_view

# VLC never starts in this probe: play_media records what it was handed
# and adopts it as current — exactly what the real play_media does minus
# the media player itself.
plays = []


def fake_play_media(playable, start_at=0.0):
    plays.append(dict(playable))
    pv.current = dict(playable)


pv.play_media = fake_play_media


class FakeLiveClient:
    def live_url(self, sid):
        return f"http://x/live/{sid}"


win.live_tab.client = FakeLiveClient()

# Four raw provider rows in the visible list (the shape _show_items puts
# in UserRole): stream_ids 11, 22, 33, 44 in list order.
ROWS = [{"stream_id": s, "name": f"Chan {s}", "stream_icon": ""}
        for s in (11, 22, 33, 44)]
for it in ROWS:
    wi = QtWidgets.QListWidgetItem(it["name"])
    wi.setData(QtCore.Qt.UserRole, dict(it))
    win.live_tab.list.addItem(wi)
win.live_tab.all_items = [dict(it) for it in ROWS]


def current_at(sid):
    pv.current = {"kind": "live", "title": f"Chan {sid}", "stream_id": sid,
                  "fav_key": f"live:{sid}", "url": f"http://x/live/{sid}"}


def played_sid():
    return plays[-1].get("stream_id") if plays else None


print("[1] visibility + enabled for a live channel")
current_at(22)
pv._update_control_state()
app.processEvents()
check("live: prev button visible + enabled",
      not pv.btn_prev.isHidden() and pv.btn_prev.isEnabled())
check("live: next button still visible + enabled",
      not pv.btn_next.isHidden() and pv.btn_next.isEnabled())

print("[2] stepping via the real button (click -> signal -> main window)")
pv.btn_prev.click()
app.processEvents()
check("prev from 22 plays 22-1 = 11", played_sid() == 11,
      f" got {played_sid()}")
check("selection followed playback to 11",
      win.live_tab.list.currentRow() == 0)
check("playable is kind=live with a made URL",
      plays[-1].get("kind") == "live"
      and plays[-1].get("url") == "http://x/live/11")

pv.btn_next.click()
app.processEvents()
check("next from 11 plays 22 (next still works)", played_sid() == 22)

print("[3] wrap-around both ways")
current_at(11)
pv.btn_prev.click()
app.processEvents()
check("prev from the TOP wraps to the bottom (44)",
      played_sid() == 44, f" got {played_sid()}")
current_at(44)
pv.btn_next.click()
app.processEvents()
check("next from the BOTTOM wraps to the top (11)",
      played_sid() == 11, f" got {played_sid()}")

print("[4] P key routes live to prev-channel too")
current_at(33)
ev = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_P,
                      QtCore.Qt.NoModifier)
pv.keyPressEvent(ev)
app.processEvents()
check("P on live steps to 22", played_sid() == 22, f" got {played_sid()}")

print("[5] guards: not playing live / not in the list")
plays.clear()
pv.current = {"kind": "vod", "title": "Movie", "fav_key": "vod:1",
              "url": "http://x/m.mkv"}
pv._update_control_state()
check("movie: prev hidden again",
      pv.btn_prev.isHidden())
pv._play_prev_clicked()
app.processEvents()
check("movie: P/button does nothing", not plays)

current_at(99)   # not one of the visible rows
sb = win.statusBar()
sb.clearMessage()
win.play_prev_channel()
check("current not in list: status explains, nothing plays",
      not plays and "not in the Live list" in sb.currentMessage())

print("[6] next-channel regression: refactored core identical for next")
current_at(33)
win.play_next_channel()
check("direct next 33 -> 44", played_sid() == 44, f" got {played_sid()}")
current_at(22)
win.play_next_channel()
check("direct next 22 -> 33", played_sid() == 33, f" got {played_sid()}")

print("[7] one-row list falls back to all_items (Recently-Played shape)")
win.live_tab.list.clear()
wi = QtWidgets.QListWidgetItem("Chan 22")
wi.setData(QtCore.Qt.UserRole, dict(ROWS[1]))
win.live_tab.list.addItem(wi)
current_at(22)
win.play_prev_channel()
check("single visible row: fallback steps within all_items",
      played_sid() == 11, f" got {played_sid()}")

# ---- favorites context: ⏭/⏮ must walk the FAVORITES list ----
# Favorites in display order: two channels, a favorited MOVIE between
# them (must be skipped — the button changes channels), and 22 last.
FAVS = [
    {"kind": "live", "title": "Chan 11", "stream_id": 11,
     "fav_key": "live:11", "url": "http://x/live/11", "icon": ""},
    {"kind": "vod", "title": "Movie M", "fav_key": "vod:77",
     "url": "http://x/m.mkv", "icon": ""},
    {"kind": "live", "title": "Chan 44", "stream_id": 44,
     "fav_key": "live:44", "url": "http://x/live/44", "icon": ""},
    {"kind": "live", "title": "Chan 22", "stream_id": 22,
     "fav_key": "live:22", "url": "http://x/live/22", "icon": ""},
]
cfg.data["favorites"] = [dict(f) for f in FAVS]
win.fav_tab.refresh()


def launch_from_favs(row):
    # through the real signal path: _activate -> media_activated ->
    # _play_from_favorites (a direct play() here would hide a broken
    # signal connection)
    win.fav_tab._activate(win.fav_tab.list.item(row))
    # the stubbed play_media skips the control-state refresh the real one
    # does — without this the buttons stay whatever [5] last left them
    pv._update_control_state()
    app.processEvents()


print("[8] launched from Favorites: steps walk the favorites list")
launch_from_favs(3)          # Chan 22, the LAST favorite
check("activation played favorite Chan 22", played_sid() == 22)
check("nav context is favorites", win._live_nav_source == "favorites")
pv.btn_next.click()
app.processEvents()
check("next from the favorites BOTTOM wraps to the top (11)",
      played_sid() == 11, f" got {played_sid()}")
check("favorites row highlight followed (row 0)",
      win.fav_tab.list.currentRow() == 0)
pv.btn_prev.click()
app.processEvents()
check("prev from the favorites TOP wraps to the bottom (22)",
      played_sid() == 22, f" got {played_sid()}")
check("favorites row highlight followed (row 3)",
      win.fav_tab.list.currentRow() == 3)
pv.btn_prev.click()
app.processEvents()
check("prev from 22 -> 44 (one up in favorites)",
      played_sid() == 44, f" got {played_sid()}")
pv.btn_prev.click()
app.processEvents()
check("prev from 44 -> 11, SKIPPING the favorited movie above it "
      "(library order would give 33)",
      played_sid() == 11, f" got {played_sid()}")
check("favorites row highlight followed (row 0, past the movie)",
      win.fav_tab.list.currentRow() == 0)
pv.btn_next.click()
app.processEvents()
check("next from 11 -> 44, skipping the movie forward too "
      "(library order would give 22)",
      played_sid() == 44, f" got {played_sid()}")

print("[9] launched from the Live tab: library stepping unchanged")
win.play({"kind": "live", "title": "Chan 22", "stream_id": 22,
          "fav_key": "live:22", "url": "http://x/live/22"})
pv._update_control_state()
app.processEvents()
check("nav context back to live", win._live_nav_source == "live")
pv.btn_next.click()
app.processEvents()
check("next from 22 uses the library list again (33)",
      played_sid() == 33, f" got {played_sid()}")

print("[10] favorites with a single live channel: explains, plays nothing")
cfg.data["favorites"] = [dict(FAVS[0])]
win.fav_tab.refresh()
launch_from_favs(0)
plays.clear()
sb.clearMessage()
win.play_next_channel()
check("one live favorite: status explains, nothing plays",
      not plays and "No other channel in Favorites" in sb.currentMessage())

print("[11] favorites-launched but current left the list")
cfg.data["favorites"] = [dict(f) for f in FAVS]
win.fav_tab.refresh()
launch_from_favs(3)          # Chan 22 from favorites
current_at(99)               # now on a channel not in the favorites
plays.clear()
sb.clearMessage()
win.play_next_channel()
check("current not a favorite: status explains, nothing plays",
      not plays and "not in the Favorites list" in sb.currentMessage())

print("[12] search-filtered favorites view: full-list fallback")
current_at(22)
win.fav_tab.search.setText("Chan 22")   # view narrows to one row
win.play_prev_channel()
check("filtered view: falls back to the full favorites list (22 -> 44)",
      played_sid() == 44, f" got {played_sid()}")
win.fav_tab.search.clear()

pv.stop()
app.processEvents()
print(f"\n{'ALL PASS' if fails[0] == 0 else str(fails[0]) + ' FAILURES'}")
sys.exit(1 if fails[0] else 0)
