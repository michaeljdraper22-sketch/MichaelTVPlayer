# -*- coding: utf-8 -*-
"""Tests for the app-rendered caption overlay: cue store timing/roll-up,
the styled Qt widget, track classification, view engagement (live CC +
fallback to VLC rendering) and the SrtParser keep-lines mode.

Run:  .venv\\Scripts\\python.exe test_caption_overlay.py
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src import live_cc as live_cc_mod  # noqa: E402
from src.profanity import SrtParser, mask_text  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402
from src.ui.caption_overlay import (CueStore, CaptionOverlay,  # noqa: E402
                                    displayed_video_rect)

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

# rip-house padding: cues/lines made only of bidi controls + NBSP paint
# NOTHING — they must not render the "background box with no text"
cs3 = CueStore()
cs3.add(20.0, 22.0, "\u202b\xa0\u202c")            # bidi + NBSP only
check("invisible-only cue skipped (no empty box)", cs3.text_at(21.0) == [])
cs3.add(24.0, 26.0, "hello\n\u202b\xa0\u202b\nworld")   # padded middle line
check("invisible-only LINES dropped, visible ones kept",
      cs3.text_at(25.0) == ["hello", "world"])
cs3.add(28.0, 30.0, "\ufeffreal\u200b \u202bmarked")
check("zero-width marks stripped from visible text",
      cs3.text_at(29.0) == ["real marked"])
cs3.add(32.0, 34.0, "\u202b\u0645\u0631\u062d\u0628\u0627")   # Arabic: kept
check("Arabic glyphs survive the cleanup", cs3.text_at(33.0) is not None
      and "\u0645" in cs3.text_at(33.0)[0])
# second round of the same report: soft hyphens, LRM/RLM marks and
# zero-width joiners ALSO paint nothing — a line made only of them
# still rendered the background box with no text inside
cs3.add(36.0, 38.0, "\u00ad\u00ad\u00ad")          # soft hyphens only
check("soft-hyphen-only cue skipped (no empty box)",
      cs3.text_at(37.0) == [])
cs3.add(40.0, 42.0, "\u200e\u200f\u200e")          # LRM/RLM only
check("LRM/RLM-only cue skipped (no empty box)",
      cs3.text_at(41.0) == [])
cs3.add(44.0, 46.0, "\u200d\u200d")                # ZWJ only
check("ZWJ-only cue skipped (no empty box)",
      cs3.text_at(45.0) == [])
cs3.add(48.0, 50.0, "ca\u00adffe\u0301 \u200eo\u200f")   # marks INSIDE words
check("soft hyphen/LRM stripped from visible text",
      cs3.text_at(49.0) == ["caffe\u0301 o"])
arabic_lig = "\u0645\u200d\u0631"                  # ZWJ between Arabic
cs3.add(52.0, 54.0, arabic_lig)
check("ZWJ kept inside Arabic text (shaping)",
      cs3.text_at(53.0) == [arabic_lig])

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

print("[3a] preview line (settings-dialog live feedback)")
ov.set_preview("Subtitle preview")
check("preview shows the widget with no lines", ov.isVisible())
ov.set_preview("Subtitle preview")
check("same preview is a no-op", ov._preview == "Subtitle preview")
ov.set_lines(["real cue"])
check("real cues take precedence over the preview",
      ov.isVisible() and ov._lines == ["real cue"])
ov.set_lines([])
check("preview survives cue gaps", ov.isVisible())
ov.set_preview("")
check("clearing the preview hides the widget again", not ov.isVisible())

print("[3b] displayed-video rect math (captions anchor to the picture)")
check("16:9 video in a 16:9 surface fills it",
      displayed_video_rect((1920, 1080), "fit", 1920, 1080)
      == (0, 0, 1920, 1080))
check("2.35:1 movie letterboxed in a 16:9 surface",
      displayed_video_rect((2350, 1000), "fit", 1920, 1080)
      == (0, 131, 1920, 817))
check("4:3 video pillarboxed in a 16:9 surface",
      displayed_video_rect((1440, 1080), "fit", 1920, 1080)
      == (240, 0, 1440, 1080))
check("portrait video pillarboxed",
      displayed_video_rect((1080, 1920), "fit", 1920, 1080)
      == (656, 0, 608, 1080))
check("crop covers the whole surface", displayed_video_rect(
    (2350, 1000), "crop", 1920, 1080) == (0, 0, 1920, 1080))
check("stretch covers the whole surface", displayed_video_rect(
    (1440, 1080), "stretch", 1920, 1080) == (0, 0, 1920, 1080))
check("unknown size (0,0) falls back to the whole surface",
      displayed_video_rect((0, 0), "fit", 1920, 1080) == (0, 0, 1920, 1080))
check("degenerate surface doesn't crash",
      displayed_video_rect((1920, 1080), "fit", 0, 0) == (0, 0, 0, 0))
# acceptance core: same caption size RELATIVE TO THE PICTURE whatever the
# letterboxing — config 40 px @1080p against each picture height
px16 = ov._font_px(ap, 1080)
px235 = ov._font_px(ap, displayed_video_rect((2350, 1000), "fit",
                                             1920, 1080)[3])
check("16:9 and letterboxed captions share one size relative to the picture",
      abs(px16 / 1080.0 - px235 / 817.0) < 0.001)


print("[3c] one painted caption size/position: 16:9 live vs letterboxed movie")


def _ink_rows(w):
    """Top/bottom pixel rows carrying ink in a grab of the widget — the
    overlay paints text over transparency, so ink rows == the text."""
    img = w.grab().toImage()
    if img.format() != img.Format_ARGB32:
        img = img.convertToFormat(img.Format_ARGB32)
    data = img.constBits().asstring(img.sizeInBytes())
    bpl = img.bytesPerLine()
    rows = [y for y in range(img.height())
            if any(data[y * bpl + 3:(y + 1) * bpl:4])]
    return (rows[0], rows[-1]) if rows else None


# The overlay anchors to the DISPLAYED picture rect (displayed_video_rect
# + _layout_overlays), so a 16:9 channel filling the window and a 2.35:1
# movie letterboxed inside it must PAINT captions at the same size and
# height relative to the PICTURE — measured through the real paint path
# (grab + ink scan), not just the geometry math.
r_live = displayed_video_rect((1920, 1080), "fit", 1920, 1080)
r_film = displayed_video_rect((2350, 1000), "fit", 1920, 1080)
check("live-sized surface: picture fills it", r_live == (0, 0, 1920, 1080))
check("movie-sized surface: 2.35:1 letterboxed in 16:9",
      r_film == (0, 131, 1920, 817))
check("font px pinned on the live picture (40 @1080p)",
      ov._font_px(ap, r_live[3]) == 40)
check("font px pinned on the letterboxed picture (30 @817p)",
      ov._font_px(ap, r_film[3]) == 30)
ov.set_bottom_inset(0)   # drop the constant control-bar clearance: the
#                         # invariant under test is picture-relative math
metrics = {}
for tag, rect in (("live", r_live), ("film", r_film)):
    ov.resize(rect[2], rect[3])
    ov.set_lines(["the quick brown fox"])
    app.processEvents()
    top, bot = _ink_rows(ov) or (-1, -1)
    metrics[tag] = (rect[3], bot - top, rect[3] - bot)
ov.set_lines([])
ov.set_bottom_inset(24)
h_l, th_l, bm_l = metrics["live"]
h_f, th_f, bm_f = metrics["film"]
check(f"both pictures painted ink (live {th_l}px, film {th_f}px)",
      th_l > 0 and th_f > 0)
check(f"painted text height identical relative to the picture "
      f"({th_l}/{h_l} vs {th_f}/{h_f})",
      abs(th_l / h_l - th_f / h_f) < 0.004)
check(f"painted bottom anchor identical relative to the picture "
      f"({bm_l}/{h_l} vs {bm_f}/{h_f})",
      abs(bm_l / h_l - bm_f / h_f) < 0.004)

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
check("VobSub-named tracks are bitmap (VLC renders them)",
      kind("English (VobSub) - [English]") == "bitmap"
      and kind("VobSub") == "bitmap")

print("[4c] language matching (CC-menu word vs MKV ISO code)")
from src.mkv_subs import MkvSubParser as _MkvP, lang_matches, \
    is_language_name  # noqa: E402
check("full word matches ISO code", lang_matches("english", "eng", ""))
check("ISO code matches full word", lang_matches("eng", "", "English"))
check("two-letter code matches ISO code", lang_matches("en", "eng", ""))
check("mismatched languages never match",
      not lang_matches("english", "ara", "")
      and not lang_matches("english", "ger", "German"))
check("short codes never substring-match",
      not lang_matches("en", "", "French"))
check("known language words recognized",
      is_language_name("english") and is_language_name("en")
      and is_language_name("german"))
check("junk hints rejected",
      not is_language_name("track") and not is_language_name("closed")
      and not is_language_name(""))

# the regression this file guards: an 'English' pick used to fall through
# to the FIRST text track (Arabic on multi-language rips) because the
# MKV Language element says 'eng', not 'english'
p = _MkvP(prefer_language="english")
p._track_meta = {
    3: {"codec": "S_TEXT/UTF8", "lang": "ar", "name": ""},
    4: {"codec": "S_TEXT/UTF8", "lang": "eng", "name": ""},
    5: {"codec": "S_HDMV/PGS", "lang": "eng", "name": ""},
}
p._select_track()
check("'english' hint selects the eng text track", p._selected == 4)

p2 = _MkvP(prefer_language="english")   # no English TEXT track exists
p2._track_meta = {
    3: {"codec": "S_TEXT/UTF8", "lang": "ar", "name": ""},
    4: {"codec": "S_HDMV/PGS", "lang": "eng", "name": ""},
}
p2._select_track()
check("no wanted-language text -> first text track (UI then hands off)",
      p2._selected == 3)

print("[4b] overlay eligibility by content kind (unified subtitles)")


class _EligShim:
    """Just enough PlayerView for _cap_eligible: a ``current`` media of
    the wanted kind, borrowing the real classifier methods."""

    _is_vod = PlayerView._is_vod
    _cap_track_kind = staticmethod(PlayerView._cap_track_kind)

    def __init__(self, kind):
        self.current = {"kind": kind, "url": "http://x/m.mkv",
                        "title": "T"}


def elig(kind, name):
    return PlayerView._cap_eligible(_EligShim(kind), name)


# text always renders in the overlay; ASS and plain names only on VOD
# (the relay's MKV parser flattens ASS there; live plain names may hide
# DVB bitmaps); bitmap tracks never qualify.
check("ASS is overlay-eligible on VOD (the relay flattens it)",
      elig("vod", "English (ASS)") and elig("series", "Signs (SSA)"))
check("ASS stays on VLC for live", not elig("live", "English (ASS)"))
check("plain names are overlay-eligible on VOD (SRT MKVs carry them)",
      elig("vod", "English (United States) - [English]"))
check("plain names stay on VLC for live",
      not elig("live", "English (United States) - [English]"))
check("DVB bitmap tracks are never overlay-eligible",
      not elig("vod", "English (DVB)") and not elig("live", "Danish (DVB)"))
check("CC/SRT text tracks are overlay-eligible everywhere",
      elig("live", "Closed captions 1") and elig("vod", "Track 1 - [SRT]"))

print("[4d] CC-menu language hints (relay track re-selection)")

# VLC names unnamed MKV tracks "Track N - [Language]" — the bracket is the
# only language cue; the bare head word is "track" (junk). Regression:
# the junk hint defeated the relay's prefer-language match AND the
# _cap_vod_check language check on multi-track rips.
check("bracketed language extracted from 'Track N - [Language]'",
      PlayerView._cap_lang_hint("Track 2 - [English]") == "english")
check("bracketed language works for any known language",
      PlayerView._cap_lang_hint("Track 7 - [German]") == "german")
check("named tracks keep the head-word hint",
      PlayerView._cap_lang_hint(
          "English (United States) - [English]") == "english")
check("junk without a language falls through empty",
      PlayerView._cap_lang_hint("Track 2 - [Commentary]") == ""
      and PlayerView._cap_lang_hint("") == "")

print("[5] view: live engagement + fallback to VLC rendering")
cfg = temp_config()
view = PlayerView(cfg)

CCStarts = []


class StubCC(QtCore.QObject):
    cue = QtCore.pyqtSignal(float, float, str)

    def __init__(self, parent=None):
        super().__init__()

    def start(self, ts, offset=0.0, join_bytes=0):
        CCStarts.append((ts, offset, join_bytes))
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

    def is_playing(self):
        return self.times.get("playing", True)

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

# ---- live cues: TRUE-POSITION anchoring + dead-reckoned caption clock ----
# Stage 2: the newest FRESH cue pins at edge - L on the app's content
# axis (L = measured CCX lag; edge = dead-reckoned write head, falling
# back to the frontier before a transport seeds the backlog). With no
# PCR probe available here the lag falls back to _CC_LAG_S.
# Stage 3: the decision is DEFERRED to the caption tick — _cc_flush_pending
# applies ONE anchor per arrival batch, written by the batch's newest cue.
view._frontier_s = lambda: 100.0           # mature buffer
view._on_cc_cue(50.0, 52.0, "what the hell is this")
check("lone first cue on a mature buffer does not anchor (untrusted)",
      view._cc_off is None and not view._cap_cues.cues)
view._on_cc_cue(52.0, 54.0, "what the hell is this")   # fresh successor
view._cc_flush_pending()
LAG = pv_mod._CC_LAG_S
off = view._cc_off
check("fresh successor cue anchors the CCX->app clock",
      off is not None and abs(off - (100.0 - LAG - 54.0)) < 1e-9)
view._on_cc_cue(70.0, 120.0, "ancient catch-up burst")
check("catch-up burst does not re-anchor (advance >> wall time)",
      view._cc_off == off)
# snap-and-rebase: a fresh cue whose implied correction exceeds
# _CC_REBASE_S sets the offset immediately (no EWMA crawl) AND slides
# every stored window with it, so the store's timeline stays coherent
# (the cue that ESTABLISHES the first anchor is not stored itself — its
# batch arrived while no offset existed; roll-up re-delivers that text)
view._cc_last_t = time.time() - 1.0        # plausible elapsed for advance
view._on_cc_cue(118.0, 122.0, "fresh successor after the burst")  # fresh
view._cc_flush_pending()
target2 = 100.0 - LAG - 122.0
check("big correction snap-rebases the anchor (no EWMA crawl)",
      abs(view._cc_off - target2) < 1e-9)
w0 = [c for c in view._cap_cues.cues
      if c[2] == "fresh successor after the burst"]
check("stored windows slide with the rebase (coherent timeline)",
      w0 and abs(w0[0][0] - (118.0 + target2)) < 1e-6
      and abs(w0[0][1] - (122.0 + target2)) < 1e-6)
# small corrections still EWMA part-way toward their estimate
view._cc_last_t = time.time() - 1.0
view._on_cc_cue(122.0, 123.0, "next line")      # fresh, ~1 s correction
view._cc_flush_pending()
target3 = 100.0 - LAG - 123.0
expected3 = target2 + (target3 - target2) * pv_mod._CC_ANCHOR_ALPHA
check("small correction steps the anchor part-way (EWMA, not a jump)",
      abs(view._cc_off - expected3) < 1e-9
      and abs(view._cc_off - target3) > 1e-6)
# stage-3 batch whipsaw guard: a flush BURST of individually-fresh cues
# (CCX stdout blocks) must anchor on the batch's NEWEST cue only — the
# stale interior cues' targets (a full batch width low) may never land.
# WP3 robust snap: the burst's correction lands out-of-band but < HARD,
# so the FIRST flush rides the EWMA (no snap, store unmoved — sample
# noise must not slam the store); a SECOND batch still out-of-band
# confirms a real jump and snaps.
view._cc_last_t = time.time() - 1.0
burst_off = view._cc_off
for b in range(30):                        # 30 cues spanning ~6 s, one burst
    view._on_cc_cue(123.0 + b * 0.2, 123.2 + b * 0.2, f"burst line {b}")
check("burst delivery alone does not move the anchor (deferred)",
      view._cc_off == burst_off)
burst_store = [c[:2] for c in view._cap_cues.cues]
view._cc_flush_pending()
target_burst = 100.0 - LAG - (123.2 + 29 * 0.2)
check("lone out-of-band burst correction rides the EWMA (robust snap)",
      abs(view._cc_off - (burst_off + (target_burst - burst_off)
                           * pv_mod._CC_ANCHOR_ALPHA)) < 1e-9)
# WP3 pin-time store: the ride moves the anchor only — stored cues keep
# their pin positions (whole-store shifts happen on rebase snaps only)
check("the ride leaves stored cues at their pin positions",
      [c[:2] for c in view._cap_cues.cues] == burst_store)
view._cc_last_t = time.time() - 1.0
view._on_cc_cue(132.0, 132.2, "still shifted regime")   # fresh successor
view._cc_flush_pending()
target_2nd = 100.0 - LAG - 132.2
check("confirmed out-of-band correction snaps (no EWMA crawl)",
      abs(view._cc_off - target_2nd) < 1e-9)
w_shift = [c for c in view._cap_cues.cues
           if c[2] == "still shifted regime"]
check("the confirming snap slides stored windows with it",
      w_shift and abs(w_shift[0][0] - (132.0 + target_2nd)) < 1e-6
      and abs(w_shift[0][1] - (132.2 + target_2nd)) < 1e-6)
view._frontier_s = lambda: 1000.0          # un-clamp the seed for show checks
# the anchor-establishing cue's window after the ride + confirm-snap
# (stored at its DELIVERY-time off, then slid by the rebase deltas)
w_fs = next(c for c in view._cap_cues.cues
            if c[2] == "fresh successor after the burst")
c0, c1 = w_fs[0], w_fs[1]


def _show_at(t):
    """One caption tick with the clock seeded at ``t`` (a fresh-media
    clock state: no reading, zero value — seeding follows _vid_s)."""
    view._cap_raw_s = None
    view._cap_clock_s = 0.0
    view._vid_s = t
    view._caption_tick()
    return view._cap_wid._lines


check("arrival-anchored cue renders inside its window",
      _show_at(c0 + 0.5) == ["fresh successor after the burst"])
_all_end = max(c[1] for c in view._cap_cues.cues)
check("cue clears after its anchored window",
      _show_at(_all_end + 1.5) == [])

# caption timing keys on the DISPLAYED position through outlier-rejected
# DELTAS only: raw advancing ~rate x wall folds in (the clock follows
# VLC's real timeline); absolute numbers are never snapped to
# WP2 (c): park the clock coherently with the baseline reading first —
# the anti-lead clamp now bounds a playing clock to raw + _CC_LEAD_MAX_S,
# and the old setup (clock left ~5 s past the reading by the _show_at
# seeds above) clamps instead of folding.
fake.times["t"] = int((c0 + 0.5) * 1000)    # baseline reading — only its
view._cap_raw_s = None                      # DELTA matters from here on
view._cap_clock_s = c0 + 0.5
view._cap_wall = time.time()
before = view._cap_clock_s
view._caption_tick()                        # baseline tick (branch: base)
time.sleep(0.12)
fake.times["t"] = int((c0 + 0.65) * 1000)   # advanced ~wall since the
view._caption_tick()                        # reading changed: fold it in
check("raw deltas that agree with wall fold into the clock",
      0.05 < view._cap_clock_s - before < 0.35)
fake.times["t"] += 30_000                   # PTS renumbering: numbers jump,
view._caption_tick()                        # frames keep playing 1:1
after_renum = view._cap_clock_s
check("renumbered raw cannot yank the clock (divergence recorded)",
      after_renum - before < 1.0 and view._cap_div_ok
      and abs(view._cap_div_s
              - ((c0 + 30.65) - after_renum)) < 0.05)
d = view._cap_div_s
check("divergence converts content seeks to VLC numbers",
      abs(view._cap_vlc_time_for(100.0) - (100.0 + d)) < 1e-9
      and abs(view._cap_content_for_raw(130.0) - (130.0 - d)) < 1e-9)
fake.times["t"] = -1                        # back to no-reading mode for
#                                            # the _vid_s-seeded tests below
for _ in range(2):                 # VLC's clock frozen at the same value:
    time.sleep(0.05)               # caption time integrates forward
    view._caption_tick()
check("frozen VLC clock keeps caption time moving (integration)",
      view._cap_clock_s > c0 + 0.55)
fake.times["t"] = -5_000                  # negative garbage reading
view._caption_tick()
check("garbage negative reading cannot yank the clock back",
      view._cap_clock_s >= c0 + 0.55)
fake.times["playing"] = False
before = view._cap_clock_s
fake.times["t"] = -1                # transient -1, player mid-reopen
time.sleep(0.05)
view._caption_tick()
check("transient -1 with the player down holds the clock",
      abs(view._cap_clock_s - before) < 1e-9)
fake.times["playing"] = True
fake.times["t"] = -1
view._cap_clock_s = 0.0             # back to no-VLC-clock mode for the
view._vid_s = 60.0                  # remaining _vid_s-fallback tests
view._cap_raw_s = None

# delay applies live (arithmetic, no rebuild). POSITIVE = LATER — the
# overlay must agree with config.py's wording, the +/- tooltip and VLC's
# own spu-delay path (player.py: positive ms = later): the tick queries
# the store at t - delay, holding every cue back by the delay.
view._cap_cues.clear()              # isolate the probe from the anchored
view._cap_cues.add(80.0, 82.0, "delay probe cue")   # live-cue windows
cfg.subtitle_appearance = dict(cfg.subtitle_appearance, delay_ms=2000)
check("positive delay holds the cue back at its true position (+ = later)",
      _show_at(80.5) == [])
check("positive delay shows the cue 2 s past its true window",
      _show_at(83.5) == ["delay probe cue"])
cfg.subtitle_appearance = dict(cfg.subtitle_appearance, delay_ms=0)
check("delay 0 paints the true window again",
      _show_at(80.5) == ["delay probe cue"])

# _caption_tick stays non-fatal on errors, but never SILENT: each
# DISTINCT error logs once (repeats suppressed; a new error logs once
# more)
import logging as _logging  # noqa: E402
_tick_log = []


class _TickLogCatcher(_logging.Handler):
    def emit(self, record):
        _tick_log.append(record.getMessage())


_tick_handler = _TickLogCatcher()
pv_mod.log.addHandler(_tick_handler)


class _BoomCues:
    cues = []

    def clear(self):
        pass

    def text_at(self, _t):
        raise RuntimeError("tick-boom")


_real_cues = view._cap_cues
try:
    view._cap_cues = _BoomCues()
    for _ in range(3):                # the SAME error three times…
        view._caption_tick()

    class _BoomCues2(_BoomCues):
        def text_at(self, _t):
            raise KeyError("tick-other-boom")

    view._cap_cues = _BoomCues2()
    view._caption_tick()              # …then a DIFFERENT error
finally:
    view._cap_cues = _real_cues
    pv_mod.log.removeHandler(_tick_handler)
view._caption_tick()                  # healthy store again: still painting
check("caption tick survives the errors (paints again after)",
      view._cap_wid._lines == ["delay probe cue"])
_tick_errs = [m for m in _tick_log if "tick failed" in m]
check("caption tick errors logged once per distinct error",
      len(_tick_errs) == 2
      and sum("tick-boom" in m for m in _tick_errs) == 1
      and sum("tick-other-boom" in m for m in _tick_errs) == 1)

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

# bitmap tracks (and ASS on live) never engage the overlay
view._select_spu(3, "English (DVB)")
check("bitmap track stays on VLC", not view._cap_on and fake.spu == 3)
view._select_spu(4, "Signs (ASS)")
check("ASS track stays on VLC (live)", not view._cap_on and fake.spu == 4)

# the CC menu must open even with NO VLC tracks (live channels expose
# theirs seconds after start, some never) — regression: 'off' was only
# bound inside `if tracks:` and the click crashed with UnboundLocalError,
# leaving captions unselectable on live TV
menus = []
view._popup_above = lambda menu, btn: menus.append(menu)
fake.tracks = []
view._subs_menu()
labels = [a.text() for a in menus[-1].actions()]
check("trackless stream still opens the CC menu",
      "Off" in labels and "Subtitle settings\u2026" in labels)
fake.tracks = [(2, "Closed captions 1")]

# VOD: the relay's parser flattens ASS to text — the overlay renders it
view.current = {"kind": "vod", "url": "http://x/m.mkv", "title": "M"}

class _RelayStub:
    def set_prefer_language(self, prefer):
        pass

view._vod_relay = _RelayStub()
view._select_spu(5, "English (ASS)")
check("ASS track engages the overlay on VOD", view._cap_on
      and fake.spu == -1 and view._cap_want)
view._spu_want = 5
view._cap_relay_failed("tap parser crashed: RuntimeError('boom')")
check("relay failure hands captions back to VLC", view._cap_fail
      and not view._cap_on and fake.spu == 5)

# _cap_vod_check language handoff: the user picked 'English' but the
# parser's only text track is Arabic (English lives on PGS bitmaps) —
# the overlay must hand captions back to VLC, not sit mute
class _RelayStub2:
    def __init__(self, selected, meta, tracks):
        self.parser_selected = selected
        self.parser_tracks_meta = meta
        self.parser_tracks = tracks

    def set_prefer_language(self, prefer):
        pass


view._cap_fail = False
view._vod_relay = _RelayStub2(
    3, {3: {"codec": "S_TEXT/UTF8", "lang": "ar", "name": ""},
        4: {"codec": "S_HDMV/PGS", "lang": "eng", "name": ""}},
    {3: "S_TEXT/UTF8", 4: "S_HDMV/PGS"})
view._select_spu(4, "English (United States) - [English]")
check("plain-name VOD pick engages the overlay", view._cap_on)
view._cap_vod_check()
check("Arabic-only text track hands captions back to VLC",
      view._cap_fail and not view._cap_on and fake.spu == 4)

# a matching eng text track keeps the app overlay
view._cap_fail = False
view._vod_relay = _RelayStub2(
    5, {3: {"codec": "S_TEXT/UTF8", "lang": "ar", "name": ""},
        5: {"codec": "S_TEXT/UTF8", "lang": "eng", "name": ""}},
    {3: "S_TEXT/UTF8", 5: "S_TEXT/UTF8"})
view._select_spu(5, "English (United States) - [English]")
check("re-engage with a matching text track", view._cap_on)
view._cap_vod_check()
check("eng text track keeps the app overlay",
      not view._cap_fail and view._cap_on)

# a tap that never produced ANY metadata (dead after a cache rebase)
# must hand captions back to VLC once the retries run out — sitting
# mute forever was the "no subtitles on movies" failure mode
view._cap_fail = False
view._cap_on = True
view._cap_want = True
view._spu_want = 5
view._vod_relay = _RelayStub2(None, {}, {})
view._cap_vod_tries = 0
view._cap_vod_check()
check("dead tap (no track metadata) hands captions back to VLC",
      view._cap_fail and not view._cap_on and fake.spu == 5)

print("[5c] stale relay deliveries never touch the next movie's store")
# VodRelay emits cue/failed from its worker threads over queued
# connections. play_media's teardown (relay disconnect+stop, store clear,
# session bump) races emissions that were already in flight — and
# `failed` isn't disconnected at all — so a delivery from the PREVIOUS
# media's relay can land after the next movie attached its own relay:
# stray caption, phantom profanity-mute window, cap_fail latched against
# a healthy new relay. The handlers must drop anything that isn't from
# the current media's relay. relay_a's connections stay live to model
# that in-flight delivery (this Qt build purges pending events on
# disconnect; the race window is an emit racing the disconnect itself).
class _StaleRelay(QtCore.QObject):
    cue = QtCore.pyqtSignal(float, float, str)
    failed = QtCore.pyqtSignal(str)

    def stop(self):
        pass

    def deleteLater(self):
        pass


relay_a = _StaleRelay()
relay_a.cue.connect(view._on_vod_cue)
relay_a.failed.connect(view._cap_relay_failed)
view._vod_relay = relay_a
view._cap_relay_gen = view._session          # the attach marker
view._cap_on = True                          # movie A: overlay engaged
# --- play_media teardown for movie B: overlay released, store cleared,
# --- session bumped, then a FRESH relay attached for the new media
view._cap_fail = False
view._cap_cues.clear()
view._filter_engine.clear()
view._filter_engine.words = [("hell", "exact")]   # a delivered stale cue
#                                                 # would open a window
view._session += 1
relay_b = _StaleRelay()
relay_b.cue.connect(view._on_vod_cue)
relay_b.failed.connect(view._cap_relay_failed)
view._vod_relay = relay_b
view._cap_relay_gen = view._session
# the stale deliveries arrive NOW, queued from relay A's worker thread
_stale_done = threading.Event()


def _stale_emit():
    relay_a.cue.emit(500.0, 502.0, "previous movie hell line")
    relay_a.failed.emit("tap reader died during teardown")
    _stale_done.set()


_stale_thread = threading.Thread(target=_stale_emit)
_stale_thread.start()
_stale_done.wait()
_stale_thread.join()
app.processEvents()                          # stale queued events land
check("stale cue from the previous relay never lands in the store",
      not view._cap_cues.cues)
check("stale cue opens no phantom profanity-mute window",
      not view._filter_engine.windows)
check("stale relay failure does not latch against the new media",
      not view._cap_fail)
relay_a.cue.disconnect()
relay_a.failed.disconnect()
relay_b.cue.disconnect()
relay_b.failed.disconnect()
view._set_cap_on(False)

view._vod_relay = None
view.current = {"kind": "live", "url": "http://x/s.ts", "title": "L"}

print("[5b] CCSource piping: whole buffer, and mid-buffer join")

# The live contract: which BYTES get piped. Default joins the WHOLE
# buffer (young chase buffers — CCX catch-up is trivial); a mid-session
# engage on a long-running buffer joins near the playback position
# (join_bytes) so CCX doesn't replay minutes of content nobody will
# display — display times come from the arrival anchor either way.
# CCExtractor itself is stubbed (launching the real one inside the
# offscreen test crashed the Qt event loop); a recording stdin captures
# exactly what would be piped.
_blob = b"\x47" * (188 * 100 + 100)      # 100 packets + a ragged tail
_writes = []


class _FakeProc:
    """Per-INSTANCE fake CCExtractor: a recording stdin plus a stdout
    that blocks until THIS proc is killed (a shared latched Event let
    the second source's reader see instant EOF and kill the source
    before the tailer ever wrote)."""

    def __init__(self):
        self.killed = threading.Event()
        outer = self

        class _In:
            def write(self, data):
                _writes.append(bytes(data))

            def close(self):
                pass

        class _Out:
            def read(self, _n):
                # block like a real pipe: instant EOF raced the
                # tailer's first write and "killed" the source
                # before it fed
                outer.killed.wait(timeout=5)
                return b""

        self.stdin = _In()
        self.stdout = _Out()

    def kill(self):
        self.killed.set()


_real_popen = live_cc_mod.subprocess.Popen
live_cc_mod.subprocess.Popen = lambda *a, **k: _FakeProc()
_real_find = live_cc_mod.find_ccextractor
live_cc_mod.find_ccextractor = lambda: "C:/fake/ccx.exe"
try:
    fd, ts = tempfile.mkstemp(suffix=".ts")
    os.write(fd, _blob)
    os.close(fd)
    real_src = live_cc_mod.CCSource()
    ok_start = real_src.start(ts, content_offset_s=123.0)
    check("CCSource starts on a non-empty buffer", ok_start)
    for _ in range(40):             # let the tailer thread finish its pass
        if sum(len(w) for w in _writes) >= len(_blob):
            break
        time.sleep(0.05)
    fed = b"".join(_writes)
    check("frontier offset ignored (offset 0, VLC-clock axis)",
          real_src._offset_s == 0.0)
    check("piped from byte 0 through the ragged tail (whole buffer)",
          fed == _blob)
    real_src.stop()
    # mid-session join: the pipe starts at the aligned join byte and
    # carries everything after it
    _writes.clear()
    real_src2 = live_cc_mod.CCSource()
    ok_join = real_src2.start(ts, join_bytes=188 * 50 + 55)
    check("joined start accepted", ok_join)
    for _ in range(40):
        if sum(len(w) for w in _writes) >= len(_blob) - 188 * 50:
            break
        time.sleep(0.05)
    fed2 = b"".join(_writes)
    check("join starts at the packet-aligned byte and carries the tail",
          fed2[:188] == _blob[188 * 50:188 * 51]
          and _blob[188 * 50:] in fed2)
    real_src2.stop()
    # stage-3 guard, flipped by WP4b: the vendored binary is now the
    # streaming-capable 0.96.6 subset, so a zero-install engage (only
    # the bundled CCX exists) must be ACCEPTED. The old static 0.88
    # build was refused here — it rejected the modern streaming flags
    # outright; the new build emits SRT as cues complete.
    bundled = live_cc_mod.bundled_ccextractor()
    if bundled:
        fails = []
        src3 = live_cc_mod.CCSource()
        src3.failed.connect(fails.append)
        live_cc_mod.find_ccextractor = lambda: bundled
        ok_bundled = src3.start(ts)
        check("bundled CCX accepted for live streaming",
              ok_bundled is True and not fails)
        src3.stop()
    os.remove(ts)
finally:
    live_cc_mod.subprocess.Popen = _real_popen
    live_cc_mod.find_ccextractor = _real_find

print("[6] view: profanity masking in the overlay render")
view._cap_fail = False
view._select_spu(2, "Closed captions 1")
view._filter_engine.enabled = True
view._filter_engine.words = [("hell", "exact")]
view._cap_cues.clear()                     # isolate: fresh anchor scenario
view._cc_off = None                       # fresh anchor scenario
view._cc_last_c = None
view._on_cc_cue(70.0, 70.5, "warm-up")    # lone opener: untrusted, dropped
view._on_cc_cue(70.5, 72.0, "what the hell is this")   # fresh: anchors
view._cc_flush_pending()
off6 = view._cc_off
view._cap_cues.add(70.5 + off6, 72.0 + off6, "what the hell is this")
shown6 = _show_at(71.0 + off6)             # inside the anchored window
check("masked line rendered while the filter is on",
      shown6 == [mask_text("what the hell is this", [("hell", "exact")])]
      and "*" in shown6[0])
view._filter_engine.enabled = False
check("unmasked once the filter is off",
      _show_at(70.5 + off6) == ["what the hell is this"])

# stage-3: the VOD filter path opens mute windows ~0.4 s early (movies
# were measured to miss mutes by ~0.5 s) without touching caption times
view._filter_engine.clear()
view._filter_engine.enabled = True
view._cap_cues.clear()
view._on_vod_cue(100.0, 104.0, "what the hell is this")
wv = view._filter_engine.windows[0]
_vtxt = "what the hell is this"
_raw = 100.0 + _vtxt.find("hell") / len(_vtxt) * 4.0
check("VOD mute window opens early by the mute-lead trim",
      abs(wv[0] - (_raw - pv_mod._VOD_MUTE_LEAD_S)) < 0.01,
      )
check("VOD caption times unchanged by the filter trim",
      view._cap_cues.cues
      and abs(view._cap_cues.cues[0][0] - 100.0) < 1e-9)
view._filter_engine.enabled = False

view._set_cap_on(False)
view._stop_cc_source()
view.dvr = None
view.current = None

print("[7] view: overlay anchors to the displayed video rect")
view.resize(1920, 1080)
view.layout().activate()
app.processEvents()
view._layout_overlays()           # deterministic relayout after the resize
g = view.surface.geometry()
check("view laid out: surface fills the 1920x1080 view",
      (g.width(), g.height()) == (1920, 1080))

# size unknown (fake has no vout yet) -> historic whole-surface overlay
fake.video_size = lambda: (0, 0)
view._poll_video_size()
check("unknown size keeps the whole-surface overlay",
      view._cap_wid.geometry() == QtCore.QRect(0, 0, 1920, 1080))

# 2:1 movie letterboxed in the 16:9 view -> overlay shrinks to the picture
fake.video_size = lambda: (2000, 1000)
view._poll_video_size()
check("letterboxed movie: overlay = displayed picture rect",
      view._cap_wid.geometry() == QtCore.QRect(0, 60, 1920, 960))

# the inset clears the whole-surface control bar (picture bottom above it)
view._wake()
view._layout_overlays()
bar_top = 1080 - view.ctl.height() - 10
check("bottom inset clears the control bar from the picture bottom",
      view._cap_wid._bottom_inset >= (60 + 960) - bar_top + 4)
view.ctl.hide()
view._layout_overlays()
check("controls hidden -> plain 24 px inset above the picture",
      view._cap_wid._bottom_inset == 24)

# crop mode: picture covers the surface again
view._set_scale_mode("crop")
view._apply_scale()
view._layout_overlays()
check("crop mode: overlay back to the full surface",
      view._cap_wid.geometry() == QtCore.QRect(0, 0, 1920, 1080))
view._set_scale_mode("fit")

# 16:9 channel at the same window size: full-surface overlay, same
# size-relative-to-picture as the letterboxed movie above
fake.video_size = lambda: (1920, 1080)
view._poll_video_size()
check("16:9 channel: overlay = full surface",
      view._cap_wid.geometry() == QtCore.QRect(0, 0, 1920, 1080))
d169 = view._cap_wid.height()
check("16:9 and 2:1 overlays keep one caption scale relative to the picture",
      abs(view._cap_wid._font_px(ap, d169) / d169
          - view._cap_wid._font_px(ap, 960) / 960.0) < 0.001)
fake.video_size = lambda: (0, 0)

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
