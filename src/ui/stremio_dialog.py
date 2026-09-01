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

This dialog controls both, plus what happens AFTER a handoff: which
stream addons are queried for the next episode, the local Stremio
streaming server that serves torrent streams, and the preferred
resolution.
"""

from PyQt5 import QtCore, QtWidgets

from .. import fileassoc


class StremioDialog(QtWidgets.QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Stremio handoff")
        self.main_window = parent

        lay = QtWidgets.QVBoxLayout(self)

        intro = QtWidgets.QLabel(
            "In Stremio (app or web), set Settings \u25b8 Player \u25b8 "
            "\u201cPlay in external player\u201d to \u201cM3U "
            "Playlist\u201d. Opening a stream then downloads playlist.m3u "
            "\u2014 MichaelTV (while running) picks it up from Downloads "
            "and plays it straight away, then autoplays the next episode "
            "on its own. Your VLC is never touched.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        # ---- Downloads auto-play ------------------------------------
        watch_box = QtWidgets.QGroupBox("Downloads auto-play")
        watch_lay = QtWidgets.QVBoxLayout(watch_box)
        self.watch_chk = QtWidgets.QCheckBox(
            "Auto-play Stremio playlist downloads (playlist.m3u landing "
            "in the Downloads folder)")
        self.watch_chk.setChecked(config.stremio_watch_downloads)
        watch_lay.addWidget(self.watch_chk)
        self.watch_lbl = QtWidgets.QLabel("")
        self.watch_lbl.setWordWrap(True)
        watch_lay.addWidget(self.watch_lbl)
        lay.addWidget(watch_box)

        lay.addSpacing(4)

        # ---- default .m3u handler -----------------------------------
        lay.addWidget(QtWidgets.QLabel(
            "Playlist (.m3u) handling (only matters for files you open "
            "by hand):"))
        self.status_lbl = QtWidgets.QLabel("")
        self.status_lbl.setWordWrap(True)
        lay.addWidget(self.status_lbl)
        row = QtWidgets.QHBoxLayout()
        self.btn_default = QtWidgets.QPushButton("Make MichaelTV the default")
        self.btn_default.clicked.connect(self._make_default)
        self.btn_open_settings = QtWidgets.QPushButton("Windows default apps")
        self.btn_open_settings.clicked.connect(self._open_defaults)
        row.addWidget(self.btn_default)
        row.addWidget(self.btn_open_settings)
        row.addStretch(1)
        lay.addLayout(row)

        lay.addSpacing(4)

        # ---- Stremio's in-player external-player menu ---------------
        # (that menu entry is the local streaming server spawning a
        # hardcoded vlc.exe; the patch redirects it at MichaelTV and
        # relabels it)
        self.patch_lbl = QtWidgets.QLabel("")
        self.patch_lbl.setWordWrap(True)
        lay.addWidget(self.patch_lbl)
        row2 = QtWidgets.QHBoxLayout()
        self.btn_repatch = QtWidgets.QPushButton("Redirect it to MichaelTV")
        self.btn_repatch.clicked.connect(self._repatch)
        self.btn_unpatch = QtWidgets.QPushButton("Restore VLC")
        self.btn_unpatch.clicked.connect(self._unpatch)
        row2.addWidget(self.btn_repatch)
        row2.addWidget(self.btn_unpatch)
        row2.addStretch(1)
        lay.addLayout(row2)

        lay.addSpacing(8)

        # ---- next-episode sources ------------------------------------
        lay.addWidget(QtWidgets.QLabel(
            "Stream addons for autoplay-next — paste each addon\u2019s "
            "manifest URL from Stremio\u2019s Addons page (one per line). "
            "Your configured Torrentio / Debridio instances carry their "
            "debrid keys in the URL, so next episodes come back as "
            "direct debrid links:"))
        self.addons_edit = QtWidgets.QPlainTextEdit()
        self.addons_edit.setPlaceholderText(
            "https://torrentio.strem.fun/manifest/\u2039your-config\u203a"
            "/manifest.json")
        self.addons_edit.setFixedHeight(68)
        self.addons_edit.setPlainText(
            "\n".join(config.stremio_addons))
        lay.addWidget(self.addons_edit)

        res_row = QtWidgets.QHBoxLayout()
        res_row.addWidget(QtWidgets.QLabel("Preferred resolution:"))
        self.res_combo = QtWidgets.QComboBox()
        for label, val in (("Best available (2160p)", 2160),
                           ("1440p", 1440), ("1080p", 1080),
                           ("720p", 720), ("480p", 480)):
            self.res_combo.addItem(label, val)
        idx = self.res_combo.findData(config.stremio_prefer_resolution)
        self.res_combo.setCurrentIndex(idx if idx >= 0 else 2)
        res_row.addWidget(self.res_combo)
        res_row.addStretch(1)
        lay.addLayout(res_row)

        srv_row = QtWidgets.QHBoxLayout()
        srv_row.addWidget(QtWidgets.QLabel(
            "Stremio streaming server (leave as-is):"))
        self.srv_edit = QtWidgets.QLineEdit(config.stremio_server)
        self.srv_edit.setMaximumWidth(260)
        srv_row.addWidget(self.srv_edit)
        srv_row.addStretch(1)
        lay.addLayout(srv_row)

        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._refresh_watch_status()
        self._refresh_status()
        self._refresh_patch_status()

    # ---- Downloads auto-play ----

    def _refresh_watch_status(self):
        from ..watchfolder import downloads_dir
        folder = downloads_dir()
        running = bool(getattr(self.main_window, "_downloads_watcher",
                               None))
        if running and self.watch_chk.isChecked():
            self.watch_lbl.setText("\u2713 Watching %s" % folder)
        elif self.watch_chk.isChecked():
            self.watch_lbl.setText(
                "Starts with MichaelTV \u2014 keep MichaelTV running "
                "while you browse Stremio. Will watch:\n%s" % folder)
        else:
            self.watch_lbl.setText(
                "Off \u2014 Stremio playlist downloads are ignored.")

    # ---- Stremio in-player menu redirect ----

    def _refresh_patch_status(self):
        from .. import streampatch
        st = streampatch.status()
        if not st["found"]:
            self.patch_lbl.setText(
                "Stremio’s streaming server (server.js) was not found "
            "— the in-player external-player menu will keep "
            "opening VLC.")
            self.btn_repatch.setEnabled(False)
            self.btn_unpatch.setEnabled(False)
            return
        self.btn_repatch.setEnabled(True)
        self.btn_unpatch.setEnabled(st["patched"] and st["backup"])
        if st["patched"]:
            self.patch_lbl.setText(
                "✓ Stremio’s in-player menu offers “Play in "
                "MichaelTV” and it launches MichaelTV (takes effect "
                "after Stremio restarts; re-applied automatically after "
                "Stremio updates).")
        elif st["titled"] or st["backup"]:
            self.patch_lbl.setText(
                "Partly set up — redirect it to MichaelTV?")
        else:
            self.patch_lbl.setText(
                "Stremio’s in-player external-player menu currently "
                "launches VLC. Redirect it to MichaelTV?")

    def _repatch(self):
        from .. import streampatch
        streampatch.patch()
        self._refresh_patch_status()

    def _unpatch(self):
        from .. import streampatch
        streampatch.restore()
        self._refresh_patch_status()

    # ---- default handler ----

    def _refresh_status(self):
        if fileassoc.is_default():
            self.status_lbl.setText(
                "\u2713 MichaelTV is the default .m3u player \u2014 "
                "Stremio handoffs open straight into playback.")
            self.btn_default.setEnabled(False)
            return
        registered = self._registered()
        self.btn_default.setEnabled(True)
        if registered:
            self.status_lbl.setText(
                "MichaelTV appears in the .m3u \u201cOpen with\u201d menu, "
                "but is not the default. Make it the default for fully "
                "automatic handoffs (or pick \u201cAlways\u201d once in "
                "the browser\u2019s download bar).")
        else:
            self.status_lbl.setText(
                "MichaelTV is not registered for .m3u yet.")

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

    def _open_defaults(self):
        QtGui = QtWidgets.QDesktopServices
        QtGui.openUrl(QtCore.QUrl("ms-settings:defaultapps"))

    # ---- save ----

    def accept(self):
        addons = [line.strip() for line in
                  self.addons_edit.toPlainText().splitlines()
                  if line.strip()]
        self.config.stremio_addons = addons
        self.config.stremio_prefer_resolution = \
            self.res_combo.currentData() or 1080
        self.config.stremio_server = self.srv_edit.text()
        self.config.stremio_watch_downloads = \
            self.watch_chk.isChecked()
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
