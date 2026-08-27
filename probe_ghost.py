# -*- coding: utf-8 -*-
"""Live probe: reproduce the splitter-drag ghost trails over the video area.

Shows the real MainWindow (lowered, never activated, no stream, no audio),
injects fake channel rows, drives a synthetic splitter drag (real mouse
events into the handle, real cursor untouched), then captures the window's
DWM content with PrintWindow(PW_RENDERFULLCONTENT) and counts non-black
pixels in the video region = stale channel-list ghosts.
"""
import ctypes
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets, sip  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


# ---- win32 capture ----------------------------------------------------------

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.UINT), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi


def window_bounds(hwnd):
    rect = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS = 9 (visible bounds, no invisible borders)
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect),
                                    ctypes.sizeof(rect)) == 0:
        return rect.left, rect.top, rect.right, rect.bottom
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def capture_window(hwnd):
    """Capture the window's own composed surface -> QImage (top-down).

    PrintWindow(PW_RENDERFULLCONTENT) proved partial on this setup; use
    GetWindowDC + BitBlt from the window's redirection surface instead.
    """
    x0, y0, x1, y1 = window_bounds(hwnd)
    w, h = x1 - x0, y1 - y0
    hdc_window = user32.GetWindowDC(hwnd)
    hdc = gdi32.CreateCompatibleDC(hdc_window)
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(bmi)
    bmi.biWidth, bmi.biHeight = w, -h      # negative = top-down
    bmi.biPlanes, bmi.biBitCount = 1, 32
    bmi.biCompression = 0
    ptr = ctypes.c_void_p()
    dib = gdi32.CreateDIBSection(hdc_window, ctypes.byref(bmi), 0,
                                 ctypes.byref(ptr), None, 0)
    gdi32.SelectObject(hdc, dib)
    ok = gdi32.BitBlt(hdc, 0, 0, w, h, hdc_window, 0, 0, 0x00CC0020)  # SRCCOPY
    img = None
    if ok and ptr.value:
        img = QtGui_QImage_from(ptr.value, w, h)
    gdi32.DeleteObject(dib)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(hwnd, hdc_window)
    return img, (x0, y0, w, h)


def QtGui_QImage_from(addr, w, h):
    return QtGui.QImage(sip.voidptr(addr), w, h,
                        QtGui.QImage.Format_ARGB32).copy()


def wait(app, ms):
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec_()
    app.processEvents()


def surface_region(win, box):
    """The video surface's rect in capture coordinates."""
    x0, y0, w, h = box
    tl = win.player_view.surface.mapToGlobal(QtCore.QPoint(0, 0))
    sz = win.player_view.surface.size()
    return (tl.x() - x0, tl.y() - y0, tl.x() - x0 + sz.width(),
            tl.y() - y0 + sz.height())


def count_ghost_pixels(img, win, box):
    """Bright pixels strictly inside the video surface rect (should be ~0)."""
    gx0, gy0, gx1, gy1 = surface_region(win, box)
    gx0, gy0 = max(0, gx0 + 2), max(0, gy0 + 2)
    gx1, gy1 = min(box[2] - 2, gx1 - 2), min(box[3] - 2, gy1 - 2)
    n = 0
    for y in range(gy0, gy1):
        for gx in range(gx0, gx1):
            px = img.pixel(gx, y)
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            if r > 40 or g > 40 or b > 40:
                n += 1
    return n, gx0


