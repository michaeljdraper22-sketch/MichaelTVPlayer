# -*- coding: utf-8 -*-
"""Offscreen regression test for the live-seen 2026-09-03 ~07:00 incident:
"spammed the play next button and it ended up doing nothing."

Forensics from that session (player.log + settings.json):
  - an identified Stremio episode was playing, autoplay toggle ON, the
    lookahead fresh and correctly keyed — every ingredient for a working
    switch was in place, yet the spam produced ZERO log lines and no
    switch (recents[0] proved _start_next never ran);
  - the user had pressed LIVE on the file episode before the spam,
    which seeks it to its end — autoplay should have rolled into the
    next episode and never fired, not even its eof-note log line;
  - the WINDOWED exe has no stderr, so a raise inside an untrapped
    clicked slot vanishes entirely (the documented _open_ctl_panel
    failure shape).

This suite pins the fixes: the whole click chain is trapped + logged
(no silent bails anywhere), a broken banner can't kill the switch,
lookups coalesce under spam, autoplay fires on a clean "ended" even
when get_length() reads 0 — but ONLY once the media actually played
(a dead debrid open parks a fresh handoff in "ended" within seconds;
autoplay fired 3 s after the click, live-seen 2026-09-03 20:57) —
and the tick's non-vod branch no longer zeroes the tracked position
out from under end-of-media detection.

The 2026-09-05 13:12 IPTV incident rides along: an ENDED series
episode with a stale pause latch (one Space/click over the ending
credits) logged its finish note and then silently held autoplay
FOREVER — an ended media cannot be paused, so the latch is provably
stale and is cleared at the fire gate, while a REAL paused-at-credits
hold (state='paused') still holds; every hold now names itself in the
log exactly once.

The 2026-09-05 19:46 zen incident gets its own section at MainWindow
level ([9]): a next-episode click in zen mode left the immersive
session and took "several clicks" to get back.  The switch chain keeps
the zen flags (pinned), play_media now re-asserts the immersive chrome
+ player focus after every media swap (healing knocked-loose chrome,
logged), and the immersive keys H / F / Ctrl+L moved from menu-action
shortcuts (dead while the menu bar is hidden — zen/fullscreen hide it;
F was additionally double-bound = ambiguous) to window-level
QShortcuts; Esc now exits zen as the help text always claimed.

Run:  .venv\\Scripts\\python.exe test_playnext_spam.py   (sets QT_QPA_PLATFORM itself)
"""
import logging
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from src import stremio  # noqa: E402
from src.config import Config  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else " FAIL ") + name)


app = QtWidgets.QApplication(sys.argv)
# tests never run logging_setup.configure() — without this the mtp
# logger sits at the root default (WARNING) and every INFO line the
# suite asserts on is dropped before reaching the capture handler
logging.getLogger("mtp").setLevel(logging.INFO)


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append(record.getMessage())


def e42():
    return {"kind": "stremio",
            "title": "Adventure Time — S05E42 James",
            "url": "http://127.0.0.1:11470/hash231/231",
            "fav_key": "stremio:tt1305826:5:42",
            "stremio_imdb": "tt1305826", "season": 5, "episode": 42,
            "series_name": "Adventure Time", "info_hash": "hash231",
            "file_idx": 231}


def e43():
    return {"kind": "stremio",
            "title": "Adventure Time — S05E43 Root Beer Guy",
            "url": "http://127.0.0.1:11470/hash232/232",
            "fav_key": "stremio:tt1305826:5:43",
            "stremio_imdb": "tt1305826", "season": 5, "episode": 43,
            "series_name": "Adventure Time", "info_hash": "hash232",
            "file_idx": 232}


