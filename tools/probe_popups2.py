# -*- coding: utf-8 -*-
"""Native-message probe: drive the popup buttons through REAL Windows
messages (PostMessage to the overlay HWND) — the same delivery path as the
user's physical mouse, unlike QTest/sendEvent which inject straight into
Qt's event queue.

Also spies on what the app-level filter actually receives.

Run:  .venv\\Scripts\\python.exe tools\\probe_popups2.py
"""
import ctypes
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402
from src.xtream import XtreamClient  # noqa: E402

user32 = ctypes.windll.user32
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001

PASS, FAIL = [], []
PRESSLOG = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else " FAIL  ") + name + extra)


class HideTracer(QtCore.QObject):
    """Print a mini-stack whenever the overlay / ctl bar / menu buttons get
    hidden — catches the hider red-handed."""

    def __init__(self):
        super().__init__()
        self.targets = {}

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Hide:
            import traceback
            stack = traceback.extract_stack()[:-1][-6:]
            txt = " <- ".join(f"{os.path.basename(fr.filename)}:{fr.lineno}"
                              f".{fr.name}" for fr in reversed(stack))
            print(f"[HIDE] {obj.objectName() or obj.__class__.__name__}: {txt}")
            sys.stdout.flush()
        return False


class PressSpy(QtCore.QObject):
    """App-level filter mirroring _OutsideCloser's view: log every
    spontaneous MouseButtonPress it sees."""

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.MouseButtonPress:
            PRESSLOG.append((obj.objectName() or obj.__class__.__name__,
                             ev.globalPos().x(), ev.globalPos().y()))
        return False


def post_click(hwnd, x, y):
    lParam = (y << 16) | (x & 0xFFFF)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lParam)
    QtCore.QThread.msleep(60)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lParam)
    QtCore.QThread.msleep(120)


def pump(app, ms=400):
    end = QtCore.QDeadlineTimer(ms)
    while not end.hasExpired():
        app.processEvents(QtCore.QEventLoop.AllEvents, 50)
        QtCore.QThread.msleep(20)


