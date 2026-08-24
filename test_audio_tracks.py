# -*- coding: utf-8 -*-
"""Offscreen test for the audio-track selector (♪ button, A key).

Run:  .venv\\Scripts\\python.exe test_audio_tracks.py   (sets QT_QPA_PLATFORM itself)
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import BUTTON_KEYS, Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

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

    print("[8] menu contents")
    fake.tracks = [(1, "Track 1 - [Spanish]"), (2, "English")]
    fake.active = 1
    reset_pick(view)
    captured = []
    view._popup_above = lambda menu, btn: captured.append(menu)
    view._audio_menu()
    labels = [a.text() for a in captured[-1].actions()]
    check("menu = Auto + English-first tracks",
          labels == ["Auto (English when available)", "English",
                     "Track 1 - [Spanish]"])
    checked = [a.text() for a in captured[-1].actions() if a.isChecked()]
    check("Auto checked in auto mode (active track not checked)",
          checked == ["Auto (English when available)"])

    print("[9] empty-track menu still opens with Auto")
    fake.tracks = []
    view._audio_menu()
    labels = [a.text() for a in captured[-1].actions()]
    check("menu = Auto + disabled note",
          labels == ["Auto (English when available)", "",
                     "No audio tracks on this stream yet"])
    check("note entry disabled",
          not captured[-1].actions()[-1].isEnabled())

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
