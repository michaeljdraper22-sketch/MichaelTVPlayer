# -*- coding: utf-8 -*-
"""Profanity filter settings — Settings ▸ Profanity filter…

Live TV is filtered from the channel's closed captions (read from the
always-on DVR buffer); movies & series from the file's own text subtitle
track (read by the local relay that playback is routed through — no
second connection, and subtitles do NOT need to be visible). This dialog
manages the word list (with per-word match levels), the mute padding and
the sync offset.
"""

from PyQt5 import QtCore, QtWidgets

from ..config import PROFANITY_DEFAULTS
from ..profanity import DEFAULT_WORDS, LEVELS


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
        self.setMinimumSize(460, 560)
        lay = QtWidgets.QVBoxLayout(self)

        self.ck_on = QtWidgets.QCheckBox(
            "Mute audio during profanity")
        self.ck_on.setChecked(bool(prof.get("enabled")))
        self.ck_on.setStyleSheet("font-weight:bold;")
        lay.addWidget(self.ck_on)
        top_note = QtWidgets.QLabel(
            "LIVE TV: reads the channel's CLOSED CAPTIONS from the always-on\n"
            "live buffer (playback runs your Live-delay setting behind live,\n"
            "at least 5 s; captions need the cushion).\n"
            "MOVIES & SERIES: the file's own subtitle track is read in "
            "the background (no playback delay, no second connection) "
            "and the audio is muted ahead of the dialogue.\n"
            "Subtitles do not need to be turned on. Requires CCExtractor "
            "for live TV (bundled with the app); the movies & series path "
            "needs nothing external. Image subtitles (PGS/DVB) carry no "
            "text and are not covered.")
        top_note.setWordWrap(True)
        top_note.setStyleSheet("color:#9aa0a6;")
        lay.addWidget(top_note)

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
        self.sp_lead = QtWidgets.QSpinBox()
        self.sp_lead.setRange(0, 10000)
        self.sp_lead.setSuffix(" ms")
        self.sp_lead.setValue(int(prof.get("lead_ms", 1500)))
        self.sp_sync = QtWidgets.QSpinBox()
        self.sp_sync.setRange(-10000, 10000)
        self.sp_sync.setSuffix(" ms")
        self.sp_sync.setValue(int(prof.get("sync_ms", 0)))
        tl.addWidget(QtWidgets.QLabel("Mute before word"), 0, 0)
        tl.addWidget(self.sp_before, 0, 1)
        tl.addWidget(QtWidgets.QLabel("Mute after word"), 0, 2)
        tl.addWidget(self.sp_after, 0, 3)
        tl.addWidget(QtWidgets.QLabel("Mute lead"), 1, 0)
        tl.addWidget(self.sp_lead, 1, 1)
        tl.addWidget(QtWidgets.QLabel("Sync offset"), 1, 2)
        tl.addWidget(self.sp_sync, 1, 3)
        tl.addWidget(QtWidgets.QLabel(
            "captions lag speech — lead shifts mutes earlier.\n"
            "+ sync = mutes later"), 2, 0, 1, 4)
        lay.addLayout(tl)

        self.ck_whole = QtWidgets.QCheckBox(
            "Mute the whole line — keep audio muted for as long as a "
            "filtered word is in the subtitle (instead of just around "
            "the word itself)")
        self.ck_whole.setChecked(bool(prof.get("whole_cue")))
        self.ck_whole.setToolTip(
            "Catches every word at the cost of muting more audio:\n"
            "the mute spans the subtitle's full display time.")
        lay.addWidget(self.ck_whole)

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
        b_reset_all = QtWidgets.QPushButton("Reset all settings")
        b_add.clicked.connect(self._add_word)
        b_del.clicked.connect(self._remove_selected)
        b_reset.clicked.connect(self._reset_words)
        b_reset_all.clicked.connect(self._reset_all)
        b_reset_all.setToolTip(
            "Restore every setting here to its factory default\n"
            "(enable state, timing, whole-line mode and the word list).")
        btns.addWidget(b_add)
        btns.addWidget(b_del)
        btns.addStretch(1)
        btns.addWidget(b_reset)
        btns.addWidget(b_reset_all)
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

    def _reset_all(self):
        """Every control back to factory defaults (the user can still
        Cancel — nothing is written until OK)."""
        self.ck_on.setChecked(bool(PROFANITY_DEFAULTS["enabled"]))
        self.sp_before.setValue(int(PROFANITY_DEFAULTS["pad_before_ms"]))
        self.sp_after.setValue(int(PROFANITY_DEFAULTS["pad_after_ms"]))
        self.sp_lead.setValue(int(PROFANITY_DEFAULTS["lead_ms"]))
        self.sp_sync.setValue(int(PROFANITY_DEFAULTS["sync_ms"]))
        self.ck_whole.setChecked(bool(PROFANITY_DEFAULTS["whole_cue"]))
        self._reset_words()

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
            "enabled": bool(self.ck_on.isChecked()),
            "words": words,
            "pad_before_ms": int(self.sp_before.value()),
            "pad_after_ms": int(self.sp_after.value()),
            "sync_ms": int(self.sp_sync.value()),
            "lead_ms": int(self.sp_lead.value()),
            "whole_cue": bool(self.ck_whole.isChecked()),
        }
        self.config.save()
        try:
            self._on_saved()
        except Exception:  # noqa: BLE001
            pass
        super().accept()
