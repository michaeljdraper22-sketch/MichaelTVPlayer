# -*- coding: utf-8 -*-
"""Live probe: measure the REAL composited pixels of the control-bar
button plates, pre-stream (video = pure black — the exact case in the
user's screenshot).  Window lowered, never activated, no stream, no audio.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402


def wait(app, ms):
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec_()
    app.processEvents()


def main():
    from src.config import Config
    from src.ui.main_window import MainWindow
    from src.ui.theme import apply_theme

    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    MainWindow._restore_state = lambda self: None
    win = MainWindow(Config.load())
    win._save_state = lambda: None
    win.resize(1180, 760)
    win.show()
    win.lower()
    app.processEvents()
    wait(app, 900)

    pv = win.player_view
    pv._wake()
    app.processEvents()
    wait(app, 300)

    btns = [pv.btn_back60, pv.btn_play, pv.btn_cc, pv.btn_scale,
            pv.btn_mute, pv._btn_panel]
    print("button plate pixels (composited over the black video):")
    worst = 0
    for b in btns:
        if not b.isVisible():
            b.setVisible(True)
            app.processEvents()
        gp = b.mapToGlobal(QtCore.QPoint(0, 0))
        pm = app.primaryScreen().grabWindow(0, gp.x(), gp.y(),
                                           b.width(), b.height())
        img = pm.toImage().convertToFormat(QtGui.QImage.Format_RGB888)
        vals = set()
        for x in range(2, b.width() - 2, 3):
            for y in range(2, b.height() - 2, 3):
                c = img.pixel(x, y)
                vals.add((c & 0xff, (c >> 8) & 0xff, (c >> 16) & 0xff))
        # the darkest common value = the plate over black (glyph pixels
        # are brighter)
        from collections import Counter
        cnt = Counter()
        for x in range(2, b.width() - 2):
            for y in range(2, b.height() - 2):
                c = img.pixel(x, y)
                cnt[(c & 0xff, (c >> 8) & 0xff, (c >> 16) & 0xff)] += 1
        dark = sorted(cnt.items(), key=lambda kv: sum(kv[0]))[:3]
        print("  %-12s darkest pixels: %s" % (
            b.objectName() or b.toolTip()[:12], dark))
        worst = max(worst, sum(dark[0][0]) if dark else 0)

    # reference: pure video area away from controls
    gp = pv.surface.mapToGlobal(QtCore.QPoint(10, 10))
    pm = app.primaryScreen().grabWindow(0, gp.x(), gp.y(), 40, 40)
    img = pm.toImage().convertToFormat(QtGui.QImage.Format_RGB888)
    c = img.pixel(20, 20)
    print("  video background reference:", (c & 0xff, (c >> 8) & 0xff,
                                            (c >> 16) & 0xff))
    print()
    print("VERDICT:", "INVISIBLE (plates match background)"
          if worst <= 3 else "STILL VISIBLE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
