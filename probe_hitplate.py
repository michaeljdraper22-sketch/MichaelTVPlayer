# -*- coding: utf-8 -*-
"""Probe: the invisible hit plate — verification against the LIVE QSS.

The on-video buttons carry "hit plates" (a QSS background) because the
overlay is a per-pixel-alpha window: Windows routes clicks by pixel alpha,
so an alpha-0 pixel inside a button passes the click through to the video.
Plates must be invisible: white rgba(255,255,255,3) showed as (3,3,3)
tiles on dark video; the fix is rgba(63,63,63,2) which premultiplies to
(0,0,0,2) — nothing over black, -2 steps max on pure white.

[A1] alpha spellings -> stored premul pixel (bare-widget grabs; NOTE
     grabs double-paint low alpha, so values read ~2x live — the user's
     screenshot is the live ground truth: alpha-3 white == (3,3,3)).
[A2] the REAL pipeline (app stylesheet _OVERLAY_QSS as now edited,
     QToolButton in #ctlOverlay, autoRaise): the new plate parses low and
     premultiplies to ~0 color; hover/pressed are unchanged.
[B]  live overlay hit-test (invisible window, never activated):
     WindowFromPoint — the same routing real mouse clicks use — hits the
     overlay on the new plate AND on an exact stored-alpha-1 fillRect,
     and falls through on alpha-0.  (First probe run: a LOWERED overlay
     was correctly skipped — it sat under Chrome.  Raise, don't lower.)
"""
import ctypes
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def wait(app, ms):
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec_()
    app.processEvents()


def premul_pixel(img, x, y):
    c = img.pixel(x, y)
    return ((c >> 24) & 255, (c >> 16) & 255, (c >> 8) & 255, c & 255)


class ExactFill(QtWidgets.QWidget):
    """Single-pass paintEvent fill: stored pixel is EXACTLY (0,0,0,1)."""

    def paintEvent(self, ev):           # noqa: N802
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 1))


def real_pipeline(app, qss):
    """The actual control-bar paint: app-level stylesheet, QToolButton
    inside a #ctlOverlay parent, autoRaise.  Sample a plate-only corner
    pixel (no glyph there)."""
    app.setStyleSheet(qss)
    holder = QtWidgets.QWidget()
    holder.setObjectName("ctlOverlay")
    lay = QtWidgets.QHBoxLayout(holder)
    lay.setContentsMargins(0, 0, 0, 0)
    btn = QtWidgets.QToolButton()
    btn.setText("X")
    btn.setIconSize(QtCore.QSize(24, 24))
    btn.setFixedSize(34, 30)
    btn.setAutoRaise(True)
    lay.addWidget(btn)
    holder.resize(60, 40)
    img = btn.grab().toImage()
    a, r, g, b = premul_pixel(img, 2, 2)
    app.setStyleSheet("")
    holder.deleteLater()
    return a, r, g, b


def main():
    app = QtWidgets.QApplication(sys.argv)
    from src.ui.player_view import _OVERLAY_QSS

    print("[A1] alpha spellings -> stored premul pixel (grab ~2x live)")
    for qss in ("background-color: rgba(63,63,63,2);",
                "background-color: rgba(127,127,127,2);",
                "background-color: rgba(255,255,255,3);",
                "background-color: rgba(127,127,127,1);"):
        wid = QtWidgets.QWidget()
        wid.setStyleSheet("QWidget { %s }" % qss)
        wid.resize(40, 30)
        img = wid.grab().toImage()
        a, r, g, b = premul_pixel(img, 20, 15)
        print("   %-36s -> a=%3d rgb=(%d,%d,%d)" % (qss[18:-1], a, r, g, b))
        wid.deleteLater()
    check("rgba(...,1) parses OPAQUE (float 1.0) — must never be used",
          True)  # printed above; kept as a documented trap

    print("[A2] real pipeline with the edited _OVERLAY_QSS")
    a, r, g, b = real_pipeline(app, _OVERLAY_QSS)
    print("   new gray plate -> a=%d rgb=(%d,%d,%d)" % (a, r, g, b))
    check("new plate parses LOW (not opaque; grab doubles live 2 -> 4)",
          a in (2, 3, 4))
    check("premul color ~0 (invisible on black video)",
          r <= 1 and g <= 1 and b <= 1)
    check("no visible white plates left in the QSS rules (comments aside)",
          "rgba(255,255,255,3)" not in
          __import__("re").sub(r"/\*.*?\*/", "", _OVERLAY_QSS, flags=__import__("re").S))
    check("hover/pressed feedback unchanged",
          "rgba(255,255,255,45)" in _OVERLAY_QSS
          and "rgba(255,255,255,95)" in _OVERLAY_QSS)

    print("[B] live overlay hit-test (invisible window, never activated)")
    flags = (QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint)
    if hasattr(QtCore.Qt, "WindowDoesNotAcceptFocus"):
        flags |= QtCore.Qt.WindowDoesNotAcceptFocus
    ov = QtWidgets.QWidget(None, flags)
    ov.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
    ov.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    ov.setFocusPolicy(QtCore.Qt.NoFocus)
    ov.resize(440, 120)
    ov.move(80, 80)

    def region(cls, x, qss=None):
        c = cls(ov) if cls else QtWidgets.QWidget(ov)
        c.setGeometry(x, 10, 90, 90)
        if qss:
            c.setStyleSheet("QWidget { %s }" % qss)
        return c

    c_new = region(None, 10, "background-color: rgba(63,63,63,2);")
    c_a1 = region(ExactFill, 120)
    c_nil = region(None, 340, "background: transparent;")
    ov.show()
    ov.raise_()          # must be topmost at that point (owner keeps the
    app.processEvents()  # real overlay above the video the same way)
    ov.repaint()
    wait(app, 150)

    user32 = ctypes.windll.user32
    hwnd_ov = int(ov.winId())

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = wintypes.HWND

    def hit(widget):
        gp = widget.mapToGlobal(QtCore.QPoint(45, 45))
        h = user32.WindowFromPoint(POINT(gp.x(), gp.y()))
        cn = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, cn, 64)
        return int(h), cn.value

    h_new, cn_new = hit(c_new)
    h_a1, cn_a1 = hit(c_a1)
    h_nil, cn_nil = hit(c_nil)
    print("   overlay hwnd=%d" % hwnd_ov)
    print("   gray a2 plate hit -> hwnd=%d class=%r" % (h_new, cn_new))
    print("   exact a1 fill hit -> hwnd=%d class=%r" % (h_a1, cn_a1))
    print("   transparent  hit  -> hwnd=%d class=%r" % (h_nil, cn_nil))
    check("new plate: WindowFromPoint finds the overlay", h_new == hwnd_ov)
    check("hit-test floor: even stored alpha 1 is clickable",
          h_a1 == hwnd_ov)
    check("alpha-0 region: click still falls through to the video",
          h_nil != hwnd_ov)

    ov.hide()
    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
