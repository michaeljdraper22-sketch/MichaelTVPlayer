# -*- coding: utf-8 -*-
"""Offscreen regression test: captions must survive the controls sleeping.

Report 2026-09-03 (Stremio handoff, latest Adventure Time episode):
captions only showed while the on-video control buttons were on screen and
vanished with them.  Root cause: _sleep() hides the WHOLE overlay window
when nothing is on it — decided at the instant the controls sleep.  A
sleep that lands in a gap between cues (no cue active right then) hides
the window, and every later cue painted into it invisibly: showing the
caption child never shows its parent top-level.  _ensure_cap_window now
re-opens the window when a cue arrives, controls staying asleep.

Part 2: while the Subtitle settings dialog previews on the video, VLC's
own spu renderer must be muted — an unparseable handoff file (or a bitmap
track) VLC kept rendering painted its auto-sized sample right through the
dialog on top of the app-styled preview ("two samples, one HUGE one
normal").

Run:  .venv\\Scripts\\python.exe test_caption_window.py   (sets QT_QPA_PLATFORM itself)
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else " FAIL ") + name)


def temp_config() -> Config:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    return Config({}, Path(path))


app = QtWidgets.QApplication(sys.argv)


class FakeVLC:
    def __init__(self):
        self.spu = -1
        self.tracks = []
        self.times = {}

    def set_spu(self, tid):
        self.spu = tid

    def active_spu(self):
        return self.spu

    def spu_tracks(self):
        return list(self.tracks)

    def set_filter_mute(self, on):
        pass

    def set_spu_delay(self, ms):
        pass

    def get_time(self):
        return self.times.get("t", -1)

    def is_playing(self):
        return self.times.get("playing", True)

    def is_mute(self):
        return False

    def audio_tracks(self):
        return []

    def active_audio(self):
        return -1

    def video_size(self):
        return (1920, 1080)

    def apply_scale(self, w=0, h=0):
        pass


def make_view():
    """A real PlayerView on a stremio handoff, overlay owning captions."""
    view = PlayerView(temp_config())
    fake = FakeVLC()
    view.vlc = fake
    view._filter_engine.player = fake
    view.current = {"kind": "stremio", "url": "http://x/0",
                    "title": "Adventure Time", "fav_key": "stremio:a:0"}
    view.dvr = None                      # not chase — the stremio path
    view._cap_cues.add(10.0, 12.0, "hello there")
    view._set_cap_on(True)
    view.resize(1920, 1080)
    view.show()                          # ancestors visible: isVisible() works
    return view, fake


print("[1] fullscreen sleep landing in a cue GAP strands the window — "
      "and the next cue brings it back, controls staying asleep")
view, fake = make_view()
view.set_fullscreen_mode(True)           # immersive: corner buttons sleep too
view._wake()                             # controls up
check("controls awake after wake",
      view.ctl.isVisible() and view.overlay.isVisible())
fake.times["t"] = 20000                  # 20 s: cue (10-12) NOT active — gap
view._caption_tick()
view._sleep()                            # 4 s idle, gap at the instant
check("the bug state: sleep in a gap hid the whole overlay window",
      view.overlay.isHidden())
fake.times["t"] = 11000                  # 11 s: cue active again
view._caption_tick()
check("cue re-opens the overlay window", view.overlay.isVisible())
check("cue actually painted", view._cap_wid._lines == ["hello there"])
check("controls stayed asleep (no wake from captions)",
      view.ctl.isHidden() and view._btn_panel.isHidden()
      and view._btn_ovfs.isHidden() and view._btn_reload.isHidden())

print("[2] sleep while a cue IS active keeps the window (historic behavior)")
fake.times["t"] = 11000
view._sleep()
check("active cue keeps the overlay window", view.overlay.isVisible())
check("caption still painted", view._cap_wid._lines == ["hello there"])

print("[3] suppression wins: never raise the overlay over another app's "
      "windows just for captions")
view._overlay_suppressed = True
fake.times["t"] = 20000
view._caption_tick()                     # gap: the cue's window ENDS first,
view._sleep()                            # then the idle sleep hides it
check("window hidden again at a gap", view.overlay.isHidden())
fake.times["t"] = 11000
view._caption_tick()
check("suppressed: the cue does NOT re-show the window",
      view.overlay.isHidden())
view._overlay_suppressed = False

print("[4] windowed mode: the corner buttons never sleep, so the window "
      "never goes away — captions ride through unchanged")
view2, fake2 = make_view()
view2._wake()
fake2.times["t"] = 20000                 # gap
view2._caption_tick()
view2._sleep()
check("windowed: corner buttons keep the window up",
      view2.overlay.isVisible()
      and view2._btn_panel.isVisible() and view2._btn_ovfs.isVisible())
fake2.times["t"] = 11000
view2._caption_tick()
check("windowed: caption painted through the sleep",
      view2._cap_wid._lines == ["hello there"])

print("[5] Subtitle settings dialog: VLC's own spu muted while the "
      "preview is on the video, sticky choice restored after close")
view3, fake3 = make_view()
fake3.tracks = [(4, "Track 1"), (7, "English")]
fake3.spu = 7                            # VLC is rendering a track
view3._spu_want = 7
view3._spu_name = "English"
view3._set_cap_on(False)                 # overlay NOT the renderer
seen_during = {}


class FakeDialog(QtWidgets.QDialog):
    def __init__(self, *a, **k):
        super().__init__(k.get("parent") or None)
        self.apply_live = None

    def exec_(self):
        # a tick landed mid-dialog (timers run through the modal loop):
        # the enforce must respect the mute and keep VLC's spu OFF
        view3._enforce_spu()
        seen_during["spu"] = fake3.spu
        seen_during["muted"] = view3._sub_dlg_mute
        seen_during["preview"] = view3._cap_wid._preview
        return 0


import src.ui.subtitle_dialog as sd_mod  # noqa: E402
sd_orig = sd_mod.SubtitleDialog
sd_mod.SubtitleDialog = FakeDialog
try:
    view3._open_sub_settings()
finally:
    sd_mod.SubtitleDialog = sd_orig
check("VLC's spu muted while the dialog is open", seen_during["spu"] == -1)
check("mute flag held through the dialog", seen_during["muted"] is True)
check("preview line was up for the dialog", seen_during["preview"])
check("mute released after close", view3._sub_dlg_mute is False)
view3._enforce_spu()                     # the next tick after close
check("sticky track restored by the next enforce", fake3.spu == 7)
check("preview cleared after close", view3._cap_wid._preview == "")

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1)
