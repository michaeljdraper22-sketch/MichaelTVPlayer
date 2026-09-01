# -*- coding: utf-8 -*-
"""Offscreen probe: the Subtitle-settings PREVIEW actually previews.

User report: "The subtitle preview thing seems to be messed up. I think
that's why I didn't think the subtitles were being styled at all" —
correct instinct. Two defects fed that read:

  1. FROZEN PREVIEW — _apply_sub_style_live only repainted when the
     caption overlay owned rendering (_cap_on). On Stremio pre-v1.5.13
     that was NEVER (VLC freetype rendered the subs), and it stays false
     whenever subs are Off / VLC-rendered — so every style tweak inside
     the dialog changed NOTHING on screen: the preview sat frozen at the
     dialog-open style. paintEvent reads the config live; nothing ever
     called update().
  2. EPISODE RESTART — _reapply_sub_style (the dialog-close rebuild for
     VLC-rendered tracks) resumed vod/series/catchup at position but NOT
     stremio: a style change restarted the handoff episode from 0:00.

No window, no focus, no audio, no network: spies + a rendered grab.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import DEFAULTS, Config          # noqa: E402
from src.ui.player_view import PlayerView        # noqa: E402

from PyQt5 import QtWidgets                     # noqa: E402

app = QtWidgets.QApplication(sys.argv)
cfg = Config(dict(DEFAULTS), None)
pv = PlayerView(cfg)
pv.resize(1280, 720)
fails = [0]


def check(name, cond, extra=""):
    print(("  ok   " if cond else "FAIL ") + name
          + ("" if cond or not extra else "  [%s]" % extra))
    if not cond:
        fails[0] += 1


print("[1] live style change repainted EVEN when VLC renders (the freeze)")
upd, lays = [], []
pv._cap_wid.update = lambda: upd.append(1)
pv._layout_overlays = lambda: lays.append(1)
pv._cap_on = False                    # subs Off / VLC-rendered — the
pv._apply_sub_style_live()            # preview's whole reason to exist
check("update() scheduled with _cap_on=False", len(upd) == 1)
check("geometry relaid out (bar toggle is placement)", len(lays) == 1)
pv._cap_on = True
pv._apply_sub_style_live()
check("no regression with _cap_on=True (still repaints)",
      len(upd) == 2 and len(lays) == 2)
pv._cap_on = False
check("closing state: no update storm (one per change)", len(upd) == 2)


def ink_rows(img):
    """Rows of the grabbed overlay that contain painted pixels."""
    rows = []
    for y in range(img.height()):
        for x in range(0, img.width(), 2):
            if img.pixel(x, y) != 0:
                rows.append(y)
                break
    return rows


print("[2] the preview RENDER reads the live config (grab, two sizes)")
wid = pv._cap_wid
wid.setParent(None)
wid.setGeometry(0, 0, 640, 360)
wid.set_bar_top(None)
wid.set_bottom_inset(24)
wid.set_preview("Subtitle preview")
wid._lines = []                       # between cues: preview paints
cfg.data["subtitle_appearance"]["size"] = 20
img20 = wid.grab().toImage()
cfg.data["subtitle_appearance"]["size"] = 60
img60 = wid.grab().toImage()
r20, r60 = ink_rows(img20), ink_rows(img60)
check("preview paints at both sizes", bool(r20) and bool(r60))
check("size 60 paints visibly taller than size 20",
      (r60[-1] - r60[0]) > (r20[-1] - r20[0]) * 1.8,
      "h20=%d h60=%d" % (r20[-1] - r20[0], r60[-1] - r60[0]))
cfg.data["subtitle_appearance"]["size"] = 40
wid.set_preview("")


class FakeVLC:
    def __init__(self, t_ms=0):
        self.t_ms = t_ms

    def get_time(self):
        return self.t_ms

    def is_mute(self):
        return False

    def stop_and_release(self):
        pass

    def __getattr__(self, name):
        def _noop(*_a, **_k):
            return None
        return _noop


print("[3] dialog-close rebuild resumes a stremio handoff at position")
pv._cap_wid.update = None              # drop the spy from [1]
del pv._cap_wid.update
del pv._layout_overlays
pv.vlc = FakeVLC(t_ms=65000)
plays = []
orig_play = pv.play_media
pv.play_media = lambda cur, start_at=0.0: plays.append(
    (cur.get("kind"), round(start_at, 1)))
pv.current = {"kind": "stremio", "title": "Silo — S01E01",
              "url": "http://x/s", "fav_key": "stremio:t:0"}
pv._reapply_sub_style()
check("stremio reopens at t-1, not from 0:00",
      plays and plays[-1] == ("stremio", 64.0), repr(plays[-1:]))
pv.current = {"kind": "vod", "title": "Movie", "url": "http://x/m.mp4",
              "fav_key": "vod:1"}
pv.vlc = FakeVLC(t_ms=65000)
pv._reapply_sub_style()
check("vod still resumes (regression)", plays[-1] == ("vod", 64.0))
pv.current = {"kind": "live", "title": "Chan", "url": "http://x/s.ts",
              "fav_key": "live:1"}
pv.vlc = FakeVLC(t_ms=65000)
pv._reapply_sub_style()
check("live stays at the edge (0)", plays[-1] == ("live", 0.0),
      repr(plays[-1]))
pv.current = {}
n = len(plays)
pv._reapply_sub_style()
check("nothing playing: no rebuild", len(plays) == n)
pv.play_media = orig_play
try:                                  # release the real rebuilt player
    pv.vlc.stop_and_release()
except Exception:
    pass

print()
if fails[0]:
    print("FAILURES: %d" % fails[0])
    sys.exit(1)
print("ALL PASS")
