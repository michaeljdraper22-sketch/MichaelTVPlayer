# -*- coding: utf-8 -*-
"""Offscreen regression test: captions must survive the controls sleeping.

Report 2026-09-03 (Stremio handoff, latest Adventure Time episode):
captions only showed while the on-video control buttons were on screen and
vanished with them.  Root cause: _sleep() hides the WHOLE overlay window
when nothing is on it — decided at the instant the controls sleep — so a
sleep landing in a gap between cues stranded every later cue in a hidden
window (showing the caption child never shows its parent top-level).

Cut 2 (same day, after a LIVE series session via the vod relay still lost
captions on every control sleep despite the cue-rescue re-show): while
_cap_on (the overlay is the ACTIVE caption renderer) _sleep() now never
hides the window at all — it is transparent, click-through and paints
nothing between cues, so keeping it up costs nothing and makes the
stranding class structurally impossible.  _ensure_cap_window stays as the
rescue for the other hiders (minimized-restore leftover, suppression
aftermath).  VLC-rendered mode (overlay disengaged) keeps the classic
hide.

Also: while the Subtitle settings dialog previews on the video, VLC's own
spu renderer must be muted — an unparseable handoff file (or a bitmap
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


print("[1] fullscreen sleep landing in a cue GAP: the window now STAYS "
      "while the caption renderer is active — stranding is structurally "
      "impossible, controls still sleep")
view, fake = make_view()
view.set_fullscreen_mode(True)           # immersive: corner buttons sleep too
view._wake()                             # controls up
check("controls awake after wake",
      view.ctl.isVisible() and view.overlay.isVisible())
fake.times["t"] = 20000                  # 20 s: cue (10-12) NOT active — gap
view._caption_tick()
view._sleep()                            # 4 s idle, gap at the instant
check("gap sleep keeps the overlay window (renderer active)",
      view.overlay.isVisible())
check("controls asleep", view.ctl.isHidden() and view._btn_panel.isHidden()
      and view._btn_ovfs.isHidden() and view._btn_reload.isHidden())
fake.times["t"] = 11000                  # 11 s: cue active again
view._caption_tick()
check("cue painted with the controls asleep",
      view._cap_wid._lines == ["hello there"])
check("window still up (never stranded)", view.overlay.isVisible())

print("[2] a window hidden by the OTHER hiders (minimized-restore "
      "leftover, suppression aftermath) is rescued by the next cue")
view.overlay.hide()                      # simulate a non-_sleep hider
fake.times["t"] = 11000
view._caption_tick()
check("cue re-opens the overlay window", view.overlay.isVisible())
check("controls stayed asleep (no wake from captions)",
      view.ctl.isHidden())
fake.times["t"] = 20000
view._caption_tick()                     # gap: cue's window ends first
view.overlay.hide()
check("window hidden again at a gap", view.overlay.isHidden())

print("[3] suppression wins: never raise the overlay over another app's "
      "windows just for captions")
view._overlay_suppressed = True
fake.times["t"] = 11000
view._caption_tick()
check("suppressed: the cue does NOT re-show the window",
      view.overlay.isHidden())
view._overlay_suppressed = False

print("[4] VLC-rendered subtitles (overlay disengaged): the classic "
      "window hide at a sleep is preserved")
view4, fake4 = make_view()
view4._set_cap_on(False)                 # VLC owns rendering
fake4.times["t"] = 20000
view4._caption_tick()                    # tick no-ops (renderer off)
view4.set_fullscreen_mode(True)
view4._wake()
view4._sleep()
check("disengaged: sleep hides the window as before",
      view4.overlay.isHidden())

print("[5] windowed mode: the corner buttons never sleep, so the window "
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

print("[6] Subtitle settings dialog: VLC's own spu muted while the "
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
