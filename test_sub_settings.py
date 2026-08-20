# -*- coding: utf-8 -*-
"""Offscreen tests for the subtitle appearance settings (delay/style).

Run:  .venv\\Scripts\\python.exe test_sub_settings.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import SUBTITLE_DEFAULTS, Config  # noqa: E402
from src.player import VLCPlayer, subtitle_instance_args  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402
from src.ui.subtitle_dialog import SubtitleDialog  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def temp_config() -> Config:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    return Config({}, Path(path))


def main():
    app = QtWidgets.QApplication(sys.argv)

    print("[1] config defaults = VLC's current look + a concrete size")
    d = SUBTITLE_DEFAULTS
    check("no delay, numeric default size, default font",
          d["delay_ms"] == 0 and d["size"] == 40 and d["font"] == "")
    check("no background, white text, black outline on",
          not d["bg_enabled"] and d["text_color"] == "#FFFFFF"
          and d["outline_enabled"] and d["outline_thickness"] == 4)
    cfg = temp_config()
    check("defaults merge from empty config",
          cfg.subtitle_appearance == SUBTITLE_DEFAULTS)
    cfg.subtitle_appearance = {"font": "Arial", "delay_ms": -250}
    check("setter keeps unknown keys out and merges",
          cfg.subtitle_appearance["font"] == "Arial"
          and cfg.subtitle_appearance["delay_ms"] == -250
          and cfg.subtitle_appearance["size"] == 40)

    print("[2] argument builder — the default size is emitted explicitly")
    check("all-default config -> just the size arg",
          subtitle_instance_args(SUBTITLE_DEFAULTS)
          == ["--freetype-fontsize=40"])
    check("size 0 (Auto) -> no VLC args", subtitle_instance_args({}) == [])
    args = subtitle_instance_args({
        "font": "Segoe UI", "size": 24, "pos_pct": 50,
        "text_color": "#FF0000"})
    check("font/size/pos/color mapped",
          "--freetype-font=Segoe UI" in args
          and "--freetype-fontsize=24" in args
          and "--sub-margin=270" in args
          and "--freetype-color=16711680" in args)
    args = subtitle_instance_args({"pos_pct": -100})
    check("negative position allowed", "--sub-margin=-540" in args)
    args = subtitle_instance_args({"bg_enabled": True, "bg_color": "#0000FF",
                                   "bg_opacity": 50})
    check("background color+opacity mapped",
          "--freetype-background-color=255" in args
          and "--freetype-background-opacity=128" in args)
    args = subtitle_instance_args({"outline_enabled": False})
    check("outline off -> opacity 0",
          args == ["--freetype-outline-opacity=0"])
    args = subtitle_instance_args({"outline_enabled": True,
                                   "outline_color": "#FFFFFF",
                                   "outline_thickness": 8})
    check("outline color mapped, non-default thickness mapped",
          "--freetype-outline-color=16777215" in args
          and "--freetype-outline-thickness=8" in args)
    args = subtitle_instance_args({"outline_enabled": True,
                                   "outline_thickness": 4})
    check("default thickness emits nothing extra", args == [])
    args = subtitle_instance_args({"text_color": "notahex"})
    check("bad hex falls back silently (no arg)",
          args == [])

    print("[3] VLCPlayer delay API (real libvlc, no media)")
    p = VLCPlayer(timeshift=False,
                  sub_args=subtitle_instance_args({"size": 24}),
                  spu_delay_ms=-250)
    check("constructed with style args + initial delay",
          p.get_spu_delay() == -250)
    p.set_spu_delay(500)
    check("set/get round trip", p.get_spu_delay() == 500)
    calls = []
    orig = p._apply_spu_delay
    p._apply_spu_delay = lambda pl: calls.append(pl)
    fresh = p.instance.media_player_new()
    p._setup_player(fresh)      # what a hung-stop swap does
    p._apply_spu_delay = orig
    check("a swapped-in fresh player gets the delay re-applied",
          fresh in calls)
    p.stop_and_release()

    print("[4] menu gains the settings entry")
    view = PlayerView(temp_config())

    class FakeSpu:
        def __init__(self):
            self.delay_calls = []

        def spu_tracks(self):
            return [(1, "English")]

        def active_spu(self):
            return -1

        def set_spu(self, tid):
            pass

        def set_spu_delay(self, ms):
            self.delay_calls.append(ms)

    fake = FakeSpu()
    view.vlc.spu_tracks = fake.spu_tracks
    view.vlc.active_spu = fake.active_spu
    view.vlc.set_spu = fake.set_spu
    view.vlc.set_spu_delay = fake.set_spu_delay
    view._refresh_spu_button()
    captured = []
    view._popup_above = lambda menu, btn: captured.append(menu)
    view._subs_menu()
    labels = [a.text() for a in captured[-1].actions()]
    check("menu = Off + track + settings entry",
          "Off" in labels and "English" in labels
          and "Subtitle settings\u2026" in labels)
    view._apply_sub_delay(250)
    check("view routes the live delay to the player",
          fake.delay_calls == [250])

    print("[5] dialog: delay nudge is instant and persists")
    cfg2 = temp_config()
    applied = []
    dlg = SubtitleDialog(cfg2, applied.append)
    dlg._nudge_delay(+1)
    dlg._nudge_delay(+1)
    check("two + clicks -> +0.50 s applied twice",
          applied == [250, 500] and dlg._delay_ms == 500)
    check("label shows +0.50 s", dlg.l_delay.text() == "+0.50 s")
    dlg._nudge_delay(0, reset=True)
    check("reset returns to 0 and applies",
          applied[-1] == 0 and dlg._delay_ms == 0
          and dlg.l_delay.text() == "0.00 s")
    check("delay persisted to config",
          cfg2.subtitle_appearance["delay_ms"] == 0
          and applied[-1] == 0)

    print("[6] dialog: OK writes style, Cancel leaves config alone")
    dlg = SubtitleDialog(cfg2, lambda ms: None)
    if dlg.cb_font.count() > 1:       # offscreen Qt may expose no families
        dlg.cb_font.setCurrentIndex(1)   # first real family, not "Default"
        expected_font = dlg.cb_font.itemData(1)
    else:
        expected_font = ""
    dlg.sp_size.setValue(24)
    dlg.sl_pos.setValue(25)
    dlg.ck_bg.setChecked(True)
    dlg.sl_bg.setValue(80)
    dlg.ck_out.setChecked(False)
    dlg.accept()
    ap = cfg2.subtitle_appearance
    check(f"font saved ({expected_font!r})",
          ap["font"] == expected_font)
    check("size/position saved", ap["size"] == 24 and ap["pos_pct"] == 25)
    check("background saved on", ap["bg_enabled"] and ap["bg_opacity"] == 80)
    check("outline saved off", ap["outline_enabled"] is False)
    before = dict(ap)
    dlg2 = SubtitleDialog(cfg2, lambda ms: None)
    dlg2.sp_size.setValue(48)
    dlg2.reject()
    check("cancel discards style edits",
          cfg2.subtitle_appearance["size"] == before["size"])

    print("[7] reset to defaults restores every control")
    dlg3 = SubtitleDialog(cfg2, lambda ms: None)
    dlg3._reset_all()
    check("config back to defaults",
          cfg2.subtitle_appearance == SUBTITLE_DEFAULTS)

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
