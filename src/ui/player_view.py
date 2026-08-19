"""Video surface, overlay info, on-video playback controls and DVR rewind."""

import html
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse

from PyQt5 import QtCore, QtGui, QtWidgets

from ..dvr import VlcRecorder
from ..player import VLCPlayer, subtitle_instance_args, USER_AGENT
from .. import profanity as prof_mod
from ..live_cc import CCSource, find_ccextractor
from . import icons as ic
from .worker import AsyncRunner, FileDownloader

log = logging.getLogger("mtp")


def _decode(text: str) -> str:
    """Xtream EPG strings are often URL/HTML encoded."""
    if not text:
        return ""
    try:
        text = urllib.parse.unquote(text)
    except Exception:
        pass
    return html.unescape(text)


def _fmt(ms: int) -> str:
    if ms is None or ms < 0:
        return "--:--"
    total = int(ms) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# Chase-mode safety margin: never seek closer than this to the buffer's write
# position (VLC stalls when it runs into the end of a still-growing file), and
# the minimum spacing between watchdog reopens of the buffer.
_CHASE_SAFETY_S = 3.0
_REOPEN_COOLDOWN_S = 5.0

# Playback speeds offered by the speed button (DVR mode only).
# Capped at 4x: VLC mutes the audio output entirely above ~4x playback
# speed ("fast forward 5x goes silent").
_SPEEDS = (0.125, 0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)

# Window flags for the on-video overlay layer (see PlayerView.__init__):
# ToolTip = frameless, no taskbar entry, always above its owner window and
# never steals focus from it (keyboard shortcuts keep working while the
# on-video controls are clickable).
_OVERLAY_WIN_FLAGS = QtCore.Qt.ToolTip | QtCore.Qt.FramelessWindowHint
if hasattr(QtCore.Qt, "WindowDoesNotAcceptFocus"):   # Qt >= 5.10
    _OVERLAY_WIN_FLAGS |= QtCore.Qt.WindowDoesNotAcceptFocus


_OVERLAY_QSS = """
#infoOverlay { background-color: rgba(0,0,0,165); border-radius: 8px; }
#infoTitle { color: #ffffff; font-weight: 600; }
#infoEpg { color: #c8c8c8; }
/* All on-video overlays live in ONE translucent top-level window (see
   PlayerView.__init__): the video surface is native and libvlc draws
   through its own child HWND, so sibling widgets could never reliably
   rise above the video.  Controls paint NOTHING at rest — no background
   plates at all — so the white glyphs float directly on the video.  (The
   old rgba(0,0,0,1) "hit plates" rendered as solid black boxes on some
   Windows setups.)  Buttons stay clickable: Qt routes mouse hits by child
   geometry, not by painted pixels. */
#ovButton { background: transparent; border: none; border-radius: 5px;
            color: #ffffff; font-size: 13px; }
#ovButton:hover { background-color: rgba(255,255,255,45); }
#ovButton:pressed { background-color: rgba(255,255,255,95); }

/* ---- on-video playback controls (float over the video, no box) ---- */
#ctlOverlay { background: transparent; }
#ctlOverlay QWidget { background: transparent; }
#ctlOverlay QToolButton { background: transparent; border: none;
                          border-radius: 6px; }
#ctlOverlay QToolButton:hover { background-color: rgba(255,255,255,45); }
#ctlOverlay QToolButton:pressed { background-color: rgba(255,255,255,95); }
#ctlSep { color: rgba(255,255,255,70); background: transparent; }
#ctlTimeLabel { color: #ffffff; background: transparent; font-size: 12px;
                font-weight: 600; }
#ctlOverlay QSlider { background: transparent; }

/* DVR start-up progress pill (buffer filling before chase playback) */
#ovStatus { background-color: rgba(0,0,0,165); color: #ffffff;
            border-radius: 8px; padding: 8px 16px; font-size: 13px;
            font-weight: 600; }
#ctlOverlay QSlider::groove:horizontal { background: rgba(255,255,255,80);
                                         height: 5px; border-radius: 2px; }
#ctlOverlay QSlider::sub-page:horizontal { background: rgba(255,255,255,210);
                                           border-radius: 2px; }
#ctlOverlay QSlider::handle:horizontal { background: #ffffff; width: 14px;
                                         height: 14px; margin: -5px 0;
                                         border-radius: 7px; }
#ctlOverlay QSlider::handle:horizontal:hover { background: #ffffff;
                                                width: 16px; height: 16px;
                                                margin: -6px 0;
                                                border-radius: 8px; }
#ctlOverlay QSlider::handle:horizontal:pressed { background: #ffffff;
                                                  width: 16px; height: 16px;
                                                  margin: -6px 0;
                                                  border-radius: 8px; }
QMenu#ctlMenu { background-color: rgba(178,190,181,242); color: #17181a;
                border-radius: 8px; padding: 5px; }
QMenu#ctlMenu::item { padding: 5px 24px; border-radius: 5px; }
QMenu#ctlMenu::item:selected { background-color: rgba(0,0,0,45); }
QMenu#ctlMenu::item:disabled { color: rgba(23,24,26,110); }
QMenu#ctlMenu::separator { height: 1px; background: rgba(0,0,0,40);
                           margin: 4px 8px; }
"""


class JumpSlider(QtWidgets.QSlider):
    """QSlider where a CLICK anywhere on the groove jumps straight to that
    point (standard QSlider only page-steps). A click also enters drag
    mode, so you can keep holding and fine-tune."""

    def mousePressEvent(self, ev):
        if (ev.button() == QtCore.Qt.LeftButton
                and self.orientation() == QtCore.Qt.Horizontal):
            st = self.style()
            handle = st.pixelMetric(QtWidgets.QStyle.PM_SliderLength,
                                    None, self)
            span = max(1, self.width() - handle)
            x = ev.pos().x() - handle // 2
            frac = min(1.0, max(0.0, x / span))
            val = self.minimum() + round(
                (self.maximum() - self.minimum()) * frac)
            val = max(self.minimum(), min(self.maximum(), val))
            self.blockSignals(True)
            self.setSliderPosition(val)
            self.blockSignals(False)
            self.sliderMoved.emit(val)   # tell the view a user drag began
            # Hand the press to QSlider AFTER moving the handle under the
            # cursor: that arms Qt's internal drag state, so (a) holding the
            # button keeps dragging from the new position and — crucially —
            # (b) the release actually emits sliderReleased. Without this,
            # a click NEVER completed: the seek never ran and the view's
            # _seeking flag stayed True forever, freezing the timestamps.
            super().mousePressEvent(ev)
            ev.accept()
            return
        super().mousePressEvent(ev)


class VideoSurface(QtWidgets.QWidget):
    """Native render target handed to libvlc. Forwards mouse/wheel gestures."""

    double_clicked = QtCore.pyqtSignal()
    wheel_changed = QtCore.pyqtSignal(int)
    hovered = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_NativeWindow, True)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QtGui.QPalette.Window, QtCore.Qt.black)
        self.setPalette(pal)
        self.setMinimumSize(160, 90)
        self.setMouseTracking(True)

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()

    def wheelEvent(self, event):
        self.wheel_changed.emit(event.angleDelta().y())

    def mouseMoveEvent(self, event):
        self.hovered.emit()
        super().mouseMoveEvent(event)


