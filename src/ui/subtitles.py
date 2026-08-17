# -*- coding: utf-8 -*-
"""Auto-caption rendering + the Subtitles settings dialog.

``CaptionWidget`` replaces the old QLabel: it word-wraps long transcriptions
into a few centered lines (so fast talkers can never produce a box bigger
than the video), draws either a translucent background box or an outlined
text stroke, and can be DRAGGED anywhere on the video (the position is
remembered). Everything is styleable live from ``SubtitlesSettingsDialog``.
"""

from PyQt5 import QtCore, QtGui, QtWidgets

MAX_LINES = 3          # caption lines shown at once (oldest words dropped)
MAX_CHARS = 260        # hard cap on displayed characters


def _qcolor(hexstr: str, alpha: int = 255) -> QtGui.QColor:
    c = QtGui.QColor(hexstr)
    if not c.isValid():
        c = QtGui.QColor("#ffffff")
    c.setAlpha(max(0, min(255, alpha)))
    return c


class CaptionWidget(QtWidgets.QWidget):
    """Draggable, styleable auto-caption overlay (lives in the overlay
    window, same coordinates as the video surface)."""

    moved = QtCore.pyqtSignal()

    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setCursor(QtCore.Qt.SizeAllCursor)
        self._lines = []
        self._drag_off = None
        self._style = {}
        self._font = QtGui.QFont()
        self._raw = ""
        self.apply_style()
        self.hide()

    # ---- style ----
    def apply_style(self):
        st = self.config.subtitle_style
        self._style = st
        self._font = QtGui.QFont(
            st.get("font_family") or "Segoe UI",
            int(st.get("font_size") or 18),
            QtGui.QFont.Bold)
        self._relayout()
        self.update()

    def reset_position(self):
        st = self.config.subtitle_style
        st["pos_x"] = None
        st["pos_y"] = None
        self.config.subtitle_style = st
        self.config.save()
        self.apply_style()
        self.moved.emit()

    # ---- text ----
    def set_text(self, text: str):
        text = (text or "").strip()
        if text != self._raw:
            self._raw = text
            self._relayout()
        if text:
            if not self.isVisible():
                self.show()
            self.raise_()
        else:
            self.hide()

    def _relayout(self):
        """Wrap the text into <= MAX_LINES centered lines that fit the
        video; drop the OLDEST words when there is too much text."""
        st = self._style
        self._lines = []
        if not self._raw:
            self.resize(0, 0)
            return
        video = self.parentWidget().size() if self.parentWidget() \
            else QtCore.QSize(1280, 720)
        fm = QtGui.QFontMetrics(self._font)
        max_w = max(160, int(video.width() * 0.85))

        def wrap(text):
            lines = [""]
            for w in text.split(" "):
                cand = (lines[-1] + " " + w).strip()
                if fm.horizontalAdvance(cand) <= max_w or not lines[-1]:
                    lines[-1] = cand
                else:
                    lines.append(w)
            return lines

        text = " ".join(self._raw.split())[:MAX_CHARS]
        lines = wrap(text)
        if len(lines) > MAX_LINES:
            lines = wrap(" ".join(text.split(" ")[-90:]))
            if len(lines) > MAX_LINES:
                lines = lines[-MAX_LINES:]
        self._lines = lines
        w = max(fm.horizontalAdvance(l) for l in lines)
        h = fm.height() * len(lines)
        pad = 14 if st.get("bg_enabled") else 6
        self.resize(min(max_w, w) + pad * 2, h + pad * 2)

    # ---- placement ----
    def place_in(self, g: QtCore.QRect, bottom_limit: int):
        """Position inside video rect ``g``; ``bottom_limit`` keeps the box
        clear of the control bar when at the default bottom spot."""
        st = self._style
        size = self.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        fx, fy = st.get("pos_x"), st.get("pos_y")
        if fx is None:
            x = g.left() + (g.width() - size.width()) // 2
        else:
            x = int(g.left() + fx * g.width() - size.width() / 2)
        if fy is None:
            y = bottom_limit - size.height()
        else:
            y = int(g.top() + fy * g.height() - size.height() / 2)
        x = max(g.left() + 2, min(x, g.right() - size.width() + 1))
        y = max(g.top() + 2, min(y, g.bottom() - size.height() + 1))
        self.move(x, y)

    # ---- dragging ----
    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self._drag_off = ev.globalPos() - self.pos()
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._drag_off is not None:
            self.move(ev.globalPos() - self._drag_off)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._drag_off is not None and self.parentWidget():
            video = self.parentWidget().rect()
            c = self.rect().center() + self.pos()
            st = self.config.subtitle_style
            st["pos_x"] = round(max(0.0, min(1.0, (c.x() - video.left())
                                              / max(1.0, video.width()))), 4)
            st["pos_y"] = round(max(0.0, min(1.0, (c.y() - video.top())
                                              / max(1.0, video.height()))), 4)
            self.config.subtitle_style = st
            self.config.save()
            # ALSO update the live style dict: place_in() reads self._style,
            # and the stale copy (pos=None) is what snapped the box back to
            # its default spot on the very next text change.
            self._style["pos_x"] = st["pos_x"]
            self._style["pos_y"] = st["pos_y"]
            self._drag_off = None
            self.moved.emit()
            ev.accept()
        elif self._drag_off is not None:
            self._drag_off = None
            ev.accept()

    # ---- painting ----
    def paintEvent(self, _ev):
        if not self._lines:
            return
        st = self._style
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
        p.setFont(self._font)
        fm = QtGui.QFontMetrics(self._font)
        pad = 14 if st.get("bg_enabled") else 6
        lh = fm.height()
        if st.get("bg_enabled") and not st.get("outline_enabled"):
            bg = _qcolor(st.get("bg_color", "#000000"),
                         int(255 * (int(st.get("bg_opacity", 70)) / 100.0)))
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 8, 8)
        text_c = _qcolor(st.get("text_color", "#ffffff"))
        outline = st.get("outline_enabled")
        ow = max(1, int(st.get("outline_width", 3)))
        oc = _qcolor(st.get("outline_color", "#000000"))
        y = pad + fm.ascent() + (self.height() - 2 * pad
                                 - lh * len(self._lines)) // 2
        for line in self._lines:
            x = (self.width() - fm.horizontalAdvance(line)) // 2
            if outline:
                path = QtGui.QPainterPath()
                path.addText(QtCore.QPointF(max(0, x), y), self._font, line)
                p.setPen(QtGui.QPen(oc, ow, QtCore.Qt.SolidLine,
                                    QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
                p.setBrush(QtCore.Qt.NoBrush)
                p.drawPath(path)
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(text_c)
                p.drawPath(path)
            else:
                p.setPen(text_c)
                p.drawText(QtCore.QPointF(max(0, x), y), line)
            y += lh
        p.end()

class SubtitlesSettingsDialog(QtWidgets.QDialog):
    """Live-applying appearance settings for the auto-generated captions."""

    def __init__(self, config, view, parent=None):
        super().__init__(parent)
        self.config = config
        self.view = view
        self.setWindowTitle("Subtitle settings")
        self.setModal(False)
        lay = QtWidgets.QGridLayout(self)
        lay.setHorizontalSpacing(10)
        lay.setVerticalSpacing(8)
        st = dict(config.subtitle_style)
        self._st = st

        row = 0
        lay.addWidget(QtWidgets.QLabel("Font:"), row, 0)
        self.font_box = QtWidgets.QFontComboBox()
        self.font_box.setCurrentFont(QtGui.QFont(st["font_family"]))
        self.font_box.currentFontChanged.connect(self._changed)
        lay.addWidget(self.font_box, row, 1)
        self.size_spin = QtWidgets.QSpinBox()
        self.size_spin.setRange(10, 72)
        self.size_spin.setValue(int(st["font_size"]))
        self.size_spin.setSuffix(" pt")
        self.size_spin.valueChanged.connect(self._changed)
        lay.addWidget(self.size_spin, row, 2)

        row += 1
        lay.addWidget(QtWidgets.QLabel("Text color:"), row, 0)
        self.text_btn = self._color_btn(st["text_color"])
        lay.addWidget(self.text_btn, row, 1)

        row += 1
        self.bg_chk = QtWidgets.QCheckBox("Background box")
        self.bg_chk.setChecked(bool(st["bg_enabled"]))
        self.bg_chk.toggled.connect(self._changed)
        lay.addWidget(self.bg_chk, row, 0)
        self.bg_btn = self._color_btn(st["bg_color"])
        lay.addWidget(self.bg_btn, row, 1)
        self.bg_op = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.bg_op.setRange(0, 100)
        self.bg_op.setValue(int(st["bg_opacity"]))
        self.bg_op.valueChanged.connect(self._changed)
        lay.addWidget(self.bg_op, row, 2)

        row += 1
        self.out_chk = QtWidgets.QCheckBox("Outlined text "
                                           "(instead of background)")
        self.out_chk.setChecked(bool(st["outline_enabled"]))
        self.out_chk.toggled.connect(self._changed)
        lay.addWidget(self.out_chk, row, 0, 1, 3)

        row += 1
        lay.addWidget(QtWidgets.QLabel("Outline color:"), row, 0)
        self.out_btn = self._color_btn(st["outline_color"])
        lay.addWidget(self.out_btn, row, 1)
        self.out_w = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.out_w.setRange(1, 10)
        self.out_w.setValue(int(st["outline_width"]))
        self.out_w.valueChanged.connect(self._changed)
        lay.addWidget(QtWidgets.QLabel("Thickness:"), row, 2)
        row += 1
        hint = QtWidgets.QLabel(
            "Tip: drag the subtitle box on the video to move it.")
        hint.setStyleSheet("color:#9aa0a6;")
        lay.addWidget(hint, row, 0, 1, 2)
        self.pos_btn = QtWidgets.QPushButton("Reset position")
        self.pos_btn.clicked.connect(self._reset_pos)
        lay.addWidget(self.pos_btn, row, 2)

        row += 1
        lay.addWidget(QtWidgets.QLabel("Caption sync:"), row, 0)
        sync_row = QtWidgets.QHBoxLayout()
        self.sync_minus = QtWidgets.QPushButton("\u2212 0.5 s")
        self.sync_minus.setToolTip("Show captions sooner (when they lag "
                                   "behind the speech)")
        self.sync_plus = QtWidgets.QPushButton("+ 0.5 s")
        self.sync_plus.setToolTip("Show captions later (when they run ahead "
                                  "of the speech)")
        self.sync_value = QtWidgets.QLabel("")
        self.sync_reset = QtWidgets.QPushButton("Reset")
        self.sync_minus.clicked.connect(lambda: self._bump_sync(-0.5))
        self.sync_plus.clicked.connect(lambda: self._bump_sync(0.5))
        self.sync_reset.clicked.connect(lambda: self._bump_sync(None))
        sync_row.addWidget(self.sync_minus)
        sync_row.addWidget(self.sync_value)
        sync_row.addWidget(self.sync_plus)
        sync_row.addWidget(self.sync_reset)
        wrap = QtWidgets.QWidget()
        wrap.setLayout(sync_row)
        lay.addWidget(wrap, row, 1, 1, 2)
        self._sync_label()

        row += 1
        from .. import captions as capmod
        self.model_btn = None
        if not capmod.large_model_ready():
            self.model_btn = QtWidgets.QPushButton(
                "Download high-accuracy speech model (~1.3 GB)\n"
                "much better with accents — used once it finishes")
            self.model_btn.clicked.connect(self._dl_model)
            lay.addWidget(self.model_btn, row, 0, 1, 3)
            row += 1
        else:
            note = QtWidgets.QLabel("High-accuracy speech model: active")
            note.setStyleSheet("color:#7fbf7f;")
            lay.addWidget(note, row, 0, 1, 3)
            row += 1

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        self.revert_btn = QtWidgets.QPushButton("Revert to defaults")
        self.revert_btn.clicked.connect(self._revert)
        btns.addButton(self.revert_btn, QtWidgets.QDialogButtonBox.ActionRole)
        lay.addWidget(btns, row, 0, 1, 3)
        lay.setColumnStretch(2, 1)

    # ---- helpers ----
    def _color_btn(self, hexcolor: str) -> QtWidgets.QPushButton:
        b = QtWidgets.QPushButton()
        b.setFixedSize(64, 24)
        b.setCursor(QtCore.Qt.PointingHandCursor)
        b._hex = hexcolor
        b.setStyleSheet(
            f"background-color: {hexcolor}; border: 1px solid #888;")

        def pick():
            c = QtWidgets.QColorDialog.getColor(
                QtGui.QColor(b._hex), self, "Choose color")
            if c.isValid():
                b._hex = c.name()
                b.setStyleSheet(
                    f"background-color: {c.name()};"
                    " border: 1px solid #888;")
                self._changed()
        b.clicked.connect(pick)
        return b

    def _collect(self) -> dict:
        st = self._st
        st["font_family"] = self.font_box.currentFont().family()
        st["font_size"] = self.size_spin.value()
        st["text_color"] = self.text_btn._hex
        st["bg_enabled"] = self.bg_chk.isChecked()
        st["bg_color"] = self.bg_btn._hex
        st["bg_opacity"] = self.bg_op.value()
        st["outline_enabled"] = self.out_chk.isChecked()
        st["outline_color"] = self.out_btn._hex
        st["outline_width"] = self.out_w.value()
        return st

    def _changed(self, *_):
        self.config.subtitle_style = self._collect()
        self.config.save()
        if self.view is not None:
            self.view.apply_subtitle_style()

    def _reset_pos(self):
        if self.view is not None:
            self.view.captions_reset_position()

    # ---- manual caption sync (applied live to the caption feed) ----
    def _sync_label(self):
        v = self.config.caption_sync_offset
        self.sync_value.setText(f"{v:+.1f} s" if v else "auto")

    def _bump_sync(self, delta):
        if delta is None:
            self.config.caption_sync_offset = 0.0
        else:
            self.config.caption_sync_offset = (
                self.config.caption_sync_offset + delta)
        self.config.save()
        self._sync_label()
        if self.view is not None:
            self.view.captions_apply_sync()

    def _revert(self):
        from ..config import DEFAULTS
        self._st = dict(DEFAULTS["subtitle_style"])
        # Reset every control SILENTLY first — each programmatic change
        # fires _changed(), and a mid-reset collect would read the still-
        # stale widgets and clobber the revert. Persist once at the end.
        for w in (self.font_box, self.size_spin, self.bg_chk,
                  self.bg_op, self.out_chk, self.out_w):
            w.blockSignals(True)
        try:
            self.font_box.setCurrentFont(QtGui.QFont(self._st["font_family"]))
            self.size_spin.setValue(int(self._st["font_size"]))
            for btn, key in ((self.text_btn, "text_color"),
                             (self.bg_btn, "bg_color"),
                             (self.out_btn, "outline_color")):
                btn._hex = self._st[key]
                btn.setStyleSheet(
                    f"background-color: {self._st[key]};"
                    " border: 1px solid #888;")
            self.bg_chk.setChecked(bool(self._st["bg_enabled"]))
            self.bg_op.setValue(int(self._st["bg_opacity"]))
            self.out_chk.setChecked(bool(self._st["outline_enabled"]))
            self.out_w.setValue(int(self._st["outline_width"]))
        finally:
            for w in (self.font_box, self.size_spin, self.bg_chk,
                      self.bg_op, self.out_chk, self.out_w):
                w.blockSignals(False)
        # persist the AUTHORITATIVE defaults (a fontComboBox on a system
        # without the default font reports a substitute family — the saved
        # style must not depend on that)
        self.config.subtitle_style = dict(self._st)
        self.config.save()
        if self.view is not None:
            self.view.captions_reset_position()
            self.view.apply_subtitle_style()

    def _dl_model(self):
        from .. import captions as capmod
        self.model_btn.setEnabled(False)
        self.model_btn.setText("Downloading… (can take a while)")
        self._dl = capmod.ModelDownloader(self)
        self._dl.progress.connect(
            lambda d, t: self.model_btn.setText(
                f"Downloading… {d // 1048576} / {t // 1048576} MB"))
        self._dl.finished.connect(self._dl_done)
        self._dl.start(large=True)

    def _dl_done(self, ok, msg):
        if ok:
            self.model_btn.setText(
                "High-accuracy model downloaded — captions now use it")
            if self.view is not None:
                self.view.captions_restart_for_new_model()
        else:
            QtWidgets.QMessageBox.warning(
                self, "Subtitles", f"Model download failed: {msg}")
            self.model_btn.setEnabled(True)
            self.model_btn.setText(
                "Download high-accuracy speech model (~1.3 GB)")
