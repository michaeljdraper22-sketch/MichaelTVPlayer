# -*- coding: utf-8 -*-
"""Stremio-style track picker — a sharp dark-glass list panel over the video.

The audio-track selector used to be a flat checkable QMenu; this panel is
the modern replacement: a rounded card anchored above the control-bar
button with a caps section header, one row per track (the selected row
carries a checkmark and full-white text), a two-line Auto row and a dimmed
empty-state row while VLC has not surfaced the track list yet.

Presentation only — the PlayerView owns the data (English-first ordering,
auto/pick state, per-tick enforcement) and feeds `set_rows`; the panel
emits `picked(track_id, name)` on a row click (track_id None = Auto) and
`closed` whenever it hides (row pick, click outside, Escape, host hide).
It is a child of the on-video overlay window so it paints above the
native video HWND, takes no focus (keyboard shortcuts keep working), and
closes itself on any press outside its own rect.
"""

from PyQt5 import QtCore, QtGui, QtWidgets

from . import icons as ic

_PANEL_W = 252          # fixed width keeps rows and the checkmark aligned

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
"""


class _OutsideCloser(QtCore.QObject):
    """Closes the panel on any application mouse press outside its rect."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel

    def eventFilter(self, _obj, ev):
        if ev.type() == QtCore.QEvent.MouseButtonPress:
            p = self._panel
            if p.isVisible():
                gp = ev.globalPos()
                rect = QtCore.QRect(p.mapToGlobal(QtCore.QPoint(0, 0)),
                                    p.size())
                if not rect.contains(gp):
                    p.close_panel()
        return False


class _Row(QtWidgets.QWidget):
    """One selectable track row (main label + optional sub label +
    right-aligned checkmark on the selected row)."""

    triggered = QtCore.pyqtSignal()

    def __init__(self, main: str, sub: str = "", selected: bool = False,
                 dim: bool = False, tip: str = "", parent=None):
        super().__init__(parent)
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


class TrackPanel(QtWidgets.QWidget):
    """The picker card itself — see the module docstring."""

    picked = QtCore.pyqtSignal(object, str)     # (track_id | None, name)
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
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 9, 8, 9)
        v.setSpacing(2)
        head = QtWidgets.QLabel("AUDIO")
        head.setObjectName("tpHead")
        v.addWidget(head)
        v.addSpacing(4)
        self._host = QtWidgets.QVBoxLayout()
        self._host.setSpacing(1)
        v.addLayout(self._host)
        self._closer = _OutsideCloser(self, self)
        self.hide()

    # ---- data ----
    def set_rows(self, rows: list):
        """Rebuild the rows.

        ``rows``: list of dicts {id (None=Auto), main, sub?, name?,
        checked?, enabled?} — PlayerView supplies English-first ordering
        and cleaned labels (see _audio_menu)."""
        self._rows = [dict(r) for r in rows]
        while self._host.count():
            item = self._host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for r in self._rows:
            row = _Row(str(r.get("main", "")),
                       sub=str(r.get("sub", "") or ""),
                       selected=bool(r.get("checked")),
                       dim=not bool(r.get("enabled", True)),
                       tip=str(r.get("tip", "") or r.get("name", "") or ""))
            row._track_id = r.get("id")     # row identity (tests/debug)
            row.triggered.connect(
                lambda rid=r.get("id"), nm=str(r.get("name", "") or ""):
                self._row_picked(rid, nm))
            self._host.addWidget(row)
        self.adjustSize()
        self.updateGeometry()

    def rows(self) -> list:
        """The last data fed to set_rows — test introspection."""
        return [dict(r) for r in self._rows]

    def _row_picked(self, track_id, name):
        if any(r.get("id") == track_id and not r.get("enabled", True)
               for r in self._rows):
            return                  # dimmed empty-state rows do nothing
        self.picked.emit(track_id, name)
        self.close_panel()

    # ---- open/close ----
    def popup(self, anchor: QtWidgets.QWidget):
        """Show above ``anchor`` (a control-bar button), right-aligned
        with it, clamped inside the host overlay window; flip below the
        button when there is no room above."""
        host = anchor.window()
        if host is None:
            return
        self.adjustSize()
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
        QtWidgets.QApplication.instance().installEventFilter(self._closer)

    def close_panel(self):
        if not self.isVisible():
            return
        self.hide()
        QtWidgets.QApplication.instance().removeEventFilter(self._closer)
        self.closed.emit()

    def hideEvent(self, event):
        # every hide path (including the host window hiding) uninstalls
        # the click-outside watcher
        try:
            QtWidgets.QApplication.instance().removeEventFilter(self._closer)
        except Exception:  # noqa: BLE001
            pass
        super().hideEvent(event)
