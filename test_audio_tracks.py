# -*- coding: utf-8 -*-
"""Offscreen test for the audio-track selector (♪ button, A key).

Run:  .venv\\Scripts\\python.exe test_audio_tracks.py   (sets QT_QPA_PLATFORM itself)
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

from src.config import BUTTON_KEYS, Config  # noqa: E402
from src.ui.player_view import PlayerView, _SPEEDS  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


class FakeAudio:
    """Stands in for the VLCPlayer's audio-track methods (per-instance)."""

    def __init__(self, tracks, active=-1):
        self.tracks = tracks
        self.active = active
        self.set_calls = []

    def audio_tracks(self):
        return self.tracks

    def active_audio(self):
        return self.active

    def set_audio(self, tid):
        self.set_calls.append(tid)
        self.active = tid

    def install(self, view):
        view.vlc.audio_tracks = self.audio_tracks
        view.vlc.active_audio = self.active_audio
        view.vlc.set_audio = self.set_audio


def reset_pick(view):
    view._audio_want = None
    view._audio_name = ""
    view._audio_auto_tid = None


def main():
    app = QtWidgets.QApplication(sys.argv)
    cfg = Config.load()
    view = PlayerView(cfg)

    print("[1] config + button wiring")
    check("'audio' is a known button key", "audio" in BUTTON_KEYS)
    check("'audio' defaults to visible",
          cfg.control_buttons.get("audio") is True)
    check("btn_audio lives in the control row",
          view.btn_audio.parent() is view.ctl_row)
    check("btn_audio sits next to btn_cc (before btn_scale)",
          view.ctl_row.layout().indexOf(view.btn_cc)
          < view.ctl_row.layout().indexOf(view.btn_audio)
          < view.ctl_row.layout().indexOf(view.btn_scale))

    print("[2] auto mode: no English track -> leave VLC's choice alone")
    fake = FakeAudio([(1, "Track 1"), (2, "Spanish")])
    fake.active = 1
    fake.install(view)
    reset_pick(view)
    view._enforce_audio()
    check("no switch, no set calls", fake.active == 1 and not fake.set_calls)

    print("[3] auto mode: English default when the stream has one")
    fake.tracks = [(1, "Track 1 - [Spanish]"), (2, "English (AAC)")]
    fake.active = 1
    view._enforce_audio()
    check("switched to the English track",
          fake.active == 2 and 2 in fake.set_calls)
    view._enforce_audio()
    check("stable on the English track (exactly one set)",
          fake.active == 2 and fake.set_calls.count(2) == 1)

    print("[4] auto mode: already English -> untouched")
    fake.tracks = [(1, "English"), (2, "Spanish")]
    fake.active = 1
    fake.set_calls.clear()
    view._enforce_audio()
    check("no redundant switch", fake.active == 1 and not fake.set_calls)

    print("[5] user pick: applied + sticky re-match after a media change")
    fake.tracks = [(1, "Spanish"), (2, "English")]
    fake.active = 1
    view._select_audio(2, "English")
    check("pick applied", fake.active == 2 and 2 in fake.set_calls)
    fake.tracks = [(7, "Français"), (8, "English (United States)")]
    fake.active = 7
    view._enforce_audio()
    check("re-matched by name to the new id",
          view._audio_want == 8 and 8 in fake.set_calls)

    print("[6] user pick gone -> falls back to AUTO (never off)")
    fake.tracks = [(3, "Deutsch")]
    fake.active = 3
    view._enforce_audio()
    check("mode reverted to auto",
          view._audio_name == "" and view._audio_want is None)
    check("audio still playing (no off-state call)",
          fake.active == 3 and -1 not in fake.set_calls)

    print("[7] A key cycles Auto -> English-first -> ... -> Auto")
    reset_pick(view)
    fake.tracks = [(1, "Track 1 - [Spanish]"), (2, "English"),
                   (3, "French")]
    fake.active = 1
    view._cycle_audio()
    check("cycle 1: the English track (not the first)",
          view._audio_want == 2 and fake.active == 2)
    view._cycle_audio()
    check("cycle 2: next track (1)", view._audio_want == 1)
    view._cycle_audio()
    check("cycle 3: track 3", view._audio_want == 3)
    view._cycle_audio()
    check("cycle 4: back to Auto",
          view._audio_want is None and view._audio_name == "")
    fake.tracks.clear()
    view._cycle_audio()
    check("no tracks -> cycle is a no-op", view._audio_want is None)

    print("[8] picker panel contents (Stremio-style card)")
    fake.tracks = [(1, "Track 1 - [Spanish]"), (2, "English")]
    fake.active = 1
    reset_pick(view)
    view._audio_menu()
    rows = view._ctl_panel.rows()
    check("panel = Auto + English-first tracks",
          [r["main"] for r in rows] == ["Auto", "English", "Spanish"])
    check("Auto carries its English-when-available sub-label",
          rows[0]["sub"] == "English when available")
    check("Auto checked in auto mode (active track not checked)",
          [r["id"] for r in rows if r["checked"]] == [None])
    check("picker open keeps the controls on (_popup_open)",
          view._popup_open and view._ctl_panel.isVisible())
    view._ctl_panel.close_panel()
    check("closing restores the auto-hide cycle + stops the refresh timer",
          not view._popup_open and not view._ctl_panel_timer.isActive())

    print("[8b] picked-mode checkmark + pick through the panel")
    view._select_audio(1, "Track 1 - [Spanish]")
    view._audio_menu()
    rows = view._ctl_panel.rows()
    check("the pick itself carries the only checkmark",
          [r["id"] for r in rows if r["checked"]] == [1])
    row2 = None
    for i in range(view._ctl_panel._lay.count()):
        w = view._ctl_panel._lay.itemAt(i).widget()
        if getattr(w, "_track_id", None) == 2:
            row2 = w
    check("track rows exist as clickable widgets", row2 is not None)
    if row2 is not None:
        # a REAL click (press+release on the row widget): the 8/24 overhaul
        # shipped rows with no mouse handling — every menu "did nothing"
        # while this suite passed by emitting the signal by hand
        QTest.mouseClick(row2, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
                         row2.rect().center())
    check("row click routes into _select_audio",
          view._audio_want == 2 and fake.active == 2)
    check("panel hid itself after the pick",
          not view._ctl_panel.isVisible() and not view._popup_open)
    view._ctl_panel.close_panel()
    reset_pick(view)

    print("[8b2] row click semantics: drag-off cancels, dim rows inert")
    fake.tracks = [(1, "Track 1 - [English]"), (2, "Track 2 - [Spanish]")]
    fake.active = 1
    view._audio_menu()
    victim = None
    for i in range(view._ctl_panel._lay.count()):
        w = view._ctl_panel._lay.itemAt(i).widget()
        if getattr(w, "_track_id", None) == 2:
            victim = w
    if victim is not None:
        QTest.mousePress(victim, QtCore.Qt.LeftButton,
                         QtCore.Qt.NoModifier, victim.rect().center())
        QTest.mouseRelease(victim, QtCore.Qt.LeftButton,
                           QtCore.Qt.NoModifier,
                           QtCore.QPoint(-50, -50))
        check("press dragged off the row does NOT pick",
              view._audio_want != 2 and view._ctl_panel.isVisible())
        QTest.mouseClick(victim, QtCore.Qt.LeftButton,
                         QtCore.Qt.NoModifier, victim.rect().center())
        check("press+release inside the row picks",
              view._audio_want == 2 and not view._ctl_panel.isVisible())
    else:
        check("row widgets present for click tests", False)
    fake.tracks = []
    view._audio_menu()
    dim = None
    for i in range(view._ctl_panel._lay.count()):
        w = view._ctl_panel._lay.itemAt(i).widget()
        if getattr(w, "_track_id", None) == "empty":
            dim = w
    if dim is not None:
        QTest.mouseClick(dim, QtCore.Qt.LeftButton,
                         QtCore.Qt.NoModifier, dim.rect().center())
        check("click on the dimmed empty-state row does nothing",
              view._ctl_panel.isVisible())
    view._ctl_panel.close_panel()
    reset_pick(view)

    print("[8c] raw track names cleaned for the rows")
    lbl = PlayerView._audio_row_label
    check("bracketed MKV style", lbl("Track 2 - [English]", 2)
          == ("English", ""))
    check("parenthesised qualifier -> sub-label",
          lbl("English (United States)", 3) == ("English", "United States"))
    check("dash style keeps the head as sub", lbl("1 - Slovenščina", 1)
          == ("Slovenščina", "1"))
    check("plain name passes through", lbl("Deutsch", 4) == ("Deutsch", ""))
    check("empty name falls back to Track N",
          lbl("", 5) == ("Track 5", ""))

    print("[8d] the SAME button toggles the card closed (click-outside "
          "press on the opener is ignored)")
    view._audio_menu()
    check("first click opens", view._ctl_panel.isVisible()
          and view._ctl_panel_btn is view.btn_audio)
    # a press ON the opener button must NOT be treated as click-outside
    # (its click toggles); synthesize one and deliver it through the app
    gp = view.btn_audio.mapToGlobal(QtCore.QPoint(3, 3))
    ev = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress,
                           QtCore.QPointF(3, 3), QtCore.QPointF(gp),
                           QtCore.Qt.LeftButton, QtCore.Qt.LeftButton,
                           QtCore.Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(view.btn_audio, ev)
    check("press on the opener leaves the card open",
          view._ctl_panel.isVisible())
    view._audio_menu()                      # the button's own click
    check("second click on the same button closes the card",
          not view._ctl_panel.isVisible() and not view._popup_open)
    # a press elsewhere (another button) still closes it
    view._audio_menu()
    gp2 = view.btn_cc.mapToGlobal(QtCore.QPoint(3, 3))
    ev2 = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress,
                            QtCore.QPointF(3, 3), QtCore.QPointF(gp2),
                            QtCore.Qt.LeftButton, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(view.btn_cc, ev2)
    check("press outside (not the opener) closes the card",
          not view._ctl_panel.isVisible())
    reset_pick(view)

    print("[8e] scale + speed popups are cards too")
    view._scale_menu()
    rows = view._ctl_panel.rows()
    check("scale card = fit/stretch/crop with the current one checked",
          [r["id"] for r in rows] == ["fit", "stretch", "crop"]
          and [r["id"] for r in rows if r["checked"]] == ["fit"])
    view._ctl_panel.close_panel()
    view.btn_speed.setEnabled(True)
    view._sync_transport = lambda *a, **k: None
    view._mode = "chase"   # _set_rate keeps the value only in chase/VOD
    view._scale_menu()     # open something first so the toggle path covers
    view._speed_menu()      # a DIFFERENT button swapping content
    rows = view._ctl_panel.rows()
    check("speed card lists the ladder with the current rate checked",
          [r["main"] for r in rows] == [f"{s:g}\u00d7" for s in _SPEEDS]
          and [r["main"] for r in rows if r["checked"]] == ["1\u00d7"])
    check("speed card caps its height (scrolls, not past the video)",
          view._ctl_panel.height() <= 400)
    row_x = None
    for i in range(view._ctl_panel._lay.count()):
        w = view._ctl_panel._lay.itemAt(i).widget()
        if getattr(w, "_track_id", None) == 2.0:
            row_x = w
    if row_x is not None:
        QTest.mouseClick(row_x, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
                         row_x.rect().center())
    check("speed pick routes into _set_rate", abs(view._rate - 2.0) < 1e-9)
    view._ctl_panel.close_panel()

    print("[9] empty-track panel still opens with Auto")
    fake.tracks = []
    view._audio_menu()
    rows = view._ctl_panel.rows()
    check("panel = Auto + dim note",
          [r["main"] for r in rows] == ["Auto", "No audio tracks yet"])
    check("note row disabled (not pickable)",
          rows[-1].get("enabled") is False)
    fake.tracks = [(2, "English")]            # arrive late, as VOD rips do
    view._refresh_audio_rows()
    check("the 1 s refresh fills the list the moment tracks arrive",
          [r["main"] for r in view._ctl_panel.rows()]
          == ["Auto", "English"])
    view._ctl_panel.close_panel()

    print("[10] settings visibility honours 'audio'")
    cfg.data["control_buttons"] = dict(cfg.control_buttons, audio=False)
    view._apply_button_visibility()
    check("btn_audio hidden when turned off in settings",
          not view.btn_audio.isVisible() and view.btn_audio.isHidden())
    cfg.data["control_buttons"] = dict(cfg.control_buttons, audio=True)
    view._apply_button_visibility()
    check("btn_audio shown again", not view.btn_audio.isHidden())

    print("[11] _tick drives the enforcement (VLC re-selected behind us)")
    fake.tracks = [(1, "Spanish"), (2, "English")]
    fake.active = 1          # VLC reverted after an ES update
    view._select_audio(2, "English")
    fake.active = 1
    view._tick()
    check("tick re-asserted the user pick", fake.active == 2)

    print("[11b] picks are PER-PROGRAM: play_media resets to Auto")
    view._select_audio(1, "Spanish")   # a wrong pick made while exploring
    check("pick applied before the reset", view._audio_name == "Spanish")
    view._attach_done = True
    view._closing = False
    played = []
    view.vlc.play = lambda *a, **k: played.append(a)
    view._engage_chase = lambda: None
    view._on_media_for_profanity = lambda kind: None
    view.play_media({"kind": "vod", "url": "http://x/v.mkv",
                     "title": "t"}, start_at=0.0)
    check("next program starts at Auto (pick cleared)",
          view._audio_name == "" and view._audio_want is None
          and view._audio_auto_tid is None)
    check("playback was started", bool(played))

    print("[12] teardown safety")
    view._closing = True
    fake.set_calls.clear()
    view._enforce_audio()    # must not touch anything
    view._cycle_audio()
    check("guards after stop()", not fake.set_calls)

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
