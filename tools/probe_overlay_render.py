# -*- coding: utf-8 -*-
"""Probe: does CaptionOverlay ever paint a background box with NO text?

Renders cues through the REAL CaptionOverlay.paintEvent into an offscreen
pixmap using the user's saved subtitle appearance (Arial + red background),
across the scripts this provider's rips carry (Latin, Arabic, Hebrew,
CJK, Hindi, Thai, music notes, bidi-wrapped lines). A cue whose text
paints nothing while bg_enabled paints the box is exactly the reported
"background shows but no text inside" bug.

Run: .venv\\Scripts\\python.exe -X utf8 tools\\probe_overlay_render.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtGui, QtWidgets  # noqa: E402

from src.ui.caption_overlay import CaptionOverlay, visible_lines  # noqa: E402

app = QtWidgets.QApplication(sys.argv)

# the user's actual saved appearance (fallback to defaults)
import json  # noqa: E402
try:
    cfgp = os.path.join(os.environ["APPDATA"], "MichaelTVPlayer",
                        "settings.json")
    ap = json.load(open(cfgp, encoding="utf-8"))["subtitle_appearance"]
except Exception:  # noqa: BLE001
    ap = {"font": "Arial", "size": 35, "bg_enabled": True,
          "bg_color": "#ff0000", "bg_opacity": 50,
          "outline_enabled": True, "outline_color": "#000000",
          "outline_thickness": 4, "text_color": "#FFFFFF", "pos_pct": -16}
print(f"appearance: font={ap.get('font')!r} bg={ap.get('bg_enabled')}")

CASES = [
    ("latin", "Where the hell are you going?"),
    ("arabic", "إلى أين تذهب يا رجل؟"),
    ("hebrew", "לאן אתה הולך?"),
    ("cjk", "你要去哪里？"),
    ("hindi", "तुम कहाँ जा रहे हो?"),
    ("thai", "คุณกำลังจะไปไหน"),
    ("music", "♪ (music playing) ♫"),
    ("bidi-wrap", "‏english line wrapped in bidi marks‏"),
    ("mixed", "english مع عربية mixed"),
    ("symbols", "«¿¡ — … «quotes» [brackets]"),
]


def render(lines, w=640, h=360):
    """Paint one cue through the real widget into a pixmap."""
    from PyQt5 import QtCore
    wid = CaptionOverlay()
    wid.bind_config(lambda: ap)
    wid._lines = [ln for ln in lines if ln]      # bypass set_lines visibility
    wid.setGeometry(0, 0, w, h)
    pix = QtGui.QPixmap(w, h)
    pix.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pix)
    wid.render(p)                                # real paintEvent
    p.end()
    return pix


def count_text_pixels(pix, bg_rgb=(255, 0, 0)):
    """Pixels that are neither transparent nor the background box color."""
    img = pix.toImage()
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixel(x, y)
            a = (c >> 24) & 0xFF
            r, g, b = (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF
            if a == 0:
                continue
            # background box: reddish (bg at 50% over nothing = ~127,0,0)
            if r > 90 and g < 60 and b < 60:
                continue
            n += 1
    return n


fails = 0
for name, text in CASES:
    vis = visible_lines(text)
    if not vis:
        print(f"  {name:10s}: visible_lines() DROPPED the cue entirely "
              f"(no box either) — text={text[:30]!r}")
        continue
    pix = render(vis)
    n = count_text_pixels(pix)
    ok = n > 150        # any real glyph run is thousands of pixels
    if not ok:
        fails += 1
    print(f"  {name:10s}: text pixels={n:6d} {'OK' if ok else '*** EMPTY BOX ***'}")

print("empty-box cases:", fails)
sys.exit(0)
