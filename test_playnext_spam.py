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

Run:  .venv\\Scripts\\python.exe test_playnext_spam.py   (sets QT_QPA_PLATFORM itself)
"""
import logging
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

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

    logging.getLogger("mtp").removeHandler(cap)
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
