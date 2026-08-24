"""Main application window: content tabs + player + menus."""

import logging

from PyQt5 import QtCore, QtGui, QtWidgets

from ..config import Config
from ..xtream import XtreamClient
from . import icons as ic
from .browsers import (
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
        self.fav_tab = FavoritesTab(config)
        self.custom_tab = CustomTab(config)

        self.tabs.addTab(self.live_tab, "Live TV")
        self.tabs.addTab(self.vod_tab, "Movies")
        self.tabs.addTab(self.series_tab, "Series")
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

        for tab in (self.live_tab, self.vod_tab, self.series_tab):
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

        self.fav_tab.refresh()
        self._restore_state()

    def _on_tab_changed(self, idx):
        self.config.last_tab = idx
        self.config.save()

    def _exit_fullscreen_only(self):
        """Esc: leave fullscreen, never re-enter it (F is the toggle)."""
        if getattr(self.player_view, "_fullscreen", False):
            self.toggle_fullscreen()

    def _on_countries_changed(self):
        """A country filter changed: reload every browser it can affect."""
        for tab in (self.live_tab, self.vod_tab, self.series_tab):
            tab._reload_categories()

    def _setup_shortcuts(self):
        QtWidgets.QShortcut(QtGui.QKeySequence("Space"), self,
                            activated=self.player_view.toggle_pause)
        QtWidgets.QShortcut(QtGui.QKeySequence("Left"), self,
                            activated=lambda: self.player_view.seek_relative(-10000))
        QtWidgets.QShortcut(QtGui.QKeySequence("Right"), self,
                            activated=lambda: self.player_view.seek_relative(10000))
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Left"), self,
                            activated=lambda: self.player_view.seek_relative(-60000))
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Right"), self,
                            activated=lambda: self.player_view.seek_relative(60000))
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
        play_menu.addAction(act_pause)
        play_menu.addAction(act_live)
        play_menu.addAction(act_stop)

        settings_menu = menu_bar.addMenu("&Settings")
        act_buttons = QtWidgets.QAction("Playback controls…", self)
        act_buttons.triggered.connect(self.edit_control_buttons)
        act_pf = QtWidgets.QAction("Profanity filter…", self)
        act_pf.triggered.connect(self.edit_profanity)
        act_folder = QtWidgets.QAction("Recording folder…", self)
        act_folder.triggered.connect(self.choose_record_folder)
        act_dvr_window = QtWidgets.QAction("DVR buffer length…", self)
        act_dvr_window.triggered.connect(self.edit_dvr_window)
        act_delay = QtWidgets.QAction("Live delay (behind live)\u2026", self)
        act_delay.triggered.connect(self.edit_chase_delay)
        act_cache = QtWidgets.QAction("Network cache size…", self)
        act_cache.triggered.connect(self.edit_cache)
        settings_menu.addAction(act_buttons)
        settings_menu.addAction(act_pf)
        settings_menu.addSeparator()
        settings_menu.addAction(act_folder)
        settings_menu.addAction(act_dvr_window)
        settings_menu.addAction(act_delay)
        settings_menu.addAction(act_cache)

        help_menu = menu_bar.addMenu("&Help")
        act_help = QtWidgets.QAction("About / Shortcuts", self)
        act_help.triggered.connect(self._show_help)
        help_menu.addAction(act_help)

    def play(self, playable: dict):
        if playable.get("kind") == "series_meta":
            # A series entry was somehow activated directly; open its episodes.
            self.series_tab._open_series(playable)
            return
        self.player_view.play_media(playable)
        self.config.add_recent(playable)
        self.config.data["last_channel"] = playable
        self.config.save()

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
        for tab in (self.live_tab, self.vod_tab, self.series_tab):
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
            "the original file to your recordings folder); scrub, jump-to-\n"
            "begin, LIVE (skip to end) and playback speed all work on a\n"
            "full file.\n\n"
            "Settings -> Network cache size adjusts buffering (0–50,000 ms).\n\n"
            "Your settings are saved in %APPDATA%\\MichaelTVPlayer.\n\n"
            "Bug reports: attach the log file, %APPDATA%\\MichaelTVPlayer"
            "\\player.log (recreated on every launch).",
        )

