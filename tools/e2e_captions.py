# -*- coding: utf-8 -*-
"""E2E: real channel -> always-on chase -> CC track selected -> app-rendered
caption overlay. Verifies: chase engages automatically; VLC lists the CC
tracks on the re-muxed buffer; selecting one starts CCSource (one provider
connection total); cues arrive and the Qt overlay paints styled lines; VLC's
own spu stays OFF under the overlay.

Run:  .venv\\Scripts\\python.exe -X utf8 tools/e2e_captions.py
(a small real window flashes while the test runs)
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402
from src.live_cc import find_ccextractor  # noqa: E402

CH = {
    "kind": "live",
    "title": "US: FOX NEWS HD",
    "url": "http://cf.534842.xyz/live/726352471c/d809266e91/324923.ts",
    "stream_id": 324923,
}

app = QtWidgets.QApplication(sys.argv)
cfg = Config.load()
cfg.data["chase_delay"] = 5
view = PlayerView(cfg)
view._filter_engine.enabled = False   # isolate captions from the filter
view.resize(960, 540)
view.show()

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name, flush=True)


def pump(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.03)


def wait_until(pred, timeout, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    print(f"  (timeout waiting for {what} after {timeout}s)", flush=True)
    return False


assert find_ccextractor(), "CCExtractor not found"

try:
    print("[1] play live channel (chase auto-engages)", flush=True)
    view.play_media(dict(CH))
    ok = wait_until(lambda: view._mode == "chase", 30, "chase mode")
    check("always-on chase engages on play", ok)
    ok = wait_until(lambda: view.vlc.is_playing(), 30, "chase playing")
    check("chase playback running", ok)

    print("[2] CC tracks visible on the chase buffer", flush=True)
    ok = wait_until(
        lambda: any("caption" in n.lower() or n.lower().startswith("cc")
                    for _, n in view.vlc.spu_tracks()),
        20, "CC tracks")
    tracks = view.vlc.spu_tracks()
    check(f"CC track listed ({[n for _, n in tracks][:3]})", ok)
    if not ok:
        raise SystemExit("no CC tracks on this channel — pick another CH")

    print("[3] select the CC track -> overlay owns captions", flush=True)
    tid, name = next((t, n) for t, n in tracks
                     if "caption" in n.lower() or n.lower().startswith("cc"))
    view._select_spu(tid, name)
    check("overlay on", view._cap_on)
    check("CCSource started",
          view._cc_source is not None and view._cc_source._alive)
    check("VLC spu forced OFF under the overlay", view.vlc.active_spu() == -1)

    print("[4] captions flow into the overlay", flush=True)
    # NOTE: channels run long un-captioned stretches (commercial breaks,
    # night programming) — the first cue can take minutes of content.
    ok = wait_until(lambda: len(view._cap_cues.cues) > 0, 180, "first cue")
    check(f"cues arriving from the buffer "
          f"({len(view._cap_cues.cues)} so far)", ok)
    if ok:
        # Jump the playhead INTO the newest cue: verifies the render path
        # deterministically instead of racing real-world caption gaps.
        c = view._cap_cues.cues[-1]
        mid = (c[0] + min(c[1], c[0] + 2.0)) / 2.0
        view._chase_seek(mid)
        ok2 = wait_until(lambda: bool(view._cap_wid._lines), 10,
                         "overlay lines in cue window")
        check(f"overlay paints inside the cue window "
              f"({view._cap_wid._lines[:1]})", ok2)
    else:
        ok2 = False
        check("overlay paints inside the cue window", False)
    check("no fallback latched", not view._cap_fail)

    print("[5] jump to live keeps captions alive (5 s cushion)", flush=True)
    view._jump_live()
    pump(5)
    check("still playing after jump", view.vlc.is_playing())
    check("no fallback after jump", not view._cap_fail)
    if ok2 and view._cap_cues.cues:
        c = view._cap_cues.cues[-1]
        mid = (c[0] + min(c[1], c[0] + 2.0)) / 2.0
        view._chase_seek(mid)
        ok3 = wait_until(lambda: bool(view._cap_wid._lines), 10,
                         "overlay lines after jump")
        check("overlay still paints after jump to live", ok3)
    else:
        check("overlay still paints after jump to live", True)  # n/a

    print("[6] Off returns rendering to VLC", flush=True)
    view._select_spu(-1, "")
    check("overlay off", not view._cap_on)
    check("CCSource stopped when nothing needs it",
          view._cc_source is None)
    check("still playing after captions off", view.vlc.is_playing())
finally:
    view.stop()
    pump(1)

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed", flush=True)
for f in FAIL:
    print("  FAILED:", f, flush=True)
os._exit(1 if FAIL else 0)
