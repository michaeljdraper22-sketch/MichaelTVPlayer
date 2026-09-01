# -*- coding: utf-8 -*-
"""Headless probe: play_next / play_prev glyph shrink (v1.5.8).

Renders the transport-row glyphs BEFORE (full-canvas mockup geometry) and
AFTER (75% geometry now in icons.py) into probe_icon_size.png so the
visual weight can be compared against the play / begin / live neighbors.
Offscreen platform — no window, no focus, no audio.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from src.ui import icons as ic  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def old_play_next(p, c):
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(c)
    p.drawPolygon(QtGui.QPolygonF(
        [ic._F(1.8, 5.4), ic._F(1.8, 18.6), ic._F(17.8, 12.0)]))
    p.drawRoundedRect(QtCore.QRectF(19.4, 5.4, 2.6, 13.2), 1.3, 1.3)


def old_play_prev(p, c):
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(c)
    p.drawPolygon(QtGui.QPolygonF(
        [ic._F(22.2, 5.4), ic._F(22.2, 18.6), ic._F(6.2, 12.0)]))
    p.drawRoundedRect(QtCore.QRectF(2.0, 5.4, 2.6, 13.2), 1.3, 1.3)


def glyph_bounding_width(draw_fn):
    """Ink bounding-box width of a glyph on the 24px logical grid."""
    pm = QtGui.QPixmap(24, 24)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing, False)
    draw_fn(p, ic.WHITE)
    p.end()
    img = pm.toImage()
    minX, maxX, minY, maxY = 24, -1, 24, -1
    for y in range(24):
        for x in range(24):
            if img.pixelColor(x, y).alpha() > 0:
                minX = min(minX, x); maxX = max(maxX, x)
                minY = min(minY, y); maxY = max(maxY, y)
    return maxX - minX + 1, maxY - minY + 1


def ink_area(draw_fn):
    """Count painted pixels — proxy for visual weight."""
    pm = QtGui.QPixmap(24, 24)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing, False)
    draw_fn(p, ic.WHITE)
    p.end()
    img = pm.toImage()
    n = 0
    for y in range(24):
        for x in range(24):
            if img.pixelColor(x, y).alpha() > 0:
                n += 1
    return n


def new_next(p, c):
    ic.play_next().paint(p, QtCore.QRect(0, 0, 24, 24), QtCore.Qt.AlignCenter,
                         QtGui.QIcon.Normal, QtGui.QIcon.Off)


def new_prev(p, c):
    ic.play_prev().paint(p, QtCore.QRect(0, 0, 24, 24), QtCore.Qt.AlignCenter,
                         QtGui.QIcon.Normal, QtGui.QIcon.Off)


def main():
    app = QtWidgets.QApplication([])

    # --- geometry / weight checks -------------------------------------
    ow, oh = glyph_bounding_width(old_play_next)
    nw, nh = glyph_bounding_width(new_next)
    pw, ph = glyph_bounding_width(new_prev)
    play_w, play_h = glyph_bounding_width(
        lambda p, c: ic.play().paint(p, QtCore.QRect(0, 0, 24, 24),
                                     QtCore.Qt.AlignCenter,
                                     QtGui.QIcon.Normal, QtGui.QIcon.Off))
    print(f"  old next bbox {ow}x{oh}, new next {nw}x{nh}, "
          f"new prev {pw}x{ph}, play {play_w}x{play_h}")
    check("new next is narrower than old", nw < ow)
    check("new prev is narrower than old", pw < ow)
    check("twins stay mirrored (same bbox)", (nw, nh) == (pw, ph))
    oa, na, pa, la = (ink_area(old_play_next), ink_area(new_next),
                      ink_area(new_prev),
                      ink_area(lambda p, c: ic.live().paint(
                          p, QtCore.QRect(0, 0, 24, 24), QtCore.Qt.AlignCenter,
                          QtGui.QIcon.Normal, QtGui.QIcon.Off)))
    print(f"  ink area: old next {oa}, new next {na}, new prev {pa}, "
          f"live {la}")
    check("new next weight within 40% of live", abs(na - la) <= 0.4 * la)
    check("new prev weight within 40% of live", abs(pa - la) <= 0.4 * la)
    check("shrink actually happened (>=20% less ink than the old glyph)",
          na <= 0.8 * oa)

    # --- before/after strip -------------------------------------------
    S, GAP, PAD = 24, 12, 10
    row_old = [("begin", lambda p, c: ic.begin().paint(
                    p, QtCore.QRect(0, 0, 24, 24), QtCore.Qt.AlignCenter,
                    QtGui.QIcon.Normal, QtGui.QIcon.Off)),
               ("live", lambda p, c: ic.live().paint(
                    p, QtCore.QRect(0, 0, 24, 24), QtCore.Qt.AlignCenter,
                    QtGui.QIcon.Normal, QtGui.QIcon.Off)),
               ("play", lambda p, c: ic.play().paint(
                    p, QtCore.QRect(0, 0, 24, 24), QtCore.Qt.AlignCenter,
                    QtGui.QIcon.Normal, QtGui.QIcon.Off)),
               ("prev OLD", old_play_prev),
               ("next OLD", old_play_next)]
    row_new = [("begin", row_old[0][1]), ("live", row_old[1][1]),
               ("play", row_old[2][1]),
               ("prev NEW", new_prev), ("next NEW", new_next)]

    W = PAD * 2 + len(row_old) * (S + GAP) - GAP
    H = PAD * 2 + 2 * S + 34
    pm = QtGui.QPixmap(W, H)
    pm.fill(QtGui.QColor(30, 30, 30))
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    f = QtGui.QFont("Segoe UI", 7)
    for r, (row, ytop, tag) in enumerate(((row_old, PAD, "BEFORE"),
                                          (row_new, PAD + S + 34, "AFTER"))):
        p.setPen(QtGui.QColor(160, 160, 160))
        p.setFont(f)
        p.drawText(PAD, ytop - 2, tag)
        for i, (name, fn) in enumerate(row):
            x = PAD + i * (S + GAP)
            p.save()
            p.translate(x, ytop)
            fn(p, ic.WHITE)
            p.restore()
            p.setPen(QtGui.QColor(120, 120, 120))
            p.drawText(x, ytop + S + 10, name)
    p.end()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "probe_icon_size.png")
    pm.save(out)
    print(f"  strip -> {out}")

    print(f"RESULT: {len(PASS)} pass, {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
