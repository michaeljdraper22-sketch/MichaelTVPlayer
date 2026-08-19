# -*- coding: utf-8 -*-
"""Profanity filter settings — Settings ▸ Profanity filter…

The filter reads a movie/series' subtitle track in the background (VLC has
no API for the text, so a parallel ffmpeg read is used — subtitles do NOT
need to be visible) and mutes the audio during matched words. This dialog
manages the word list (with per-word match levels), the mute padding and
the sync offset.
"""

from PyQt5 import QtCore, QtWidgets

from ..config import PROFANITY_DEFAULTS
from ..profanity import DEFAULT_WORDS, LEVELS, PROFANITY_AVAILABLE


class ProfanityDialog(QtWidgets.QDialog):
    def __init__(self, config, on_saved, parent=None):
        """``on_saved()`` runs after the config is written (the PlayerView
        re-applies engine settings / restarts extraction)."""
        super().__init__(parent)
        self.config = config
        self._on_saved = on_saved
        prof = config.profanity
        self._words = [list(w) for w in
                       (prof.get("words") or [list(d) for d in DEFAULT_WORDS])]

        self.setWindowTitle("Profanity filter")
        self.setMinimumSize(460, 520)
        lay = QtWidgets.QVBoxLayout(self)

        self.ck_on = QtWidgets.QCheckBox(
            "Mute audio during profanity (movies & series)")
        self.ck_on.setChecked(bool(prof.get("enabled")))
        self.ck_on.setStyleSheet("font-weight:bold;")
        if not PROFANITY_AVAILABLE:
            self.ck_on.setEnabled(False)
            self.ck_on.setToolTip("Being reworked — see note below")
        lay.addWidget(self.ck_on)
        top_note = QtWidgets.QLabel(
            "Reads the video's subtitle track in the background — subtitles "
            "do not need to be turned on. Live TV is not covered (its "
            "subtitles are images, not text). Requires ffmpeg installed.")
        top_note.setWordWrap(True)
        top_note.setStyleSheet("color:#9aa0a6;")
        lay.addWidget(top_note)
        if not PROFANITY_AVAILABLE:
            park = QtWidgets.QLabel(
                "\u26a0\ufe0f The filter is temporarily disabled: its first "
                "engine used a second stream connection, which accounts "
                "with a one-connection limit cannot allow (it disrupted "
                "playback). A single-connection version is in the works — "
                "your word list and settings below are kept ready for it.")
            park.setWordWrap(True)
            park.setStyleSheet("color:#e0a030;")
            lay.addWidget(park)

        # ---- timing ----
        tl = QtWidgets.QGridLayout()
        tl.setHorizontalSpacing(10)
        self.sp_before = QtWidgets.QSpinBox()
        self.sp_before.setRange(0, 5000)
        self.sp_before.setSuffix(" ms")
        self.sp_before.setValue(int(prof.get("pad_before_ms", 120)))
        self.sp_after = QtWidgets.QSpinBox()
        self.sp_after.setRange(0, 5000)
        self.sp_after.setSuffix(" ms")
        self.sp_after.setValue(int(prof.get("pad_after_ms", 250)))
        self.sp_sync = QtWidgets.QSpinBox()
        self.sp_sync.setRange(-10000, 10000)
        self.sp_sync.setSuffix(" ms")
        self.sp_sync.setValue(int(prof.get("sync_ms", 0)))
        tl.addWidget(QtWidgets.QLabel("Mute before word"), 0, 0)
        tl.addWidget(self.sp_before, 0, 1)
        tl.addWidget(QtWidgets.QLabel("Mute after word"), 0, 2)
        tl.addWidget(self.sp_after, 0, 3)
        tl.addWidget(QtWidgets.QLabel("Sync offset"), 1, 0)
        tl.addWidget(self.sp_sync, 1, 1)
        tl.addWidget(QtWidgets.QLabel("+ = mutes later"), 1, 2, 1, 2)
        lay.addLayout(tl)

        # ---- word list ----
        lay.addWidget(QtWidgets.QLabel("Filtered words"))
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Word", "Match level"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 170)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        lay.addWidget(self.table, 1)
        for w in self._words:
            self._add_row(w[0], w[1])

        btns = QtWidgets.QHBoxLayout()
        b_add = QtWidgets.QPushButton("Add word\u2026")
        b_del = QtWidgets.QPushButton("Remove selected")
        b_reset = QtWidgets.QPushButton("Restore default list")
        b_add.clicked.connect(self._add_word)
        b_del.clicked.connect(self._remove_selected)
        b_reset.clicked.connect(self._reset_words)
        btns.addWidget(b_add)
        btns.addWidget(b_del)
        btns.addStretch(1)
        btns.addWidget(b_reset)
        lay.addLayout(btns)

        lvl_note = QtWidgets.QLabel(
            "Exact: 'dog' \u2192 '*** in the doghouse'   "
            "Partial: '*** in the ***house'   "
            "Whole: '*** in the ********'")
        lvl_note.setWordWrap(True)
        lvl_note.setStyleSheet("color:#9aa0a6;")
        lay.addWidget(lvl_note)

        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # ---- table handling ----
    def _add_row(self, word: str, level: str):
        r = self.table.rowCount()
        self.table.insertRow(r)
        it = QtWidgets.QTableWidgetItem(word)
        self.table.setItem(r, 0, it)
        cb = QtWidgets.QComboBox()
        cb.addItems(list(LEVELS))
        cb.setCurrentIndex(list(LEVELS).index(level)
                           if level in LEVELS else 0)
        self.table.setCellWidget(r, 1, cb)

    def _add_word(self):
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Add word", "Word to filter:", text="")
        text = text.strip().lower()
        if not ok or not text:
            return
        level, ok = QtWidgets.QInputDialog.getItem(
            self, "Add word", f"Match level for '{text}':",
            list(LEVELS), 0, False)
        if ok:
            self._add_row(text, level)

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _reset_words(self):
        self.table.setRowCount(0)
        for w in DEFAULT_WORDS:
            self._add_row(w[0], w[1])

    def _collect_words(self):
        words = []
        seen = set()
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            cb = self.table.cellWidget(r, 1)
            if it is None or cb is None:
                continue
            w = it.text().strip().lower()
            if w and w not in seen:
                seen.add(w)
                words.append([w, cb.currentText()])
        return words

    # ---- save ----
    def accept(self) -> None:
        words = self._collect_words() or [list(d) for d in DEFAULT_WORDS]
        self.config.profanity = {
            "enabled": bool(self.ck_on.isChecked())
            and PROFANITY_AVAILABLE,
            "words": words,
            "pad_before_ms": int(self.sp_before.value()),
            "pad_after_ms": int(self.sp_after.value()),
            "sync_ms": int(self.sp_sync.value()),
        }
        self.config.save()
        try:
            self._on_saved()
        except Exception:  # noqa: BLE001
            pass
        super().accept()