class InfoOverlay(QtWidgets.QWidget):
    """Semi-transparent now/next overlay shown over the video."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("infoOverlay")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)
        self.title = QtWidgets.QLabel("")
        self.title.setObjectName("infoTitle")
        self.epg = QtWidgets.QLabel("")
        self.epg.setObjectName("infoEpg")
        self.epg.setWordWrap(True)
        lay.addWidget(self.title)
        lay.addWidget(self.epg)
        self.adjustSize()
        # start hidden: children of the (initially hidden) overlay window are
        # "visible by default" in Qt, so showing the window would otherwise
        # pop an empty banner chip over the video
        self.hide()
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)

    def set_info(self, title: str, epg: str = ""):
        self.title.setText(title)
        self.epg.setText(epg)
        self.epg.setVisible(bool(epg))
        self.adjustSize()


class PlayerView(QtWidgets.QWidget):
    request_fullscreen = QtCore.pyqtSignal()
    request_toggle_panel = QtCore.pyqtSignal()
    request_toggle_channels = QtCore.pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.client = None
        self.current = None
        self._seeking = False
        self._fullscreen = False
        self._zen = False
        self._last_epg = ""
        self.dvr = None
        self._rec_path = None
        self._mode = "live"        # "live" (direct stream) | "chase" (watch buffer)
        self._chase_paused = False
        self._dvr_t0 = None        # wall clock of current DVR session (logs/wait)
        self._session = 0          # generation counter: invalidates deferred callbacks
        self._closing = False      # stop() ran: no VLC calls from timers after teardown
        self._attached = False     # video surface bound to the VLC player yet?
        self._attach_done = False  # attach finished (ok or gave up) — unblocks playback
        self._pending_media = None # play_media() queued before attach finished
        self._dvr_base = 0.0       # content-seconds written by earlier recorder runs
        self._dvr_first_data = None  # wall clock when current run first wrote data
        self._dvr_content_s = 0.0  # CONFIRMED content-seconds this run (see _note_dvr_data)
        self._dvr_size = -1        # last seen buffer file size (drift guard)
        self._dvr_tick_t = None    # wall clock of the last _note_dvr_data sample
        self._dvr_last_growth = None  # wall clock of the last growth sighting
        self._stall_ticks = 0      # consecutive not-playing ticks (watchdog)
        self._last_reopen = 0.0    # cooldown for frontier reopens
        self._chase_started = False  # chase playback actually began once
        self._last_slider_max = -1
        self._rate = 1.0              # current playback speed (DVR/VOD)
        self._vid_s = 0.0             # TRACKED playback position in the
                                       # buffer/content (VLC timestamps can be
                                       # garbage broadcast PTS on these
                                       # streams — see _tick)
        self._last_raw = None         # previous raw VLC time (chase/VOD tracker)
        self._tick_t = None           # wall clock of the previous _tick
        self._live_paused = False     # paused in plain LIVE mode (timeshift)
        self._scale_mode = config.scale_mode   # fit | stretch | crop
        self._downloading = False     # a VOD download is in flight
        self._compact_level = -1      # control-row compaction step (see _fit_ctl)
        self._compact_hidden = set()  # buttons hidden by compaction
        self._in_fit_ctl = False      # _fit_ctl re-entrancy guard
        self._was_playing = False     # for the audio re-apply on transitions
        self._scrub_on = False        # scrubber row currently shown (VOD/chase)
        self._popup_open = False      # a ctl popup menu is open (don't hide)
        self._spu_want = -1           # DESIRED subtitle track id (-1 = off)
        self._spu_name = ""           # its name — re-matched after media opens
        self._spu_ui = None           # (enabled, on, name) last painted on btn_cc
        # profanity filter (live TV: captions from the DVR buffer + engine)
        self._filter_extractor = None
        self._cc_source = None        # live closed-caption reader
        self._filter_gen = 0          # session guard for probe callbacks
        self.runner = AsyncRunner()
        self.runner.finished.connect(self._on_epg)
        self.vlc = VLCPlayer(
            timeshift=config.timeshift, volume=config.volume,
            network_caching=config.network_caching,
            sub_args=subtitle_instance_args(config.subtitle_appearance),
            spu_delay_ms=int(config.subtitle_appearance.get("delay_ms", 0)
                             or 0))
        # the freetype args this running VLC instance was BUILT with — used
        # to tell the user when a style change needs a restart to apply
        self._sub_args_built = subtitle_instance_args(
            config.subtitle_appearance)
        self._filter_engine = prof_mod.ProfanityEngine(self.vlc)
        self._prof_runner = AsyncRunner()
        self._prof_runner.finished.connect(self._on_prof_probe)
        self._filter_at = 0.0        # -ss point of the running extraction
        self._filter_timer = QtCore.QTimer(self)
        self._filter_timer.setInterval(100)
        self._filter_timer.timeout.connect(self._filter_tick)
        self._apply_profanity_config()

        app = QtWidgets.QApplication.instance()
        app.setStyleSheet(app.styleSheet() + _OVERLAY_QSS)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.surface = VideoSurface()
        root.addWidget(self.surface, 1)

        # ALL on-video overlays (controls, corner buttons, info banner,
        # pill) live in this ONE frameless translucent window that
        # exactly covers the video surface and follows it around.  A
        # separate top-level is the only reliable way to float controls
        # over the video: the video surface is a native window and libvlc
        # draws through its own child HWND, so ordinary sibling widgets can
        # never stack above it — Qt used to promote them to native windows
        # on raise(), which painted an opaque gray/black box behind the
        # controls.  True per-pixel alpha also means fully transparent
        # areas let clicks fall through to the video (so double-click
        # fullscreen keeps working everywhere except on the controls).
        self.overlay = QtWidgets.QWidget(self, _OVERLAY_WIN_FLAGS)
        self.overlay.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.overlay.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.overlay.setFocusPolicy(QtCore.Qt.NoFocus)

        # overlays (children of the overlay window, positioned over the video)
        self.info_overlay = InfoOverlay(self.overlay)
        self._btn_panel = QtWidgets.QPushButton(self.overlay)
        self._btn_ovfs = QtWidgets.QPushButton(self.overlay)
        for b in (self._btn_panel, self._btn_ovfs):
            b.setObjectName("ovButton")
            b.setFixedSize(34, 28)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.setIconSize(QtCore.QSize(18, 18))
        self._btn_panel.setIcon(ic.menu())
        self._btn_panel.setToolTip("Zen mode (H) — hide all controls")
        self._btn_ovfs.setIcon(ic.fullscreen())
        self._btn_ovfs.setToolTip("Fullscreen (F)")
        self._btn_panel.clicked.connect(self.request_toggle_panel.emit)
        self._btn_ovfs.clicked.connect(self.request_fullscreen.emit)

        # floating "show channel list" chevron (only while the panel is
        # hidden — MainWindow drives that via set_panel_hidden())
        self._panel_hidden = False
        self._btn_showpanel = QtWidgets.QPushButton(self.overlay)
        self._btn_showpanel.setObjectName("ovButton")
        self._btn_showpanel.setIcon(ic.panel_expand())
        self._btn_showpanel.setIconSize(QtCore.QSize(18, 18))
        self._btn_showpanel.setFixedSize(30, 48)
        self._btn_showpanel.setToolTip("Show channel list (Ctrl+L)")
        self._btn_showpanel.setCursor(QtCore.Qt.PointingHandCursor)
        self._btn_showpanel.setFocusPolicy(QtCore.Qt.NoFocus)
        self._btn_showpanel.hide()
        self._btn_showpanel.clicked.connect(self.request_toggle_channels.emit)

        # transparent on-video playback controls (bottom of the video)
        self.ctl = QtWidgets.QWidget(self.overlay)
        self.ctl.setObjectName("ctlOverlay")
        ctl_lay = QtWidgets.QVBoxLayout(self.ctl)
        ctl_lay.setContentsMargins(14, 6, 14, 6)
        ctl_lay.setSpacing(4)

        def ctl_btn(icon, tip, checkable=False):
            b = QtWidgets.QToolButton()
            b.setIcon(icon)
            b.setIconSize(QtCore.QSize(24, 24))
            b.setFixedSize(34, 30)
            b.setToolTip(tip)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.setCheckable(checkable)
            b.setAutoRaise(True)
            return b

        def ctl_sep():
            s = QtWidgets.QFrame()
            s.setObjectName("ctlSep")
            s.setFrameShape(QtWidgets.QFrame.VLine)
            s.setFixedWidth(1)
            return s

        # time scrubber row (DVR / Record): current time - slider - total
        self.scrub_row = QtWidgets.QWidget()
        sl = QtWidgets.QHBoxLayout(self.scrub_row)
        sl.setContentsMargins(2, 0, 2, 0)
        sl.setSpacing(8)
        self.time_left = QtWidgets.QLabel("0:00")
        self.time_left.setObjectName("ctlTimeLabel")
        self.time_left.setMinimumWidth(44)
        self.time_left.setAlignment(QtCore.Qt.AlignRight |
                                    QtCore.Qt.AlignVCenter)
        self.slider = JumpSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setMinimumWidth(180)
        self.slider.setFocusPolicy(QtCore.Qt.NoFocus)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.time_right = QtWidgets.QLabel("0:00")
        self.time_right.setObjectName("ctlTimeLabel")
        self.time_right.setMinimumWidth(44)
        self.time_right.setAlignment(QtCore.Qt.AlignLeft |
                                     QtCore.Qt.AlignVCenter)
        sl.addWidget(self.time_left)
        sl.addWidget(self.slider, 1)
        sl.addWidget(self.time_right)
        ctl_lay.addWidget(self.scrub_row)
        self.scrub_row.hide()

        self.btn_back60 = ctl_btn(ic.rewind60(), "Rewind 60 seconds")
        self.btn_back10 = ctl_btn(ic.rewind10(), "Rewind 10 seconds")
        self.btn_play = ctl_btn(ic.play(), "Play / Pause (Space)")
        self.btn_fwd10 = ctl_btn(ic.fwd10(), "Forward 10 seconds")
        self.sep1 = ctl_sep()
        self.btn_begin = ctl_btn(ic.begin(),
                                 "Jump to the beginning of the stream")
        self.btn_live = ctl_btn(ic.live(), "Jump to the live edge "
                                           "(from pause or rewind)")
        self.btn_dvr = ctl_btn(
            ic.dvr(False),
            "DVR mode: record this channel so you can pause and rewind it "
            "(plays a few seconds behind live; also runs the profanity "
            "filter on caption channels)", checkable=True)
        # replaces the DVR button while a movie / series episode plays (the
        # file is already fully seekable — there is nothing to timeshift)
        self.btn_dl = ctl_btn(ic.download(),
                              "Download this video to the recordings folder")
        self.btn_rec = ctl_btn(ic.rec(False),
                               "Record this channel to a file "
                               "(Settings > Recording folder)", checkable=True)
        self.sep2 = ctl_sep()
        # subtitles: opens the track menu (enabled once the stream's
        # subtitle tracks are discovered — movies/series almost always carry
        # SRT language tracks, live channels occasionally carry DVB ones)
        self.btn_cc = ctl_btn(ic.cc(False), "Subtitles (C)")
        self.btn_scale = ctl_btn(ic.scale(), "Video scaling "
                                             "(fit / stretch / crop)")
        # a touch wider than the rest: the scale glyph is wide and its hit
        # area felt imprecise at the standard 34 px
        self.btn_scale.setFixedSize(42, 30)
        self.btn_speed = ctl_btn(ic.speed(), "Playback speed "
                                              "(DVR mode, movies & series)")
        self.sep3 = ctl_sep()
        self.btn_mute = ctl_btn(ic.volume(True), "Mute (M)", checkable=True)
        # JumpSlider = click anywhere on the bar to set the volume directly
        self.vol_slider = JumpSlider(QtCore.Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(config.volume)
        self.vol_slider.setMinimumWidth(48)
        self.vol_slider.setFixedWidth(84)
        self.vol_slider.setToolTip("Volume")
        self.vol_slider.setFocusPolicy(QtCore.Qt.NoFocus)

        row = QtWidgets.QWidget()
        self.ctl_row = row
        rl = QtWidgets.QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        for w in (self.btn_back60, self.btn_back10, self.btn_play,
                  self.btn_fwd10, self.sep1, self.btn_begin, self.btn_live,
                  self.btn_dvr, self.btn_dl, self.btn_rec, self.sep2,
                  self.btn_cc, self.btn_scale, self.btn_speed, self.sep3,
                  self.btn_mute, self.vol_slider):
            rl.addWidget(w)
        ctl_lay.addWidget(row)
        # rewinds / jump buttons / speed work in DVR (chase) mode and for
        # movies / series episodes (the whole file is already available)
        for b in (self.btn_back60, self.btn_back10, self.btn_fwd10,
                  self.btn_begin, self.btn_live, self.btn_speed,
                  self.btn_cc):
            b.setEnabled(False)

        # DVR start-up pill ("DVR 12s / 20s buffered…"), centered on the
        # video while the chase buffer fills — the screen would otherwise
        # be blank and look broken during the fill.
        self._dvr_status = QtWidgets.QLabel(self.overlay)
        self._dvr_status.setObjectName("ovStatus")
        self._dvr_status.setAlignment(QtCore.Qt.AlignCenter)
        self._dvr_status.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents,
                                      True)
        self._dvr_status.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self._dvr_status.hide()
        # The overlay window is a ToolTip-style top-level: Windows keeps it
        # above EVERYTHING (other apps included) even when the main window
        # is minimized or buried. Suppress it whenever the app loses focus,
        # so controls/captions can never float over other apps.
        self._overlay_suppressed = False
        QtWidgets.QApplication.instance().focusChanged.connect(
            self._on_focus_changed)

        # wiring
        self.btn_back60.clicked.connect(lambda: self._seek_ms(-60000))
        self.btn_back10.clicked.connect(lambda: self._seek_ms(-10000))
        self.btn_fwd10.clicked.connect(lambda: self._seek_ms(10000))
        self.btn_play.clicked.connect(self._toggle_pause)
        self.btn_begin.clicked.connect(self._jump_begin)
        self.btn_live.clicked.connect(self._jump_live)
        self.btn_cc.clicked.connect(self._subs_menu)
        self.btn_scale.clicked.connect(self._scale_menu)
        self.btn_speed.clicked.connect(self._speed_menu)
        self.btn_mute.toggled.connect(self._on_mute)
        self.btn_dvr.toggled.connect(self._on_dvr_toggled)
        self.btn_dl.clicked.connect(self._start_download)
        self.btn_rec.toggled.connect(self._on_rec_toggled)
        self.vol_slider.valueChanged.connect(self._on_volume)
        # sliderMoved too: a plain CLICK on the groove sets the position
        # with signals blocked, so valueChanged alone would miss it.
        self.vol_slider.sliderMoved.connect(self._on_volume)
        self.vol_slider.sliderReleased.connect(self._on_volume_released)
        self.surface.double_clicked.connect(self.request_fullscreen.emit)
        self.surface.wheel_changed.connect(self._on_wheel)
        self.surface.hovered.connect(self._on_hover)

        # timers
        self.hide_timer = QtCore.QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(4000)   # controls + cursor auto-hide
        self.hide_timer.timeout.connect(self._sleep)
        self.info_timer = QtCore.QTimer(self)
        self.info_timer.setSingleShot(True)
        self.info_timer.setInterval(4500)
        self.info_timer.timeout.connect(self.hide_info)
        # Debounced video-size apply: calling into libvlc from EVERY
        # resizeEvent blocked the GUI thread (round-trip to VLC's video
        # output thread), which is what left "shadow window" trails
        # behind the window while drag-resizing.  The timer restarts on
        # each resize, so a continuous drag applies the scale exactly
        # once, shortly after it ends.
        self._scale_timer = QtCore.QTimer(self)
        self._scale_timer.setSingleShot(True)
        self._scale_timer.setInterval(120)
        self._scale_timer.timeout.connect(self._apply_scale)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        # debounced volume persistence (wheel/click changes save shortly
        # after the last change instead of only on slider release)
        self._vol_save_timer = QtCore.QTimer(self)
        self._vol_save_timer.setSingleShot(True)
        self._vol_save_timer.setInterval(600)
        self._vol_save_timer.timeout.connect(self._save_volume)

        # Reliable "cursor moved over the player" detection: the video is a
        # NATIVE window and VLC can swallow mouse events, so plain Qt
        # mouseMove delivery is not dependable — poll the global cursor
        # position instead (cheap) and wake on any movement inside the view.
        self._last_cursor = None
        self.cursor_timer = QtCore.QTimer(self)
        self.cursor_timer.setInterval(100)
        self.cursor_timer.timeout.connect(self._poll_cursor)
        self.cursor_timer.start()

        QtWidgets.QApplication.instance().installEventFilter(self)
        QtCore.QTimer.singleShot(300, self._attach_vlc)
        self.setMouseTracking(True)
        self._apply_button_visibility()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_overlays()
        # Video aspect/crop is re-applied debounced (see _scale_timer):
        # never block the GUI thread with libvlc calls mid-drag.
        self._scale_timer.start()

    def _layout_overlays(self):
        g = self.surface.geometry()
        # keep the overlay window glued to the video surface (the surface
        # sits in the main window's layout; the overlay is a separate window)
        try:
            top = self.surface.mapToGlobal(QtCore.QPoint(0, 0))
            orect = QtCore.QRect(top, g.size())
            if orect != self.overlay.geometry():
                self.overlay.setGeometry(orect)
        except Exception:  # noqa: BLE001
            pass
        m = 8
        self.info_overlay.move(g.left() + m, g.top() + m)
        self.info_overlay.raise_()
        x = g.right() - m
        for b in (self._btn_ovfs, self._btn_panel):
            x -= (b.width() + 4)
            b.move(x, g.top() + m)
            b.raise_()
        # floating restore-channels chevron: left edge, vertically centered
        self._btn_showpanel.move(g.left() + 6,
                                 g.top() + (g.height()
                                            - self._btn_showpanel.height()) // 2)
        if self._btn_showpanel.isVisible():
            self._btn_showpanel.raise_()
        # controls overlay: bottom-center of the video
        if g.width() > 40 and g.height() > 30:
            self._fit_ctl(g.width() - 16)   # compact to fit narrow windows
            sh = self.ctl.sizeHint()
            w = min(g.width() - 16, sh.width())
            h = sh.height()
            if w != self.ctl.width() or h != self.ctl.height():
                self.ctl.resize(w, h)
            self.ctl.move(g.left() + (g.width() - w) // 2,
                          g.bottom() - h - 10)
            self.ctl.raise_()
        # DVR start-up pill: centered on the video
        if self._dvr_status.isVisible():
            ss = self._dvr_status.sizeHint()
            self._dvr_status.resize(ss)
            self._dvr_status.move(g.left() + (g.width() - ss.width()) // 2,
                                  g.top() + (g.height() - ss.height()) // 2)
            self._dvr_status.raise_()

    def set_client(self, client):
        self.client = client

    # ---- content kinds ----
    def _is_vod(self) -> bool:
        """A movie or series episode: the whole file already exists, so it
        is seekable/scrubbable without any DVR machinery."""
        return bool(self.current
                    and self.current.get("kind") in ("vod", "series"))

    def _is_dvrable(self) -> bool:
        """DVR/timeshift only makes sense for a live stream."""
        return bool(self.current and self.current.get("kind") == "live")

    # ---- adaptive control row (half/quarter-screen windows) ----
    def _fit_ctl(self, avail: int):
        """Progressively compact the control row until it fits ``avail`` px:
        tighter spacing, then no separators, then a shorter volume slider,
        then hiding the least-important seek buttons. Full layout is kept
        whenever there is room for it."""
        if self._in_fit_ctl:
            return   # _apply_compact re-enters via _apply_button_visibility
        self._in_fit_ctl = True
        try:
            for level in range(6):
                self._apply_compact(level)
                if self.ctl.sizeHint().width() <= avail:
                    return
            # level 5 (most compact) still doesn't fit: the row clips at
            # the edges rather than squashing the buttons into each other.
        finally:
            self._in_fit_ctl = False

    def _apply_compact(self, level: int):
        if level == self._compact_level:
            return
        self._compact_level = level
        hide = set()
        if level >= 4:
            hide.add("back60")
        if level >= 5:
            hide.update(("back10", "fwd10"))
        self._compact_hidden = hide
        self.ctl_row.layout().setSpacing(2 if level >= 1 else 6)
        self.vol_slider.setFixedWidth(48 if level >= 3 else 84)
        self._apply_button_visibility()

    def rebuild(self):
        """Settings changed: re-apply them to the EXISTING VLCPlayer.

        Never construct a new VLCPlayer here — that would create a second
        vlc.Instance in the process, which deadlocks on Windows (see the note
        in dvr.py). Instance-level options such as network-caching cannot be
        changed after creation, so they are logged and take effect on the next
        app restart; runtime-changeable settings (timeshift flag, volume) are
        applied immediately.
        """
        current = self.current
        try:
            log.info("rebuild: reusing display VLCPlayer (single vlc.Instance);"
                     " network_caching=%d applies after app restart",
                     self.config.network_caching)
        except Exception:
            pass
        try:
            self.vlc.timeshift = self.config.timeshift
            self.vlc.set_volume(self.config.volume)
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("rebuild: applying runtime options failed: %r", exc)
            except Exception:
                pass
        self._attach_vlc()
        if current:
            self.play_media(current)

    def _attach_vlc(self, attempt: int = 1):
        """Bind the video surface to the VLC player.

        winId() can legitimately not be ready 300 ms into the app, so the
        bind is retried up to 3 times (~300 ms apart); every failure is
        logged, nothing is ever raised. After the final attempt playback is
        unblocked either way (video, or audio-only as a degraded mode).
        """
        if self._closing:
            return   # shutting down — never touch VLC after teardown
        wid = 0
        try:
            wid = int(self.surface.winId())
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("vlc attach: winId() failed (attempt %d/3): %r",
                            attempt, exc)
            except Exception:
                pass
        if wid:
            try:
                self.vlc.set_window(wid)
                self._attached = True
                self._attach_done = True
            except Exception as exc:  # noqa: BLE001
                wid = 0
                try:
                    log.warning("vlc attach: set_window failed (attempt "
                                "%d/3): %r", attempt, exc)
                except Exception:
                    pass
        if wid:
            self._layout_overlays()
            self._flush_pending_media()
            return
        if attempt < 3:
            QtCore.QTimer.singleShot(
                300, lambda: self._attach_vlc(attempt + 1))
            return
        self._attach_done = True   # gave up — unblock playback (audio-only)
        try:
            log.error("vlc attach: giving up after 3 attempts — playback "
                      "continues audio-only until restart")
        except Exception:
            pass
        self._flush_pending_media()

    def _flush_pending_media(self):
        """Start playback that was queued while the surface wasn't bound."""
        if self._pending_media is None:
            return
        pending, self._pending_media = self._pending_media, None
        try:
            log.info("vlc attach resolved: starting deferred playback %r",
                     pending.get("title", ""))
        except Exception:
            pass
        self.play_media(pending)

    def show_info(self, title: str, epg: str = ""):
        if epg:
            self._last_epg = epg
        elif title and not epg and self._last_epg:
            epg = self._last_epg
        self.info_overlay.set_info(title, epg)
        self.info_overlay.show()
        if (not self._immersive and not self.overlay.isVisible()
                and not self._overlay_suppressed):
            self.overlay.show()   # the info banner lives in the overlay window
        self._layout_overlays()
        self.info_timer.start()

    def hide_info(self):
        self.info_overlay.hide()

    def play_media(self, playable: dict):
        if not self._attach_done and not self._closing:
            # Video surface not bound yet (startup attach pending/retrying):
            # queue the item — the attach callback starts playback as soon
            # as the surface is bound (or audio-only if attach gives up).
            self._pending_media = playable
            try:
                log.info("play_media deferred until vlc attach: %r",
                         playable.get("title", ""))
            except Exception:
                pass
            return
        self.current = playable
        self._session += 1   # invalidate any deferred DVR callbacks
        # Re-arm after an earlier stop() (Playback > Stop): the teardown
        # guards and the tick timer must not outlive the teardown they
        # protect. Re-attaching the surface is a no-op for the same player
        # and REQUIRED if stop_and_release() had to swap in a fresh one
        # (hung libvlc stop — see VLCPlayer.stop).
        self._closing = False
        if not self.timer.isActive():
            self.timer.start()
        if not self.cursor_timer.isActive():
            # stop() halts the cursor poll — and it is the ONLY reliable
            # wake path over the native video window (VLC swallows mouse
            # events).  Without this restart the controls could never be
            # woken again after Stop + replay.
            self.cursor_timer.start()
        if self._attached:
            self._attach_vlc()
        kind = playable.get("kind", "live")
        title = playable.get("title", "Playing")
        try:
            log.info("play_media kind=%s title=%r", kind, title)
        except Exception:
            pass
        self._last_epg = ""
        self.show_info(title)
        # Per-channel reset: DVR / Record never carry over to the next channel.
        # Default is the plain live stream (single connection).
        # Handoff order (ONE provider stream max, Windows-safe temp cleanup):
        #   1) release record-output bookkeeping,
        #   2) stop the DISPLAY player (closes its live stream / DVR buffer
        #      file handle),
        #   3) safe_stop() the recorder — its temp dir is deleted only now,
        #      after BOTH players are idle,
        #   4) only then open the new URL on the display player.
        self._stop_recording(stopping=True)
        self.vlc.stop_and_release()
        self._ensure_dvr_stopped()
        for btn, icon in ((self.btn_dvr, ic.dvr(False)),
                          (self.btn_rec, ic.rec(False))):
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.setIcon(icon)
            btn.blockSignals(False)
        self._mode = "live"
        self._chase_paused = False
        self._dvr_t0 = None
        self._dvr_base = 0.0
        self._reset_dvr_clock()
        self._stall_ticks = 0
        self._last_reopen = 0.0
        self._chase_started = False
        self._dvr_status.hide()
        self._scrub_on = False
        self._vid_s = 0.0
        self._last_raw = None
        self._live_paused = False
        self._set_rate(1.0)
        self._update_control_state()
        self._apply_scale()
        self.vlc.play(playable.get("url", ""))
        self._poke_audio()
        self._wake()
        # Subtitle choice is sticky by language across channels: _enforce_spu (via the
        # tick) re-selects a track with the same NAME once the new media's
        # tracks appear, and leaves subtitles off when it has none.
        self._on_media_for_profanity(kind)
        if kind == "live" and self.client and playable.get("stream_id"):
            self.runner.run(self.client.short_epg, playable["stream_id"], 4)

    def _on_epg(self, result):
        ok, val = result
        if ok != "ok" or not val:
            return
        now = val[0]
        nxt = val[1] if len(val) > 1 else None
        text = "Now: " + _decode(now.title)
        if now.start and now.end:
            text += f"  ({now.start}\u2013{now.end})"
        if nxt:
            text += "\nNext: " + _decode(nxt.title)
        title = self.current.get("title", "") if self.current else ""
        self.show_info(title, text)

    # ---- controls ----
    def _toggle_pause(self):
        if self._mode == "chase":
            # Watching the buffer: a plain file pause — the recorder keeps
            # filling the file, so this is a flawless pause of live TV.
            try:
                down = self.vlc.state_name() in ("ended", "stopped", "error")
            except Exception:  # noqa: BLE001
                down = False
            if self._chase_paused:
                # resume — unless the player died at the file edge meanwhile
                if down:
                    cur = self.vlc.get_time()
                    at = (cur / 1000.0) if cur >= 0 else self._frontier_s()
                    self._chase_seek(at, resume=True)
                else:
                    self._chase_paused = False
                    self.vlc.resume()
            elif down:
                # stalled at the edge (not user-paused): play revives it
                cur = self.vlc.get_time()
                at = (cur / 1000.0) if cur >= 0 else self._frontier_s()
                self._chase_seek(at, resume=True)
            else:
                self._chase_paused = True
                self.vlc.pause()
            return
        # Live / VOD: remember the paused state so the LIVE button can be
        # enabled to jump back to the edge (timeshift pause works here too)
        self._live_paused = not self._live_paused
        self.vlc.toggle_pause()
        self._update_control_state()

    def toggle_pause(self):
        self._toggle_pause()

    def seek_relative(self, ms):
        self._seek_ms(ms)

    def _frontier_s(self) -> float:
        """Seconds of CONFIRMED content currently in the chase buffer.

        ``_dvr_base`` carries what earlier recorder generations wrote;
        ``_dvr_content_s`` only advances while the buffer file is actually
        growing (see ``_note_dvr_data``). The estimate can therefore lag
        the real write position but never lead it, so every seek clamp
        lands on data that really exists — the old wall-clock estimate
        kept ticking during provider hiccups, overshot the end of the
        file, and made fast-forward/LIVE land on a stalled player.
        """
        return max(0.0, self._dvr_base + self._dvr_content_s)

    def _reset_dvr_clock(self):
        """Zero the content clock for a fresh recorder run."""
        self._dvr_first_data = None
        self._dvr_content_s = 0.0
        self._dvr_size = -1
        self._dvr_tick_t = None
        self._dvr_last_growth = None

    def _note_dvr_data(self):
        """Sample the buffer file and advance the content clock — but only
        while the file is actually growing.

        A live capture writes roughly one second of content per second of
        wall time, so the wall time BETWEEN TWO GROWTH SIGHTINGS counts as
        content (VLC flushes the file in bursts, so crediting only the gap
        since the previous sample badly under-counted bursty writers and
        made the frontier lag minutes behind reality). When the file stands
        still (provider hiccup) the clock freezes instead of drifting ahead
        of the real write position — that drift was what let seeks land
        past EOF and killed rewind/fast-forward.
        """
        if not (self.dvr and self.dvr.file_path):
            return
        try:
            size = os.path.getsize(self.dvr.file_path)
        except OSError:
            return
        if size <= 0:
            return
        now = time.time()
        if self._dvr_first_data is None:
            self._dvr_first_data = now
            self._dvr_content_s = 0.0
            self._dvr_size = size
            self._dvr_tick_t = now
            self._dvr_last_growth = now
            try:
                log.info("dvr data flowing: content clock starts "
                         "(base=%.1fs)", self._dvr_base)
            except Exception:
                pass
            return
        if size > self._dvr_size:
            # growth since the last GROWTH sighting confirms the interval
            # between them was recorded. The cap only guards against
            # pathological stalls — VLC flushes the sout file in 2-4 s
            # bursts, so a tight cap here under-counted real content and
            # made jump-to-live land far behind the true edge.
            if self._dvr_last_growth is not None:
                self._dvr_content_s += min(15.0, max(
                    0.0, now - self._dvr_last_growth))
            self._dvr_last_growth = now
        self._dvr_size = size
        self._dvr_tick_t = now

    def _safe_seek_target(self, target: float) -> float:
        """Clamp a chase seek so it can never land past the write frontier."""
        limit = self._frontier_s() - _CHASE_SAFETY_S
        return max(0.0, min(max(0.0, target), limit))

    def _seek_ms(self, ms):
        if self._mode == "chase" and self.dvr:
            # Pure file seek inside the buffer — instant and exact, clamped
            # to a safe distance behind the write position. Base on the
            # TRACKED position: VLC timestamps on these streams can be
            # garbage broadcast PTS (huge/jumping values) that would send
            # every seek to the wrong place.
            self._chase_seek(self._vid_s + ms / 1000.0)
            return
        # Live / VOD: normal seek (works for VOD; live streams ignore it).
        self.vlc.seek_ms(ms)

    def _jump_begin(self):
        """The inverse of LIVE: restart playback at the very beginning of
        the DVR buffer (or of the movie)."""
        if self._mode == "chase" and self.dvr:
            self._set_rate(1.0)
            self._chase_seek(0.0, resume=True)
        elif self.vlc.get_length() > 0:
            self.vlc.set_time(0)

    def _chase_seek(self, target_s: float, resume: bool = False):
        """Seek within the chase buffer — and revive a dead player.

        ``set_time()`` is a NO-OP once VLC ran into the end of the growing
        buffer file and stopped, which used to leave every rewind / FF /
        LIVE / play press dead until the watchdog noticed. When the display
        player is down (ended/stopped/error) the buffer file is reopened AT
        the target instead (~half a second) — a local file operation, so
        the single 8kstrong connection is never touched.
        """
        if not (self._mode == "chase" and self.dvr):
            return
        target = self._safe_seek_target(float(target_s))
        try:
            down = self.vlc.state_name() in ("ended", "stopped", "error")
        except Exception:  # noqa: BLE001
            down = False
        if not down:
            self.vlc.set_time(int(target * 1000))
            self._vid_s = target
            if resume and self._chase_paused:
                self._chase_paused = False
                self.vlc.resume()
            return
        buf = self.dvr.buffer_file()
        if not buf:
            return
        try:
            log.warning("chase revive: player was down — play_at %.1fs",
                        target)
        except Exception:
            pass
        # A revive always resumes: the user pressed a transport control on
        # a frozen player, so the intent is to get playback going again.
        self._chase_paused = False
        self._chase_started = False   # re-armed by the first playing tick
        self._stall_ticks = 0
        self._last_reopen = time.time()
        self._vid_s = target
        self.vlc.play_at(buf, target)
        self._poke_audio()
        self._poke_rate()

    def _poke_rate(self):
        """Re-apply the user's playback speed after a player swap (a fresh
        VLC player always starts at 1x — same Windows quirk as volume)."""
        if self._closing:
            return
        try:
            self.vlc.set_rate(self._rate)
        except Exception:  # noqa: BLE001
            pass
        QtCore.QTimer.singleShot(700, self._poke_rate_late)

    def _poke_rate_late(self):
        if self._closing or self._mode != "chase":
            return
        try:
            self.vlc.set_rate(self._rate)
        except Exception:  # noqa: BLE001
            pass

    def _jump_live(self):
        """The single "go to live" control.

        In DVR chase mode it jumps from pause/rewind to the newest safe
        moment in the buffer — a hair behind the write frontier — and
        resumes playback. In plain LIVE mode it jumps the (timeshifted)
        stream to its live edge; if the player died (e.g. the provider
        dropped a long pause), the stream is reconnected instead — LIVE is
        the escape hatch for a wedged live stream. For movies / series it
        simply skips to the end of the file.
        """
        try:
            log.info("_jump_live mode=%s", self._mode)
        except Exception:
            pass
        if self._mode == "chase" and self.dvr:
            self._set_rate(1.0)   # jumping to live always resumes normal speed
            try:
                log.info("jump to live edge: target=%.1fs frontier=%.1fs",
                         self._safe_seek_target(self._frontier_s()),
                         self._frontier_s())
            except Exception:
                pass
            self._chase_seek(self._frontier_s(), resume=True)
            return
        # Plain LIVE mode (timeshift) / VOD skip-to-end
        try:
            down = self.vlc.state_name() in ("ended", "stopped", "error")
        except Exception:  # noqa: BLE001
            down = False
        if down and self.current and self.current.get("url") \
                and not self._is_vod():
            # long-paused past the provider's patience: reconnect at live
            self._reopen_display()
            self._live_paused = False
            self._update_control_state()
            return
        self.vlc.jump_to_live()
        if self._live_paused:
            self._live_paused = False
            self.vlc.resume()
            self._update_control_state()

    def _on_volume(self, value):
        self.vlc.set_volume(value)
        self._vol_save_timer.start()   # persist wheel/click changes too

    def _save_volume(self):
        self.config.volume = self.vol_slider.value()
        self.config.save()

    def _on_volume_released(self):
        self._vol_save_timer.stop()
        self._save_volume()

    def _on_mute(self, on):
        self.vlc.set_mute(bool(on))
        self.btn_mute.setIcon(ic.volume(False) if on else ic.volume(True))
        self.btn_mute.setToolTip("Unmute (M)" if on else "Mute (M)")

    def _on_wheel(self, delta):
        self._wake()
        step = 5 if delta > 0 else -5
        self.vol_slider.setValue(max(0, min(100, self.vol_slider.value() + step)))

    def _on_slider_moved(self, _v):
        self._seeking = True

    def _on_slider_released(self):
        self._wake()
        try:
            if self._mode == "chase" and self.dvr:
                # never let a slider drag land on/past the write position
                # (_chase_seek clamps and revives a stalled player)
                self._chase_seek(self.slider.value() / 1000.0)
            else:
                self.vlc.set_time(self.slider.value())
        finally:
            # ALWAYS clear the drag flag — a stuck True froze the scrubber
            # timestamps until the next successful drag.
            self._seeking = False

    # ---- DVR / chase mode (single connection: recorder owns the stream) ----
    def _on_dvr_toggled(self, on):
        try:
            log.info("_on_dvr_toggled on=%s", bool(on))
        except Exception:
            pass
        self._session += 1   # deferred chase callbacks from before are stale
        self.config.dvr_enabled = bool(on)
        self.config.save()
        self.btn_dvr.setIcon(ic.dvr(bool(on)))
        if on:
            if not (self.current and self.current.get("kind") == "live"
                    and self.current.get("url")):
                self.btn_dvr.blockSignals(True)
                self.btn_dvr.setChecked(False)
                self.btn_dvr.setIcon(ic.dvr(False))
                self.btn_dvr.blockSignals(False)
                return
            if (self._mode == "chase" and self.dvr and self.dvr.running):
                # Already timeshifting (e.g. REC started the pipeline) —
                # nothing to hand over, the buffer keeps growing.
                self._update_control_state()
                return
            # ONE connection, strict handoff order:
            #   1) the DISPLAY player stops the network URL first (when REC
            #      was on in live mode this also stops its record output),
            #   2) the recorder opens the single connection — with the kept
            #      recording file as a second output when REC is on (dual
            #      output), so no second vlc.play(url) is ever issued,
            #   3) once the buffer holds data the display player switches to
            #      watching the buffer file behind the live edge.
            # The display player never plays the network URL while the
            # recorder runs.
            self._dvr_t0 = time.time()
            self._dvr_base = 0.0
            self._reset_dvr_clock()
            self._stall_ticks = 0
            self._last_reopen = 0.0
            self._chase_started = False
            try:
                log.info("dvr on handoff: display player off the network "
                         "(record=%s)", self.btn_rec.isChecked())
            except Exception:
                pass
            self.vlc.stop_and_release()                 # (1)
            self._ensure_dvr_stopped()                  # drop any stale buffer
            record = self.btn_rec.isChecked()
            self._restart_recorder(record=record)       # (2)
            self._wait_and_enter_chase(self._session)   # (3)
        else:
            if self.btn_rec.isChecked():
                # REC alone keeps the single-connection chase pipeline (the
                # recording stays scrubbable), so only the UI state changes.
                self._update_control_state()
                return
            self._exit_chase_to_live()

    def _exit_chase_to_live(self):
        """Leave chase mode and return to the plain live stream.

        Safe order (ONE provider connection at all times): the display player
        stops first (it holds the buffer file handle), then the recorder and
        its temp dir go, and only then does the display player dial the live
        URL again.
        """
        self._mode = "live"
        self._chase_paused = False
        self._dvr_t0 = None
        self._dvr_base = 0.0
        self._reset_dvr_clock()
        self._stall_ticks = 0
        self._chase_started = False
        self._vid_s = 0.0
        self._last_raw = None
        self._set_dvr_status(None)
        self._set_rate(1.0)
        self._update_control_state()
        self.vlc.stop_and_release()
        self._ensure_dvr_stopped()
        self._reopen_display()

    def _reopen_display(self, at: float = None) -> bool:
        """(Re)open whatever the display player should be showing right now —
        the DVR buffer in chase mode, otherwise the current URL — with the
        record output attached when active. ``at"
        re-enters a chase buffer at a content position (so the playback
        position is preserved instead of jumping to live).
        """
        rec = self._rec_path if (self._rec_path and self._is_vod()) else None
        if self._mode == "chase" and self.dvr:
            buf = self.dvr.buffer_file()
            if not buf:
                return False
            target = self._vid_s if at is None else float(at)
            self._chase_started = False
            self._stall_ticks = 0
            self._last_reopen = time.time()
            self.vlc.play_at(buf, target)
            self._vid_s = target
            self._poke_audio()
            self._poke_rate()
            return True
        url = self.current.get("url", "") if self.current else ""
        if not url:
            return False
        if rec:
            self.vlc.play_at(url, record_path=rec)
        else:
            self.vlc.play(url)
        self._poke_audio()
        return True

    def _restart_recorder(self, record: bool):
        """(Re)start the single-connection recorder on the current buffer.

        Reuses the existing buffer file with append so the timeline is
        continuous; adds the recording file as a second output when requested.

        A restart creates a gap in the data (stop → reconnect), so the
        content clock is frozen here: what the current run has written so far
        is snapshotted into ``_dvr_base`` and the next run accrues from zero
        again. Wall-clock time across the gap never counts, so the frontier
        can't overshoot the real write position.
        """
        if self.dvr and self.dvr.running and self._dvr_first_data is not None:
            self._dvr_base = max(0.0, self._frontier_s() - 1.0)
            self._reset_dvr_clock()
            try:
                log.info("recorder restart: content clock frozen at %.1fs",
                         self._dvr_base)
            except Exception:
                pass
        buf = self.dvr.buffer_file() if self.dvr else None
        url = self.current.get("url", "") if self.current else ""
        try:
            log.info("_restart_recorder record=%s reuse_buffer=%s",
                     bool(record), bool(buf and os.path.exists(buf)))
        except Exception:
            pass
        old_dir_keep = self.dvr._dir if self.dvr else None
        if self.dvr:
            self.dvr.stop(delete=False)
            self.dvr = None
        self.dvr = VlcRecorder(self.config.dvr_max_minutes,
                               self.config.network_caching,
                               instance=self.vlc.instance)
        rec_path = self._rec_path if record else None
        if buf and os.path.exists(buf):
            self.dvr.start(url, output_path=rec_path, buffer_path=buf)
        else:
            self.dvr.start(url, output_path=rec_path)
        if old_dir_keep and not self.dvr._dir:
            self.dvr._dir = old_dir_keep

    def _ensure_dvr_stopped(self):
        """Stop the DVR recorder if running. safe_stop() never raises and is
        safe to call twice; call this only AFTER the display player is idle so
        the temp-dir delete cannot race an open VLC file handle."""
        try:
            log.info("_ensure_dvr_stopped had_dvr=%s display_busy=%s",
                     self.dvr is not None, self.vlc.is_busy())
        except Exception:
            pass
        if self.dvr:
            d = self.dvr
            self.dvr = None
            d.safe_stop(delete=True)  # stops + retries temp dir delete, no raise

    def _set_dvr_status(self, text):
        """Show/hide the small 'DVR starting' pill (chase buffer filling)."""
        if self._closing:
            return
        if text:
            if self._dvr_status.text() != text:
                self._dvr_status.setText(text)
            # Only surface the overlay with the app in the foreground — the
            # tick keeps updating this pill in the background, and the
            # ToolTip-style overlay would paint it over other apps.
            if not self.overlay.isVisible() and not self._overlay_suppressed:
                self.overlay.show()
            self._dvr_status.show()
            self._dvr_status.raise_()
        else:
            self._dvr_status.hide()
        self._layout_overlays()

    def _wait_and_enter_chase(self, gen: int, tries_left: int = -1):
        """Wait until the fresh buffer holds its first seconds, then start
        watching it from (frontier - chase_delay) — which clamps to 0, i.e.
        a couple of seconds behind live, and the gap grows naturally.

        This is the original fast entry (~2-4 s): waiting for the full live
        delay first ("DVR 0s/20s buffered…") made users stare at a dead
        screen; VLC reads a growing file at its own pace just fine, and the
        frontier clock keeps every seek clamped to data that exists.  If the
        recorder never produces data at all, give up gracefully and fall
        back to the plain live stream.

        ``gen`` is the session generation captured when DVR was switched on.
        If the channel changed / DVR toggled / view stopped in the meantime,
        the chain aborts silently and never calls vlc.play/play_at."""
        if gen != self._session:
            return   # stale — a newer session owns the player now
        if not (self.dvr and self.dvr.running):
            self._set_dvr_status(None)
            return
        self._note_dvr_data()
        if tries_left < 0:
            tries_left = 50          # ~20 s: provider throttling recovery
        waited = 0.0 if self._dvr_t0 is None else (time.time() - self._dvr_t0)
        ready = self.dvr.buffer_file() is not None and waited >= 2.5
        if ready:
            self._set_dvr_status(None)
            self._start_chase_now(gen)
            return
        self._set_dvr_status("DVR starting\u2026")
        if tries_left > 0:
            QtCore.QTimer.singleShot(
                400, lambda: self._wait_and_enter_chase(gen, tries_left - 1)
            )
        else:
            # Recorder failed (provider blocked it / network) — revert cleanly
            # instead of hanging on a black screen.
            try:
                log.warning("chase wait: gave up after %.1fs -- back to live",
                            waited)
            except Exception:
                pass
            self._set_dvr_status(None)
            self._ensure_dvr_stopped()
            self._mode = "live"
            self._chase_paused = False
            self._dvr_t0 = None
            self._dvr_base = 0.0
            self._reset_dvr_clock()
            self.btn_dvr.blockSignals(True)
            self.btn_dvr.setChecked(False)
            self.btn_dvr.setIcon(ic.dvr(False))
            self.btn_dvr.blockSignals(False)
            self._update_control_state()
            self._reopen_display()

    def _start_chase_now(self, gen: int):
        if gen != self._session:
            return   # stale — never play_at for an old session
        if not (self.dvr and self.dvr.buffer_file()):
            try:
                log.info("chase start aborted: buffer not ready")
            except Exception:
                pass
            return
        self._note_dvr_data()
        self._mode = "chase"
        self._chase_paused = False
        self._chase_started = False   # armed on the first playing tick
        self._stall_ticks = 0
        self._set_rate(1.0)
        self._update_control_state()
        # profanity filter: begin reading captions from the buffer now
        if (prof_mod.PROFANITY_AVAILABLE and self._filter_engine.enabled
                and self._cc_source is None and not self._closing):
            self._start_cc_when_buffer(tries_left=10)
        delay = self.config.chase_delay
        target = self._safe_seek_target(self._frontier_s() - delay)
        try:
            log.info("chase start: delay=%.1fs target=%.1fs frontier=%.1fs",
                     delay, target, self._frontier_s())
        except Exception:
            pass
        # are on (the wav then logs the buffered audio as it is displayed)
        self._reopen_display(at=target)

    def _reopen_chase(self, gen: int):
        """Watchdog path: reopen the buffer where the viewer actually WAS.

        Aborts silently when the session changed (channel switch / DVR toggle
        / stop) so a stale tick can never call play_at. The classic trigger
        is the chase catching up with the file's write head — there the
        tracked position sits at the frontier anyway, so behavior matches
        the old frontier reopen. But a mid-file stall (e.g. across a buffer
        append gap) now stays WHERE IT WAS instead of jumping to live.
        """
        if gen != self._session:
            return
        if not (self._mode == "chase" and self.dvr and self.dvr.buffer_file()):
            return
        target = self._safe_seek_target(self._vid_s)
        try:
            log.warning("chase reopen: play_at %.1fs (frontier=%.1fs "
                        "was_at=%.1fs)", target, self._frontier_s(),
                        self._vid_s)
        except Exception:
            pass
        self._chase_paused = False
        self._chase_started = False
        self._set_rate(1.0)
        self._reopen_display(at=target)
        self._poke_audio()

    # ---- permanent recording (kept file; still a single connection) ----
    def _on_rec_toggled(self, on):
        self.btn_rec.setIcon(ic.rec(bool(on)))
        if on:
            folder = self.config.record_folder
            if not folder or not os.path.isdir(folder):
                folder = QtWidgets.QFileDialog.getExistingDirectory(
                    self, "Choose where recordings are saved"
                )
                if folder:
                    self.config.record_folder = folder
                    self.config.save()
            if not folder or not (self.current and self.current.get("url")):
                self.btn_rec.blockSignals(True)
                self.btn_rec.setChecked(False)
                self.btn_rec.setIcon(ic.rec(False))
                self.btn_rec.blockSignals(False)
                return
            safe = re.sub(r"[^\w\-.]+", "_",
                          self.current.get("title", "stream")).strip("._")[:60]
            safe = safe or "stream"
            self._rec_path = os.path.join(
                folder, f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}.ts"
            )
            if self.dvr and self.dvr.running:
                # Recorder already owns the single connection (chase mode or
                # its entry window): restart it with the recording file as a
                # second output (buffer keeps growing).
                self._restart_recorder(record=True)
            elif self.current.get("kind") == "live":
                # Live TV: recording joins the single-connection chase
                # pipeline so the timeline is scrubbable/seekable — the same
                # handoff DVR mode uses (display player off the network,
                # recorder on with dual output, then watch the buffer).
                self._dvr_t0 = time.time()
                self._dvr_base = 0.0
                self._reset_dvr_clock()
                self._stall_ticks = 0
                self._last_reopen = 0.0
                self._chase_started = False
                self.vlc.stop_and_release()
                self._ensure_dvr_stopped()
                self._restart_recorder(record=True)
                self._wait_and_enter_chase(self._session)
            else:
                # VOD: the main player watches AND records in one go.
                self.vlc.play_at(self.current["url"],
                                 record_path=self._rec_path)
        else:
            self._stop_recording()

    def _stop_recording(self, stopping: bool = False):
        """Finalize a permanent recording (the file is kept on disk).

        ``stopping=True`` (channel change / stop / shutdown): never restart or
        reconfigure playback — just release the record output.
        """
        self._rec_path = None
        if stopping:
            return
        if self.dvr and self.dvr.running:
            if self.btn_dvr.isChecked():
                # DVR still needs the single-connection recorder: drop only
                # the recording output — the buffer keeps growing and the
                # display player stays where it is. NEVER play the network
                # URL here: that would open a second connection.
                self._restart_recorder(record=False)
            else:
                # REC was the only reason for the chase pipeline — back to
                # the plain live stream.
                self._exit_chase_to_live()
        elif self.current and self.current.get("url") and self._mode == "live":
            # Plain live/VOD mode: the display player carried the record
            # output, restart it as a plain viewer (still one connection,
            #).
            self._reopen_display()
        if self.btn_rec.isChecked():
            self.btn_rec.blockSignals(True)
            self.btn_rec.setChecked(False)
            self.btn_rec.setIcon(ic.rec(False))
            self.btn_rec.blockSignals(False)

    # ---- VOD download (replaces the DVR button for movies / series) ----
    def _start_download(self):
        """Save the original movie/episode file to the recordings folder.
        Unlike REC (which re-records the decode), this copies the provider's
        bytes verbatim in a background thread."""
        if self._downloading or not self._is_vod():
            return
        if not (self.current and self.current.get("url")):
            return
        folder = self.config.record_folder
        if not folder or not os.path.isdir(folder):
            folder = QtWidgets.QFileDialog.getExistingDirectory(
                self, "Choose where downloads are saved")
            if folder:
                self.config.record_folder = folder
                self.config.save()
        if not folder:
            return
        url = self.current["url"]
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".mp4"
        safe = re.sub(r"[^\w\-.]+", "_",
                      self.current.get("title", "video")).strip("._")[:60]
        safe = safe or "video"
        path = os.path.join(folder, f"{safe}{ext}")
        n = 1
        while os.path.exists(path):        # never clobber an earlier download
            path = os.path.join(folder, f"{safe} ({n}){ext}")
            n += 1
        self._downloading = True
        self.btn_dl.setEnabled(False)
        self._set_dvr_status("Downloading\u2026")
        self._dl = FileDownloader(self)
        self._dl.progress.connect(self._on_dl_progress)
        self._dl.finished.connect(self._on_dl_finished)
        self._dl.start(url, path)
        try:
            log.info("download start: %s -> %s", url, path)
        except Exception:
            pass

    def _on_dl_progress(self, done, total):
        if total:
            self._set_dvr_status(
                f"Downloading\u2026 {done // 1048576} / {total // 1048576} MB")
        else:
            self._set_dvr_status(f"Downloading\u2026 {done // 1048576} MB")

    def _on_dl_finished(self, ok, msg):
        self._downloading = False
        self.btn_dl.setEnabled(self._is_vod())
        if ok:
            self._set_dvr_status(f"Downloaded: {os.path.basename(msg)}")
        else:
            self._set_dvr_status(f"Download failed: {msg}"[:80])
        try:
            log.info("download finished ok=%s msg=%s", ok, msg)
        except Exception:
            pass
        QtCore.QTimer.singleShot(5000, self._hide_dl_pill)

    def _hide_dl_pill(self):
        if (not self._downloading and self._dvr_status.isVisible()
                and self._dvr_status.text().startswith("Download")):
            self._set_dvr_status(None)

    # ---- controls auto-hide (always while playing; immersive too) ----
    @property
    def _immersive(self):
        return self._fullscreen or self._zen

    def set_fullscreen_mode(self, on, hide_overlay=False):
        self._fullscreen = on
        self._apply_immersive(hide_overlay)

    def set_zen(self, on):
        self._zen = on
        self._apply_immersive(False)

    def _apply_immersive(self, hide_overlay=False):
        # Immersive modes hide EVERYTHING (corner buttons included) until
        # the cursor moves; leaving them re-shows the always-present corner
        # buttons. (hide_overlay is kept for API compatibility.)
        if self._immersive:
            self._sleep(force=True)
        else:
            self._wake()

    def eventFilter(self, obj, event):
        et = event.type()
        if et in (QtCore.QEvent.MouseMove, QtCore.QEvent.HoverMove,
                  QtCore.QEvent.Wheel):
            if isinstance(obj, QtWidgets.QWidget) and (
                    self.isAncestorOf(obj) or obj is self.overlay):
                self._wake()
        elif et in (QtCore.QEvent.Move, QtCore.QEvent.Resize,
                    QtCore.QEvent.Show, QtCore.QEvent.WindowStateChange):
            # the overlay window must follow the main window (drags, snaps,
            # maximize/restore/fullscreen) — its children use surface-local
            # coordinates, so re-laying out repositions everything.
            if not self._closing and obj is self.window():
                self._layout_overlays()
        return super().eventFilter(obj, event)

    def _on_hover(self):
        self._wake()

    def _poll_cursor(self):
        """Wake on real cursor movement inside the player (see cursor_timer).
        Movement over the channel list / menus does NOT wake the controls.

        Also the backstop that keeps the overlay window glued to the video
        surface (drags / snaps / DPI changes the event filter might miss)
        and hides it while the window is minimized."""
        if self._closing:
            return
        try:
            win = self.window()
            if win is not None and win.isMinimized():
                if self.overlay.isVisible():
                    self.overlay.hide()
                return
            if self.overlay.isVisible() and (
                    self.overlay.geometry().topLeft()
                    != self.surface.mapToGlobal(QtCore.QPoint(0, 0))):
                self._layout_overlays()
        except Exception:  # noqa: BLE001
            pass
        pos = QtGui.QCursor.pos()
        moved = self._last_cursor is not None and pos != self._last_cursor
        self._last_cursor = pos
        # self-heal a latched suppression (native dialogs / video HWND can
        # swallow the focus events that would have cleared it)
        if self._overlay_suppressed and self._app_foreground():
            self._overlay_suppressed = False
            self._overlay_was_visible = False
        if moved and self.rect().contains(self.mapFromGlobal(pos)):
            self._wake()
            return
        # Self-heal for "controls stuck on screen": if the idle hide should
        # have run but the single-shot timer was lost (a menu closed without
        # its aboutToHide, a swallowed timer restart), enforce it here.
        if (self.ctl.isVisible() and not self._seeking
                and not self._popup_open and not self._cursor_on_controls()
                and not self.hide_timer.isActive()):
            self._sleep()

    def _app_foreground(self) -> bool:
        """True when a window of THIS process owns the OS foreground.

        The honest test for 'another app is in front'. Qt's activeWindow()
        alone is not: native dialogs (the settings' color picker) and the
        video HWND swallow focus without Qt noticing, which used to latch
        the overlay suppression forever — 'all controls disappeared'."""
        try:
            app = QtWidgets.QApplication.instance()
            if app is not None and app.activeWindow() is not None:
                return True
        except Exception:  # noqa: BLE001
            pass
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                if not hwnd:
                    return False
                pid = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(
                    hwnd, ctypes.byref(pid))
                return bool(pid.value) and pid.value == os.getpid()
            except Exception:  # noqa: BLE001
                return False
        return False

    def _wake(self):
        """Show the on-video controls (stream start, channel change, cursor
        move). The zen/fullscreen corner buttons are always shown: in normal
        windowed mode they never auto-hide — only the bottom control bar and
        the restore chevron follow the idle timer."""
        if self._closing:
            return
        if self._overlay_suppressed:
            if not self._app_foreground():
                # Another app truly has the foreground (see
                # _on_focus_changed): the ToolTip-style overlay paints above
                # OTHER apps' windows too, so a cursor passing over the
                # app's exposed area behind them must not surface the
                # controls over e.g. Chrome.
                return
            # our own process owns the foreground — the flag latched
            # spuriously (native dialog / video HWND swallowed the focus
            # events); self-heal instead of hiding the controls forever
            self._overlay_suppressed = False
            self._overlay_was_visible = False
        self.unsetCursor()
        if not self.cursor_timer.isActive():
            self.cursor_timer.start()   # self-heal: stop() halts the poll
        shown = False
        if not self.overlay.isVisible():
            self.overlay.show()
            shown = True
        for w in (self._btn_panel, self._btn_ovfs, self.ctl):
            if not w.isVisible():
                w.show()
                shown = True
            w.raise_()
        if self._panel_hidden:
            if not self._btn_showpanel.isVisible():
                self._btn_showpanel.show()
                shown = True
            self._btn_showpanel.raise_()
        if shown:
            self._layout_overlays()
        self.hide_timer.start()

    def _sleep(self, force=False):
        """4 s of no cursor movement: hide the idle-only controls.

        In normal windowed mode the zen/fullscreen corner buttons stay put
        (sharing the auto-hide cycle made them "randomly disappear"); in
        fullscreen/zen everything hides — cursor included — and any cursor
        movement brings it all back. Never hides mid-interaction (scrub
        drag, open popup, cursor resting on one of the controls)."""
        if self._closing:
            return
        if not force and (self._seeking or self._popup_open
                          or self._cursor_on_controls()):
            self.hide_timer.start()
            return
        self.ctl.hide()
        self._btn_showpanel.hide()
        if self._immersive:
            self._btn_panel.hide()
            self._btn_ovfs.hide()
            self.setCursor(QtCore.Qt.BlankCursor)
        if not any(w.isVisible() for w in (
                self._btn_panel, self._btn_ovfs, self._btn_showpanel,
                self.info_overlay, self._dvr_status)):
            self.overlay.hide()   # nothing left to show over the video

    def _cursor_on_controls(self) -> bool:
        for w in (self.ctl, self._btn_panel, self._btn_ovfs,
                  self._btn_showpanel):
            if w.isVisible() and w.underMouse():
                return True
        return False

    def set_panel_hidden(self, on: bool):
        """MainWindow reports whether the channel list is hidden; while it
        is, a floating chevron on the video's left edge brings it back."""
        self._panel_hidden = bool(on)
        if self._panel_hidden:
            self._wake()          # positions + shows the chevron
        else:
            self._btn_showpanel.hide()
        # Hiding/showing the channel list resizes the video surface: re-fit
        # the video a few times as the resize settles, or VLC can keep the
        # aspect ratio of the OLD widget size (the 'black side bars' bug).
        for ms in (0, 150, 400):
            QtCore.QTimer.singleShot(ms, self._apply_scale)

    def _tick(self):
        # Teardown guard: once stop() has run, no timer may touch VLC —
        # the player instance may already be released.
        if self._closing:
            return
        playing = self.vlc.is_playing()
        self.btn_play.setIcon(ic.pause() if playing else ic.play())
        # Subtitles: enforce the user's choice every tick. VLC re-selects
        # (and renders) a stream's own subtitle track on media opens and ES
        # updates, a fresh player after a hung-stop swap loses the selection
        # entirely, and remote MKVs only report their SRT tracks a couple of
        # seconds after Playing — one-shot calls can cover none of that.
        self._enforce_spu()
        mute = self.vlc.is_mute()
        if mute != self.btn_mute.isChecked():
            self.btn_mute.blockSignals(True)
            self.btn_mute.setChecked(mute)
            self.btn_mute.setIcon(ic.volume(False) if mute else ic.volume(True))
            self.btn_mute.setToolTip("Unmute (M)" if mute else "Mute (M)")
            self.btn_mute.blockSignals(False)
        if playing and not self._was_playing:
            # playback (re)started after a player swap — make sure the
            # volume actually reached the freshly opened audio output
            self._poke_audio()
        self._was_playing = playing

        if self._mode == "chase" and self.dvr:
            gen = self._session   # the watchdog reopen must respect it
            self._note_dvr_data()
            frontier = self._frontier_s()
            raw = self.vlc.get_time() / 1000.0
            now = time.time()
            dt = 0.4 if self._tick_t is None else min(1.0, now - self._tick_t)
            self._tick_t = now
            if playing:
                self._chase_started = True   # arm the watchdog from now on
            # ---- tracked playback position ----
            # VLC's timestamps on these streams are often garbage broadcast
            # PTS (huge / jumping / frozen), so keep our own position: snap
            # to VLC's clock only when it MOVED and agrees with the tracked
            # value; otherwise integrate from wall time. Snapping to a
            # frozen-but-plausible VLC clock was what made the time labels
            # stall for 5-15 s at a time.
            sane = 0.0 <= raw <= frontier + 30.0
            if (sane and raw != self._last_raw
                    and abs(raw - self._vid_s) <= 3.0):
                self._vid_s = min(raw, frontier)
            elif playing and not self._chase_paused and not self._seeking:
                self._vid_s = min(frontier, self._vid_s + dt * self._rate)
            self._last_raw = raw
            current = self._vid_s
            # Frontier watchdog: VLC stops at the file's end — reopen just
            # behind the write frontier so playback continues seamlessly.
            # Only counts once playback has actually started (the open/seek
            # phase of play_at reports "not playing" too), and debounced: a
            # single "not playing" tick usually means VLC is just buffering
            # mid-file, and reopening then caused jank/freezes.
            if (not playing and not self._chase_paused and raw >= 0
                    and self._chase_started):
                self._stall_ticks += 1
                if (self._stall_ticks >= 3
                        and now - self._last_reopen > _REOPEN_COOLDOWN_S
                        and self.dvr.buffer_file()):
                    self._last_reopen = now
                    try:
                        log.warning(
                            "chase watchdog reopen: frontier=%.1fs "
                            "stall_ticks=%d", frontier, self._stall_ticks)
                    except Exception:
                        pass
                    self._stall_ticks = 0
                    self._reopen_chase(gen)
                    return
            self._stall_ticks = 0
            if not self._seeking:
                self._set_scrub(int(frontier * 1000), int(current * 1000))
            # Self-heal: the scrubber must be up while chasing.
            if not self._scrub_on:
                self._scrub_on = True
                self._set_scrub_visible(True)
            # Fast-forward ran into the live edge → resume normal speed.
            # The threshold is SPEED-AWARE (at 4x the gap shrinks fast, so a
            # fixed threshold let VLC hit the file end before the next tick
            # could reset the rate).
            if (self._rate > 1.0 and playing and not self._chase_paused
                    and frontier - current
                    <= _CHASE_SAFETY_S + 1.5 + self._rate * 0.5):
                try:
                    log.info("speed catch-up: %.2fx reached live edge — "
                             "back to 1x", self._rate)
                except Exception:
                    pass
                self._set_rate(1.0)
            return

        # Live / VOD: the scrubber only appears for VOD (known length) —
        # plain live has nothing to scrub (rewind buttons are disabled).
        prev_tick = self._tick_t
        now = time.time()
        self._tick_t = now
        length = self.vlc.get_length()
        raw = self.vlc.get_time()
        vod = length > 0
        if vod != self._scrub_on:
            self._scrub_on = vod
            for b in (self.btn_back60, self.btn_back10, self.btn_fwd10,
                      self.btn_begin):
                b.setEnabled(vod)
            self._set_scrub_visible(vod)
        # LIVE works in plain live mode (jump back to the edge and resume
        # after a timeshift pause — or reconnect a dead stream) and skips
        # to the end for movies/series.
        self.btn_live.setEnabled(bool(self.current))
        if vod:
            # Same tracked-position approach as chase: some VOD streams
            # carry broken timestamps too, and a frozen/stuck VLC clock
            # froze the time labels with them.
            dt = 0.4 if prev_tick is None else min(1.0, now - prev_tick)
            raw_s = raw / 1000.0
            if (0.0 <= raw_s <= length / 1000.0 + 5.0
                    and raw_s != self._last_raw
                    and abs(raw_s - self._vid_s) <= 3.0):
                self._vid_s = min(raw_s, length / 1000.0)
            elif playing and not self._live_paused and not self._seeking:
                self._vid_s = min(length / 1000.0, self._vid_s + dt)
            self._last_raw = raw_s
            if not self._seeking:
                self._set_scrub(int(length), int(self._vid_s * 1000))
        else:
            self._last_raw = raw / 1000.0 if raw >= 0 else None
            self._vid_s = 0.0



    def _set_scrub(self, maximum: int, value: int):
        """Only touch the scrubber when something actually changed — repeated
        setRange/setValue calls repaint the overlay and cause lag."""
        if maximum != self._last_slider_max:
            self._last_slider_max = maximum
            self.slider.blockSignals(True)
            self.slider.setRange(0, maximum)
            self.slider.blockSignals(False)
        self.slider.blockSignals(True)
        self.slider.setValue(max(0, value))
        self.slider.blockSignals(False)
        lt = _fmt(value)
        rt = _fmt(maximum)
        if lt != self.time_left.text():
            self.time_left.setText(lt)
        if rt != self.time_right.text():
            self.time_right.setText(rt)

    def _set_scrub_visible(self, on: bool):
        vis = bool(on) and self.config.control_buttons.get("timebar", True)
        # isHidden() (not isVisible()) — the parent overlay may itself be
        # hidden, and that must not fool us into skipping the change.
        if self.scrub_row.isHidden() == vis:
            self.scrub_row.setVisible(vis)
            self._layout_overlays()

    def _poke_audio(self):
        """Re-apply volume + mute after any player swap (Windows loses the
        volume set before the audio device existed — see VLCPlayer)."""
        if self._closing:
            return
        try:
            self.vlc.set_volume(self.vol_slider.value())
            self.vlc.set_mute(self.btn_mute.isChecked())
        except Exception:  # noqa: BLE001
            pass
        QtCore.QTimer.singleShot(700, self._poke_audio_late)

    def _poke_audio_late(self):
        if self._closing:
            return
        try:
            # BOTH: a volume-only restore can never clear a latched mute
            self.vlc.set_volume(self.vol_slider.value())
            self.vlc.set_mute(self.btn_mute.isChecked())
        except Exception:  # noqa: BLE001
            pass

    def _update_control_state(self):
        """Enable/disable + show/hide controls for the current mode.

        Transport buttons (rewinds, begin, speed) work in DVR chase mode AND
        for movies / series episodes — the whole file exists, so it is
        seekable without timeshift. The DVR button is live-only; a Download
        button takes its place for VOD. LIVE jumps to the buffer's write
        frontier (chase), skips to the file end (VOD) or returns a paused
        live stream to its edge (plain live)."""
        chase = self._mode == "chase"
        vod = self._is_vod()
        for b in (self.btn_back60, self.btn_back10, self.btn_fwd10,
                  self.btn_begin, self.btn_speed):
            b.setEnabled(chase or vod)
        self.btn_live.setEnabled(chase or vod or bool(self.current))
        self.btn_dvr.setEnabled(self._is_dvrable())
        self.btn_dl.setEnabled(vod and not self._downloading)
        self._scrub_on = chase
        self._set_scrub_visible(chase)
        if not chase and not vod:
            self._set_rate(1.0)
        self._apply_button_visibility()
        self._apply_scale()
        self._poke_audio()
        self._refresh_spu_button()

    # ---- per-button visibility (Settings ▸ Playback controls…) ----
    def apply_button_visibility(self):
        self._apply_button_visibility()

    def _apply_button_visibility(self):
        vis = self.config.control_buttons
        compact = self._compact_hidden
        vod = self._is_vod()
        widgets = {
            "back60": self.btn_back60, "back10": self.btn_back10,
            "play": self.btn_play, "fwd10": self.btn_fwd10,
            "begin": self.btn_begin,
            "live": self.btn_live, "dvr": self.btn_dvr, "rec": self.btn_rec,
            "cc": self.btn_cc, "scale": self.btn_scale,
            "speed": self.btn_speed, "mute": self.btn_mute,
            "volume": self.vol_slider,
        }
        for key, w in widgets.items():
            on = bool(vis.get(key, True)) and key not in compact
            if key == "dvr":
                # one slot: DVR for live streams, Download for movies/series
                w.setVisible(on and not vod)
                self.btn_dl.setVisible(on and vod)
            else:
                w.setVisible(on)

        def any_of(*keys):
            return any(vis.get(k, True) and k not in compact for k in keys)

        # separators vanish when the row gets tight (see _apply_compact)
        seps_on = self._compact_level < 2
        self.sep1.setVisible(seps_on and
                             any_of("back60", "back10", "play", "fwd10")
                             and (any_of("begin", "live", "dvr", "rec")
                                  or any_of("cc", "scale", "speed")
                                  or any_of("mute", "volume")))
        self.sep2.setVisible(seps_on and
                             any_of("begin", "live", "dvr", "rec")
                             and (any_of("cc", "scale", "speed")
                                  or any_of("mute", "volume")))
        self.sep3.setVisible(seps_on and
                             any_of("cc", "scale", "speed")
                             and any_of("mute", "volume"))
        self._set_scrub_visible(self._scrub_on)
        self._layout_overlays()

    # ---- popup menus (speed / scale) ----
    def _ctl_menu(self):
        m = QtWidgets.QMenu(self)
        m.setObjectName("ctlMenu")
        m.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        m.aboutToShow.connect(lambda: setattr(self, "_popup_open", True))
        m.aboutToHide.connect(self._ctl_menu_closed)
        return m

    def _ctl_menu_closed(self):
        self._popup_open = False
        self._wake()

    def _popup_above(self, menu, btn):
        hint = menu.sizeHint()
        pos = btn.mapTo(self, QtCore.QPoint(0, -hint.height() - 6))
        menu.popup(self.mapToGlobal(pos))

    def _speed_menu(self):
        if not self.btn_speed.isEnabled():
            return
        m = self._ctl_menu()
        for s in _SPEEDS:
            a = m.addAction(f"{s:g}\u00d7")
            a.setCheckable(True)
            a.setChecked(abs(s - self._rate) < 1e-9)
            a.triggered.connect(lambda *_, s=s: self._set_rate(s))
        self._popup_above(m, self.btn_speed)

    def _set_rate(self, rate):
        rate = max(0.125, min(5.0, float(rate)))
        if self._mode != "chase" and not self._is_vod():
            rate = 1.0   # speed control needs DVR or a seekable file
        self._rate = rate
        try:
            self.vlc.set_rate(rate)
        except Exception:  # noqa: BLE001
            pass
        self.btn_speed.setToolTip(
            f"Playback speed — {rate:g}\u00d7 (DVR mode, movies & series)")

    def _scale_menu(self):
        m = self._ctl_menu()
        for mode, label in (("fit", "Fit (letterbox)"),
                            ("stretch", "Stretch to fill"),
                            ("crop", "Crop to fill")):
            a = m.addAction(label)
            a.setCheckable(True)
            a.setChecked(self._scale_mode == mode)
            a.triggered.connect(lambda *_, mm=mode: self._set_scale_mode(mm))
        self._popup_above(m, self.btn_scale)

    def _set_scale_mode(self, mode):
        self._scale_mode = mode
        try:
            self.config.scale_mode = mode
            self.config.save()
        except Exception:  # noqa: BLE001
            pass
        self._apply_scale()

    def _apply_scale(self):
        try:
            self.vlc.set_scale_mode(self._scale_mode)
            self.vlc.apply_scale(self.surface.width(), self.surface.height())
        except Exception:  # noqa: BLE001
            pass

    # ---- subtitles (embedded stream tracks) ----
    def _enforce_spu(self):
        """Re-assert the user's subtitle choice against the CURRENT media.

        Runs from every tick: VLC re-selects a stream's own subtitle track
        on media opens and ES updates, player swaps lose the selection, and
        remote MKVs report their tracks only seconds after Playing. The
        choice is sticky by NAME — track ids differ between medias, so a
        channel change re-selects e.g. "English" on the new stream when it
        exists and quietly turns subtitles off when it doesn't."""
        if self._closing:
            return
        try:
            tracks = self.vlc.spu_tracks()
            if self._spu_want == -1:
                if self.vlc.active_spu() != -1:
                    self.vlc.set_spu(-1)
            elif self._spu_want not in [tid for tid, _ in tracks]:
                # id unknown here: re-match by name. An EMPTY track list is
                # transient (mid-open ES update, player swap) — keep the
                # sticky choice rather than treating it as "language gone".
                match = None
                if tracks:
                    for tid, name in tracks:
                        if name and name == self._spu_name:
                            match = (tid, name)
                            break
                    if match is None and self._spu_name:
                        low = self._spu_name.lower()
                        for tid, name in tracks:
                            if name and low in name.lower():
                                match = (tid, name)
                                break
                if match is None and tracks:
                    self._spu_want = -1
                    self._spu_name = ""
                    if self.vlc.active_spu() != -1:
                        self.vlc.set_spu(-1)
                elif match is not None:
                    self._spu_want, self._spu_name = match
                    self.vlc.set_spu(self._spu_want)
            elif self.vlc.active_spu() != self._spu_want:
                self.vlc.set_spu(self._spu_want)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_spu_button()

    def _refresh_spu_button(self):
        """Paint the CC button only on state changes — this runs every tick
        and repeated setIcon/tooltip writes repaint the overlay. The button
        is ALWAYS clickable: with no tracks it still opens the settings."""
        try:
            tracks = self.vlc.spu_tracks()
        except Exception:  # noqa: BLE001
            tracks = []
        enabled = bool(tracks)
        on = self._spu_want != -1
        state = (enabled, on, self._spu_name if on else "")
        if state == self._spu_ui:
            return
        self._spu_ui = state
        self.btn_cc.setEnabled(True)
        self.btn_cc.setIcon(ic.cc(on))
        label = self._spu_name if on else "Off"
        self.btn_cc.setToolTip(
            f"Subtitles — {label} (C)" if enabled
            else "Subtitles — settings (none on this stream)")

    def _select_spu(self, track_id: int, name: str = ""):
        """User picked a track from the menu (or -1 for Off)."""
        self._spu_want = int(track_id)
        self._spu_name = name or ""
        try:
            self.vlc.set_spu(self._spu_want)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_spu_button()

    def _cycle_spu(self):
        """C key: Off -> track 1 -> track 2 -> ... -> Off."""
        if self._closing:
            return
        try:
            tracks = self.vlc.spu_tracks()
        except Exception:  # noqa: BLE001
            return
        if not tracks:
            return
        ids = [-1] + [tid for tid, _ in tracks]
        names = {tid: name for tid, name in tracks}
        try:
            idx = ids.index(self._spu_want)
        except ValueError:
            idx = 0
        nxt = ids[(idx + 1) % len(ids)]
        self._select_spu(nxt, names.get(nxt, ""))
        self._flash_spu(nxt, names.get(nxt, ""))

    def _subs_menu(self):
        try:
            tracks = self.vlc.spu_tracks()
        except Exception:  # noqa: BLE001
            tracks = []
        m = self._ctl_menu()
        if tracks:
            off = m.addAction("Off")
        off.setCheckable(True)
        off.setChecked(self._spu_want == -1)
        off.triggered.connect(lambda *_, t=-1, n="": self._select_spu(t, n))
        for tid, name in tracks:
            label = name or f"Track {tid}"
            low = label.lower()
            if "dvb" in low or "teletext" in low:
                label += "  (image \u2014 not adjustable)"
            elif "caption" in low or low.startswith("cc"):
                label += "  (text \u2014 adjustable)"
            a = m.addAction(label)
            a.setCheckable(True)
            a.setChecked(tid == self._spu_want)
            a.triggered.connect(lambda *_, t=tid, n=name:
                                self._select_spu(t, n))
        m.addSeparator()
        a = m.addAction("Subtitle settings\u2026")
        a.triggered.connect(self._open_sub_settings)
        self._popup_above(m, self.btn_cc)

    def _open_sub_settings(self):
        from .subtitle_dialog import SubtitleDialog
        before = subtitle_instance_args(self.config.subtitle_appearance)
        SubtitleDialog(self.config, self._apply_sub_delay,
                       parent=self.window()).exec_()
        if subtitle_instance_args(self.config.subtitle_appearance) != before:
            QtWidgets.QMessageBox.information(
                self.window(), "Restart to apply",
                "The new subtitle style takes effect after you restart "
                "Michael TV.\n"
                "(The delay you set applies immediately.)")

    def _apply_sub_delay(self, ms: int):
        """Delay is the one subtitle setting with a live runtime API —
        applied instantly (and re-applied on player swaps by VLCPlayer)."""
        try:
            self.vlc.set_spu_delay(int(ms))
        except Exception:  # noqa: BLE001
            pass

    def _flash_spu(self, track_id: int, name: str):
        """Brief on-video confirmation while cycling with the keyboard."""
        if self._closing or not self._dvr_status.isHidden():
            return   # the pill is busy with DVR start-up info
        text = name if track_id != -1 else "Off"
        self._set_dvr_status(f"Subtitles: {text}")
        QtCore.QTimer.singleShot(1200,
                                 lambda: self._set_dvr_status("")
                                 if self._dvr_status.text() ==
                                 f"Subtitles: {text}" else None)

    # ---- profanity filter (VOD subtitle track -> timed audio mute) ----
    def _apply_profanity_config(self):
        """Config -> engine (words, pads, sync, lead, on/off)."""
        prof = self.config.profanity
        words = prof.get("words") or [tuple(w) for w in prof_mod.DEFAULT_WORDS]
        self._filter_engine.words = [tuple(w) for w in words]
        self._filter_engine.pad_before_s = \
            int(prof.get("pad_before_ms", 120)) / 1000.0
        self._filter_engine.pad_after_s = \
            int(prof.get("pad_after_ms", 250)) / 1000.0
        self._filter_engine.sync_s = int(prof.get("sync_ms", 0)) / 1000.0
        self._filter_engine.lead_s = int(prof.get("lead_ms", 1500)) / 1000.0
        self._filter_engine.enabled = bool(prof.get("enabled"))

    def apply_profanity_settings(self):
        """The settings dialog saved: re-apply; engage on the current
        channel if it qualifies (live TV -> DVR + caption reader)."""
        self._apply_profanity_config()
        if not prof_mod.PROFANITY_AVAILABLE \
                or not self._filter_engine.enabled or self._closing:
            self._stop_profanity()
            return
        self._on_media_for_profanity((self.current or {}).get("kind"))

    def _on_media_for_profanity(self, kind: str = None):
        """play_media(): fresh media decides whether the filter engages.

        NOTHING here ever changes playback. Live always starts live at the
        edge; the caption-based filter only rides DVR/chase mode, which the
        USER turns on (DVR button) — with the trade-off stated in its
        tooltip. When the filter is enabled but live playback isn't in DVR
        mode, a short notice says how to turn it on. VOD: not covered yet.
        """
        self._stop_profanity()
        if not prof_mod.PROFANITY_AVAILABLE:
            return
        if not self.config.profanity.get("enabled"):
            return
        if kind != "live" or not self._is_dvrable():
            return
        if not find_ccextractor():
            try:
                log.warning("profanity: enabled but CCExtractor not found "
                            "(install CCExtractor for live-TV filtering)")
            except Exception:
                pass
            return
        if self._mode == "chase" and self._cc_source is None:
            self._start_cc_when_buffer(tries_left=40)
        elif not self.btn_dvr.isChecked() and self._dvr_status.isHidden():
            # informative only — never auto-engages anything
            self._set_dvr_status(
                "Profanity filter: press DVR to filter this channel "
                "(watches behind live)")
            QtCore.QTimer.singleShot(
                2600, lambda: self._set_dvr_status("")
                if self._dvr_status.text().startswith("Profanity filter:")
                else None)

    def _start_cc_when_buffer(self, tries_left: int = 40):
        """Wait for the DVR buffer to hold data, then start the caption
        reader joined at the current frontier (~2 s poll, ~80 s max)."""
        if self._closing or tries_left <= 0:
            return
        if not self._filter_engine.enabled or self.dvr is None:
            return
        if self.config.chase_delay < 5:
            # captions trail speech by 1-3 s — a shorter cushion than this
            # cannot mute in time. Never touched the setting, just say so.
            self._set_dvr_status(
                f"Profanity filter: live delay {self.config.chase_delay} s "
                "is too short (needs \u2265 5 s)")
            try:
                log.info("profanity: chase_delay=%d too short, skipping",
                         self.config.chase_delay)
            except Exception:
                pass
            return
        buf = None
        try:
            buf = self.dvr.buffer_file()
        except Exception:  # noqa: BLE001
            pass
        if buf:
            try:
                frontier = self._frontier_s()
            except Exception:  # noqa: BLE001
                frontier = 0.0
            src = CCSource(self)
            src.cue.connect(self._on_cc_cue)
            if src.start(buf, max(0.0, frontier)):
                self._cc_source = src
                try:
                    log.info("profanity: caption reader on %s "
                             "(frontier %.1fs)", buf, frontier)
                except Exception:
                    pass
                if self._mode == "chase" and self._dvr_status.isHidden():
                    self._set_dvr_status("Profanity filter active "
                                         "(captions)")
                    QtCore.QTimer.singleShot(
                        2000, lambda: self._set_dvr_status("")
                        if self._dvr_status.text()
                        == "Profanity filter active (captions)" else None)
                return
        QtCore.QTimer.singleShot(
            2000, lambda: self._start_cc_when_buffer(tries_left - 1))

    def _on_cc_cue(self, start: float, end: float, text: str):
        if self._closing:
            return
        self._filter_engine.add_cue(start, end, text)

    def _start_profanity_extraction(self, at: float = 0.0):
        """Probe the file's subtitle tracks (background thread), then start
        ffmpeg streaming the chosen track out as SRT (see _on_prof_probe)."""
        url = (self.current or {}).get("url", "")
        if not url or self._closing:
            return
        keep = at > 1.0     # catch-up restart keeps the windows it already has
        self._stop_profanity(keep_windows=keep)
        prefer = ""
        if self._spu_name:
            prefer = self._spu_name.split("(")[0].split("-")[0].strip()
        ex = prof_mod.SubtitleExtractor(self)
        ex._prefer_language = prefer.lower()
        self._filter_extractor = ex
        self._filter_at = max(0.0, float(at))
        try:
            log.info("profanity: probing subtitle tracks (prefer %r)", prefer)
        except Exception:
            pass
        self._prof_runner.run(ex.probe_track, url, USER_AGENT)

    def _on_prof_probe(self, result):
        if self._closing or self._filter_extractor is None:
            return
        if self._filter_timer.isActive():
            return   # this session already started (double-probe guard)
        ok, val = result
        if ok != "ok" or not val:
            try:
                log.info("profanity: no usable subtitle track (%r)", result)
            except Exception:
                pass
            return
        url = (self.current or {}).get("url", "")
        if not url:
            return
        ex = self._filter_extractor
        ex.cue.connect(self._on_prof_cue)
        if ex.start(url, USER_AGENT, ex._prefer_language, self._filter_at):
            self._filter_timer.start()
            try:
                log.info("profanity: extracting subtitle track #%s",
                         ex._want_index)
            except Exception:
                pass
        else:
            try:
                ex.cue.disconnect()
            except Exception:
                pass
            self._filter_extractor = None

    def _on_prof_cue(self, start: float, end: float, text: str):
        if self._closing:
            return
        self._filter_engine.add_cue(start, end, text)

    def _filter_tick(self):
        """100 ms: apply the filter mute for the current playback position.

        Live/chase: the tracked position IS buffer content time — the same
        clock the caption cues live on. VOD would use the file clock (VOD
        support arrives with a later engine)."""
        if self._closing or not self._filter_engine.enabled \
                or not self._filter_engine.windows:
            return
        if self._mode == "chase":
            self._filter_engine.evaluate(self._vid_s)
        elif self._is_vod():
            t = self._vid_s if self._vid_s > 0.0 else \
                max(0.0, self.vlc.get_time() / 1000.0)
            self._filter_engine.evaluate(t)

    def _stop_profanity(self, keep_windows: bool = False):
        """Kill the caption reader / extractor + clear the filter mute."""
        if self._cc_source is not None:
            try:
                self._cc_source.cue.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._cc_source.stop()
                self._cc_source.deleteLater()
            except Exception:  # noqa: BLE001
                pass
            self._cc_source = None
        if self._filter_extractor is not None:
            try:
                self._filter_extractor.cue.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._filter_extractor.stop()
                self._filter_extractor.deleteLater()
            except Exception:  # noqa: BLE001
                pass
            self._filter_extractor = None
        try:
            self._filter_timer.stop()
        except Exception:  # noqa: BLE001
            pass
        if keep_windows:
            self._filter_engine.set_muted(False)
        else:
            self._filter_engine.clear()












    def _on_focus_changed(self, _old, now):
        """Hide the on-video overlays when the app loses focus (another app
        took it, or the window was minimized): the ToolTip-style overlay
        window would otherwise stay painted on top of OTHER apps."""
        if self._closing:
            return
        if now is None and QtWidgets.QApplication.activeWindow() is None \
                and not self._app_foreground():
            if self.overlay.isVisible():
                self._overlay_suppressed = True
                self._overlay_was_visible = True
                self.overlay.hide()
        elif now is not None and self._overlay_suppressed:
            self._overlay_suppressed = False
            if getattr(self, "_overlay_was_visible", False) \
                    and not self._closing:
                self.overlay.show()
                self._layout_overlays()
            self._overlay_was_visible = False











    def stop(self):
        """Ordered full teardown. Every step is wrapped in try/except + log
        so one failure can never block the rest (the app must still exit
        quickly and silently). Runs for Playback > Stop too — play_media()
        re-arms the tick timer and guards afterwards.

        (a) halt the 400 ms tick timer + overlay timers and bump the session
            generation — after this no timer/single-shot callback may touch
            VLC (pending chase chains abort on the generation check),
        (b) release the REC output bookkeeping, then stop the DISPLAY
            player — it may hold an open handle on the DVR buffer file,
        (c) recorder safe_stop + buffer delete: only now, with BOTH players
            idle, can the mtp_dvr_* temp dir be deleted on Windows (the
            display player must go first or the delete races an open handle
            and strands the folder),
        (d) reset mode/UI state. The caller (closeEvent) then saves state
            and accepts the close.
        """
        self._closing = True
        # (a) timers first — no _tick may observe a half-torn-down player.
        try:
            self.timer.stop()
            self.hide_timer.stop()
            self.info_timer.stop()
            self.cursor_timer.stop()
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("stop: timer shutdown failed: %r", exc)
            except Exception:
                pass
        # Profanity filter: kill the ffmpeg extractor and clear the filter
        # mute BEFORE the player goes away (engine.clear() touches VLC).
        try:
            self._stop_profanity()
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("stop: profanity shutdown failed: %r", exc)
            except Exception:
                pass
        try:
            log.info("stop: timers halted; session %d -> %d",
                     self._session, self._session + 1)
        except Exception:
            pass
        self._session += 1        # invalidate deferred DVR callbacks
        self._pending_media = None
        self.current = None
        # Hide the on-video overlay IMMEDIATELY — before any VLC teardown.
        # A wedged libvlc stop can leave the process (and with it this
        # frameless overlay window) alive for a while after the main window
        # closes; that was the "controls stuck on screen after closing the
        # app" glitch.
        try:
            self.ctl.hide()
            self._dvr_status.hide()
            self.info_overlay.hide()
            self.overlay.hide()
        except Exception:
            pass
        # (b) REC bookkeeping + display player down.
        try:
            self._stop_recording(stopping=True)
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("stop: record finalize failed: %r", exc)
            except Exception:
                pass
        try:
            self.vlc.stop_and_release()
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("stop: display player release failed: %r", exc)
            except Exception:
                pass
        # (c) recorder safe_stop + temp buffer delete (never raises).
        try:
            self._ensure_dvr_stopped()
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("stop: recorder cleanup failed: %r", exc)
            except Exception:
                pass
        # (d) mode/UI reset.
        self._mode = "live"
        self._chase_paused = False
        self._dvr_t0 = None
        self._dvr_base = 0.0
        self._reset_dvr_clock()
        self._stall_ticks = 0
        self._last_reopen = 0.0
        self._chase_started = False
        self._vid_s = 0.0
        self._last_raw = None
        self._live_paused = False
        self._scrub_on = False
        self._set_rate(1.0)
        try:
            self._update_control_state()
            self.btn_play.setIcon(ic.play())
            self.ctl.hide()
            self._dvr_status.hide()
            self.info_overlay.hide()
            self.overlay.hide()
            self.unsetCursor()
        except Exception:
            pass
        try:
            log.info("stop: teardown complete")
        except Exception:
            pass

    def keyPressEvent(self, event):
        key = event.key()
        if key == QtCore.Qt.Key_Space:
            self._toggle_pause()
        elif key == QtCore.Qt.Key_Left:
            self._seek_ms(-10000)
        elif key == QtCore.Qt.Key_Right:
            self._seek_ms(10000)
        elif key == QtCore.Qt.Key_Down:
            self._seek_ms(-60000)
        elif key == QtCore.Qt.Key_Up:
            self._seek_ms(60000)
        elif key == QtCore.Qt.Key_M:
            self.btn_mute.toggle()
        elif key == QtCore.Qt.Key_C:
            self._cycle_spu()
        else:
            super().keyPressEvent(event)