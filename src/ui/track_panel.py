# -*- coding: utf-8 -*-
"""Stremio-style picker card — one sharp dark-glass list panel for every
control-bar popup (audio tracks, subtitles, scale, speed).

The popups used to be flat checkable QMenus; this card is the modern
replacement: a rounded panel anchored above the control-bar button with a
caps section header, one row per entry (the selected row carries a
checkmark and full-white text), optional sub-labels, separator rows and a
dimmed empty state. Long lists (the speed ladder) scroll inside the card
rather than growing past the video.

Presentation only — the PlayerView owns the data (English-first ordering,
selection state, per-tick enforcement) and feeds `set_rows`; the panel
emits `picked(row_dict)` on a row click and `closed` whenever it hides
(row pick, click outside, Escape, host hide). It is a child of the
on-video overlay window so it paints above the native video HWND, takes
no focus (keyboard shortcuts keep working), and closes itself on any
press outside its own rect.
"""

from PyQt5 import QtCore, QtGui, QtWidgets

from . import icons as ic

import ctypes.wintypes
import logging
import sys

log = logging.getLogger("mtp")


def _log(fmt: str, *args) -> None:
    """Logging can never break the UI (house rule — see logging_setup)."""
    try:
        log.info(fmt, *args)
    except Exception:  # noqa: BLE001
        pass

_PANEL_W = 252          # fixed width keeps rows and the checkmark aligned
_PANEL_MAX_H = 400      # longer lists scroll inside the card

_QSS = """
#trackPanel { background-color: rgba(17,19,22,238); border-radius: 10px;
              border: 1px solid rgba(255,255,255,30); }
#tpHead { color: rgba(255,255,255,150); font-size: 11px; font-weight: 700;
          letter-spacing: 2px; }
#tpRow { background: transparent; border: none; border-radius: 6px; }
#tpRow:hover { background-color: rgba(255,255,255,26); }
#tpMain { color: rgba(255,255,255,208); font-size: 13px; }
#tpRow[sel="true"] #tpMain { color: #ffffff; }
#tpSub { color: rgba(255,255,255,120); font-size: 11px; }
#tpRow[dim="true"] #tpMain { color: rgba(255,255,255,92); }
#tpRow[dim="true"] { background: transparent; }
#tpSep { color: rgba(255,255,255,30); background: transparent; }
#tpScroll { background: transparent; border: none; }
#tpScroll QScrollBar:vertical { background: transparent; width: 6px;
                                margin: 2px; }
#tpScroll QScrollBar::handle:vertical {
    background: rgba(255,255,255,70); border-radius: 3px;
    min-height: 24px; }
#tpScroll QScrollBar::add-line:vertical,
#tpScroll QScrollBar::sub-line:vertical { height: 0; }
#tpScroll QScrollBar::add-page:vertical,
#tpScroll QScrollBar::sub-page:vertical { background: transparent; }
"""


