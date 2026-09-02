# -*- coding: utf-8 -*-
"""Offscreen probe: '♥ Support Developer' menu-bar button next to Settings.

Mocks QDesktopServices.openUrl so no browser window opens (no focus steal).
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def main():
    from src.config import Config
    from src.ui.main_window import MainWindow
    from src.ui.theme import apply_theme

    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    MainWindow._restore_state = lambda self: None
    win = MainWindow(Config.load())
    win._save_state = lambda: None

    print("[1] menu-bar item exists, is clickable, sits next to Settings")
    mb = win.menuBar()
    acts = mb.actions()
    texts = [a.text() for a in acts]
    print("   menu bar:", texts)
    supp = next((a for a in acts if "Support Developer" in a.text()), None)
    check("support item present", supp is not None)
    check("no dropdown (plain button)", supp is not None and supp.menu() is None)
    check("enabled", supp is not None and supp.isEnabled())
    i = texts.index(supp.text()) if supp else -1
    check("right after Settings",
          0 <= i < len(texts) - 1 and texts[i - 1] == "&Settings")

    print("[2] clicking it opens the Cash App link (mocked, no browser)")
    opened = []
    QtGui.QDesktopServices.openUrl = staticmethod(
        lambda url: opened.append(url.toString()))
    supp.trigger()
    app.processEvents()
    check("exactly one url opened", len(opened) == 1)
    check("url is cash.app/$Michaeljdraper",
          opened == ["https://cash.app/$Michaeljdraper"])

    print()
    print("ALL PASS" if not FAIL else f"FAILURES: {FAIL}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
