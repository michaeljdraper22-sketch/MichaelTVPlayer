# -*- coding: utf-8 -*-
"""Tests for the app-rendered caption overlay: cue store timing/roll-up,
the styled Qt widget, track classification, view engagement (live CC +
fallback to VLC rendering) and the SrtParser keep-lines mode.

Run:  .venv\\Scripts\\python.exe test_caption_overlay.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.profanity import SrtParser, mask_text  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402
from src.ui.caption_overlay import CueStore, CaptionOverlay  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def temp_config() -> Config:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    return Config({}, Path(path))


app = QtWidgets.QApplication(sys.argv)

print("[1] CueStore: timing, newest-wins, roll-up window")
cs = CueStore()
cs.add(10.0, 12.0, "first")
cs.add(12.0, 14.0, "second")
check("no cue before its time", cs.text_at(5.0) == [])
check("first cue active", cs.text_at(11.0) == ["first"])
check("cue held briefly past its end (anti-flicker)",
      cs.text_at(12.1) == ["second"])   # newest wins in an overlap
cs.add(14.0, 16.0, "l1\nl2\nl3\nl4")
check("roll-up window keeps the last 3 lines",
      cs.text_at(15.0) == ["l2", "l3", "l4"])
cs.add(14.0, 16.0, "l1\nl2\nl3\nl4")     # duplicate
check("duplicate cues drop out", len([c for c in cs.cues
                                      if c[2] == "l1\nl2\nl3\nl4"]) == 1)
cs2 = CueStore()
for i in range(6000):
    cs2.add(float(i), float(i) + 1.0, f"c{i}")
check("store bounded (6000 adds -> <= 5000 cues)", len(cs2.cues) <= 5000)
check("oldest evicted, newest kept",
      cs2.cues[-1][2] == "c5999" and cs2.cues[0][0] > 900.0)

print("[2] SrtParser keep_lines (roll-up line structure)")
p = SrtParser(keep_lines=True)
cues = p.feed("1\r\n00:00:01,000 --> 00:00:03,000\r\n<i>hello</i>\r\n"
              "{\\an8}world\r\n\r\n")
check("line breaks preserved, tags/braces stripped",
      cues and cues[0][2] == "hello\nworld")
p2 = SrtParser()
cues2 = p2.feed("1\r\n00:00:01,000 --> 00:00:03,000\r\n<i>hello</i>\r\n"
                "world\r\n\r\n")
check("default parser still collapses to one line",
      cues2 and cues2[0][2] == "hello world")

print("[3] CaptionOverlay: style from the config")
ov = CaptionOverlay()
ov.resize(1920, 1080)
ap = {
    "delay_ms": 0, "font": "", "size": 40, "pos_pct": 0,
    "text_color": "#FFFFFF", "bg_enabled": False, "bg_color": "#000000",
    "bg_opacity": 50, "outline_enabled": True, "outline_color": "#000000",
    "outline_thickness": 4,
}
ov.bind_config(lambda: ap)
check("font px scales with the surface (40 px @1080p on 1080p)",
      ov._font_px(ap, 1080) == 40)
check("font px scales with the surface (40 px @1080p on 540p)",
      ov._font_px(ap, 540) == 20)
check("size 0 = auto (~5% of the height)",
      ov._font_px({"size": 0}, 1080) == round(1080 * 0.05))
fm = QtGui.QFontMetrics(QtGui.QFont())
long_line = "this is a quite long caption line that must wrap " * 3
wrapped = ov._wrap(fm, long_line, 300)
check("long lines wrap to the width",
      len(wrapped) > 1 and all(fm.horizontalAdvance(w) <= 300
                               for w in wrapped))
ov.set_lines(["hello", "world"])
check("set_lines shows the widget", ov.isVisible())
ov.set_lines([])
check("empty set_lines hides the widget", not ov.isVisible())
col = ov._color("#FF0000", "#FFFFFF", 128)
check("color parses hex + alpha",
      col.red() == 255 and col.green() == 0 and col.alpha() == 128)
bad = ov._color("not-a-color", "#FFFFFF", 10)
check("invalid color falls back", bad.isValid() and bad.alpha() == 10)

print("[4] track classification")
kind = PlayerView._cap_track_kind
check("live CEA-608 tracks are text",
      kind("Closed captions 1") == "text"
      and kind("CC1") == "text" and kind("608/708 captions") == "text")
check("DVB/teletext/PGS tracks are bitmap",
      kind("English (DVB)") == "bitmap"
      and kind("Teletext 1") == "bitmap"
      and kind("Danish (PGS)") == "bitmap")
check("ASS tracks are ass",
      kind("English (ASS)") == "ass" and kind("Signs (SSA)") == "ass")
check("plain language names are 'other'",
      kind("English (United States) - [English]") == "other")

print("[5] view: live engagement + fallback to VLC rendering")
cfg = temp_config()
view = PlayerView(cfg)

CCStarts = []


class StubCC(QtCore.QObject):
    cue = QtCore.pyqtSignal(float, float, str)

    def __init__(self, parent=None):
        super().__init__()

    def start(self, ts, offset=0.0):
        CCStarts.append((ts, offset))
        return True

    def stop(self):
        pass

    def deleteLater(self):
        pass


pv_mod.CCSource = StubCC
pv_mod.find_ccextractor = lambda: "C:/fake/ccx.exe"

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

    def stop_and_release(self):
        pass


fake = FakeVLC()
view.vlc = fake
view._filter_engine.player = fake

view.current = {"kind": "live", "url": "http://x/s.ts", "title": "L"}
view._mode = "chase"
view.dvr = type("FakeDVR", (), {
    "running": True, "file_path": "X:/buffer.ts",
    "buffer_file": lambda self: "X:/buffer.ts"})()
view._frontier_s = lambda: 42.0

# selecting a text caption track engages the overlay + starts CCSource
view._select_spu(2, "Closed captions 1")
check("selecting a CC track turns the overlay on", view._cap_on)
check("overlay claim is sticky", view._cap_want)
check("CCSource started on the buffer",
      CCStarts and CCStarts[0][0] == "X:/buffer.ts"
      and view._cc_source is not None)
check("VLC's own spu forced OFF under the overlay", fake.spu == -1)
check("cap timer runs while the overlay owns captions",
      view._cap_timer.isActive())

# cues flow to the store and render at the tracked position (+delay)
view._on_cc_cue(50.0, 52.0, "what the hell is this")
view._vid_s = 51.0
view._caption_tick()
check("active cue rendered by the overlay",
      view._cap_wid._lines == ["what the hell is this"])
view._vid_s = 60.0
view._caption_tick()
check("cue clears after its window", view._cap_wid._lines == [])

# delay applies live (arithmetic, no rebuild)
cfg.subtitle_appearance = dict(cfg.subtitle_appearance, delay_ms=2000)
view._vid_s = 48.5
view._caption_tick()
check("positive delay shows the cue 2 s early-at-position",
      view._cap_wid._lines == ["what the hell is this"])
cfg.subtitle_appearance = dict(cfg.subtitle_appearance, delay_ms=0)

# _enforce_spu keeps VLC's spu off while the overlay owns captions
view._spu_want = 2
fake.spu = 2                       # as if an ES update re-selected it
view._enforce_spu()
check("enforcement re-forces spu off under the overlay", fake.spu == -1)

# source failure -> VLC rendering for the rest of the media
view._cap_source_failed("CCExtractor exited")
check("failure latch set", view._cap_fail)
check("overlay off after failure", not view._cap_on)
check("CCSource torn down after failure", view._cc_source is None)
fake.tracks = [(2, "Closed captions 1")]
view._enforce_spu()
check("VLC spu restored by enforcement after failure", fake.spu == 2)

# a new media resets the latch (fresh session)
view._cap_fail = False

# Off disengages the overlay
view._cap_fail = False
view._select_spu(2, "Closed captions 1")     # re-engage
check("re-engage after fresh media works", view._cap_on)
view._select_spu(-1, "")
check("Off stops the overlay + claim", not view._cap_on
      and not view._cap_want)

# bitmap / ASS tracks never engage the overlay
view._select_spu(3, "English (DVB)")
check("bitmap track stays on VLC", not view._cap_on and fake.spu == 3)
view._select_spu(4, "Signs (ASS)")
check("ASS track stays on VLC", not view._cap_on and fake.spu == 4)

print("[6] view: profanity masking in the overlay render")
view._cap_fail = False
view._select_spu(2, "Closed captions 1")
view._filter_engine.enabled = True
view._filter_engine.words = [("hell", "exact")]
view._on_cc_cue(70.0, 72.0, "what the hell is this")
view._vid_s = 71.0
view._caption_tick()
check("masked line rendered while the filter is on",
      view._cap_wid._lines == [mask_text("what the hell is this",
                                         [("hell", "exact")])]
      and "*" in view._cap_wid._lines[0])
view._filter_engine.enabled = False
view._vid_s = 70.5
view._caption_tick()
check("unmasked once the filter is off",
      view._cap_wid._lines == ["what the hell is this"])

view._set_cap_on(False)
view._stop_cc_source()
view.dvr = None
view.current = None

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
