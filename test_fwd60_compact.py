# -*- coding: utf-8 -*-
"""Offscreen test: Forward-60 button + narrow-window compaction policy.

Field report 2026-11-14: "when in narrower than half screen, stremio
streams don't show the rewind 10/60 or fast forward buttons. I would
rather hide the buttons for audio track, jump to end, download and
scaling." Plus: "a forward 60 seconds for all videos".

The compaction ladder (_apply_compact) therefore NEVER hides the seek
buttons anymore: level 4 drops audio/scale, level 5 adds live/dl. And a
btn_fwd60 joins the transport row (all seekable kinds: stremio, VOD,
catch-up, local files, live chase).

Run:  .venv\\Scripts\\python.exe test_fwd60_compact.py   (sets offscreen itself)
"""
import copy
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config, DEFAULTS  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def main():
    app = QtWidgets.QApplication(sys.argv)
    # private config on a temp path — never touch the user's settings.json
    cfg = Config(copy.deepcopy(DEFAULTS),
                 Path(tempfile.gettempdir()) / "mtp_fwd60_settings.json")
    view = PlayerView(cfg)
    view.resize(1280, 720)
    view.show()
    app.processEvents()

    print("[1] fwd60 wired: click -> _seek_ms(+60000)")
    view.current = {"kind": "stremio", "url": "http://127.0.0.1/x",
                    "title": "S01E01"}
    view._update_control_state()
    app.processEvents()
    seeks = []
    orig = view._seek_ms
    view._seek_ms = lambda ms: seeks.append(ms)
    try:
        view.btn_fwd60.click()
        view.btn_fwd10.click()
        view.btn_back60.click()
    finally:
        view._seek_ms = orig
    check("fwd60 click seeks +60000", seeks == [60000, 10000, -60000])

    print("[2] full width: everything visible for a stremio stream")
    view._fit_ctl(2000)
    check("compaction relaxed to level 0", view._compact_level == 0)
    for name in ("btn_back60", "btn_back10", "btn_fwd10", "btn_fwd60",
                 "btn_play", "btn_begin", "btn_live", "btn_dl",
                 "btn_cc", "btn_audio", "btn_scale", "btn_speed",
                 "btn_stream", "btn_mute"):
        check(f"{name} visible", not getattr(view, name).isHidden())

    print("[3] narrower than half screen: seeks STAY, pickers go")
    view._fit_ctl(300)   # quarter-screen-ish: ladder must climb to 5
    check("ladder climbed past level 4", view._compact_level >= 4)
    for name in ("btn_back60", "btn_back10", "btn_fwd10", "btn_fwd60",
                 "btn_play"):
        check(f"{name} STILL visible at level {view._compact_level}",
              not getattr(view, name).isHidden())
    for name in ("btn_audio", "btn_scale", "btn_live", "btn_dl"):
        check(f"{name} hidden at level {view._compact_level}",
              getattr(view, name).isHidden())

    print("[4] widening back restores the pickers")
    view._fit_ctl(2000)
    check("level back to 0", view._compact_level == 0)
    for name in ("btn_audio", "btn_scale", "btn_live", "btn_dl"):
        check(f"{name} restored", not getattr(view, name).isHidden())

    print("[5] live TV: REC keeps its slot at max compaction")
    view.current = {"kind": "live", "url": "http://127.0.0.1/y",
                    "title": "CHAN"}
    view._update_control_state()
    app.processEvents()
    view._fit_ctl(300)
    check("btn_rec visible at max compaction (live)",
          not view.btn_rec.isHidden())
    check("btn_dl stays kind-hidden on live",
          view.btn_dl.isHidden())
    check("seeks still visible on live",
          not view.btn_fwd60.isHidden())

    print("[6] VOD (movie): fwd60 enabled alongside the others")
    view.current = {"kind": "vod", "url": "http://127.0.0.1/z",
                    "title": "MOV"}
    view._update_control_state()
    app.processEvents()
    for name in ("btn_back60", "btn_back10", "btn_fwd10", "btn_fwd60"):
        check(f"{name} enabled for vod", getattr(view, name).isEnabled())

    view.close()
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
