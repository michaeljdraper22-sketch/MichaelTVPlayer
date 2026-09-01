"""Main application window: content tabs + player + menus."""

import logging
import os

from PyQt5 import QtCore, QtGui, QtWidgets

from .. import diagnostics as diag
from .. import updater
from ..config import APP_VERSION, Config
from ..xtream import XtreamClient
from . import icons as ic
from .browsers import (
    CatchupBrowser,
    CustomTab,
    FavoritesTab,
    LiveBrowser,
    SeriesBrowser,
    VodBrowser,
)
from .countries_dialog import CountriesDialog
from .login_dialog import LoginDialog
from .player_view import PlayerView
from .worker import AsyncRunner

log = logging.getLogger("mtp")


class ChromeTabBar(QtWidgets.QTabBar):
    """Google-Chrome-style tab bar for the main content tabs.

    - Tabs SHARE the bar's width equally and shrink together as the
      panel narrows (labels elide to "…"), like Chrome's tabs.
    - Each tab stops shrinking at ``MIN_TAB_WIDTH``.  Once even that no
      longer fits, QTabBar's little left/right scroll arrows appear and
      step through the tabs — at the panel's minimum width exactly one
      tab is visible at a time.
    """

    MIN_TAB_WIDTH = 80    # px — smallest a tab gets before arrows appear
    MAX_TAB_WIDTH = 240   # px — widest a tab stretches on big windows
    # width the two scroll arrows eat when they show (estimate; keeps the
    # layout stable right at the overflow transition)
    _ARROWS_W = 44

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setElideMode(QtCore.Qt.ElideRight)
        self.setUsesScrollButtons(True)

    # ---- sizing ----
    def sizeHint(self):
        """Span the QTabWidget's full width.  QTabWidget sizes the tab BAR
        widget to the bar's sizeHint (clamped to the frame); the default
        hint only sums the tabs' minimum sizes, which would freeze the bar
        at that width forever.  Ask for the parent's current width instead
        so the bar always spans the panel (and the per-tab width can
        follow it)."""
        sh = super().sizeHint()
        tw = self.parentWidget()
        if isinstance(tw, QtWidgets.QTabWidget) and tw.width() > sh.width():
            return QtCore.QSize(tw.width(), sh.height())
        return sh

    def _available_width(self) -> int:
        """Width the tabs may divide among themselves."""
        n = max(1, self.count())
        w = self.width()
        # a QTabWidget corner widget (the hide-channels button) lives
        # INSIDE the bar row and must not be covered by tabs
        tw = self.parentWidget()
        if isinstance(tw, QtWidgets.QTabWidget):
            for corner in (tw.cornerWidget(QtCore.Qt.TopLeftCorner),
                           tw.cornerWidget(QtCore.Qt.TopRightCorner)):
                if corner is not None and corner.isVisibleTo(tw):
                    w -= corner.width() + 6
        # once the tabs cannot fit at MIN width, the scroll arrows show up
        if n * self.MIN_TAB_WIDTH > w:
            w -= self._ARROWS_W
        return max(0, w)

    def tabSizeHint(self, index):
        base = super().tabSizeHint(index)
        n = max(1, self.count())
        w = self._available_width() // n
        w = max(self.MIN_TAB_WIDTH, min(self.MAX_TAB_WIDTH, w))
        return QtCore.QSize(int(w), base.height())

    def minimumTabSizeHint(self, index):
        base = super().minimumTabSizeHint(index)
        return QtCore.QSize(self.MIN_TAB_WIDTH, base.height())

    # ---- relayout plumbing ----
    def _relayout(self):
        """QTabBar caches its tab layout. Poking it with a StyleChange
        event makes its changeEvent re-run the layout, so tabSizeHint is
        asked again for every tab at the bar's CURRENT width (i.e. tabs
        really do shrink/grow on resize)."""
        if self.count() == 0:
            return
        QtWidgets.QApplication.sendEvent(
            self, QtCore.QEvent(QtCore.QEvent.StyleChange))
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def showEvent(self, event):
        super().showEvent(event)
        self._relayout()

    def tabInserted(self, index):
        super().tabInserted(index)
        self._relayout()

    def tabRemoved(self, index):
        super().tabRemoved(index)
        self._relayout()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.setWindowTitle("MichaelTV")
        self.resize(1340, 820)
        # Must fit the splitter's children minimums (170 + 6 handle + 280):
        # a smaller window minimum made Qt's layout fight the splitter while
        # dragging, producing window trails ("shadows") and crashes.
        self.setMinimumSize(470, 320)

        self.client = XtreamClient(
            config.normalized_server(), config.username, config.password
        )

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        layout.addWidget(self.splitter)

        self.tabs = QtWidgets.QTabWidget()
        # Chrome-style tab bar: the Live TV / Movies / Series / … tabs
        # share the bar's width evenly, shrinking together (labels elide
        # to "…") as the panel narrows.  Below their minimum width the
        # little left/right scroll arrows take over and step through the
        # tabs — one at a time at the smallest sizes.
        self.tabs.setTabBar(ChromeTabBar())
        self.player_view = PlayerView(config)
        self.player_view.set_client(self.client)
        self.player_view.request_fullscreen.connect(self.toggle_fullscreen)
        self.player_view.request_toggle_panel.connect(self.toggle_zen)
        self.player_view.request_toggle_channels.connect(self.toggle_channels)
        self.player_view.request_next_channel.connect(self.play_next_channel)

        self.splitter.addWidget(self.tabs)
        self.splitter.addWidget(self.player_view)
        # Fully free-dragging splitter: no collapse "snapping", explicit min
        # widths so any custom channel-list width works.
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setOpaqueResize(True)
        self.splitter.setHandleWidth(6)
        self.tabs.setMinimumWidth(170)
        self.player_view.setMinimumWidth(280)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes(self.config.splitter_sizes or [460, 880])

        self.live_tab = LiveBrowser(config, self.client, "live")
        self.vod_tab = VodBrowser(config, self.client, "vod")
        self.series_tab = SeriesBrowser(config, self.client, "series")
        self.catchup_tab = CatchupBrowser(config, self.client, "catchup")
        self.fav_tab = FavoritesTab(config)
        self.custom_tab = CustomTab(config)

        self.tabs.addTab(self.live_tab, "Live TV")
        self.tabs.addTab(self.vod_tab, "Movies")
        self.tabs.addTab(self.series_tab, "Series")
        self.tabs.addTab(self.catchup_tab, "Catch-Up")
        self.tabs.addTab(self.fav_tab, "★ Favorites")
        self.tabs.addTab(self.custom_tab, "➕ Custom")

        # "hide channel list" button at the TOP-RIGHT of the channel bar
        self._channels_hidden = False
        self.btn_hide_channels = QtWidgets.QToolButton(self)
        self.btn_hide_channels.setIcon(ic.panel_collapse())
        self.btn_hide_channels.setIconSize(QtCore.QSize(18, 18))
        self.btn_hide_channels.setToolTip("Hide channel list (Ctrl+L)")
        self.btn_hide_channels.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_hide_channels.setStyleSheet(
            "QToolButton { background: transparent; border: none;"
            " border-radius: 4px; padding: 3px; }"
            "QToolButton:hover { background: #3a3a3a; }")
        self.btn_hide_channels.clicked.connect(self.toggle_channels)
        self.tabs.setCornerWidget(self.btn_hide_channels,
                                  QtCore.Qt.TopRightCorner)

        for tab in (self.live_tab, self.vod_tab, self.series_tab,
                    self.catchup_tab):
            tab.media_activated.connect(self.play)
            tab.favorite_changed.connect(self.fav_tab.refresh)
        self.fav_tab.media_activated.connect(self.play)
        self.custom_tab.media_activated.connect(self.play)
        self.countries_dialog = CountriesDialog(config, self.client, self)
        self.countries_dialog.changed.connect(self._on_countries_changed)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self._build_menu()

        # No permanent status bar: it took up space at the bottom of the window.

        self._setup_shortcuts()

        self._acc_runner = AsyncRunner()
        self._acc_runner.finished.connect(self._on_account)
        self._acc_runner.run(self.client.authenticate)

        self._upd_runner = AsyncRunner()
        self._upd_runner.finished.connect(self._on_update_checked)

        self.fav_tab.refresh()
        self._restore_state()

    def _on_tab_changed(self, idx):
        self.config.last_tab = idx
        self.config.save()
        try:
            from .. import feedback
            name = (self.tabs.tabText(idx) or "?").lower()
            feedback.usage("tab_" + name.split()[0].lower())
            feedback.crumb("tab -> %s" % name)
        except Exception:
            pass

    def _exit_fullscreen_only(self):
        """Esc: leave fullscreen, never re-enter it (F is the toggle).
        A live download-window selection is cancelled first — Esc is its
        dedicated escape hatch."""
        if self.player_view._win_cancel_if_active():
            return
        if getattr(self.player_view, "_fullscreen", False):
            self.toggle_fullscreen()

    def _on_countries_changed(self):
        """A country filter changed: reload every browser it can affect."""
        for tab in (self.live_tab, self.vod_tab, self.series_tab,
                    self.catchup_tab):
            tab._reload_categories()

    def _setup_shortcuts(self):
        QtWidgets.QShortcut(QtGui.QKeySequence("Space"), self,
                            activated=self.player_view.toggle_pause)
        # Left/Right go through seek_or_nudge: with the catch-up download
        # window markers active they nudge the selected gold marker (1 s,
        # Shift = 10 s) instead of seeking playback
        QtWidgets.QShortcut(QtGui.QKeySequence("Left"), self,
                            activated=lambda: self.player_view.seek_or_nudge(-10, 1))
        QtWidgets.QShortcut(QtGui.QKeySequence("Right"), self,
                            activated=lambda: self.player_view.seek_or_nudge(10, 1))
        QtWidgets.QShortcut(QtGui.QKeySequence("Shift+Left"), self,
                            activated=lambda: self.player_view.seek_or_nudge(-10, 10))
        QtWidgets.QShortcut(QtGui.QKeySequence("Shift+Right"), self,
                            activated=lambda: self.player_view.seek_or_nudge(10, 10))
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Left"), self,
                            activated=lambda: self.player_view.seek_or_nudge(-60000, 60))
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Right"), self,
                            activated=lambda: self.player_view.seek_or_nudge(60000, 60))
        QtWidgets.QShortcut(QtGui.QKeySequence("F"), self,
                            activated=self.toggle_fullscreen)
        QtWidgets.QShortcut(QtGui.QKeySequence("Escape"), self,
                            activated=self._exit_fullscreen_only)
        QtWidgets.QShortcut(QtGui.QKeySequence("F5"), self,
                            activated=self.reload_all)
        QtWidgets.QShortcut(QtGui.QKeySequence("C"), self,
                            activated=self.player_view._cycle_spu)
        QtWidgets.QShortcut(QtGui.QKeySequence("A"), self,
                            activated=self.player_view._cycle_audio)
        QtWidgets.QShortcut(QtGui.QKeySequence("N"), self,
                            activated=self.player_view._play_next_clicked)

    def _build_menu(self):
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&File")
        act_account = QtWidgets.QAction("Account…", self)
        act_account.triggered.connect(self.open_account)
        act_reload = QtWidgets.QAction("Reload all lists", self)
        act_reload.triggered.connect(self.reload_all)
        act_quit = QtWidgets.QAction("Quit", self)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_account)
        file_menu.addAction(act_reload)
        file_menu.addSeparator()
        file_menu.addAction(act_quit)

        chan_menu = menu_bar.addMenu("&Channels")
        act_add = QtWidgets.QAction("Add custom channel…", self)
        act_add.triggered.connect(self.add_custom)
        chan_menu.addAction(act_add)

        view_menu = menu_bar.addMenu("&View")
        self.act_panel = QtWidgets.QAction("Hide controls (Zen mode)", self)
        self.act_panel.setShortcut("H")
        self.act_panel.triggered.connect(self.toggle_zen)
        self.act_chan = QtWidgets.QAction("Hide channel list", self)
        self.act_chan.setShortcut("Ctrl+L")
        self.act_chan.triggered.connect(self.toggle_channels)
        act_fs = QtWidgets.QAction("Fullscreen", self)
        act_fs.setShortcut("F")
        act_fs.triggered.connect(self.toggle_fullscreen)
        view_menu.addAction(self.act_panel)
        view_menu.addAction(self.act_chan)
        view_menu.addSeparator()
        view_menu.addAction(act_fs)

        countries_menu = menu_bar.addMenu("C&ountries")
        act_countries = QtWidgets.QAction("Filter by Country…", self)
        act_countries.triggered.connect(self.open_countries)
        countries_menu.addAction(act_countries)

        play_menu = menu_bar.addMenu("&Playback")
        act_live = QtWidgets.QAction("Jump to Live", self)
        act_live.triggered.connect(self.player_view._jump_live)
        act_pause = QtWidgets.QAction("Pause / Resume", self)
        act_pause.triggered.connect(self.player_view.toggle_pause)
        act_stop = QtWidgets.QAction("Stop", self)
        act_stop.triggered.connect(self.player_view.stop)
        act_next = QtWidgets.QAction("Play next (N)", self)
        act_next.triggered.connect(self.player_view._play_next_clicked)
        play_menu.addAction(act_pause)
        play_menu.addAction(act_live)
        play_menu.addAction(act_next)
        play_menu.addAction(act_stop)

        settings_menu = menu_bar.addMenu("&Settings")
        act_buttons = QtWidgets.QAction("Playback controls…", self)
        act_buttons.triggered.connect(self.edit_control_buttons)
        act_pf = QtWidgets.QAction("Profanity filter…", self)
        act_pf.triggered.connect(self.edit_profanity)
        act_folder = QtWidgets.QAction("Recording folder…", self)
        act_folder.triggered.connect(self.choose_record_folder)
        act_dlfolder = QtWidgets.QAction("Download folder…", self)
        act_dlfolder.triggered.connect(self.choose_download_folder)
        act_dvr_window = QtWidgets.QAction("DVR buffer length…", self)
        act_dvr_window.triggered.connect(self.edit_dvr_window)
        act_delay = QtWidgets.QAction("Live delay (behind live)\u2026", self)
        act_delay.triggered.connect(self.edit_chase_delay)
        act_cache = QtWidgets.QAction("Network cache size\u2026", self)
        act_cache.triggered.connect(self.edit_cache)
        act_stremio = QtWidgets.QAction("Stremio handoff\u2026", self)
        act_stremio.triggered.connect(self.edit_stremio)
        settings_menu.addAction(act_buttons)
        settings_menu.addAction(act_pf)
        settings_menu.addAction(act_stremio)
        settings_menu.addSeparator()
        settings_menu.addAction(act_folder)
        settings_menu.addAction(act_dlfolder)
        settings_menu.addAction(act_dvr_window)
        settings_menu.addAction(act_delay)
        settings_menu.addAction(act_cache)
        settings_menu.addSeparator()
        act_diag = QtWidgets.QAction("Help improve MichaelTV\u2026", self)
        act_diag.triggered.connect(self.edit_telemetry)
        settings_menu.addAction(act_diag)
        act_update = QtWidgets.QAction("Check for updates\u2026", self)
        act_update.triggered.connect(self.check_for_updates)
        settings_menu.addAction(act_update)

        # Button-style menu-bar item (no dropdown): open the tip jar.
        act_support = menu_bar.addAction("\u2665 Support Developer")
        act_support.triggered.connect(self.support_developer)

        help_menu = menu_bar.addMenu("&Help")
        act_help = QtWidgets.QAction("About / Shortcuts", self)
        act_help.triggered.connect(self._show_help)
        help_menu.addAction(act_help)

    def support_developer(self):
        """♥ Support Developer button: open the Cash App tip link
        ($Michaeljdraper) in the user's default browser."""
        try:
            from .. import feedback
            feedback.usage("support_click")
        except Exception:  # noqa: BLE001
            pass
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl("https://cash.app/$Michaeljdraper"))

    def play(self, playable: dict, start_at: float = 0.0):
        if playable.get("kind") == "series_meta":
            # A series entry was somehow activated directly; open its episodes.
            self.series_tab._open_series(playable)
            return
        if playable.get("kind") == "catchup_channel":
            # Direct activation of an archive channel: open its program picker
            self.catchup_tab._open_picker(playable)
            return
        self.player_view.play_media(playable, start_at)
        self.config.add_recent(playable)
        self.config.data["last_channel"] = playable
        self.config.save()
        # Watching now: hand the keyboard to the player so Space (and the
        # other player keys) work even if focus sat in a browser search box.
        self.player_view.setFocus(QtCore.Qt.OtherFocusReason)

    def handle_handoff(self, args):
        """Args from an external launch — either the patched Stremio
        streaming server invoking us exactly like it invoked VLC
        (--start-time=N [--sub-file=x.srt] "<url>"), a Windows .m3u
        association launch (playlist path), or the Downloads watcher
        (plain stream URL). Parse, play, come forward. With nothing
        playable, say so — a silent no-op reads as "it worked" when
        something else happens to be playing."""
        args = [str(a) for a in (args or [])]
        try:
            log.info("handoff args: %r", [a[:300] for a in args])
        except Exception:  # noqa: BLE001
            pass
        launch = None
        try:
            from .. import stremio
            launch = stremio.parse_launch_args(args)
        except Exception:  # noqa: BLE001
            launch = None
        if launch:
            try:
                from .. import feedback
                feedback.usage("stremio_handoff")
                feedback.crumb("stremio handoff")
            except Exception:  # noqa: BLE001
                pass
            playable = stremio.playable_from_url(launch["url"])
            if launch.get("sub_file"):
                playable["sub_file"] = launch["sub_file"]
            self.play(playable, launch.get("start_at") or 0.0)
        else:
            real = [a for a in args if not a.startswith("-")]
            if real:
                log.warning("handoff: nothing playable in %r",
                            [a[:200] for a in real])
                try:
                    self.statusBar().showMessage(
                        "Stremio handoff: nothing playable in %r"
                        % real[0][:120], 8000)
                except Exception:  # noqa: BLE001
                    pass
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def edit_stremio(self):
        from src.ui.stremio_dialog import StremioDialog
        StremioDialog(self.config, self).exec_()

    def play_next_channel(self):
        """Live TV "Play next": advance to the next channel in the Live
        tab's current (filtered) list, wrapping back to the top at the end.
        The current channel must be in that list — a custom-URL stream has
        no neighbours to step through."""
        cur = self.player_view.current or {}
        sid = cur.get("stream_id") if cur.get("kind") == "live" else None
        # Step through the rows the user actually SEES — the list widget —
        # not all_items: all_items goes stale when the tab shows Recently
        # Played (playable mode keeps the old category's items), and it
        # ignores the search box's filter, so the next channel could be
        # one the displayed list doesn't contain and the blue selection
        # could never follow playback.
        lw = self.live_tab.list
        items = [lw.item(i).data(QtCore.Qt.UserRole) or {}
                 for i in range(lw.count())]
        items = [it for it in items if it.get("stream_id") is not None]
        if len(items) < 2:
            # the display has no OTHER channel to step to (e.g. a
            # one-entry Recently-Played view) — fall back to the tab's
            # full item list so "next" still zaps somewhere
            items = [it for it in (self.live_tab.all_items or [])
                     if it.get("stream_id") is not None]
        if sid is None or not items:
            self.statusBar().showMessage(
                "No channel list to advance from", 3000)
            return
        idx = next((i for i, it in enumerate(items)
                    if it.get("stream_id") == sid), None)
        if idx is None:
            self.statusBar().showMessage(
                "Current channel is not in the Live list", 3000)
            return
        nxt = items[(idx + 1) % len(items)]
        # playable-mode rows are already playables (they carry "url");
        # plain category rows are raw provider dicts needing make_playable
        playable = (nxt if nxt.get("url")
                    else self.live_tab.make_playable(nxt))
        self.play(playable)
        self._select_playing(playable)

    def _select_playing(self, playable: dict):
        """Move the blue selected row of the matching browser tab to whatever
        just started playing (Play next / autoplay next) — otherwise the old
        row keeps the highlight while a different channel/episode plays."""
        kind = playable.get("kind")
        tab = {"live": self.live_tab, "vod": self.vod_tab,
               "series": self.series_tab}.get(kind)
        if tab is None:
            return

        def _match_row():
            sid = playable.get("stream_id")
            fkey = playable.get("fav_key")
            title = playable.get("title")
            for i in range(tab.list.count()):
                data = tab.list.item(i).data(QtCore.Qt.UserRole) or {}
                if sid is not None and data.get("stream_id") == sid:
                    return i
                if fkey and data.get("fav_key") == fkey:
                    return i
                if title and data.get("title") == title:
                    return i
            return -1

        row = _match_row()
        if row < 0 and getattr(tab, "_playable_mode", False):
            # Recently-Played view: the item was added to config.recents
            # after the rows were built, so rebuild and look again —
            # otherwise the blue bar can never reach the new channel.
            tab._apply_filter(tab.search.text())
            row = _match_row()
        if row >= 0:
            tab.list.setCurrentRow(row)

    # ---- manual update (Settings ▸ Check for updates…; never auto) ----
    def check_for_updates(self):
        """User-initiated ONLY: quietly query the latest GitHub release.
        Never prompts, never nags — nothing happens unless the user is
        here, clicking this button."""
        try:
            self._upd_runner.run(updater.fetch_latest)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self, "MichaelTV — Update",
                f"Could not start the update check: {exc}")

    def _on_update_checked(self, result):
        ok, val = result
        if ok != "ok":
            logging.getLogger("mtp").error(
                "update check failed: %s", val)
            QtWidgets.QMessageBox.information(
                self, "MichaelTV — Update",
                f"Could not check for updates:\n{val}")
            return
        ver, notes, asset_url = val
        if not updater.is_newer(ver):
            QtWidgets.QMessageBox.information(
                self, "MichaelTV — Update",
                f"You are up to date (version {APP_VERSION}).")
            return
        note_txt = (notes or "").strip()
        if len(note_txt) > 800:
            note_txt = note_txt[:800] + "\n…"
        body = (f"A new version ({ver}) is available — you have "
                f"{APP_VERSION}.\n\n{note_txt}\n\n"
                "Update now? Your settings, favorites and Xtream login are "
                "kept; the app restarts itself when the update is ready.")
        btn = QtWidgets.QMessageBox.question(
            self, "MichaelTV — Update available", body)
        if btn != QtWidgets.QMessageBox.Yes:
            return
        self._download_update(asset_url, ver)

    def _download_update(self, asset_url, ver):
        prog = QtWidgets.QProgressDialog(
            f"Downloading MichaelTV {ver}\u2026", None, 0, 0, self)
        prog.setWindowTitle("MichaelTV — Update")
        prog.setWindowModality(QtCore.Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.show()
        QtWidgets.QApplication.processEvents()

        import tempfile

        def work():
            dest = os.path.join(tempfile.gettempdir(),
                                f"MichaelTV-{ver}.zip")
            return (ver, updater.download(asset_url, dest))

        self._upd_dl_runner = AsyncRunner()
        self._upd_dl_runner.finished.connect(
            lambda res: self._on_update_downloaded(res, prog))
        self._upd_dl_runner.run(work)

    def _on_update_downloaded(self, result, prog):
        prog.close()
        ok, val = result
        if ok != "ok":
            logging.getLogger("mtp").error(
                "update download failed: %s", val)
            QtWidgets.QMessageBox.warning(
                self, "MichaelTV — Update",
                f"The download failed:\n{val}")
            return
        try:
            ver, zip_path = val
            helper, _staging = updater.stage_update(zip_path)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("mtp").error(
                "update staging failed: %r", exc)
            QtWidgets.QMessageBox.warning(
                self, "MichaelTV — Update",
                f"The update could not be prepared:\n{exc}")
            return
        QtWidgets.QMessageBox.information(
            self, "MichaelTV — Update",
            "Update ready — MichaelTV will now restart with the new "
            "version.")
        updater.launch_helper(helper)
        # Hard exit on a TIMER THREAD, started BEFORE close(): the Qt
        # singleShot used here could never fire while closeEvent's VLC
        # teardown blocked the event loop, leaving a zombie app for the
        # swap helper to wait on. A daemon timer thread fires regardless.
        import threading
        t = threading.Timer(1.5, lambda: os._exit(0))
        t.daemon = True
        t.start()
        # state is persisted by closeEvent
        self.close()

    def open_account(self):
        if LoginDialog.configure(self.config, self).exec_() == QtWidgets.QDialog.Accepted:
            self.client = XtreamClient(
                self.config.normalized_server(), self.config.username, self.config.password
            )
            self.player_view.set_client(self.client)
            self.reload_all()
            self._acc_runner.run(self.client.authenticate)

    def reload_all(self):
        self.fav_tab.refresh()
        self.countries_dialog._load()
        for tab in (self.live_tab, self.vod_tab, self.series_tab,
                    self.catchup_tab):
            tab._reload_categories()

    def add_custom(self):
        self.custom_tab.add_channel_dialog(self)

    def toggle_zen(self):
        """Hide every bit of chrome so the video is as large as possible.

        Hides the menu bar, status bar and channel panel, and the control bar
        (move the cursor to the bottom of the video to bring it back
        temporarily). Click the ≡ button again (or press H) to restore.
        """
        self._zen = not getattr(self, "_zen", False)
        on = self._zen
        self.act_panel.setText("Show controls" if on else "Hide controls (Zen mode)")
        self.menuBar().setVisible(not on)
        self._apply_channels()
        self.player_view.set_zen(on)

    def toggle_channels(self):
        """Hide/show the channel list panel (corner button / Ctrl+L)."""
        if not self._channels_hidden:
            self._splitter_saved = self.splitter.sizes()
        self._channels_hidden = not self._channels_hidden
        self._apply_channels()

    def _apply_channels(self):
        """One place that decides where the channel panel + the floating
        restore chevron should be (zen mode hides both)."""
        zen = getattr(self, "_zen", False)
        hidden = self._channels_hidden
        self.tabs.setVisible(not hidden and not zen)
        self.player_view.set_panel_hidden(hidden and not zen)
        self.act_chan.setText("Show channel list" if hidden
                              else "Hide channel list")
        self.btn_hide_channels.setToolTip(
            "Show channel list (Ctrl+L)" if hidden
            else "Hide channel list (Ctrl+L)")
        if not hidden and getattr(self, "_splitter_saved", None):
            try:
                self.splitter.setSizes(self._splitter_saved)
            except Exception:  # noqa: BLE001
                pass

    def edit_dvr_window(self):
        value, ok = QtWidgets.QInputDialog.getInt(
            self, "Live TV buffer length",
            "How many minutes of live TV to keep available for rewind.\n"
            "More = more disk use while watching (deleted when you switch "
            "channels).",
            value=self.config.dvr_max_minutes, min=1, max=240, step=5,
        )
        if ok:
            self.config.dvr_max_minutes = value
            self.config.save()
            # restart the recorder for the new window if it's running
            if self.player_view.dvr and self.player_view.dvr.running:
                self.player_view._restart_recorder(
                    record=self.player_view.btn_rec.isChecked())

    def open_countries(self):
        """Open the country / region filter (filters the Live TV list)."""
        self.countries_dialog.show()
        self.countries_dialog.raise_()
        self.countries_dialog.activateWindow()

    def choose_record_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose where recordings are saved", self.config.record_folder or ""
        )
        if folder:
            self.config.record_folder = folder
            self.config.save()

    def choose_download_folder(self):
        """Settings ▸ Download folder — where catch-up window downloads and
        movie/episode downloads are saved."""
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose where downloads are saved",
            self.config.download_folder or self.config.record_folder or ""
        )
        if folder:
            self.config.download_folder = folder
            self.config.save()

    def edit_control_buttons(self):
        """Choose which playback-control buttons appear on the video."""
        names = [
            ("back60", "Rewind 60 s"), ("back10", "Rewind 10 s"),
            ("play", "Play / Pause"), ("fwd10", "Forward 10 s"),
            ("begin", "Jump to beginning"),
            ("live", "LIVE (jump to live edge)"),
            ("rec", "Record / Download"),
            ("cc", "Subtitles"),
            ("audio", "Audio tracks"),
            ("scale", "Video scaling"), ("speed", "Playback speed"),
            ("autoplay", "Autoplay next episode"),
            ("playnext", "Play next"),
            ("mute", "Mute"), ("volume", "Volume slider"),
            ("timebar", "Time bar (live rewind)"),
        ]
        current = self.config.control_buttons
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Playback controls")
        dlg.resize(360, 300)
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.addWidget(QtWidgets.QLabel(
            "Tick the buttons to show on the video overlay:"))
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(24)
        boxes = {}
        for i, (key, label) in enumerate(names):
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(bool(current.get(key, True)))
            boxes[key] = cb
            grid.addWidget(cb, i // 2, i % 2)
        wrap = QtWidgets.QWidget()
        wrap.setLayout(grid)
        lay.addWidget(wrap)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self.config.control_buttons = {
                key: cb.isChecked() for key, cb in boxes.items()
            }
            self.config.save()
            self.player_view.apply_button_visibility()

    def edit_profanity(self):
        """Settings ▸ Profanity filter… — word list, levels, timing."""
        from .profanity_dialog import ProfanityDialog
        ProfanityDialog(self.config, self.player_view.apply_profanity_settings,
                        parent=self).exec_()

    def edit_telemetry(self):
        """Settings ▸ Help improve MichaelTV… — opt-in diagnostics uploads.

        A silent on/off switch: when on, the app posts a redacted report
        (system info + the player.log tail) to GitHub whenever it hits an
        error or a playback-rescue storm, at most every 4 hours plus one
        startup heartbeat a day. Off by default, nothing is ever sent.
        """
        import threading

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Help improve MichaelTV")
        dlg.resize(560, 340)
        lay = QtWidgets.QVBoxLayout(dlg)
        cb = QtWidgets.QCheckBox(
            "Send diagnostics to help solve errors and bugs")
        cb.setChecked(self.config.telemetry_enabled)
        lay.addWidget(cb)
        info = QtWidgets.QLabel(
            "<b>When on, MichaelTV silently sends a report when it hits "
            "trouble</b> (a crash/error, or repeated playback rescues), "
            "plus one summary per day at startup — never more than one "
            "report every 4 hours.\n\n"
            "A report contains:\n"
            "\u2022 general computer info (Windows build, CPU, RAM, "
            "screens, VLC version)\n"
            "\u2022 a few playback settings (network cache, live delay)\n"
            "\u2022 the tail of the app's player.log\n\n"
            "Account details are never read. Credentials that appear in "
            "log lines (stream username/password, Windows profile paths) "
            "are automatically replaced with REDACTED. Reports are posted "
            "as issues on the project's GitHub repo and need the token "
            "below (a fine-grained GitHub token with Issues: Read and "
            "write on just that repo).")
        info.setWordWrap(True)
        lay.addWidget(info, 1)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("GitHub token:"))
        ed_token = QtWidgets.QLineEdit()
        ed_token.setPlaceholderText(
            "paste a fine-grained PAT (Issues: Read and write)")
        ed_token.setText(self.config.telemetry_token)
        row.addWidget(ed_token, 1)
        lay.addLayout(row)
        btn_row = QtWidgets.QHBoxLayout()
        btn_view = QtWidgets.QPushButton("View sent reports")
        btn_test = QtWidgets.QPushButton("Send a test report now")
        lbl_status = QtWidgets.QLabel("")
        btn_view.clicked.connect(diag.open_repo_issues)
        btn_row.addWidget(btn_view)
        btn_row.addWidget(btn_test)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)
        lay.addWidget(lbl_status)

        def _send_test():
            btn_test.setEnabled(False)
            lbl_status.setText("Sending\u2026 (up to ~30 s)")
            holder = {"text": ""}

            def work():
                holder["text"] = diag.upload_now_blocking(
                    self.config, "manual test report")

            t = threading.Thread(target=work, daemon=True)

            def poll():
                if t.is_alive():
                    QtCore.QTimer.singleShot(400, poll)
                    return
                lbl_status.setText(holder["text"])
                btn_test.setEnabled(True)

            t.start()
            QtCore.QTimer.singleShot(400, poll)

        btn_test.clicked.connect(_send_test)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self.config.telemetry_enabled = cb.isChecked()
            self.config.telemetry_token = ed_token.text()
            self.config.save()

    # ---- black title bar (Windows 10 dark / Windows 11 caption color) ----
    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_titlebar_done", False):
            self._titlebar_done = True
            QtCore.QTimer.singleShot(0, self._apply_dark_titlebar)

    def _apply_dark_titlebar(self):
        """Make the native title bar black instead of white.

        Uses the Windows DWM attributes (dark app mode on Win10 1809+,
        exact caption color on Win11). Removing the bar entirely would break
        dragging / Aero Snap, so black-on-native is the safe route; use
        fullscreen or Zen mode when you want zero chrome.
        """
        try:
            import ctypes
            hwnd = int(self.winId())
            dwm = ctypes.windll.dwmapi
            val = ctypes.c_int(1)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
                if dwm.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(val), 4) == 0:
                    break
            black = ctypes.c_uint(0x000000)
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(black), 4)  # caption
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(black), 4)  # border
        except Exception as exc:  # noqa: BLE001
            try:
                log.debug("dark titlebar not applied: %r", exc)
            except Exception:
                pass

    def edit_chase_delay(self):
        value, ok = QtWidgets.QInputDialog.getInt(
            self, "Live delay",
            "Live TV always plays this many seconds behind the live edge\n"
            "(the pause/rewind buffer and the caption cushion).\n"
            "Higher = smoother playback on bad links; lower = closer to live.",
            value=self.config.chase_delay, min=5, max=120, step=5,
        )
        if ok:
            self.config.chase_delay = value
            self.config.save()

    def edit_cache(self):
        value, ok = QtWidgets.QInputDialog.getInt(
            self, "Network cache size",
            "Network / live cache (milliseconds):\n"
            "Lower = faster channel start, more buffering on weak links.\n"
            "Higher = more stable on slow connections.\n"
            "Range: 0 – 50,000 ms.",
            value=self.config.network_caching, min=0, max=50000, step=100,
        )
        if ok:
            self.config.network_caching = value
            self.config.save()
            self.player_view.rebuild()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.player_view.set_fullscreen_mode(False)
            if not getattr(self, "_zen", False):
                self.menuBar().show()
            self._apply_channels()
            self.showNormal()
        else:
            self.menuBar().hide()
            self.tabs.hide()
            self.showFullScreen()
            self.player_view.set_fullscreen_mode(True, hide_overlay=True)

    def keyPressEvent(self, event):
        # Esc exits whatever immersive mode is active (fullscreen or zen).
        if event.key() == QtCore.Qt.Key_Escape:
            if self.isFullScreen():
                self.toggle_fullscreen()
            elif getattr(self, "_zen", False):
                self.toggle_zen()
            return
        super().keyPressEvent(event)

    # ---- Chrome-style: window shrinks => tab column shrinks too ----
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._share_shrink_with_tabs(event)

    def _share_shrink_with_tabs(self, event):
        """When the WINDOW narrows, narrow the tab column with it so the
        Live TV / Movies / … tabs get smaller as the window gets smaller
        (Chrome behavior).  The column only shrinks down to the width
        where every tab still fits; below that the tabs clamp at their
        minimum, the scroll arrows appear, and the video side gives up
        the remaining space.  GROWING the window hands all the extra
        space to the video (the column keeps its width).  Manual splitter
        drags are never overridden — this only reacts to window resizes,
        and never while the tab panel is hidden (fullscreen / zen)."""
        try:
            if self.tabs.isHidden():
                return
            old_w = event.oldSize().width()
            new_w = event.size().width()
            if old_w <= 0 or new_w <= 0 or new_w >= old_w:
                return
            sizes = self.splitter.sizes()
            if len(sizes) != 2:
                return
            panel = sizes[0]
            avail = max(0, new_w - self.splitter.handleWidth())
            # proportional share of the shrink, but never below the width
            # where all tabs still fit (or the panel minimum)
            n = self.tabs.tabBar().count() or 1
            fit_w = n * ChromeTabBar.MIN_TAB_WIDTH + 48   # corner + margins
            floor = min(panel, max(fit_w, 170))
            panel = max(floor, round(panel * new_w / old_w))
            panel = max(170, min(panel, avail - 280))
            if abs(panel - sizes[0]) >= 1:
                self.splitter.setSizes([panel, max(0, avail - panel)])
        except Exception as exc:  # noqa: BLE001
            try:
                log.debug("share_shrink failed: %r", exc)
            except Exception:
                pass

    # ---- window state persistence ----
    def _restore_state(self):
        geo = self.config.window_geometry
        if isinstance(geo, list) and len(geo) == 4:
            self.setGeometry(*geo)
        # Always open full-window.  Restoring the remembered geometry alone
        # made the app launch into a half-screen snap from an earlier
        # session; the geometry above is still applied first so restore-down
        # returns to the last windowed size/position.
        self.showMaximized()
        self.splitter.setSizes(self.config.splitter_sizes or [460, 880])
        idx = self.config.last_tab
        if 0 <= idx < self.tabs.count():
            self.tabs.setCurrentIndex(idx)

    def _save_state(self):
        if self.isFullScreen():
            state = "normal"
            geo = self.config.window_geometry
        else:
            state = "maximized" if self.isMaximized() else "normal"
            g = self.normalGeometry() if self.isMaximized() else self.geometry()
            geo = [g.x(), g.y(), g.width(), g.height()]
        self.config.window_geometry = geo
        self.config.window_state = state
        sizes = self.splitter.sizes()
        if len(sizes) == 2:
            # clamp so a restored list width is always sane for the window size
            sizes[0] = max(170, min(sizes[0], geo[2] - 300))
            sizes[1] = max(280, geo[2] - sizes[0])
        self.config.splitter_sizes = sizes
        self.config.last_tab = self.tabs.currentIndex()
        self.config.save()

    def closeEvent(self, event):
        # Ordered shutdown (full sequence in PlayerView.stop):
        #   (a)-(c) halt timers/callbacks, finalize REC, stop the display
        #           player, then recorder safe_stop + DVR buffer delete —
        #           every step guarded + logged, a failure in one never
        #           blocks the rest,
        #   (d)     only then persist state and accept the close.
        # Tearing down the window while libvlc/the recorder threads were
        # live caused exit crashes; the app must still exit quickly and
        # without a crash dialog even if a step fails.
        try:
            log.info("closeEvent: shutdown starting")
        except Exception:
            pass
        try:
            self.player_view.stop()
        except Exception as exc:  # noqa: BLE001
            try:
                log.error("closeEvent: player teardown failed: %r", exc)
            except Exception:
                pass
        try:
            self._save_state()
        except Exception as exc:  # noqa: BLE001
            try:
                log.error("closeEvent: saving state failed: %r", exc)
            except Exception:
                pass
        try:
            log.info("closeEvent: shutdown complete")
        except Exception:
            pass
        super().closeEvent(event)

    def _on_account(self, result):
        ok, val = result
        if ok == "ok":
            self.setWindowTitle("MichaelTV")
        else:
            # No status bar / popup spam — a quiet hint in the title instead.
            self.setWindowTitle("MichaelTV — account error (File > Account)")

    def _show_help(self):
        QtWidgets.QMessageBox.information(
            self, "MichaelTV — Help",
            "Click a channel/movie/episode (or press Enter) to play.\n"
            "Right-click any item for Play / Add-or-remove Favorite.\n\n"
            "Controls:\n"
            "  Space  ............ Pause / Resume\n"
            "  ← / →  ............ Seek -10s / +10s\n"
            "  ↑ / ↓  ............ Seek -60s / +60s\n"
            "  M  ................. Mute / Unmute\n"
            "  C  ................. Subtitles: Off -> English -> other tracks\n"
            "  A  ................. Audio track: Auto (English) -> tracks\n"
            "  Mouse wheel ....... Volume (over the video)\n"
            "  Double-click video  Toggle fullscreen\n"
            "  ● LIVE button ..... Jump to the live edge / end of the movie\n"
            "  F or Esc  ......... Toggle / exit fullscreen\n"
            "  H  ................. Zen mode (hide all controls)\n"
            "  F5  ................ Reload channel lists\n\n"
            "View menu:\n"
            "  Hide controls (Zen) .. Hides the menu, status bar, channel list AND the\n"
            "                        control bar so the video fills the window. Move the\n"
            "                        cursor to the bottom to reveal controls; press H or\n"
            "                        click the ≡ button to restore everything.\n"
            "  Fullscreen ......... Full screen (controls auto-hide; move cursor to the\n"
            "                       bottom to bring them back).\n\n"
            "Playback (everything runs on a SINGLE stream connection):\n"
            "  Live TV always plays through a short-term DVR buffer, a few\n"
            "  seconds behind live (Settings -> Live delay). That buys a\n"
            "  flawless pause, instant rewind buttons and the red LIVE\n"
            "  button (jump to the front of the buffer). Channels may take\n"
            "  a couple of extra seconds to start; the buffer is deleted\n"
            "  when you switch channels. Record resets to OFF on every\n"
            "  channel change.\n\n"
            "  Record (the REC button): saves the current stream to a file —\n"
            "  choose the folder once in Settings -> Recording folder. Works\n"
            "  through the same single connection; recordings are kept\n"
            "  on disk.\n\n"
            "Settings menu: recording folder, live TV buffer length, live\n"
            "delay and network cache size.\n\n"
            "The window can be made very small and snapped to halves/corners (Windows\n"
            "Snap) so you can tile it next to another player.\n\n"
            "Countries -> Filter by Country: tick the regions you want — one\n"
            "tab each for Live TV, Movies and Series; saved automatically and\n"
            "applied immediately.\n\n"
            "Movies & series: the REC button becomes a Download button (saves\n"
            "the original file to your downloads folder); scrub, jump-to-\n"
            "begin, LIVE (skip to end) and playback speed all work on a\n"
            "full file.\n\n"
            "Catch-Up: channels with a provider archive (marked with their\n"
            "archive depth). Click one and pick any recently-aired program\n"
            "to watch the recording. While it plays, the REC/DL button\n"
            "becomes the gold WINDOW button: press it once to drop two\n"
            "gold < > markers on the time bar — drag them, click the bar to\n"
            "place the nearest one, or click a marker and nudge it with\n"
            "the \u2190/\u2192 arrow keys (Shift = 10 s, Ctrl = 60 s). Press\n"
            "the gold button again to download exactly that stretch (Esc\n"
            "cancels). Downloads land in the Downloads folder (Settings \u25b8\n"
            "Download folder).\n\n"
            "Settings -> Network cache size adjusts buffering (0–50,000 ms).\n\n"
            "Your settings are saved in %APPDATA%\\MichaelTVPlayer.\n\n"
            "Bug reports: attach the log file, %APPDATA%\\MichaelTVPlayer"
            "\\player.log (recreated on every launch).",
        )

