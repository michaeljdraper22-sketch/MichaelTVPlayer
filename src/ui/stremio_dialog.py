"""Settings for the Stremio handoff (Settings > Stremio handoff…).

Two independent handoffs get a Stremio stream into MichaelTV:

* Stream list: with Stremio's "Play in external player" set to "M3U
  Playlist" (the only option Stremio offers on Windows), clicking a
  stream downloads a tiny playlist.m3u into the browser's Downloads
  folder. MichaelTV watches that folder and plays it the moment it
  lands — no file-association or download-bar click needed (Win11
  locks the .m3u default to the Store Media Player, so watching is the
  reliable route).
* In the Stremio player: the "external player" menu entry (which the
  server.js patch relabels "MichaelTV") asks the local Stremio
  streaming server to spawn the player directly.

Layout contract (learned the hard way): every long sentence is either
word-wrapped or lives in a tooltip — a plain QLabel/QCheckBox with a
paragraph of text makes the dialog's natural width the width of the
paragraph (6261 px in the wild), and Windows just clamps it to the
screen. One short line of visible text per control, one action button
per setup row, details on hover.
"""

from PyQt5 import QtWidgets

from .. import fileassoc

_GREEN = "#6cc36f"       # ✓ status lines
_GRAY = "#9aa0a6"        # hints / neutral status