def make_view(lookahead_for=None):
    """Real PlayerView in the 06:59 in-vivo state: identified E42
    current, fresh lookahead, button state applied (the control-state
    pass the real app runs from play_media). play_media is recorded
    instead of touching a player."""
    view = PlayerView(Config({}, None))
    view._closing = False
    view.current = e42()
    view._attach_done = True
    if lookahead_for is not None:
        view._stremio_lookahead = (lookahead_for, e43(), pv_mod.now_s())
    view._update_control_state()          # what play_media does after a switch
    played = []

    def _fake_play(p, s=0.0):
        played.append(p.get("fav_key"))
        view.current = p                  # the real play_media does this too
    view.play_media = _fake_play
    return view, played


def wait_for(cond, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    app.processEvents()
    return cond()


def main():
    cap = _Cap()
    logging.getLogger("mtp").addHandler(cap)

    print("[1] spam .click() with a fresh matching lookahead")
    view, played = make_view("stremio:tt1305826:5:42")
    for _ in range(8):
        view.btn_next.click()
    check("button is enabled", view.btn_next.isEnabled())
    check("exactly one episode advanced (E43)",
          wait_for(lambda: played == ["stremio:tt1305826:5:43"]))
    check("current switched to E43",
          (view.current or {}).get("fav_key") == "stremio:tt1305826:5:43")
    check("the click was logged", any("playnext: click" in r for r in cap.rows))
    check("the switch was logged",
          any("playnext: switching" in r for r in cap.rows))
    view.stop()

    print("[2] a broken banner must not kill the switch"
          " (the invisible-exception shape of the incident)")
    view, played = make_view("stremio:tt1305826:5:42")

    def boom(*a, **k):
        raise RuntimeError("overlay is dead")
    view.show_info = boom
    view.btn_next.click()
    check("switch still happened with show_info raising",
          wait_for(lambda: played == ["stremio:tt1305826:5:43"]))
    view.stop()

    print("[3] an exception deep in the chain is logged, not swallowed")
    view, played = make_view("stremio:tt1305826:5:42")

    def boom2(p, s=0.0):
        raise RuntimeError("play_media exploded")
    view.play_media = boom2
    view.btn_next.click()
    wait_for(lambda: any("handler failed" in r for r in cap.rows))
    check("failure landed in the log",
          any("handler failed" in r for r in cap.rows))
    view.stop()

    print("[4] spam coalesces into ONE lookup per episode")
    view, played = make_view("stremio:tt1305826:5:42")
    calls = []
    real_fetch = view._fetch_next

    def slow_fetch(cur):
        calls.append(cur.get("fav_key"))
        time.sleep(0.25)                  # the network-resolve shape
        return e43()
    view._fetch_next = slow_fetch
    for _ in range(6):
        view.btn_next.click()             # no pumping: results can't land yet
    check("only one lookup spawned for the base", len(calls) == 1)
    view._fetch_next = real_fetch
    check("the single lookup still switched", wait_for(
        lambda: played == ["stremio:tt1305826:5:43"]))
    check("coalesced drops were logged",
          any("coalesced" in r for r in cap.rows))
    view.stop()

    print("[5] ended episode + dead clock: autoplay must still fire")
    view, played = make_view("stremio:tt1305826:5:42")
    view.btn_auto.setChecked(True)        # the user's persisted toggle
    view.vlc.state_name = lambda: "ended"
    view.vlc.is_playing = lambda: False
    view._vid_s = 0.0                     # even the tracked clock is gone
    view._played_once = True              # the episode DID play before ending
    view._maybe_autoplay_next(False, 0, 0)   # playing, length_ms, raw_ms
    check("autoplay fired on clean 'ended' despite length 0",
          wait_for(lambda: played == ["stremio:tt1305826:5:43"]))
    check("autoplay fire was logged",
          any("autoplay fired" in r for r in cap.rows))
    view.stop()

    print("[5b] dead open: 'ended' without ever playing must NOT autoplay")
    view, played = make_view("stremio:tt1305826:5:42")
    view.btn_auto.setChecked(True)
    view.vlc.state_name = lambda: "ended"  # dead debrid link: open -> ended
    view.vlc.is_playing = lambda: False
    view._vid_s = 0.0
    view._played_once = False              # no tick ever saw playing/paused
    cap.rows.clear()
    view._maybe_autoplay_next(False, 0, 0)
    app.processEvents()
    time.sleep(0.1)                        # give a misfire room to land
    app.processEvents()
    check("autoplay did not fire on the dead open", not played)
    check("no 'media finished' note for the dead open",
          not any("media finished" in r for r in cap.rows))
    view.stop()

    def iptv_cur():
        return {"kind": "series",
                "title": "EN - Adventure Time - S04E16 - Burning Low",
                "url": "http://cf.534842.xyz/series/726352471c/"
                       "d809266e91/1985728.mkv",
                "fav_key": "episode:1985728", "series_id": 3456,
                "season": 4, "episode": 16}

    def iptv_next():
        return dict(iptv_cur(), fav_key="episode:1985730", season=4,
                    episode=17,
                    title="EN - Adventure Time - S04E17 - Breezy")

    print("[5c] the 2026-09-05 13:12 incident: stale pause latch on an "
          "ENDED series episode must not wedge autoplay")
    view, played = make_view(None)
    view.current = iptv_cur()
    view._update_control_state()
    view.btn_auto.setChecked(True)
    view.vlc.state_name = lambda: "ended"     # played out — cannot be paused
    view.vlc.is_playing = lambda: False
    view._vid_s = 705.0                       # the incident's exact numbers
    view._played_once = True                  # the episode really watched
    view._live_paused = True                  # the wedge: one Space/click
                                              # over the ending credits
    view._fetch_next = lambda cur: iptv_next()
    cap.rows.clear()
    view._maybe_autoplay_next(False, 705322, 705000)
    check("autoplay fired despite the stale pause latch",
          wait_for(lambda: played == ["episode:1985730"]))
    check("the stale latch was cleared", view._live_paused is False)
    check("the clear named itself in the log",
          any("stale pause latch cleared" in r for r in cap.rows))
    check("the 'media finished' note still logged",
          any("media finished" in r for r in cap.rows))
    view.stop()

    print("[5d] a REAL pause at the credits (state='paused') still holds "
          "autoplay — and the hold names itself once")
    view, played = make_view(None)
    view.current = iptv_cur()
    view._update_control_state()
    view.btn_auto.setChecked(True)
    view.vlc.state_name = lambda: "paused"    # VLC genuinely paused
    view.vlc.is_playing = lambda: False
    view._vid_s = 705.0
    view._played_once = True
    view._live_paused = True                  # and the latch agrees
    view._fetch_next = lambda cur: iptv_next()
    cap.rows.clear()
    view._maybe_autoplay_next(False, 705322, 705000)
    app.processEvents()
    time.sleep(0.1)                           # give a misfire room to land
    app.processEvents()
    check("autoplay held on the real pause", not played)
    check("the pause latch survived (VLC is still paused)",
          view._live_paused is True)
    held_rows = [r for r in cap.rows if "autoplay held" in r]
    check("the hold logged exactly once with live_paused=True",
          len(held_rows) == 1 and "live_paused=True" in held_rows[0])
    view._maybe_autoplay_next(False, 705322, 705000)   # tick retry: still
    app.processEvents()                                # one line, no spam
    check("the hold log is one-shot across ticks",
          len([r for r in cap.rows if "autoplay held" in r]) == 1)
    view.stop()

    print("[5e] a transient hold releases: after the drag ends autoplay "
          "still fires on a later tick")
    view, played = make_view(None)
    view.current = iptv_cur()
    view._update_control_state()
    view.btn_auto.setChecked(True)
    view.vlc.state_name = lambda: "ended"
    view.vlc.is_playing = lambda: False
    view._vid_s = 705.0
    view._played_once = True
    view._seeking = True                      # a drag mid-fire gets held
    view._fetch_next = lambda cur: iptv_next()
    cap.rows.clear()
    view._maybe_autoplay_next(False, 705322, 705000)
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()
    check("held while the drag is live", not played)
    check("the hold named seeking",
          any("autoplay held" in r and "seeking=True" in r
              for r in cap.rows))
    view._seeking = False                     # sliderReleased landed
    view._maybe_autoplay_next(False, 705322, 705000)
    check("fired on the very next tick after the release",
          wait_for(lambda: played == ["episode:1985730"]))
    view.stop()

    print("[6] tick's non-vod branch keeps polling series/stremio ends")
    view, played = make_view("stremio:tt1305826:5:42")
    view.btn_auto.setChecked(True)
    view.vlc.state_name = lambda: "ended"
    view.vlc.is_playing = lambda: False
    view.vlc.get_length = lambda: 0       # get_length dropped on the relay
    view.vlc.get_time = lambda: 0
    view._vid_s = 0.0
    view._played_once = True              # watched before the relay died
    view._last_vod_len_ms = 0             # sticky length never landed
    view._eof_note_done = False
    view._eof_next_done = False
    view._tick()                          # one full tick in the ended shape
    check("tick's else-branch armed autoplay (not zeroed+skipped)",
          wait_for(lambda: played == ["stremio:tt1305826:5:43"]))
    view.stop()

    print("[7] stale-drop and nothing-found are no longer silent")
    view, played = make_view(None)        # no lookahead -> fetch path
    stremio.next_playable = lambda cfg, cur: None
    cap.rows.clear()
    view.btn_next.click()
    wait_for(lambda: any("nothing found" in r for r in cap.rows))
    check("'nothing found' logged", any("nothing found" in r for r in cap.rows))
    view.stop()

    print("[8] a raise inside the tick is trapped and rate-limited")
    view, played = make_view("stremio:tt1305826:5:42")

    def boom3():
        raise RuntimeError("a dead widget killed the tick")
    view._poll_video_size = boom3         # very first call in _tick
    cap.rows.clear()
    view._tick_err_wall = 0.0
    view._tick_guarded()
    check("tick raise landed in the log (as exception record)",
          any("tick: iteration failed" in r for r in cap.rows))
    view._poll_video_size = lambda: None
    view.stop()

    # ---- the 2026-09-05 19:46 zen incident (MainWindow level) ----
    # "clicked the next episode on a zen-mode half-screen show: the next
    # episode loaded but it left zen mode and required several clicks to
    # get back into it."  Every switch path (next click / prev click /
    # autoplay) funnels through _start_next -> play_media, and play_media
    # now re-asserts the immersive chrome + keyboard focus after the
    # media swap.  The "several clicks" half was the keyboard: H/F/Ctrl+L
    # were menu-action shortcuts, and an action shortcut is DEAD while
    # its menu bar is hidden — zen hides the menu bar, so H could enter
    # zen but never leave it (same for F inside fullscreen); F was even
    # double-bound (QShortcut + action) = ambiguous = dead in windowed
    # mode too.  Those keys now live on window-level QShortcuts.
    print("[9] the 19:46 zen incident: switches never leave the "
          "immersive session, and the immersive keys actually work")
    from src.ui.main_window import MainWindow
    mcfg = Config({"server_url": "http://127.0.0.1", "username": "u",
                   "password": "p"}, None)
    win = MainWindow(mcfg)
    win.resize(1340, 820)
    win.show()
    app.processEvents()
    view = win.player_view
    view._closing = False

    def mk_ep(num):
        return {"kind": "series",
                "title": "EN - Mickey Mouse Clubhouse - S03E%02d" % num,
                "url": "http://127.0.0.1:9/series/x/%d.mp4" % num,
                "fav_key": "episode:2719%02d" % num, "series_id": 123,
                "season": 3, "episode": num}

    # start E07 the way the episode dialog does, then go zen
    win.play(mk_ep(7))
    wait_for(lambda: (view.current or {}).get("episode") == 7)
    win.toggle_zen()
    app.processEvents()
    check("zen engaged: tabs + menu hidden",
          not win.tabs.isVisibleTo(win)
          and not win.menuBar().isVisibleTo(win))

    # next click — the exact incident gesture
    view._fetch_next = lambda cur: mk_ep(8)
    view.clearFocus()                      # the churned video HWND shape
    view._play_next_clicked()
    check("next click switched the episode",
          wait_for(lambda: (view.current or {}).get("episode") == 8))
    check("zen chrome survived the next click",
          getattr(win, "_zen", False)
          and not win.tabs.isVisibleTo(win)
          and not win.menuBar().isVisibleTo(win))
    check("keyboard focus returned to the player",
          app.focusWidget() is view)

    # prev click rides the same chain
    view._fetch_prev = lambda cur: mk_ep(6)
    view._play_prev_clicked()
    check("prev click switched back and kept zen",
          wait_for(lambda: (view.current or {}).get("episode") == 6)
          and not win.tabs.isVisibleTo(win))

    # autoplay: chrome knocked loose mid-zen (the media swap's window
    # churn) is healed by the switch itself
    view.vlc.state_name = lambda: "ended"   # played out
    view.vlc.is_playing = lambda: False
    view._vid_s = 705.0
    view._played_once = True
    view.btn_auto.setChecked(True)
    view._fetch_next = lambda cur: mk_ep(7)
    win.tabs.setVisible(True)               # the knock
    win.menuBar().setVisible(True)
    cap.rows.clear()
    view._maybe_autoplay_next(False, 705322, 705000)
    check("autoplay fired through the real chain",
          wait_for(lambda: (view.current or {}).get("episode") == 7))
    check("knocked-loose chrome was healed (tabs+menu hidden again)",
          not win.tabs.isVisibleTo(win)
          and not win.menuBar().isVisibleTo(win))
    check("the heal named itself in the log",
          any("immersive drift healed" in r for r in cap.rows))

    def press(key):
        QtWidgets.QApplication.sendEvent(
            win, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, key,
                                 QtCore.Qt.NoModifier))
        app.processEvents()

    # H must exit zen even though zen hides the menu bar (an action
    # shortcut is DEAD while its menu is hidden — the several-clicks
    # half of the incident; assert the menu really was hidden at press)
    menu_was_hidden = not win.menuBar().isVisibleTo(win)
    press(QtCore.Qt.Key_H)
    check("H exited zen while the menu bar was hidden",
          menu_was_hidden
          and getattr(win, "_zen", False) is False
          and win.tabs.isVisibleTo(win))
    check("the zen transition was logged",
          any("chrome: zen off" in r for r in cap.rows))
    press(QtCore.Qt.Key_H)                 # back in for the Esc check
    check("H re-entered zen", getattr(win, "_zen", False) is True)
    press(QtCore.Qt.Key_Escape)
    check("Esc exits zen", getattr(win, "_zen", False) is False)

    # F pressed with the menu bar VISIBLE: the old double binding
    # (QShortcut + menu action) was ambiguous there and fired neither.
    # The second press runs inside fullscreen, where the menu bar is
    # hidden — pinning the hidden-menu side as well.
    if getattr(win, "_zen", False):
        win.toggle_zen()
    win.menuBar().setVisible(True)
    app.processEvents()
    press(QtCore.Qt.Key_F)
    check("F entered fullscreen (menu visible: no ambiguity)",
          win.isFullScreen())
    press(QtCore.Qt.Key_F)
    check("F left fullscreen (menu hidden inside fullscreen)",
          not win.isFullScreen())

    view.stop()
    win.close()
    app.processEvents()

    logging.getLogger("mtp").removeHandler(cap)
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
