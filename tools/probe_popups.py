# -*- coding: utf-8 -*-
"""Live-window probe for the control-bar popup cards (CC / audio / speed).

The offline suites pass but the USER reports the menu buttons "do nothing"
in the real app. This probe reproduces the real environment as closely as
possible while staying polite: REAL native windows + real live stream, but
the window is shown WITHOUT activating (no focus steal), audio is muted,
and it parks itself at the bottom-right corner of the screen.

Checks:
  1. clicking btn_cc opens the card (state + pixels from overlay.grab())
  2. clicking btn_audio swaps content (same card, AUDIO header)
  3. clicking btn_cc again toggles closed
  4. a press OUTSIDE the card (on the video surface) closes it
  5. overlay window is not being hidden mid-click (Show/Hide spy)
  6. any Python exception prints with full traceback (excepthook)

Run:  .venv\\Scripts\\python.exe tools\\probe_popups.py
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MTP_NO_RAISE", "1")

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402
from src.xtream import XtreamClient  # noqa: E402

PASS, FAIL = [], []
EVLOG = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else " FAIL  ") + name + extra)


class WinSpy(QtCore.QObject):
    """Log Show/Hide/Activate events on the overlay + view windows."""

    def eventFilter(self, obj, ev):
        t = ev.type()
        interesting = {QtCore.QEvent.Show, QtCore.QEvent.Hide,
                       QtCore.QEvent.ShowToParent, QtCore.QEvent.HideToParent,
                       QtCore.QEvent.WindowActivate, QtCore.QEvent.WindowDeactivate,
                       QtCore.QEvent.FocusIn, QtCore.QEvent.FocusOut,
                       QtCore.QEvent.Close}
        if t in interesting:
            name = {QtCore.QEvent.Show: "Show", QtCore.QEvent.Hide: "Hide",
                    QtCore.QEvent.ShowToParent: "ShowToParent",
                    QtCore.QEvent.HideToParent: "HideToParent",
                    QtCore.QEvent.WindowActivate: "WindowActivate",
                    QtCore.QEvent.WindowDeactivate: "WindowDeactivate",
                    QtCore.QEvent.FocusIn: "FocusIn",
                    QtCore.QEvent.FocusOut: "FocusOut",
                    QtCore.QEvent.Close: "Close"}.get(t, str(t))
            EVLOG.append((name, obj.objectName() or obj.__class__.__name__))
        return False


def main():
    def hook(t, v, tb):
        print("!!! PYTHON EXCEPTION in slot:")
        traceback.print_exception(t, v, tb)
        sys.stdout.flush()
    sys.excepthook = hook

    app = QtWidgets.QApplication(sys.argv)
    spy = WinSpy()
    app.installEventFilter(spy)

    cfg = Config.load()
    view = PlayerView(cfg)
    view.setWindowTitle("MTP popup probe")
    view.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    view.setWindowFlags(view.windowFlags()
                        | QtCore.Qt.WindowDoesNotAcceptFocus)
    screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
    view.resize(1100, 640)
    view.move(screen.right() - 1108, screen.bottom() - 648)
    view.show()
    view.vlc.set_volume(0)
    view.vlc.set_mute(True)

    # let startup attach + timers settle
    for _ in range(40):
        app.processEvents()
        QtCore.QThread.msleep(50)

    xc = XtreamClient(cfg.normalized_server(), cfg.username, cfg.password)
    streams = xc.live_streams()
    chan = next((c for c in streams if "NFL" in (c.get("name") or "").upper()),
                streams[0] if streams else None)
    if chan is None:
        print("no live channel available")
        os._exit(2)
    pl = {"kind": "live", "title": chan.get("name", "probe"),
          "url": xc.live_url(chan["stream_id"])}
    print("channel:", pl["title"])
    view.play_media(pl)

    # wait for chase playback (video actually rolling)
    deadline = QtCore.QElapsedTimer()
    deadline.start()
    playing = False
    while deadline.elapsed() < 30000:
        app.processEvents()
        QtCore.QThread.msleep(100)
        try:
            if view.vlc.is_playing() and view._mode == "chase":
                playing = True
                break
        except Exception:
            pass
    check("live playback in chase mode", playing)
    if not playing:
        finish(app, view, code=2)
    QtCore.QThread.msleep(1500)
    for _ in range(10):
        app.processEvents()
        QtCore.QThread.msleep(50)

    def wake():
        view._wake()
        for _ in range(6):
            app.processEvents()
            QtCore.QThread.msleep(30)

    def shot(tag):
        pix = view.overlay.grab()
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), f"probe_popup_{tag}.png")
        pix.save(path)
        return pix, path

    def card_pixels(pix):
        """Count card-ish (opaque dark + light-row) pixels ABOVE the ctl bar:
        crude but enough to prove the card painted."""
        img = pix.toImage()
        w, h = img.width(), img.height()
        ctl_top = None
        # find the ctl bar: bottom rows that contain many opaque pixels
        opaque_rows = []
        for y in range(h - 1, max(0, h - 140), -1):
            n = sum(1 for x in range(0, w, 8)
                    if img.pixelColor(x, y).alpha() > 100)
            opaque_rows.append((y, n))
        if not opaque_rows:
            return 0
        ctl_top = min(y for y, n in opaque_rows if n > w // 64)
        count = 0
        for y in range(0, ctl_top, 2):
            for x in range(0, w, 4):
                c = img.pixelColor(x, y)
                if c.alpha() > 200:
                    count += 1
        return count

    def click(widget):
        QTest.mouseClick(widget, QtCore.Qt.LeftButton,
                         QtCore.Qt.NoModifier, QtCore.QPoint(3, 3))
        for _ in range(6):
            app.processEvents()
            QtCore.QThread.msleep(30)

    # -- 1. CC button opens the card --------------------------------------
    EVLOG.clear()
    wake()
    click(view.btn_cc)
    vis = view._ctl_panel.isVisible()
    pix, path = shot("cc_open")
    n = card_pixels(pix)
    check("btn_cc click -> card visible", vis)
    check("card painted real pixels above the bar", n > 300, f" ({n}px)")
    print("     screenshot:", path)
    print("     events:", EVLOG[-8:])

    # -- 2. outside press closes ------------------------------------------
    EVLOG.clear()
    gp = view.surface.mapToGlobal(QtCore.QPoint(30, 30))
    ev = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, gp, QtCore.Qt.LeftButton,
                           QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(view.surface, ev)
    for _ in range(6):
        app.processEvents()
        QtCore.QThread.msleep(30)
    check("press on the video (outside) closes the card",
          not view._ctl_panel.isVisible())

    # -- 3. swap: audio button re-opens with AUDIO content -----------------
    wake()
    click(view.btn_audio)
    vis2 = view._ctl_panel.isVisible()
    rows2 = [dict(r) for r in view._ctl_panel.rows()]
    pix2, _ = shot("audio_open")
    n2 = card_pixels(pix2)
    check("btn_audio click -> card visible", vis2)
    check("audio card painted", n2 > 300, f" ({n2}px)")
    head = view._ctl_panel._head.text()
    check("audio card header is AUDIO", head == "AUDIO", f" ({head!r})")

    # -- 4. toggle closed by same button -----------------------------------
    click(view.btn_audio)
    check("btn_audio again toggles closed", not view._ctl_panel.isVisible())

    # -- 5. speed button (chase mode -> enabled) ----------------------------
    wake()
    en = view.btn_speed.isEnabled()
    print("     btn_speed enabled:", en)
    click(view.btn_speed)
    check("btn_speed click -> card visible",
          view._ctl_panel.isVisible())
    pix3, _ = shot("speed_open")
    check("speed card painted", card_pixels(pix3) > 300)
    click(view.btn_speed)

    # -- 6. click-outside via the MAIN window (browser area would be) ------
    wake()
    click(view.btn_cc)
    gp2 = view.mapToGlobal(QtCore.QPoint(20, 20))
    ev2 = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, gp2,
                            QtCore.Qt.LeftButton, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(view, ev2)
    for _ in range(6):
        app.processEvents()
        QtCore.QThread.msleep(30)
    check("press on the view (outside) closes the card",
          not view._ctl_panel.isVisible())

    finish(app, view)


def finish(app, view, code=0):
    try:
        view.stop()
    except Exception:
        traceback.print_exc()
    for _ in range(10):
        app.processEvents()
        QtCore.QThread.msleep(30)
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code if not FAIL else 1)


if __name__ == "__main__":
    main()
