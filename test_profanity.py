# -*- coding: utf-8 -*-
"""Tests for the profanity filter: matching levels, SRT parsing, windows,
engine timing, mute layering, view integration and the settings dialog.

Run:  .venv\\Scripts\\python.exe test_profanity.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.player import VLCPlayer  # noqa: E402
from src.profanity import (DEFAULT_WORDS, SrtParser, find_matches,  # noqa: E402
                           mask_text, merge_windows, windows_from_cues)
from src.profanity import ProfanityEngine, SubtitleExtractor  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402
from src.ui.profanity_dialog import ProfanityDialog  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def temp_config() -> Config:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    return Config({}, Path(path))


class FakePlayer:
    def __init__(self):
        self.calls = []

    def set_filter_mute(self, on):
        self.calls.append(bool(on))


SRT_SAMPLE = "\r\n".join([
    "1",
    "00:00:01,000 --> 00:00:04,000",
    "<i>What the {\an8}hell is this</i>",
    "",
    "2",
    "00:00:05,500 --> 00:00:07,000",
    "damn dogs in the doghouse",
    "",
])


def main():
    app = QtWidgets.QApplication(sys.argv)

    print("[1] match levels — the user's exact examples")
    t = "dog in the doghouse"
    check("exact", mask_text(t, [("dog", "exact")]) == "*** in the doghouse")
    check("partial", mask_text(t, [("dog", "partial")])
          == "*** in the ***house")
    check("whole", mask_text(t, [("dog", "whole")]) == "*** in the ********")
    check("case-insensitive", mask_text("DOG", [("dog", "exact")]) == "***")
    check("exact skips compounds",
          find_matches("the doghouse barked", [("dog", "exact")]) == [])
    check("whole catches compound",
          mask_text("what the motherfucking hell",
                    [("motherfucking", "whole")])
          == "what the " + "*" * 13 + " hell")
    check("punctuation counts as a boundary",
          mask_text("what the hell!", [("hell", "exact")])
          == "what the " + "*" * 4 + "!")

    print("[2] SRT text cleaning")
    from src.profanity import clean_text
    check("tags/braces stripped",
          clean_text("<i>{\\an8}hi<i> there</i>") == "hi there")

    print("[3] incremental SRT parser (chunks split mid-line)")
    p = SrtParser()
    cues = []
    data = SRT_SAMPLE
    for i in range(0, len(data), 7):          # deliberately awkward splits
        cues += p.feed(data[i:i + 7])
    cues += p.flush()
    check("two cues parsed", len(cues) == 2)
    check("cue 1 time + text", cues[0][0] == 1.0 and cues[0][1] == 4.0
          and cues[0][2] == "What the hell is this")
    check("cue 2 time + text", cues[1][0] == 5.5 and cues[1][1] == 7.0)

    print("[4] windows from cues (proportional word timing)")
    text = "one damn dogs"
    # cue 10s->20s, 13 chars: 'damn' at chars 4..8 -> 10 + 4/13*10 .. 10+8/13*10
    wins = windows_from_cues([(10.0, 20.0, text)], [("damn", "exact")])
    check("exact window inside the cue",
          len(wins) == 1 and abs(wins[0][0] - (10 + 40 / 13)) < 0.01
          and abs(wins[0][1] - (10 + 80 / 13)) < 0.01)
    wins = windows_from_cues([(10.0, 20.0, text)], [("dog", "partial")])
    check("partial window shorter", len(wins) == 1 and wins[0][1] - wins[0][0]
          == 30 / 13 or abs((wins[0][1] - wins[0][0]) - 30 / 13) < 0.01)
    wins = windows_from_cues([(1.0, 2.0, "clean as snow")],
                             [("damn", "exact")])
    check("clean cue produces nothing", wins == [])
    check("overlaps merge",
          merge_windows([(1, 3), (2, 5), (7, 8)]) == [(1, 5), (7, 8)])

    print("[5] engine: pads, sync and edge behaviour")
    fp = FakePlayer()
    eng = ProfanityEngine(fp)
    eng.words = [("hell", "exact")]
    eng.pad_before_s, eng.pad_after_s = 0.5, 0.5
    eng.enabled = True
    eng.windows = [(10.0, 11.0)]
    eng.evaluate(9.49)
    check("no mute before pad-before", fp.calls == [])
    eng.evaluate(9.5)
    check("pad-before opens the mute early", fp.calls[-1] is True)
    eng.evaluate(11.5)
    check("pad-after still muted (inclusive edge)", fp.calls[-1] is True)
    eng.evaluate(11.5 + 0.01)
    check("unmuted after pad", fp.calls[-1] is False)
    eng.sync_s = 1.0
    eng.evaluate(9.5)          # 9.5+1 = 10.5 -> inside word
    check("sync offset shifts the window", fp.calls[-1] is True)
    eng.evaluate(8.4)          # 8.4+1 = 9.4 -> before pad
    check("sync offset respected before", fp.calls[-1] is False)
    eng.enabled = False
    eng.evaluate(10.5)
    check("disabled engine never mutes", fp.calls[-1] is False)
    eng2 = ProfanityEngine(fp)
    eng2.enabled = True
    eng2.windows = [(10, 11)]
    eng2.set_muted(True)
    eng2.clear()
    check("clear() unmutes the filter", fp.calls[-1] is False
          and eng2.windows == [])

    print("[6] VLCPlayer: filter mute layers under the user's mute")
    vp = VLCPlayer(timeshift=False)
    vp.set_filter_mute(True)
    check("filter mute set without user mute",
          vp._filter_mute is True and vp.is_mute() is False)
    vp.set_mute(False)             # user un-mutes; filter must keep holding
    check("user mute re-apply keeps filter mute",
          vp._filter_mute is True and vp._mute is False)
    vp.set_filter_mute(False)
    check("filter release restores audible (user unmuted)",
          vp._filter_mute is False and vp._mute is False)
    vp.set_mute(True)
    vp.set_filter_mute(True)
    vp.set_filter_mute(False)
    check("user mute survives filter on/off", vp._mute is True)
    vp.stop_and_release()

    print("[7] view integration: engage/disengage/cleanup")
    cfg = temp_config()
    view = PlayerView(cfg)
    mut = []
    view.vlc.set_filter_mute = mut.append

    class StubExtractor(QtCore.QObject):
        calls = []
        cue = QtCore.pyqtSignal(float, float, str)

        def __init__(self, parent=None):
            super().__init__()
            self.proc = None
            self.frontier_s = -1.0
            self._prefer_language = ""
            self._want_index = 0

        def probe_track(self, url, ua):
            StubExtractor.calls.append(("probe", url))
            return True

        def start(self, url, ua, prefer="", at=0.0, readrate=6):
            StubExtractor.calls.append(("start", url, at))
            self.proc = object()
            return True

        def stop(self):
            StubExtractor.calls.append(("stop",))

        def deleteLater(self):
            pass

    pv_mod.prof_mod.SubtitleExtractor = StubExtractor
    pv_mod.prof_mod.find_ffmpeg = lambda: "C:/fake/ffmpeg.exe"
    # the machinery is parked behind this flag in the app (second-connection
    # engine retired); the tests exercise the machinery itself
    pv_mod.prof_mod.PROFANITY_AVAILABLE = True

    cfg.profanity = {"enabled": True}
    view.apply_profanity_settings()
    view.current = {"kind": "vod", "url": "http://x/movie.mkv",
                    "title": "M"}
    view._on_media_for_profanity("vod")
    check("extraction probed for VOD with filter on",
          ("probe", "http://x/movie.mkv") in StubExtractor.calls)
    # probe runs async in the app; here we call the slot directly
    view._on_prof_probe(("ok", True))
    check("ffmpeg started", any(c[0] == "start"
                                for c in StubExtractor.calls))

    # cue -> window -> tick mutes inside, unmutes outside
    view._filter_engine.words = [("hell", "exact")]
    view._on_prof_cue(10.0, 12.0, "what the hell is this")
    check("cue became a window", len(view._filter_engine.windows) == 1)
    view._vid_s = 11.0
    view._filter_tick()
    check("tick muted inside the word", mut and mut[-1] is True)
    view._vid_s = 40.0
    view._filter_tick()
    check("tick unmuted outside", mut[-1] is False)

    # disable -> everything torn down
    StubExtractor.calls.clear()
    cfg.profanity = {"enabled": False}
    view.apply_profanity_settings()
    check("disabling stops the extractor",
          any(c[0] == "stop" for c in StubExtractor.calls)
          and view._filter_extractor is None)
    check("disabling cleared windows",
          view._filter_engine.windows == [])

    # live channel never engages
    StubExtractor.calls.clear()
    view._on_media_for_profanity("live")
    check("live TV does not engage the filter",
          not any(c[0] == "probe" for c in StubExtractor.calls))

    print("[8] settings dialog")
    cfg2 = temp_config()
    saved = []
    from src.ui import profanity_dialog as pdlg_mod
    pdlg_mod.PROFANITY_AVAILABLE = False   # parked: as shipped right now
    dlg = ProfanityDialog(cfg2, lambda: saved.append(True))
    check("starts with the default word list",
          dlg.table.rowCount() == len(DEFAULT_WORDS))
    check("toggle disabled while parked", not dlg.ck_on.isEnabled())
    dlg._add_row("hell", "partial")
    dlg.ck_on.setChecked(True)
    dlg.accept()
    check("parked dialog refuses to enable",
          cfg2.profanity["enabled"] is False)
    pdlg_mod.PROFANITY_AVAILABLE = True    # engine available again
    dlg = ProfanityDialog(cfg2, lambda: saved.append(True))
    dlg._add_row("hell", "partial")
    dlg._add_row("hell", "whole")     # duplicate is dropped on collect
    dlg.sp_before.setValue(300)
    dlg.sp_sync.setValue(-150)
    dlg.ck_on.setChecked(True)
    dlg.accept()
    prof = cfg2.profanity
    check("saved toggle + timing",
          prof["enabled"] and prof["pad_before_ms"] == 300
          and prof["sync_ms"] == -150)
    check("custom word saved once", ["hell", "partial"] in prof["words"]
          and sum(1 for w in prof["words"] if w[0] == "hell") == 1)
    check("on_saved callback ran (both dialogs)",
          len(saved) == 2 and all(s is True for s in saved))
    dlg2 = ProfanityDialog(cfg2, saved.append)
    dlg2._reset_words()
    dlg2.accept()
    check("restore defaults works",
          [w[0] for w in cfg2.profanity["words"]]
          == [w[0] for w in DEFAULT_WORDS])

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