class _OutsideCloser(QtCore.QObject):
    """Closes the panel on any application mouse press outside its rect —
    except on the anchor button itself: that press belongs to the toggle
    (the button's click handler decides open-vs-close), so swallowing it
    here would make the card impossible to close with its own button."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel

    def maybe_close_at(self, global_pos) -> None:
        """Close the panel when ``global_pos`` (a QPoint) is outside both
        the card and the anchor button.  Shared by the Qt press filter and
        the native Windows watcher below."""
        p = self._panel
        if not p.isVisible():
            return
        rect = QtCore.QRect(p.mapToGlobal(QtCore.QPoint(0, 0)), p.size())
        if rect.contains(global_pos):
            return
        anchor = getattr(p, "_anchor", None)
        if anchor is not None and anchor.isVisible():
            arect = QtCore.QRect(anchor.mapToGlobal(QtCore.QPoint(0, 0)),
                                 anchor.size())
            if arect.contains(global_pos):
                return          # the toggle button's press
        _log("ctl panel: closed by outside press")
        p.close_panel("outside press")

    def eventFilter(self, _obj, ev):
        if ev.type() == QtCore.QEvent.MouseButtonPress:
            self.maybe_close_at(ev.globalPos())
        return False


if sys.platform == "win32":
    class _NativePressCloser(QtCore.QAbstractNativeEventFilter):
        """Windows backstop for _OutsideCloser: presses on windows Qt does
        not own never become QEvents — most importantly libvlc's own video
        child HWND, which covers the whole video area during playback.
        Clicking the video (the most obvious "outside") therefore has to be
        caught at the native message level or the card stays open."""

        WM_LBUTTONDOWN = 0x0201

        def __init__(self, closer):
            super().__init__()
            self._closer = closer

        def nativeEventFilter(self, eventType, message):
            try:
                if bytes(eventType) == b"windows_generic_MSG":
                    msg = ctypes.wintypes.MSG.from_address(int(message))
                    if msg.message == self.WM_LBUTTONDOWN:
                        self._closer.maybe_close_at(
                            QtCore.QPoint(msg.pt.x, msg.pt.y))
            except Exception:  # noqa: BLE001
                pass
            # PyQt5 requires (boolhandled, result) — a bare False here
            # raises inside the message pump and takes the app down
            return False, 0
else:
    _NativePressCloser = None


class _Row(QtWidgets.QWidget):
    """One selectable row (main label + optional sub label + right-aligned
    checkmark on the selected row). Fires ``triggered`` on a completed
    left click (press AND release inside the row) — button-style, so a
    press that drags off the row cancels."""

    triggered = QtCore.pyqtSignal()

    def __init__(self, main: str, sub: str = "", selected: bool = False,
                 dim: bool = False, tip: str = "", parent=None):
        super().__init__(parent)
        self._pressed = False
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setObjectName("tpRow")
        self.setProperty("sel", bool(selected))
        self.setProperty("dim", bool(dim))
        self.setCursor(QtCore.Qt.PointingHandCursor if not dim
                       else QtCore.Qt.ArrowCursor)
        self.setToolTip(tip)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(12, 3, 10, 3)
        lay.setSpacing(8)
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(0)
        lab = QtWidgets.QLabel(main)
        lab.setObjectName("tpMain")
        col.addWidget(lab)
        if sub:
            slab = QtWidgets.QLabel(sub)
            slab.setObjectName("tpSub")
            col.addWidget(slab)
        lay.addLayout(col, 1)
        self.chk = QtWidgets.QLabel()
        self.chk.setPixmap(ic.check().pixmap(QtCore.QSize(15, 15)))
        self.chk.setVisible(bool(selected))
        lay.addWidget(self.chk, 0, QtCore.Qt.AlignRight)
        self.setFixedHeight(42 if sub else 34)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self._pressed = True
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton and self._pressed:
            self._pressed = False
            if self.rect().contains(ev.pos()):
                self.triggered.emit()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)


class TrackPanel(QtWidgets.QWidget):
    """The picker card itself — see the module docstring."""

    picked = QtCore.pyqtSignal(dict)          # the clicked row's dict
    closed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setObjectName("trackPanel")
        self.setStyleSheet(_QSS)
        self.setFixedWidth(_PANEL_W)
        self._rows = []             # last data fed to set_rows (tests)
        self._anchor = None         # the opener button (toggle support)
        self._head = QtWidgets.QLabel("AUDIO")
        self._head.setObjectName("tpHead")
        self._rows_host = QtWidgets.QWidget()
        self._rows_host.setObjectName("tpRows")
        self._lay = QtWidgets.QVBoxLayout(self._rows_host)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(1)
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setObjectName("tpScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.setFocusPolicy(QtCore.Qt.NoFocus)
        self._scroll.setWidget(self._rows_host)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 9, 8, 9)
        v.setSpacing(2)
        v.addWidget(self._head)
        v.addSpacing(4)
        v.addWidget(self._scroll, 1)
        self._closer = _OutsideCloser(self, self)
        self._native_closer = (_NativePressCloser(self._closer)
                               if _NativePressCloser is not None else None)
        self.hide()

    # ---- data ----
    def set_rows(self, rows: list, header: str = "AUDIO"):
        """Rebuild the rows.

        ``rows``: list of dicts {id, main, sub?, name?, checked?,
        enabled?, sep?} — PlayerView supplies the ordering and cleaned
        labels. ``sep`` rows draw as a thin divider; ``enabled`` False
        rows are dimmed and not pickable."""
        self._rows = [dict(r) for r in rows]
        self._head.setText(header or "AUDIO")
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for r in self._rows:
            if r.get("sep"):
                sep = QtWidgets.QFrame()
                sep.setObjectName("tpSep")
                sep.setFrameShape(QtWidgets.QFrame.HLine)
                sep.setFixedHeight(1)
                self._lay.addWidget(sep)
                continue
            row = _Row(str(r.get("main", "")),
                       sub=str(r.get("sub", "") or ""),
                       selected=bool(r.get("checked")),
                       dim=not bool(r.get("enabled", True)),
                       tip=str(r.get("tip", "") or r.get("name", "") or ""))
            row._track_id = r.get("id")     # row identity (tests/debug)
            row._row = r
            row.triggered.connect(lambda w=None, rr=row: self._row_picked(rr))
            self._lay.addWidget(row)
        self._rows_host.adjustSize()
        # card height = chrome + rows, capped so long lists (the speed
        # ladder) scroll inside the card instead of growing past the video
        h = 46
        for r in self._rows:
            h += 7 if r.get("sep") else (42 if r.get("sub") else 34)
        self.setFixedHeight(max(120, min(h, _PANEL_MAX_H)))
        self.updateGeometry()

    def rows(self) -> list:
        """The last data fed to set_rows — test introspection."""
        return [dict(r) for r in self._rows]

    def _row_picked(self, row_widget):
        r = row_widget._row
        if not r.get("enabled", True):
            return                  # dimmed empty-state rows do nothing
        # close BEFORE the pick runs: some picks open modal dialogs
        # (Subtitle settings…) and the card must already be gone
        self.close_panel("pick")
        _log("ctl panel: picked %r", r.get("id"))
        self.picked.emit(dict(r))

    # ---- open/close ----
    def popup(self, anchor: QtWidgets.QWidget):
        """Show above ``anchor`` (a control-bar button), right-aligned
        with it, clamped inside the host overlay window; flip below the
        button when there is no room above. The anchor is remembered so
        a press on it toggles instead of count-intuitively reopening."""
        self._anchor = anchor
        host = anchor.window()
        if host is None:
            _log("ctl panel: no host window — popup refused")
            return
        w = self.width()
        h = self.height()
        top = anchor.mapTo(host, QtCore.QPoint(0, 0))
        x = top.x() + anchor.width() - w
        x = max(8, min(x, host.width() - w - 8))
        y = top.y() - h - 10
        if y < 8:
            y = min(top.y() + anchor.height() + 10,
                    max(8, host.height() - h - 8))
        self.move(x, y)
        self.show()
        self.raise_()
        app = QtWidgets.QApplication.instance()
        app.installEventFilter(self._closer)
        if self._native_closer is not None:
            # clicks on the (non-Qt) libvlc video HWND never reach the app
            # filter above — watch them at the native message level too
            app.installNativeEventFilter(self._native_closer)

    def close_panel(self, reason: str = "hide"):
        """Hide the card. ``reason`` is only for the log line — the close
        behavior is identical on every path."""
        if not self.isVisible():
            return
        self.hide()
        app = QtWidgets.QApplication.instance()
        app.removeEventFilter(self._closer)
        if self._native_closer is not None:
            try:
                app.removeNativeEventFilter(self._native_closer)
            except Exception:  # noqa: BLE001
                pass
        _log("ctl panel: closed (%s)", reason)
        self.closed.emit()

    def hideEvent(self, event):
        # every hide path (including the host window hiding) uninstalls the
        # click-outside watchers
        app = QtWidgets.QApplication.instance()
        try:
            app.removeEventFilter(self._closer)
        except Exception:  # noqa: BLE001
            pass
        if self._native_closer is not None:
            try:
                app.removeNativeEventFilter(self._native_closer)
            except Exception:  # noqa: BLE001
                pass
        super().hideEvent(event)

    def showEvent(self, event):
        # Mirror of hideEvent: hiding the card (or its whole host overlay —
        # the overlay window is suppressed on focus changes, e.g. the video
        # HWND grabbing focus) uninstalls the watchers; when the card
        # becomes visible AGAIN they must come back, or the open card can
        # never be closed by clicking outside (the "stuck menu" bug).
        # installEventFilter dedupes, so re-installing is safe.
        super().showEvent(event)
        if self._anchor is None:
            return          # pre-popup show while the panel is constructed
        try:
            app = QtWidgets.QApplication.instance()
            app.installEventFilter(self._closer)
            if self._native_closer is not None:
                app.installNativeEventFilter(self._native_closer)
        except Exception:  # noqa: BLE001
            pass