class StremioDialog(QtWidgets.QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Stremio handoff")
        self.main_window = parent

        lay = QtWidgets.QVBoxLayout(self)
        lay.setSpacing(10)

        # word-wrapped labels' preferred-width heuristic balloons the
        # dialog wide (fatally at 300% display scaling, where the old
        # layout opened screen-wide) — pin it near its siblings' size
        self.setMinimumWidth(560)
        self.setMaximumWidth(720)

        # ---- getting streams in --------------------------------------
        src_box = QtWidgets.QGroupBox("Getting streams into MichaelTV")
        sl = QtWidgets.QVBoxLayout(src_box)
        sl.setSpacing(5)

        hint = QtWidgets.QLabel(
            "Stremio \u25b8 Settings \u25b8 Player \u25b8 "
            "\u201cPlay in external player\u201d = \u201cM3U "
            "Playlist\u201d.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:%s;" % _GRAY)
        hint.setToolTip(
            "Opening a stream in Stremio then downloads playlist.m3u, "
            "which MichaelTV (while running) picks up from Downloads "
            "and plays straight away \u2014 then autoplays the next "
            "episode on its own. Your VLC is never touched.")
        sl.addWidget(hint)

        self.watch_chk = QtWidgets.QCheckBox(
            "Auto-play playlist downloads")
        self.watch_chk.setChecked(config.stremio_watch_downloads)
        self.watch_chk.setToolTip(
            "playlist.m3u landing in the Downloads folder is played the "
            "moment it arrives (while MichaelTV is running), then "
            "autoplay-next takes over.")
        self.watch_chk.toggled.connect(self._refresh_watch_status)
        sl.addWidget(self.watch_chk)

        self.watch_lbl = self._wrap_label()
        sl.addWidget(self.watch_lbl)

        self.subs_chk = QtWidgets.QCheckBox(
            "Find subtitles online when none load")
        self.subs_chk.setChecked(config.fetch_online_subs)
        self.subs_chk.setToolTip(
            "When a Stremio / VOD / series stream has no usable text "
            "subtitle track (bitmap-only, wrong language, none at all), "
            "the player silently searches a keyless OpenSubtitles addon "
            "and renders the best match in the styled caption overlay.\n"
            "If that search finds nothing, playback is exactly as it is "
            "today. Live TV is exempt.")
        sl.addWidget(self.subs_chk)

        rows = QtWidgets.QGridLayout()
        rows.setHorizontalSpacing(10)
        rows.setVerticalSpacing(6)

        rows.addWidget(QtWidgets.QLabel("In-player menu"), 0, 0)
        self.patch_lbl = self._wrap_label()
        rows.addWidget(self.patch_lbl, 0, 1)
        self.btn_patch = QtWidgets.QPushButton()
        self.btn_patch.clicked.connect(self._toggle_patch)
        rows.addWidget(self.btn_patch, 0, 2)

        rows.addWidget(QtWidgets.QLabel(".m3u files"), 1, 0)
        self.status_lbl = self._wrap_label()
        rows.addWidget(self.status_lbl, 1, 1)
        self.btn_default = QtWidgets.QPushButton("Make default")
        self.btn_default.clicked.connect(self._make_default)
        rows.addWidget(self.btn_default, 1, 2)
        rows.setColumnStretch(1, 1)
        sl.addLayout(rows)

        lay.addWidget(src_box)

        # ---- what plays next -----------------------------------------
        pick_box = QtWidgets.QGroupBox("Next-episode stream picks")
        pl = QtWidgets.QVBoxLayout(pick_box)
        pl.setSpacing(5)

        pick_box.setToolTip(
            "Applies when MichaelTV picks the stream itself: "
            "autoplay-next, the next/previous-episode buttons and the "
            "P-key picker. The stream Stremio hands over plays as-is.")
        addons_lbl = QtWidgets.QLabel(
            "Addon priority \u2014 one manifest URL per line "
            "(line 1 is preferred)")
        addons_lbl.setWordWrap(True)
        addons_lbl.setStyleSheet("color:%s;" % _GRAY)
        addons_lbl.setToolTip(
            "Paste each addon\u2019s manifest URL from Stremio\u2019s "
            "Addons page, in priority order. Your configured Torrentio "
            "/ Debridio instances carry their debrid keys in the URL, "
            "so next episodes come back as direct debrid links. "
            "Lower lines are fallbacks \u2014 they only serve ties the "
            "addons above can\u2019t.")
        pl.addWidget(addons_lbl)

        self.addons_edit = QtWidgets.QPlainTextEdit()
        self.addons_edit.setPlaceholderText(
            "https://torrentio.strem.fun/manifest/\u2026")
        self.addons_edit.setFixedHeight(64)
        self.addons_edit.setPlainText(
            "\n".join(config.stremio_addons))
        pl.addWidget(self.addons_edit)

        imp_row = QtWidgets.QHBoxLayout()
        imp_row.setSpacing(10)
        self.import_lbl = QtWidgets.QLabel("")
        imp_row.addWidget(self.import_lbl, 1)
        self.btn_import = QtWidgets.QPushButton(
            "Import from installed Stremio")
        self.btn_import.setToolTip(
            "Reads the addon list from the desktop Stremio app on this "
            "PC and fills the box above with its stream-capable addons, "
            "in the recommended priority order \u2014 the status line "
            "names each addon with its debrid provider.\nThe lines stay "
            "fully editable afterwards: reorder them or add manual URLs "
            "any time. Nothing is imported unless you click this.")
        self.btn_import.clicked.connect(self._import_addons)
        imp_row.addWidget(self.btn_import)
        pl.addLayout(imp_row)

        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)

        self.res_combo = QtWidgets.QComboBox()
        self.res_combo.setToolTip(
            "Quality targeted when picking the next episode\u2019s "
            "stream.\n\u201cMatch current\u201d keeps the resolution "
            "you\u2019re watching;\n\u201cBest available\u201d falls "
            "4K \u2192 1080p \u2192 \u2026 \u2192 480p.")
        for label, val in (("Match current stream", "match"),
                           ("Best available", "auto"),
                           ("2160p", "2160"), ("1440p", "1440"),
                           ("1080p", "1080"), ("720p", "720"),
                           ("480p", "480")):
            self.res_combo.addItem(label, val)
        idx = self.res_combo.findData(config.stremio_resolution_pref)
        self.res_combo.setCurrentIndex(idx if idx >= 0 else 4)
        form.addRow("Resolution", self.res_combo)

        self.size_combo = QtWidgets.QComboBox()
        self.size_combo.setToolTip(
            "Streams larger than this rank lower \u2014 they are never "
            "excluded.\n\u201cAny size\u201d turns the size "
            "preference off.")
        for label, val in (("Any size", 0), ("10 GB", 10),
                           ("25 GB", 25), ("50 GB", 50),
                           ("100 GB", 100)):
            self.size_combo.addItem(label, val)
        cur = config.stremio_size_demote_gb
        idx = self.size_combo.findData(cur)
        if idx < 0:                       # a hand-edited value: keep it
            self.size_combo.addItem("%d GB" % cur, cur)
            idx = self.size_combo.count() - 1
        self.size_combo.setCurrentIndex(idx)
        form.addRow("Prefer files under", self.size_combo)

        self.srv_edit = QtWidgets.QLineEdit(config.stremio_server)
        self.srv_edit.setToolTip(
            "The local Stremio streaming server that serves torrent "
            "streams.\nLeave as-is.")
        form.addRow("Server", self.srv_edit)

        pl.addLayout(form)
        lay.addWidget(pick_box)

        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._refresh_watch_status()
        self._refresh_status()
        self._refresh_patch_status()

    # ---- helpers ----

    @staticmethod
    def _wrap_label() -> QtWidgets.QLabel:
        lbl = QtWidgets.QLabel("")
        lbl.setWordWrap(True)
        return lbl

    @staticmethod
    def _set_status(lbl: QtWidgets.QLabel, ok: bool, text: str,
                    tip: str = "") -> None:
        lbl.setText(("\u2713 " if ok else "") + text)
        lbl.setStyleSheet(
            "color:%s;" % (_GREEN if ok else _GRAY))
        lbl.setToolTip(tip)

    # ---- Downloads auto-play ----

    def _refresh_watch_status(self):
        from ..watchfolder import downloads_dir
        folder = downloads_dir()
        running = bool(getattr(self.main_window, "_downloads_watcher",
                               None))
        if not self.watch_chk.isChecked():
            self._set_status(self.watch_lbl, False,
                             "Off \u2014 playlist downloads are ignored")
        elif running:
            self._set_status(self.watch_lbl, True,
                             "Watching %s" % folder)
        else:
            self._set_status(
                self.watch_lbl, False,
                "Watches %s while MichaelTV runs" % folder,
                "Keep MichaelTV running while you browse Stremio.")

    # ---- Stremio in-player menu redirect ----

    def _refresh_patch_status(self):
        from .. import streampatch
        st = streampatch.status()
        if not st["found"]:
            self._set_status(
                self.patch_lbl, False, "Stremio server not found",
                "Stremio\u2019s streaming server (server.js) was not "
                "found \u2014 the in-player external-player menu keeps "
                "opening VLC. It appears once Stremio has run once.")
            self.btn_patch.setEnabled(False)
            self.btn_patch.setText("Redirect to MichaelTV")
            return
        self.btn_patch.setEnabled(True)
        if st["patched"]:
            self._set_status(
                self.patch_lbl, True, "Opens MichaelTV",
                "Stremio\u2019s in-player menu offers \u201cPlay in "
                "MichaelTV\u201d and it launches MichaelTV.\nTakes "
                "effect after Stremio restarts; re-applied "
                "automatically after Stremio updates.")
            self.btn_patch.setText("Restore VLC")
        else:
            partial = st["titled"] or st["backup"]
            self._set_status(
                self.patch_lbl, False,
                "Opens VLC \u2014 partly set up" if partial
                else "Opens VLC",
                "Stremio\u2019s in-player external-player menu "
                "currently launches VLC. Redirect it to MichaelTV?")
            self.btn_patch.setText("Redirect to MichaelTV")

    def _toggle_patch(self):
        from .. import streampatch
        if streampatch.status().get("patched"):
            streampatch.restore()
        else:
            streampatch.patch()
        self._refresh_patch_status()

    # ---- default .m3u handler ----

    def _refresh_status(self):
        if fileassoc.is_default():
            self._set_status(
                self.status_lbl, True, "MichaelTV is the default",
                "Stremio handoffs open straight into playback.")
            self.btn_default.setVisible(False)
            return
        registered = self._registered()
        self.btn_default.setVisible(True)
        if registered:
            self._set_status(
                self.status_lbl, False, "In \u201cOpen with\u201d menu, "
                "not default",
                "Make MichaelTV the default for fully automatic "
                "handoffs (or pick \u201cAlways\u201d once in the "
                "browser\u2019s download bar). Fallback: Windows "
                "Settings \u25b8 Apps \u25b8 Default apps.")
        else:
            self._set_status(
                self.status_lbl, False, "Not registered",
                "Only matters for .m3u files you open by hand \u2014 "
                "the Downloads watcher above needs nothing.")

    @staticmethod
    def _registered() -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Classes\%s" % fileassoc.PROGID):
                return True
        except OSError:
            return False

    def _make_default(self):
        fileassoc.register()
        if not fileassoc.try_set_default():
            # Windows 11 refuses silent switches — hand the user the
            # system's own chooser (with the "Always" checkbox) pre-
            # focused on a playlist
            fileassoc.prompt_default()
        if not fileassoc.is_default():
            QtWidgets.QMessageBox.information(
                self, "Stremio handoff",
                "MichaelTV is in the .m3u \u201cOpen with\u201d menu but "
                "not the default yet.\n\nEasiest: when Stremio downloads "
                "playlist.m3u, click it once in the browser\u2019s "
                "download bar and pick MichaelTV (\u201cAlways\u201d).\n"
                "Or right-click any .m3u \u25b8 Open with \u25b8 MichaelTV "
                "\u25b8 Always.")
        self._refresh_status()

    # ---- import from installed Stremio ----

    @staticmethod
    def _name_list(names, cap: int = 60) -> str:
        """Comma-joined addon names for the import status line — kept
        to one line (truncated with an ellipsis) so a long addon list
        never wraps the dialog taller."""
        out = ""
        for name in names:
            name = str(name or "").strip()
            if not name:
                continue
            cand = "%s, %s" % (out, name) if out else name
            if out and len(cand) > cap:
                return out + ", \u2026"
            out = cand
        return out

    def _import_addons(self):
        """The Import button: fill the addon priority box from the
        desktop Stremio app's installed addons. An explicit click only
        (never on dialog open); on any failure the box is left exactly
        as the user had it."""
        from .. import stremio_profile
        try:
            found = stremio_profile.discover_stream_addons()
        except Exception:  # noqa: BLE001 - unreadable profile etc.
            found = None
        if found is None:
            self._set_status(
                self.import_lbl, False, "Stremio not found on this PC",
                "The desktop Stremio app\u2019s addon list could not be "
                "read here (not installed, never run, or an unparsed "
                "profile format) \u2014 nothing was changed. Paste addon "
                "URLs above instead.")
            return
        if not found:
            self._set_status(
                self.import_lbl, False,
                "Stremio has no stream addons installed",
                "Stremio\u2019s addon list was read fine, but none of "
                "its addons serve streams \u2014 nothing was changed. "
                "Paste addon URLs above instead.")
            return
        ordered = stremio_profile.priority_sort(found)
        urls = [str(e.get("url") or "") for e in ordered]
        self.addons_edit.setPlainText("\n".join(u for u in urls if u))
        self._set_status(
            self.import_lbl, True,
            "Imported %d addons from Stremio (%s)"
            % (len(ordered), self._name_list(
                [e.get("name") for e in ordered])),
            "Filled in the recommended priority order (Torrentio family "
            "first, then Debridio; TorBox before Premiumize). Edit or "
            "reorder the lines freely \u2014 line 1 stays the preferred "
            "addon.")

    # ---- save ----

    def accept(self):
        addons = [line.strip() for line in
                  self.addons_edit.toPlainText().splitlines()
                  if line.strip()]
        self.config.stremio_addons = addons
        self.config.stremio_resolution_pref = \
            self.res_combo.currentData() or "1080"
        self.config.stremio_size_demote_gb = \
            self.size_combo.currentData() or 0
        self.config.stremio_server = self.srv_edit.text()
        self.config.stremio_watch_downloads = \
            self.watch_chk.isChecked()
        self.config.fetch_online_subs = self.subs_chk.isChecked()
        self.config.save()
        # Apply the watcher change live when we can reach the running
        # instance's watcher (the dialog's parent is the main window).
        win = self.main_window
        if isinstance(win, QtWidgets.QWidget):
            watcher = getattr(win, "_downloads_watcher", None)
            try:
                if self.watch_chk.isChecked():
                    if watcher is None:
                        from ..watchfolder import DownloadsWatcher
                        watcher = DownloadsWatcher(parent=win)
                        if watcher.start():
                            watcher.handoff.connect(win.handle_handoff)
                            win._downloads_watcher = watcher
                elif watcher is not None:
                    watcher.stop()
                    win._downloads_watcher = None
            except Exception:  # noqa: BLE001
                pass
        super().accept()
