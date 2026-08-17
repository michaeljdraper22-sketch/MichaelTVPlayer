# -*- coding: utf-8 -*-
"""Country / region filter dialog for Live TV, Movies and Series.

Opened from the top-level "Countries" menu. Each tab lists the countries /
groups found in that library's categories; selections are saved to the
config immediately (so they survive restarts) and the ``changed`` signal
notifies the browsers to reload with the filter applied.
"""

from PyQt5 import QtCore, QtWidgets

from ..filters import group_categories_by_country
from .worker import AsyncRunner


class _CountryPane(QtWidgets.QWidget):
    """One library's country checklist (Live TV / Movies / Series)."""

    changed = QtCore.pyqtSignal()

    def __init__(self, config, client, fetch, prefix, parent=None):
        super().__init__(parent)
        self.config = config
        self.client = client
        self._fetch = fetch
        self._prefix = prefix            # config key prefix ("" | "vod_" | …)
        self._country_cats = []

        layout = QtWidgets.QVBoxLayout(self)

        btn_row = QtWidgets.QHBoxLayout()
        b_all = QtWidgets.QPushButton("Select All")
        b_none = QtWidgets.QPushButton("Select None")
        b_reload = QtWidgets.QPushButton("⟳ Reload")
        b_all.clicked.connect(lambda: self._set_all(True))
        b_none.clicked.connect(lambda: self._set_all(False))
        b_reload.clicked.connect(self._load)
        btn_row.addWidget(b_all)
        btn_row.addWidget(b_none)
        btn_row.addStretch(1)
        btn_row.addWidget(b_reload)
        layout.addLayout(btn_row)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QtWidgets.QWidget()
        self.check_layout = QtWidgets.QVBoxLayout(self.container)
        self.check_layout.setAlignment(QtCore.Qt.AlignTop)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color:#9aa0a6;")
        layout.addWidget(self.status)

        self.runner = AsyncRunner()
        self.runner.finished.connect(self._on_loaded)
        self._load()

    # ---- config plumbing ----
    def _enabled(self):
        return set(getattr(self.config,
                           self._prefix + "enabled_countries"))

    def _set_enabled(self, countries):
        setattr(self.config, self._prefix + "enabled_countries",
                list(countries))
        setattr(self.config, self._prefix + "countries_configured", True)
        self.config.save()

    # ---- loading ----
    def _load(self):
        self.status.setText("Loading categories…")
        self._clear_layout(self.check_layout)
        self.runner.run(self._fetch)

    def _on_loaded(self, result):
        ok, val = result
        if ok != "ok":
            self.status.setText(f"Error: {val}")
            return
        self._country_cats = group_categories_by_country(val or [])
        # First time: pre-select everything so nothing disappears
        # unexpectedly.
        if not getattr(self.config,
                       self._prefix + "countries_configured"):
            self._set_enabled([c for c, _ in self._country_cats])
        self._render()

    def _clear_layout(self, lay):
        while lay.count():
            child = lay.takeAt(0)
            w = child.widget()
            if w:
                w.deleteLater()

    def _render(self):
        self._clear_layout(self.check_layout)
        enabled = self._enabled()
        for country, catlist in self._country_cats:
            cb = QtWidgets.QCheckBox(f"{country}   ({len(catlist)})")
            cb.setChecked(country in enabled)
            cb.stateChanged.connect(
                lambda state, c=country: self._toggle(c, state))
            self.check_layout.addWidget(cb)
        self._update_status()

    def _toggle(self, country, state):
        enabled = self._enabled()
        if state == QtCore.Qt.Checked:
            enabled.add(country)
        else:
            enabled.discard(country)
        self._set_enabled(enabled)
        self._update_status()
        self.changed.emit()

    def _set_all(self, on):
        if on:
            self._set_enabled([c for c, _ in self._country_cats])
        else:
            self._set_enabled([])
        self._render()
        self.changed.emit()

    def _update_status(self):
        enabled = self._enabled()
        all_countries = [c for c, _ in self._country_cats]
        shown = sum(1 for c in all_countries if c in enabled)
        self.status.setText(f"{shown} of {len(all_countries)} selected")


class CountriesDialog(QtWidgets.QDialog):
    """Countries ▸ Filter by Country: one tab per library. Every change is
    saved instantly and applied to the matching browser."""

    changed = QtCore.pyqtSignal()

    def __init__(self, config, client, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filter by Country")
        self.resize(460, 580)

        layout = QtWidgets.QVBoxLayout(self)
        info = QtWidgets.QLabel(
            "Tick the countries / regions you want to see in each library.\n"
            "Your choices are saved automatically and applied immediately.")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.tabs = QtWidgets.QTabWidget()
        panes = (
            ("Live TV", client.live_categories, ""),
            ("Movies", client.vod_categories, "vod_"),
            ("Series", client.series_categories, "series_"),
        )
        for label, fetch, prefix in panes:
            pane = _CountryPane(config, client, fetch, prefix)
            pane.changed.connect(self.changed.emit)
            self.tabs.addTab(pane, label)
        layout.addWidget(self.tabs, 1)

        close_btn = QtWidgets.QPushButton("Done")
        close_btn.clicked.connect(self.hide)
        layout.addWidget(close_btn, 0, QtCore.Qt.AlignRight)

    def _load(self):
        """Reload every pane (used by File ▸ Reload all lists)."""
        for i in range(self.tabs.count()):
            self.tabs.widget(i)._load()
