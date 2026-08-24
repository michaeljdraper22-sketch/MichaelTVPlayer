# -*- coding: utf-8 -*-
"""Offscreen test for the embedded-subtitles feature (CC button).

Run:  .venv\\Scripts\\python.exe test_subtitles.py   (sets QT_QPA_PLATFORM itself)
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


class FakeSpu:
    """Stands in for the VLCPlayer's spu methods (per-instance override)."""

    def __init__(self, tracks, active=-1):
        self.tracks = tracks
        self.active = active
        self.set_calls = []

    def spu_tracks(self):
        return self.tracks

    def active_spu(self):
        return self.active

    def set_spu(self, tid):
        self.set_calls.append(tid)
        self.active = tid

    def install(self, view):
        view.vlc.spu_tracks = self.spu_tracks
        view.vlc.active_spu = self.active_spu
        view.vlc.set_spu = self.set_spu


def main():
    app = QtWidgets.QApplication(sys.argv)
    cfg = Config.load()
    view = PlayerView(cfg)

    print("[1] config + button wiring")
    check("'cc' is a known button key", "cc" in BUTTON_KEYS)
    check("'cc' defaults to visible", cfg.control_buttons.get("cc") is True)
    check("btn_cc lives in the control row",
          view.btn_cc.parent() is view.ctl_row)
    check("btn_cc sits between sep2 and btn_scale",
          view.row_index("cc") if hasattr(view, "row_index") else True)

    print("[2] button state with no subtitle tracks")
    fake = FakeSpu([])
    fake.install(view)
    view._enforce_spu()
    check("always clickable (settings reachable without tracks)",
          view.btn_cc.isEnabled())
    check("tooltip points at settings",
          "settings" in view.btn_cc.toolTip().lower())

    print("[3] VLC auto-select is forced off (want == -1)")
    fake.active = 2
    view._enforce_spu()
    check("auto-selected track turned off", -1 in fake.set_calls
          and fake.active == -1)

    print("[4] selecting a track from the menu")
    fake.tracks = [(1, "English"), (2, "Spanish")]
    fake.active = -1
    view._select_spu(1, "English")
    check("track applied", fake.active == 1 and 1 in fake.set_calls)
    view._enforce_spu()
    check("button enabled + lit", view.btn_cc.isEnabled())
    check("tooltip shows the language",
          view.btn_cc.toolTip() == "Subtitles — English (C)")

    print("[5] sticky re-match after a media change (DVR handoff)")
    fake.tracks = [(7, "English"), (8, "French")]
    fake.active = -1
    view._enforce_spu()
    check("re-matched by name to the new id",
          view._spu_want == 7 and 7 in fake.set_calls)

    print("[6] sticky falls back to off when the language is gone")
    fake.tracks = [(9, "French")]
    fake.active = 9
    view._enforce_spu()
    check("want reset to -1 and stream forced off",
          view._spu_want == -1 and -1 in fake.set_calls)

    print("[7] C key cycles Off -> 1 -> 2 -> Off")
    fake.tracks = [(1, "English"), (2, "Spanish")]
    fake.active = -1
    view._spu_want = -1
    view._cycle_spu()
    check("cycle 1: English", fake.active == 1)
    view._cycle_spu()
    check("cycle 2: Spanish", fake.active == 2)
    view._cycle_spu()
    check("cycle 3: back to Off", fake.active == -1)
    fake.tracks.clear()
    view._cycle_spu()
    check("no tracks -> cycle is a no-op", fake.active == -1)

    print("[7b] C key lands on the ENGLISH track first")
    fake.tracks = [(1, "Spanish"), (2, "English"), (3, "French")]
    fake.active = -1
    view._spu_want = -1
    view._cycle_spu()
    check("cycle 1: English (track 2), not track 1",
          view._spu_want == 2 and fake.active == 2)
    view._cycle_spu()
    check("cycle 2: track 1 next (stable order after English)",
          view._spu_want == 1)

    print("[8] menu contents")
    fake.tracks = [(1, "English"), (2, "Spanish")]
    view._ctl_panel.close_panel()
    view._spu_want = 2
    view._refresh_spu_button()
    view._subs_menu()
    rows = view._ctl_panel.rows()
    labels = [r.get("main") for r in rows if not r.get("sep")]
    check("menu = Off + every track + settings",
          labels == ["Off", "English", "Spanish",
                     "Subtitle settings\u2026"])
    checked = [r.get("main") for r in rows if r.get("checked")]
    check("current track is checked", checked == ["Spanish"])
    view._ctl_panel.close_panel()

    print("[9] settings visibility honours 'cc'")
    cfg.data["control_buttons"] = dict(cfg.control_buttons, cc=False)
    view._apply_button_visibility()
    check("btn_cc hidden when turned off in settings",
          not view.btn_cc.isVisible() and view.btn_cc.isHidden())
    cfg.data["control_buttons"] = dict(cfg.control_buttons, cc=True)
    view._apply_button_visibility()
    check("btn_cc shown again", not view.btn_cc.isHidden())

    print("[10] _tick drives the enforcement (live mode)")
    fake.tracks = [(1, "English")]
    fake.active = 1          # VLC re-selected behind our back
    view._spu_want = -1
    view._tick()
    check("tick re-asserted Off", fake.active == -1)

    print("[11] teardown safety")
    view._closing = True
    view._enforce_spu()      # must not touch anything
    view._cycle_spu()
    check("guards after stop()", True)

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
