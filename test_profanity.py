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

from src.config import Config, PROFANITY_DEFAULTS  # noqa: E402
from src.player import VLCPlayer  # noqa: E402
from src.profanity import (DEFAULT_WORDS, SrtParser, find_matches,  # noqa: E402
                           mask_text, merge_windows, windows_from_cues)
from src.profanity import ProfanityEngine  # noqa: E402
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

    print("[5b] caption lead: windows shift EARLIER by lead_s")
    fp2 = FakePlayer()
    eng3 = ProfanityEngine(fp2)
    eng3.enabled = True
    eng3.words = [("hell", "exact")]
    eng3.lead_s = 1.5
    eng3.add_cue(10.0, 12.0, "what the hell is this")
    w0 = eng3.windows[0]
    # proportional: hell = chars 9..13 of 21
    txt = "what the hell is this"
    exp_s = 10 + txt.find("hell") / len(txt) * 2 - 1.5
    check("window moved earlier by the lead",
          abs(w0[0] - exp_s) < 0.01 and w0[1] < 12.0)
    eng3.evaluate(exp_s)
    check("mutes at the shifted (earlier) position", fp2.calls[-1] is True)
    eng3.evaluate(12.5)
    check("unmuted after the shifted window", fp2.calls[-1] is False)

    print("[5c] whole-cue mode: mute the whole line while the word shows")
    w_word = windows_from_cues([(10.0, 20.0, "one damn dogs")],
                               [("damn", "exact")])
    w_line = windows_from_cues([(10.0, 20.0, "one damn dogs")],
                               [("damn", "exact")], whole_cue=True)
    check("default (mode off) keeps word-granularity windows",
          abs(w_word[0][0] - (10 + 4 / 13 * 10)) < 0.01)
    check("whole_cue covers the entire cue", w_line == [(10.0, 20.0)])
    check("whole_cue ignores clean cues",
          windows_from_cues([(1.0, 2.0, "a clean line")],
                            [("damn", "exact")], whole_cue=True) == [])
    fp3 = FakePlayer()
    eng4 = ProfanityEngine(fp3)
    eng4.enabled = True
    eng4.words = [("hell", "exact")]
    eng4.whole_cue = True
    eng4.add_cue(10.0, 12.0, "what the hell is this", lead_s=0.0)
    check("engine whole-cue window = the full cue",
          eng4.windows == [(10.0, 12.0)])
    eng4.evaluate(10.05)
    check("muted right at cue start (the word itself is far later)",
          fp3.calls[-1] is True)
    eng4.evaluate(11.95)
    check("still muted near the cue end", fp3.calls[-1] is True)
    eng4.evaluate(12.3)
    check("unmuted once the cue leaves the screen", fp3.calls[-1] is False)

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

    print("[7] view integration: live engages DVR + caption reader")
    cfg = temp_config()
    view = PlayerView(cfg)
    mut = []
    view.vlc.set_filter_mute = mut.append

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

    cfg.data["chase_delay"] = 5
    cfg.profanity = {"enabled": True, "whole_cue": True}
    view._apply_profanity_config()
    check("view propagates the whole-cue mode to the engine",
          view._filter_engine.whole_cue is True)
    view.current = {"kind": "vod", "url": "http://x/m.mkv", "title": "M"}
    view._on_media_for_profanity("vod")
    check("VOD does not engage the live filter", CCStarts == [])

    # VOD mute-lead trim (stage 3): pre-timed tracks still open their mute
    # windows a fixed 0.4 s EARLY — movies measured ~0.5 s late mutes (the
    # word is audible as the window opens). Same cue with/without the trim
    # so the in-cue word-position estimate cancels out.
    view._filter_engine.clear()
    view._filter_engine.words = [("hell", "exact")]
    view._on_vod_cue(10.0, 12.0, "what the hell is this")
    vod_win = view._filter_engine.windows[0]
    view._filter_engine.clear()
    view._filter_engine.add_cue(10.0, 12.0, "what the hell is this",
                                lead_s=0.0)
    raw_win = view._filter_engine.windows[0]
    check("VOD mute windows open lead-early (stage-3 trim)",
          abs((raw_win[0] - vod_win[0]) - pv_mod._VOD_MUTE_LEAD_S) < 1e-9
          and abs((raw_win[1] - vod_win[1]) - pv_mod._VOD_MUTE_LEAD_S) < 1e-9
          and pv_mod._VOD_MUTE_LEAD_S <= 0.5)

    # VOD splitter glue: routing through the relay must ALSO start the
    # evaluation timer (the mute loop — the live CC reader is the only
    # other start() site)
    class StubRelay(QtCore.QObject):
        cue = QtCore.pyqtSignal(float, float, str)
        failed = QtCore.pyqtSignal(str)

        def start(self, url, ua, prefer_language="eng", start_offset=0):
            return "http://127.0.0.1:1/v"

        def stop(self):
            pass

    pv_mod.VodRelay = StubRelay
    local = view._effective_url("http://x/m.mkv", "vod")
    check("VOD routes through the splitter when the filter is on",
          local == "http://127.0.0.1:1/v" and view._vod_relay is not None)
    check("VOD engagement starts the evaluation timer",
          view._filter_timer.isActive())
    view._stop_profanity()
    check("teardown stops the VOD evaluation timer",
          not view._filter_timer.isActive())

    view.current = {"kind": "live", "url": "http://x.ts", "title": "L"}
    view._on_media_for_profanity("live")
    check("live alone does not start the reader (chase entry does)",
          CCStarts == [])
    check("live NEVER rewrites the delay setting", cfg.chase_delay == 5)
    view._set_dvr_status("")

    # chase active + buffer ready -> caption reader starts at the frontier
    cfg.data["chase_delay"] = 15     # user-chosen, above the 5 s floor
    view._mode = "chase"
    view._frontier_s = lambda: 42.0
    view.dvr = type("FakeDVR", (), {
        "running": True, "file_path": "X:/buffer.ts",
        "buffer_file": lambda self: "X:/buffer.ts"})()
    view._start_cc_when_buffer(tries_left=1)
    check("caption reader started on the buffer",
          CCStarts and CCStarts[0][0] == "X:/buffer.ts"
          and view._cc_source is not None)
    check("live caption engagement starts the evaluation timer",
          view._filter_timer.isActive())
    check("young buffer joins from byte 0 (no skip)",
          CCStarts and CCStarts[0][1] == 0.0 and CCStarts[0][2] == 0)

    # caption cue -> ARRIVAL-ANCHORED window -> chase tick mutes
    # (CCX's own times are remapped onto the app clock; lead is 0 — the
    # anchor already absorbs the pipeline lag)
    view._filter_engine.words = [("hell", "exact")]
    view._filter_engine.lead_s = 1.5
    view.vlc.get_time = lambda: -1     # idle player: clock follows _vid_s
    view._on_cc_cue(50.0, 50.5, "warm-up")     # lone opener: untrusted
    view._on_cc_cue(50.5, 52.0, "what the hell is this")   # fresh: anchors
    view._cc_flush_pending()                   # stage 3: deferred anchor
    off = view._cc_off
    check("live cue anchored onto the app clock (arrival)",
          off is not None and abs(off - (42.0 - pv_mod._CC_LAG_S - 52.0))
          < 1e-9)
    check("anchored caption cue became a window",
          len(view._filter_engine.windows) == 1)
    # the dead-reckoned clock keys on transport seeds (stage 2): display
    # at the anchored word, then past it
    word_c = view._filter_engine.windows[0][0] + 0.05
    view._cap_seed_transport(word_c)             # display inside the word
    view._filter_tick()
    check("chase tick muted inside the anchored word",
          mut and mut[-1] is True)
    view._cap_seed_transport(word_c + 5.0)       # display past it
    view._filter_tick()
    check("chase tick unmuted outside", mut[-1] is False)

    # disable -> reader torn down + windows cleared
    cfg.profanity = {"enabled": False}
    view.apply_profanity_settings()
    check("disabling tears down the reader",
          view._cc_source is None and view._filter_engine.windows == [])
    check("evaluation timer stops with the reader",
          not view._filter_timer.isActive())

    # too-short cushion: reader skipped, setting untouched, notice shown
    CCStarts.clear()
    cfg.profanity = {"enabled": True}
    view._apply_profanity_config()
    cfg.data["chase_delay"] = 3
    view._start_cc_when_buffer(tries_left=1)
    check("short delay skips the reader (no late mutes)",
          CCStarts == [] and view._cc_source is None)
    check("short delay notice shown, setting untouched",
          "too short" in view._dvr_status.text() and cfg.chase_delay == 3)
    view._set_dvr_status("")

    print("[8] settings dialog")
    cfg2 = temp_config()
    saved = []
    dlg = ProfanityDialog(cfg2, lambda: saved.append(True))
    check("starts with the default word list",
          dlg.table.rowCount() == len(DEFAULT_WORDS))
    dlg._add_row("hell", "partial")
    dlg.ck_on.setChecked(True)
    dlg.accept()
    check("toggle saves enabled", cfg2.profanity["enabled"] is True)
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

    print("[8b] whole-line checkbox + reset ALL settings")
    cfg3 = temp_config()
    dlg3 = ProfanityDialog(cfg3, lambda: None)
    check("whole-line checkbox defaults OFF (opt-in)",
          not dlg3.ck_whole.isChecked())
    dlg3.ck_on.setChecked(True)
    dlg3.ck_whole.setChecked(True)
    dlg3.accept()
    prof3 = cfg3.profanity
    check("whole-line preference persists", prof3["whole_cue"] is True)
    check("config getter carries the key from defaults",
          cfg3.profanity.get("whole_cue") is True)

    # wreck every setting, then Reset all settings -> factory restore
    dlg3 = ProfanityDialog(cfg3, lambda: None)
    dlg3.sp_before.setValue(2000)
    dlg3.sp_after.setValue(1500)
    dlg3.sp_lead.setValue(0)
    dlg3.sp_sync.setValue(-3000)
    dlg3.ck_whole.setChecked(False)
    dlg3._add_row("zzz", "partial")
    dlg3._reset_all()
    check("reset-all restores the enable state",
          dlg3.ck_on.isChecked() is PROFANITY_DEFAULTS["enabled"])
    check("reset-all restores the timing spinners",
          dlg3.sp_before.value() == PROFANITY_DEFAULTS["pad_before_ms"]
          and dlg3.sp_after.value() == PROFANITY_DEFAULTS["pad_after_ms"]
          and dlg3.sp_lead.value() == PROFANITY_DEFAULTS["lead_ms"]
          and dlg3.sp_sync.value() == PROFANITY_DEFAULTS["sync_ms"])
    check("reset-all restores the whole-line checkbox",
          dlg3.ck_whole.isChecked() is PROFANITY_DEFAULTS["whole_cue"])
    check("reset-all restores the word table",
          dlg3.table.rowCount() == len(DEFAULT_WORDS)
          and not any(dlg3.table.item(r, 0).text() == "zzz"
                      for r in range(dlg3.table.rowCount())))
    dlg3.accept()
    prof3 = cfg3.profanity
    check("reset-all persists the factory set on save",
          prof3["pad_before_ms"] == PROFANITY_DEFAULTS["pad_before_ms"]
          and prof3["sync_ms"] == PROFANITY_DEFAULTS["sync_ms"]
          and prof3["lead_ms"] == PROFANITY_DEFAULTS["lead_ms"]
          and prof3["whole_cue"] is False
          and [w[0] for w in prof3["words"]]
          == [w[0] for w in DEFAULT_WORDS])

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    # Real libvlc instances + Qt teardown race at interpreter exit on
    # Windows (a segfault AFTER the result is decided) — hard-exit like
    # the e2e tools so the exit code stays trustworthy.
    os._exit(code)