def ghost_columns(img, win, box, buckets=24):
    """Coarse per-column-bucket bright-pixel counts inside the video rect."""
    gx0, gy0, gx1, gy1 = surface_region(win, box)
    gx0, gy0 = max(0, gx0 + 2), max(0, gy0 + 2)
    gx1, gy1 = min(box[2] - 2, gx1 - 2), min(box[3] - 2, gy1 - 2)
    counts = [0] * buckets
    span = max(1, gx1 - gx0)
    for y in range(gy0, gy1):
        for gx in range(gx0, gx1):
            px = img.pixel(gx, y)
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            if r > 40 or g > 40 or b > 40:
                counts[min(buckets - 1, buckets * (gx - gx0) // span)] += 1
    return counts


def brightness_grid(img, box, win, cols=14, rows=8):
    """Coarse text brightness map of the whole window (debug)."""
    x0, y0, w, h = box
    panel_right = win.tabs.mapTo(win, QtCore.QPoint(0, 0)).x() + win.tabs.width()
    print(f"    window box: {box}, tabs right edge abs-x={panel_right}")
    out = []
    for ry in range(rows):
        line = ""
        for rx in range(cols):
            xa = int(w * rx / cols)
            xb = int(w * (rx + 1) / cols)
            ya = int(h * ry / rows)
            yb = int(h * (ry + 1) / rows)
            tot = cnt = 0
            for y in range(max(0, ya), min(h, yb), max(1, (yb - ya) // 12)):
                for x in range(max(0, xa), min(w, xb), max(1, (xb - xa) // 12)):
                    px = img.pixel(x, y)
                    tot += ((px >> 16) & 0xFF) + ((px >> 8) & 0xFF) + (px & 0xFF)
                    cnt += 1
            v = tot // max(1, cnt) // 3
            line += " .:-=+*#%@"[min(9, v * 10 // 256)]
        out.append(line)
    print("\n".join("    " + ln for ln in out))


def main():
    from src.config import Config
    from src.ui.main_window import MainWindow

    app = QtWidgets.QApplication(sys.argv)
    from src.ui.theme import apply_theme
    apply_theme(app)

    # never persist probe state / never maximize over the user's screen
    MainWindow._restore_state = lambda self: None

    cfg = Config.load()
    win = MainWindow(cfg)
    win._save_state = lambda: None                      # no state writes
    win.resize(1180, 760)
    win.show()
    win.lower()                                         # bottom of z-order
    app.processEvents()
    wait(app, 600)

    # fake channel rows so the list has real painted content
    for i in range(80):
        QtWidgets.QListWidgetItem(
            f"{i + 1:3d}. UK: Investigation Discovery {i:02d}",
            win.live_tab.list)
    win.live_tab.list.repaint()
    app.processEvents()
    wait(app, 300)

    hwnd = int(win.winId())
    win.player_view._sleep(force=True)      # controls away for a clean read
    wait(app, 500)
    img0, box = capture_window(hwnd)
    brightness_grid(img0, box, win)
    base, gx0 = count_ghost_pixels(img0, win, box)
    print(f"baseline non-black px in video region: {base}")
    print("baseline col buckets:", ghost_columns(img0, win, box))

    # ---- synthetic splitter drag: oscillate the handle like a real drag ----
    handle = win.splitter.handle(1)

    def send(etype, local, button=None):
        gp = handle.mapToGlobal(local)
        btn = button if button is not None else QtCore.Qt.NoButton
        btns = QtCore.Qt.LeftButton if etype != QtCore.QEvent.MouseButtonRelease \
            else QtCore.Qt.NoButton
        ev = QtGui.QMouseEvent(etype, QtCore.QPoint(local.x(), local.y()),
                               QtCore.QPoint(gp.x(), gp.y()), btn, btns,
                               QtCore.Qt.NoModifier)
        QtWidgets.QApplication.sendEvent(handle, ev)

    center = QtCore.QPoint(handle.width() // 2, handle.height() // 2)
    send(QtCore.QEvent.MouseButtonPress, center, QtCore.Qt.LeftButton)
    app.processEvents()
    ghost_during = 0
    steps = [ -140, 60, -90, 40, -30, 90, -70, 50, -20 ]
    for dx in steps:
        send(QtCore.QEvent.MouseMove, center + QtCore.QPoint(dx, 0))
        app.processEvents()
        wait(app, 40)
    send(QtCore.QEvent.MouseButtonRelease,
         center + QtCore.QPoint(steps[-1], 0), QtCore.Qt.LeftButton)
    app.processEvents()
    img1, box = capture_window(hwnd)
    during, _ = count_ghost_pixels(img1, win, box)
    print(f"after-drag non-black px in video region: {during}")
    print("after-drag col buckets:", ghost_columns(img1, win, box))

    wait(app, 1200)
    app.processEvents()
    img2, box = capture_window(hwnd)
    after, _ = count_ghost_pixels(img2, win, box)
    print(f"settled non-black px in video region: {after}")
    print("settled col buckets:   ", ghost_columns(img2, win, box))

    img2.save(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "probe_ghost_after.png"))
    img1.save(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "probe_ghost_during.png"))
    img0.save(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "probe_ghost_before.png"))

    check("baseline video region is dark (<= 2500 px above edge bleed)",
          base <= 4757 + 2500)
    check("no persistent ghosts after the drag (<= 2500 px above baseline)",
          after <= base + 2500)
    check("mid-drag ghosts stay bounded (<= 12000 px above baseline)",
          during <= base + 12000)
    win.close()
    app.processEvents()

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
