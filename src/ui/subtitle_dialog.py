# -*- coding: utf-8 -*-
"""Subtitle appearance dialog — opened from the CC button's track menu.

EVERY change applies live while the dialog is open: the delay through the
``apply_delay`` callback (VLC's runtime API) and every visual setting
(font, size, position, colors, outline) through ``apply_live`` — the app
caption overlay re-reads the config on each paint, so sliders and color
picks are visible on the video mid-adjustment, and multiple tweaks need
no OK in between. OK simply closes; Cancel reverts the visual settings to
their dialog-open state (the delay keeps its instant-persist behavior).
VLC-rendered image tracks cannot restyle at runtime: for those the
PlayerView rebuilds the player once when the dialog closes with changes.
"""

from PyQt5 import QtCore, QtGui, QtWidgets

from ..config import SUBTITLE_DEFAULTS, SUBTITLE_KEYS

_DELAY_STEP_MS = 250          # the +/- buttons move in quarter seconds
_POS_MAX = 100                # +/- 100 % == about half a screen
_SAVE_DEBOUNCE_MS = 300       # slider drags save once, not per tick

# visual keys only — the delay persists instantly and is never reverted
_STYLE_KEYS = tuple(k for k in SUBTITLE_KEYS if k != "delay_ms")


class SubtitleDialog(QtWidgets.QDialog):
    def __init__(self, config, apply_delay, parent=None, apply_live=None):
        """``apply_delay(ms)`` runs on every +/- click (live VLC API);
        ``apply_live(appearance)`` runs on every VISUAL change so the
        caption overlay repaints mid-dialog (PlayerView passes
        ``_apply_sub_style_live``)."""
        super().__init__(parent)
        self.config = config
        self._apply_delay = apply_delay
        self._apply_live = apply_live
        self.appearance = dict(config.subtitle_appearance)   # working copy
        self._delay_ms = int(self.appearance.get("delay_ms", 0) or 0)
        # what Cancel restores (delay excluded — it applied live already)
        self._open_style = {k: self.appearance.get(k) for k in _STYLE_KEYS}
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(_SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self.config.save)

        self.setWindowTitle("Subtitle settings")
        self.setMinimumWidth(420)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setSpacing(10)

        # ---- delay (instant) ----
        dl = QtWidgets.QGridLayout()
        dl.setHorizontalSpacing(8)
        dl.addWidget(QtWidgets.QLabel("Delay"), 0, 0)
        self.b_delay_dn = QtWidgets.QToolButton()
        self.b_delay_dn.setText("\u2212")                 # minus sign
        self.b_delay_dn.setFixedSize(34, 26)
        self.b_delay_dn.setToolTip("Subtitles 0.25 s earlier")
        self.b_delay_up = QtWidgets.QToolButton()
        self.b_delay_up.setText("+")
        self.b_delay_up.setFixedSize(34, 26)
        self.b_delay_up.setToolTip("Subtitles 0.25 s later")
        self.l_delay = QtWidgets.QLabel("0.00 s")
        self.l_delay.setMinimumWidth(64)
        self.l_delay.setAlignment(QtCore.Qt.AlignCenter)
        self.b_delay_reset = QtWidgets.QPushButton("Reset")
        self.b_delay_reset.setFlat(True)
        dl.addWidget(self.b_delay_dn, 0, 1)
        dl.addWidget(self.l_delay, 0, 2)
        dl.addWidget(self.b_delay_up, 0, 3)
        dl.addWidget(self.b_delay_reset, 0, 4)
        dl.setColumnStretch(5, 1)
        lay.addLayout(dl)
        hint = QtWidgets.QLabel(
            "Positive shows subtitles later, negative earlier. "
            "Applies immediately.")
        hint.setStyleSheet("color:#9aa0a6;")
        lay.addWidget(hint)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        lay.addWidget(line)

        # ---- style (applies live to the app caption overlay) ----
        gl = QtWidgets.QGridLayout()
        gl.setHorizontalSpacing(10)
        gl.setVerticalSpacing(9)
        r = 0

        gl.addWidget(QtWidgets.QLabel("Font"), r, 0)
        self.cb_font = QtWidgets.QComboBox()
        self.cb_font.addItem("Default (VLC)", "")
        for fam in QtGui.QFontDatabase().families():
            self.cb_font.addItem(fam, fam)
        self._select_data(self.cb_font, self.appearance.get("font", ""))
        gl.addWidget(self.cb_font, r, 1)
        r += 1

        gl.addWidget(QtWidgets.QLabel("Size"), r, 0)
        self.sp_size = QtWidgets.QSpinBox()
        self.sp_size.setRange(0, 96)
        self.sp_size.setSuffix(" px")
        self.sp_size.setSpecialValueText("Auto (scales with video)")
        self.sp_size.setToolTip(
            "Subtitle text height at 1080p (scales with the video).\n"
            "0 = Auto: VLC sizes it from the video height.")
        self.sp_size.setValue(int(self.appearance.get("size", 0) or 0))
        gl.addWidget(self.sp_size, r, 1)
        r += 1

        gl.addWidget(QtWidgets.QLabel("Vertical position"), r, 0)
        pos_row = QtWidgets.QHBoxLayout()
        self.sl_pos = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.sl_pos.setRange(-_POS_MAX, _POS_MAX)
        self.sl_pos.setValue(int(self.appearance.get("pos_pct", 0) or 0))
        self.l_pos = QtWidgets.QLabel("0 %")
        self.l_pos.setMinimumWidth(44)
        self.sl_pos.valueChanged.connect(
            lambda v: self.l_pos.setText(f"{v:+d} %"))
        pos_row.addWidget(self.sl_pos, 1)
        pos_row.addWidget(self.l_pos)
        wrap = QtWidgets.QWidget()
        wrap.setLayout(pos_row)
        gl.addWidget(wrap, r, 1)
        r += 1

        self.ck_bar = QtWidgets.QCheckBox("Use the black bar below the video")
        self.ck_bar.setChecked(bool(self.appearance.get("prefer_bar", True)))
        self.ck_bar.setToolTip(
            "Windowed playback, letterboxed video: park the subtitles in\n"
            "the empty black bar under the picture (like a pro player)\n"
            "instead of over the picture's bottom edge. Fullscreen always\n"
            "uses the classic over-the-video placement.")
        gl.addWidget(self.ck_bar, r, 0, 1, 2)
        r += 1

        gl.addWidget(QtWidgets.QLabel("Text color"), r, 0)
        self.bt_text = self._color_btn(self.appearance.get("text_color",
                                                           "#FFFFFF"))
        gl.addWidget(self.bt_text, r, 1)
        r += 1

        self.ck_bg = QtWidgets.QCheckBox("Background box")
        self.ck_bg.setChecked(bool(self.appearance.get("bg_enabled")))
        gl.addWidget(self.ck_bg, r, 0)
        bg_row = QtWidgets.QHBoxLayout()
        self.bt_bg = self._color_btn(self.appearance.get("bg_color",
                                                         "#000000"))
        self.sl_bg = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.sl_bg.setRange(0, 100)
        self.sl_bg.setValue(int(self.appearance.get("bg_opacity", 50) or 0))
        self.l_bg = QtWidgets.QLabel(f"{self.sl_bg.value()} % opaque")
        self.l_bg.setMinimumWidth(88)
        self.sl_bg.valueChanged.connect(
            lambda v: self.l_bg.setText(f"{v} % opaque"))
        self.ck_bg.toggled.connect(self._bg_enabled)
        bg_row.addWidget(self.bt_bg)
        bg_row.addWidget(self.sl_bg, 1)
        bg_row.addWidget(self.l_bg)
        wrapb = QtWidgets.QWidget()
        wrapb.setLayout(bg_row)
        gl.addWidget(wrapb, r, 1)
        self._bg_enabled(self.ck_bg.isChecked())
        r += 1

        self.ck_out = QtWidgets.QCheckBox("Outline")
        self.ck_out.setChecked(bool(self.appearance.get("outline_enabled",
                                                        True)))
        gl.addWidget(self.ck_out, r, 0)
        out_row = QtWidgets.QHBoxLayout()
        self.bt_out = self._color_btn(self.appearance.get("outline_color",
                                                          "#000000"))
        self.sl_out = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.sl_out.setRange(1, 10)
        self.sl_out.setValue(int(self.appearance.get("outline_thickness",
                                                     4) or 4))
        self.l_out = QtWidgets.QLabel(f"thickness {self.sl_out.value()}")
        self.l_out.setMinimumWidth(88)
        self.sl_out.valueChanged.connect(
            lambda v: self.l_out.setText(f"thickness {v}"))
        self.ck_out.toggled.connect(self._out_enabled)
        out_row.addWidget(self.bt_out)
        out_row.addWidget(self.sl_out, 1)
        out_row.addWidget(self.l_out)
        wrapo = QtWidgets.QWidget()
        wrapo.setLayout(out_row)
        gl.addWidget(wrapo, r, 1)
        self._out_enabled(self.ck_out.isChecked())
        r += 1

        lay.addLayout(gl)

        note = QtWidgets.QLabel(
            "Changes apply live as you adjust — text captions "
            "(including flattened ASS/SSA tracks) are drawn by the app. "
            "Cancel reverts the style; the delay always keeps what you "
            "clicked. Image tracks (rendered by VLC) restyle with a "
            "brief restart on OK; movies resume where they were.")
        note.setStyleSheet("color:#9aa0a6;")
        note.setWordWrap(True)
        lay.addWidget(note)

        # ---- buttons ----
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self.b_reset = bb.addButton("Reset to defaults",
                                    QtWidgets.QDialogButtonBox.ResetRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        # wiring
        self.b_delay_dn.clicked.connect(lambda: self._nudge_delay(-1))
        self.b_delay_up.clicked.connect(lambda: self._nudge_delay(+1))
        self.b_delay_reset.clicked.connect(lambda: self._nudge_delay(0, True))
        self.b_reset.clicked.connect(self._reset_all)
        # live style: every control change writes the config (the caption
        # overlay repaints from it) — no OK needed between adjustments
        self.cb_font.currentIndexChanged.connect(lambda _i: self._live_apply())
        self.sp_size.valueChanged.connect(lambda _v: self._live_apply())
        self.sl_pos.valueChanged.connect(lambda _v: self._live_apply())
        self.ck_bg.toggled.connect(lambda _on: self._live_apply())
        self.sl_bg.valueChanged.connect(lambda _v: self._live_apply())
        self.ck_out.toggled.connect(lambda _on: self._live_apply())
        self.sl_out.valueChanged.connect(lambda _v: self._live_apply())
        self.ck_bar.toggled.connect(lambda _on: self._live_apply())

    # ---- helpers ----
    def _color_btn(self, hex_color: str) -> QtWidgets.QPushButton:
        b = QtWidgets.QPushButton()
        b.setFixedSize(64, 24)
        b.setCursor(QtCore.Qt.PointingHandCursor)
        b.setProperty("hex", str(hex_color) or "#FFFFFF")
        self._paint_color_btn(b)
        b.clicked.connect(lambda: self._pick_color(b))
        return b

    def _paint_color_btn(self, btn: QtWidgets.QPushButton) -> None:
        hexc = str(btn.property("hex") or "#FFFFFF")
        btn.setText(hexc.upper())
        btn.setStyleSheet(
            f"background-color:{hexc};"
            f"color:{'#000000' if self._luma(hexc) > 0.6 else '#FFFFFF'};")

    @staticmethod
    def _luma(hexc: str) -> float:
        try:
            h = hexc.lstrip("#")
            r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
            return 0.2126 * r + 0.7152 * g + 0.0722 * b
        except Exception:
            return 1.0

    def _pick_color(self, btn: QtWidgets.QPushButton) -> None:
        col = QtGui.QColor(str(btn.property("hex") or "#FFFFFF"))
        col = QtWidgets.QColorDialog.getColor(
            col, self, "Choose a color")
        if col.isValid():
            btn.setProperty("hex", col.name())
            self._paint_color_btn(btn)
            self._live_apply()

    @staticmethod
    def _select_data(combo: QtWidgets.QComboBox, data) -> None:
        idx = combo.findData(data)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _bg_enabled(self, on: bool) -> None:
        self.bt_bg.setEnabled(on)
        self.sl_bg.setEnabled(on)
        self.l_bg.setEnabled(on)

    def _out_enabled(self, on: bool) -> None:
        self.bt_out.setEnabled(on)
        self.sl_out.setEnabled(on)
        self.l_out.setEnabled(on)

    # ---- delay (instant, persists even on Cancel) ----
    def _nudge_delay(self, direction: int, reset: bool = False) -> None:
        if reset:
            self._delay_ms = 0
        else:
            self._delay_ms = max(-30000, min(30000,
                                             self._delay_ms
                                             + direction * _DELAY_STEP_MS))
        secs = self._delay_ms / 1000.0
        self.l_delay.setText(f"{secs:+.2f} s" if secs else "0.00 s")
        try:
            self._apply_delay(self._delay_ms)      # live on the player
        except Exception:  # noqa: BLE001
            pass
        self.appearance["delay_ms"] = self._delay_ms
        self.config.subtitle_appearance = self.appearance
        self.config.save()

    # ---- live style (applies on every change, saved debounced) ----
    def _collect(self) -> None:
        """Read the style widgets into the working copy."""
        self.appearance.update({
            "font": self.cb_font.currentData() or "",
            "size": int(self.sp_size.value()),
            "pos_pct": int(self.sl_pos.value()),
            "text_color": str(self.bt_text.property("hex") or "#FFFFFF"),
            "bg_enabled": bool(self.ck_bg.isChecked()),
            "bg_color": str(self.bt_bg.property("hex") or "#000000"),
            "bg_opacity": int(self.sl_bg.value()),
            "outline_enabled": bool(self.ck_out.isChecked()),
            "outline_color": str(self.bt_out.property("hex") or "#000000"),
            "outline_thickness": int(self.sl_out.value()),
            "prefer_bar": bool(self.ck_bar.isChecked()),
        })

    def _live_apply(self) -> None:
        """A visual control changed: write the config now (the caption
        overlay repaints from it via ``apply_live``) and save debounced —
        a slider drag fires this many times a second, so the JSON write
        waits for the drag to settle."""
        self._collect()
        clean = {k: self.appearance.get(k) for k in SUBTITLE_KEYS}
        self.config.subtitle_appearance = clean
        self._save_timer.start()
        if self._apply_live:
            try:
                self._apply_live(clean)
            except Exception:  # noqa: BLE001
                pass

    def _reset_all(self) -> None:
        self.cb_font.setCurrentIndex(0)
        self.sp_size.setValue(int(SUBTITLE_DEFAULTS["size"]))
        self.sl_pos.setValue(int(SUBTITLE_DEFAULTS["pos_pct"]))
        for btn, key in ((self.bt_text, "text_color"),
                         (self.bt_bg, "bg_color"),
                         (self.bt_out, "outline_color")):
            btn.setProperty("hex", SUBTITLE_DEFAULTS[key])
            self._paint_color_btn(btn)
        self.ck_bg.setChecked(SUBTITLE_DEFAULTS["bg_enabled"])
        self.sl_bg.setValue(int(SUBTITLE_DEFAULTS["bg_opacity"]))
        self.ck_out.setChecked(SUBTITLE_DEFAULTS["outline_enabled"])
        self.sl_out.setValue(int(SUBTITLE_DEFAULTS["outline_thickness"]))
        self.ck_bar.setChecked(bool(SUBTITLE_DEFAULTS["prefer_bar"]))
        self._live_apply()          # color buttons carry no change signal
        # delay too — it applies immediately, like the +/- clicks
        self._nudge_delay(0, reset=True)

    # ---- result ----
    def accept(self) -> None:
        self._collect()
        clean = {k: self.appearance.get(k) for k in SUBTITLE_KEYS}
        self._save_timer.stop()
        self.config.subtitle_appearance = clean
        self.config.save()
        super().accept()

    def reject(self) -> None:
        """Cancel: the style reverts to its dialog-open state (changes had
        applied live meanwhile — undo them live too); the delay keeps
        whatever was clicked (instant-persist, like the +/- buttons)."""
        self.appearance.update(self._open_style)
        clean = {k: self.appearance.get(k) for k in SUBTITLE_KEYS}
        self._save_timer.stop()
        self.config.subtitle_appearance = clean
        self.config.save()
        if self._apply_live:
            try:
                self._apply_live(clean)
            except Exception:  # noqa: BLE001
                pass
        super().reject()