def main():
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s")
    sys.excepthook = lambda t, v, tb: (traceback.print_exception(t, v, tb),
                                       sys.stdout.flush())

    app = QtWidgets.QApplication(sys.argv)
    spy = PressSpy()
    app.installEventFilter(spy)

    cfg = Config.load()
    if os.environ.get("PROBE_MAXIMIZED"):
        # mimic the app's real layout: browser panel + player in a splitter
        # inside a maximized main window (the new startup default)
        import PyQt5.QtWidgets as QW
        host = QW.QMainWindow()
        host.setWindowTitle("MTP maximized probe host")
        host.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        splitter = QW.QSplitter()
        side = QW.QTreeWidget()
        for i in range(30):
            side.addTopLevelItem(QW.QTreeWidgetItem([f"chan {i}"]))
        splitter.addWidget(side)
        view = PlayerView(cfg)
        splitter.addWidget(view)
        splitter.setSizes([317, 1200])
        host.setCentralWidget(splitter)
        host.setWindowFlags(host.windowFlags()
                            | QtCore.Qt.WindowDoesNotAcceptFocus)
        host.showMaximized()
    else:
        host = None
        view = PlayerView(cfg)
    tracer = HideTracer()
    for wdg in (view.overlay, view.ctl, view.ctl_row, view.btn_cc):
        wdg.installEventFilter(tracer)
    if host is None:
        view.setWindowTitle("MTP native-msg probe")
        view.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        view.setWindowFlags(view.windowFlags()
                            | QtCore.Qt.WindowDoesNotAcceptFocus)
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        view.resize(1100, 640)
        view.move(screen.right() - 1108, screen.bottom() - 648)
        view.show()
    view.vlc.set_volume(0)
    view.vlc.set_mute(True)
    pump(app, 2500)

    xc = XtreamClient(cfg.normalized_server(), cfg.username, cfg.password)
    streams = xc.live_streams()
    chan = next((c for c in streams if "NFL" in (c.get("name") or "").upper()),
                streams[0] if streams else None)
    view.play_media({"kind": "live", "title": chan.get("name", "probe"),
                     "url": xc.live_url(chan["stream_id"])})
    dl = QtCore.QElapsedTimer()
    dl.start()
    while dl.elapsed() < 30000:
        pump(app, 200)
        try:
            if view.vlc.is_playing() and view._mode == "chase":
                break
        except Exception:
            pass
    check("chase playback running", view._mode == "chase")
    pump(app, 2000)

    ov_hwnd = int(view.overlay.winId())
    # FAIR STATE: the user always has the controls on screen when clicking —
    # clear any latched suppression (our probe window is never OS-foreground
    # so _wake() alone can leave the overlay hidden) and show the bar.
    view._overlay_suppressed = False
    view.overlay.show()
    view.ctl.show()
    view._layout_overlays()
    pump(app, 300)
    print(f"overlay hwnd = 0x{ov_hwnd:X}  visible={view.overlay.isVisible()}"
          f"  btn_cc visible={view.btn_cc.isVisible()}"
          f"  geom={view.overlay.geometry()}")

    def btn_pt(btn):
        return btn.mapTo(view.overlay,
                         QtCore.QPoint(btn.width() // 2, btn.height() // 2))

    def fair():
        # cursor is on the button in real usage: wake() shows the bar and
        # starts the 4 s idle grace — exactly the user's state at a click
        view._overlay_suppressed = False
        view._wake()
        pump(app, 200)
        assert view.btn_cc.isVisible(), "controls asleep at click time"

    # -- native click on CC ------------------------------------------------
    fair()
    p = btn_pt(view.btn_cc)
    print(f"btn_cc at overlay-client {p.x()},{p.y()}  "
          f"visible={view.btn_cc.isVisible()}")
    PRESSLOG.clear()
    post_click(ov_hwnd, p.x(), p.y())
    pump(app, 600)
    check("native click on btn_cc -> card visible", view._ctl_panel.isVisible())
    check("press spy saw the native press", len(PRESSLOG) > 0,
          f" ({PRESSLOG[-3:] if PRESSLOG else 'NOTHING'})")

    # card rect in overlay-client coords (for the pixel check below)
    card = view._ctl_panel

    # -- native press OUTSIDE the card: on the video area -------------------
    PRESSLOG.clear()
    post_click(ov_hwnd, 40, 40)     # top-left of the video = far outside card
    pump(app, 600)
    check("native press on video (outside) closes card",
          not view._ctl_panel.isVisible())
    check("outside press reached Qt", len(PRESSLOG) > 0,
          f" ({PRESSLOG[-3:] if PRESSLOG else 'NOTHING'})")

    # -- native click on a ROW inside the card (speed) -----------------------
    fair()
    p2 = btn_pt(view.btn_speed)
    print("btn_speed enabled:", view.btn_speed.isEnabled())
    post_click(ov_hwnd, p2.x(), p2.y())
    pump(app, 600)
    check("native click on btn_speed -> card visible",
          view._ctl_panel.isVisible())
    # find the row widget for 1.5x and click its center natively
    target = None
    lay = card._lay
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is not None and getattr(w, "_track_id", None) == 1.5:
            target = w
            break
    if target is not None:
        card._scroll.ensureWidgetVisible(target)
        pump(app, 200)
        rp = target.mapTo(view.overlay,
                          QtCore.QPoint(target.width() // 2,
                                        target.height() // 2))
        print(f"row pt={rp.x()},{rp.y()}  card={card.x()},{card.y()}"
              f",{card.width()}x{card.height()}  target_visible="
              f"{target.isVisible()}")
        before = view._rate
        PRESSLOG.clear()
        post_click(ov_hwnd, rp.x(), rp.y())
        pump(app, 600)
        check("native row pick (1.5x) applied rate",
              abs(view._rate - 1.5) < 1e-9,
              f" (rate={view._rate:g}, was {before:g})")
        check("card closed after row pick", not card.isVisible())
    else:
        check("found the 1.5x row", False)

    # -- native click on AUDIO row ------------------------------------------
    fair()
    p3 = btn_pt(view.btn_audio)
    post_click(ov_hwnd, p3.x(), p3.y())
    pump(app, 600)
    check("native click on btn_audio -> card visible", card.isVisible())

    # toggle closed via native click on the same button
    post_click(ov_hwnd, p3.x(), p3.y())
    pump(app, 600)
    check("native click on btn_audio again -> closed", not card.isVisible())

    # screenshot evidence of the open card via native flow
    fair()
    post_click(ov_hwnd, p.x(), p.y())
    pump(app, 600)
    if card.isVisible():
        card.grab().save(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "probe_popup2_cc_native.png"))
        print("saved probe_popup2_cc_native.png")

    try:
        view.stop()
    except Exception:
        traceback.print_exc()
    pump(app, 500)
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
