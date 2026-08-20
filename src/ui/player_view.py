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
from .. import vod_splitter
from ..vod_splitter import VodRelay
from ..mkv_subs import is_text_codec, is_language_name, lang_matches
from . import icons as ic
from .caption_overlay import (CaptionOverlay, CueStore, displayed_video_rect)
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
# the minimum spacing between watchdog reopens of the buffer. 5 s also keeps
# a caption cushion after "jump to live": CCExtractor trails the write head by
# a couple of seconds, so the app-rendered captions still have data to show
# right at the clamped frontier.
_CHASE_SAFETY_S = 5.0
_REOPEN_COOLDOWN_S = 5.0

# Chase re-engagement after a fallback: when a recorder engagement gives
# up (~20 s without data), plain live plays while these bounded, growing
# backoffs retry the buffer — and with it rewind/speed — automatically.
# The budget resets on every channel change (play_media).
_CHASE_RETRY_DELAYS = (10.0, 30.0)

# Live-CC pipeline lag (buffer-tail poll + CCExtractor processing + SRT
# harvest), compensated when arrival-anchoring live cues: without it every
# caption would display this much after its speech. ~1 s typical (250 ms
# tail poll + CCX decode + 250 ms harvest + frontier tick quantization);
# the subtitle delay setting (±) covers personal taste on top.
_CC_LAG_S = 1.0

# Live anchor smoothing: each fresh cue re-derives the CCX->app offset,
# but per-cue estimates jitter with the pipeline (burst flushes, poll
# phase). An EWMA settles captions on the MEAN lag instead of wiggling
# cue-to-cue; it converges within a handful of cues and still tracks
# slow drift (fresh cues keep arriving).
_CC_ANCHOR_ALPHA = 0.35

# Long-buffer live engage: CCExtractor joins the DVR buffer this many
# seconds behind the current playback position (see _start_cc_when_buffer)
# instead of replaying the whole file — the arrival anchor absorbs the
# exact placement. Kept small: CCX chews 4K HEVC slower than real time,
# so every skipped second is that much faster to first caption.
_CC_JOIN_BACK_S = 8.0
_CC_JOIN_MIN_FRONTIER_S = 90.0   # below this, a byte-0 join is instant anyway

# Playback speeds offered by the speed button (chase mode / VOD).
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
        self._chase_fail_count = 0   # give-ups this channel (retry budget)
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
        self._video_wh = (0, 0)  # decoded video size (see _poll_video_size)
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
        self._cc_source = None        # live closed-caption reader
        self._vod_relay = None        # VOD splitter (single-connection)
        self._relay_start_offset = 0  # byte offset for the NEXT relay start
        #                                 # (resume / mid-movie subtitle
        #                                 # engage — consumed by
        #                                 # _effective_url)
        # caption overlay: app-rendered subtitles, one style for every
        # text source (live CC via CCExtractor, VOD SRT via the relay)
        self._cap_cues = CueStore()   # every cue, both sources
        self._cap_on = False          # the overlay owns caption rendering
        self._cap_want = False        # user picked a text track (sticky)
        self._cap_fail = False        # source dead this media: VLC renders
        self._cap_vod_tries = 0       # _cap_vod_check retries (head parse)
        self._cap_clock_s = 0.0       # caption timing clock (VLC display
        #                              # position; see _caption_clock_s)
        self._cap_raw_s = None        # last raw get_time() seen by the
        #                              # caption clock (freeze/jump guard)
        self._cap_wall = 0.0          # wall time of the last caption-clock
        #                              # update (integration dt)
        # live-CC arrival anchor: CCX's PTS axis drifts against VLC's
        # clock (probe: 12-32 s, growing), so every FRESH cue re-anchors a
        # running offset that maps CCX times onto the app's frontier axis
        # (None until the first steady cue — catch-up bursts are untrusted)
        self._cc_off = None           # live cue offset, CCX s -> app s
        self._cc_last_c = None        # end-time of the last anchored cue
        self._cc_last_t = 0.0         # wall time of the last anchor
        self._cap_timer = QtCore.QTimer(self)
        self._cap_timer.setInterval(100)
        self._cap_timer.timeout.connect(self._caption_tick)
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
        # app-rendered subtitles — created BEFORE the controls so it stays
        # beneath them in the stacking order (never raised either)
        self._cap_wid = CaptionOverlay(self.overlay)
        self._cap_wid.bind_config(lambda: self.config.subtitle_appearance)
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
        # replaces the Record button while a movie / series episode plays
        # (the file is already fully seekable — there is nothing to
        # timeshift; recording a stream re-encodes, a download is verbatim)
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
                                              "(live rewind, movies & series)")
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
                  self.btn_dl, self.btn_rec, self.sep2,
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
        # caption overlay: anchored to the DISPLAYED picture — the rect the
        # video paints in, letterboxed under fit / full surface under crop
        # and stretch (see displayed_video_rect) — so a 2.35:1 movie shows
        # the same caption size and bottom anchor relative to the PICTURE
        # as a 16:9 channel does at the same window size. Unknown video
        # size (nothing decoded yet): the whole surface, the historic
        # behavior (re-laid out the moment the size arrives).
        vx, vy, vw, vh = displayed_video_rect(
            self._video_wh, self._scale_mode, g.width(), g.height())
        cap = QtCore.QRect(g.left() + vx, g.top() + vy, vw, vh)
        if self._cap_wid.geometry() != cap:
            self._cap_wid.setGeometry(cap)
        if not self.ctl.isHidden():
            # the control bar spans the whole SURFACE bottom — the inset is
            # measured from the picture bottom, so a letterboxed picture
            # (bottom above the bar) needs less of it to clear the bar
            bar_top = g.height() - self.ctl.height() - 10
            self._cap_wid.set_bottom_inset(
                max(24, (vy + vh) - bar_top + 4))
        else:
            self._cap_wid.set_bottom_inset(24)

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

    def play_media(self, playable: dict, start_at: float = 0.0):
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
        # The OLD media's caption/filter machinery: the VOD relay holds a
        # provider connection and must release it BEFORE the new URL opens
        # (the account allows one connection — a leaked relay stalled the
        # next channel). The overlay drops its claim on those sources
        # first (while it owns them, _stop_profanity keeps them alive),
        # then the live caption reader + filter windows go.
        self._set_cap_on(False)
        self._cap_fail = False
        self._stop_profanity()
        self._cap_cues.clear()
        # fresh live-CC arrival anchor + tracked caption clock per media
        self._cc_off = None
        self._cc_last_c = None
        self._cc_last_t = 0.0
        self._cap_clock_s = 0.0
        self._cap_raw_s = None
        self._cap_wall = 0.0
        self.btn_rec.blockSignals(True)
        self.btn_rec.setChecked(False)
        self.btn_rec.setIcon(ic.rec(False))
        self.btn_rec.blockSignals(False)
        self._mode = "live"
        self._chase_paused = False
        self._dvr_t0 = None
        self._dvr_base = 0.0
        self._reset_dvr_clock()
        self._stall_ticks = 0
        self._last_reopen = 0.0
        self._chase_started = False
        # fresh chase budget: a channel that gave up on the buffer gets to
        # try again from scratch (pending retry timers die on the session
        # bump at the top of play_media)
        self._chase_fail_count = 0
        self._dvr_status.hide()
        self._scrub_on = False
        self._vid_s = 0.0
        self._last_raw = None
        self._video_wh = (0, 0)   # next media's size is unknown until the
                                  # tick polls it (captions re-anchor then)
        self._live_paused = False
        self._set_rate(1.0)
        self._update_control_state()
        self._apply_scale()
        url = playable.get("url", "")
        if kind == "live" and url:
            # Live TV ALWAYS runs through the DVR chase pipeline (user
            # approved ~5 s behind live in exchange for unified captions):
            # the recorder opens the single connection, playback watches
            # the buffer. No direct network playback on the display player.
            self._engage_chase()
        else:
            # VOD / series: the whole file exists — no timeshift wanted
            # (VLC's input-timeshift drifts A/V sync on seekable files and
            # fights the local relay). Resumes mark the relay session
            # (head prefetched separately; VLC's own seek then drives the
            # main provider stream — see VodRelay.start).
            self._relay_start_offset = 1 if start_at > 3.0 else 0
            self.vlc.play(self._effective_url(url, kind),
                          timeshift=False, start_seconds=start_at)
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
            self._cap_clock_s = float(target)   # captions jump with the
            #                                   # seek, not a beat later
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
        self._cap_clock_s = float(target)
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
                # Re-base the tracked position: the tick's snap guard
                # (|raw - _vid_s| <= 3 s) would otherwise reject VLC's
                # post-seek clock as "too far" and drag the handle back
                # to the pre-drag position.
                self._vid_s = self.slider.value() / 1000.0
        finally:
            # ALWAYS clear the drag flag — a stuck True froze the scrubber
            # timestamps until the next successful drag.
            self._seeking = False

    # ---- chase mode (single connection: the recorder owns the stream) ----
    def _engage_chase(self):
        """Start the always-on live pipeline: the recorder opens the single
        provider connection and playback watches the DVR buffer a few
        seconds behind the frontier (config.chase_delay, floor 5 s).

        ONE connection, strict handoff order:
          1) the DISPLAY player stops the network URL first (when REC was
             on in fallback-live mode this also stops its record output),
          2) the recorder opens the single connection — with the kept
             recording file as a second output when REC is on (dual
             output), so no second vlc.play(url) is ever issued,
          3) once the buffer holds data the display player switches to
             watching the buffer file behind the live edge.
        The display player never plays the network URL while the recorder
        runs."""
        self._session += 1   # deferred chase callbacks from before are stale
        if self._mode == "chase" and self.dvr and self.dvr.running:
            return            # already chasing (e.g. REC engaged it)
        self._dvr_t0 = time.time()
        self._dvr_base = 0.0
        self._reset_dvr_clock()
        self._stall_ticks = 0
        self._last_reopen = 0.0
        self._chase_started = False
        self.vlc.stop_and_release()                 # (1)
        self._ensure_dvr_stopped()                  # drop any stale buffer
        record = self.btn_rec.isChecked()
        self._restart_recorder(record=record)       # (2)
        # Caption reader starts NOW, in parallel with the chase fill wait:
        # CCExtractor's spawn + first chew of the young buffer overlap the
        # ~2.5 s buffer wait, so the first caption text is ready about when
        # the video starts instead of seconds after it ("captions as soon
        # as the stream plays"). _start_cc_when_buffer polls until the
        # buffer file exists; _start_chase_now's call is a no-op then.
        if (self._filter_engine.enabled or self._cap_want) \
                and self._cc_source is None and not self._closing:
            self._start_cc_when_buffer(tries_left=40)
        self._wait_and_enter_chase(self._session)   # (3)

    def _exit_chase_to_live(self):
        """Leave chase mode and return to the plain live stream.

        Failure fallback only (the recorder never produced data — provider
        blocked it / network down); nothing calls it directly; the give-up
        path goes through _fallback_from_chase, which re-engages chase
        automatically afterwards. Safe order (ONE provider connection at
        all times): the display player stops first (it holds the buffer
        file handle), then the recorder and its temp dir go, and only then
        does the display player dial the live URL again.
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

    def _fallback_from_chase(self, waited_s: float):
        """A chase engagement gave up: play plain live NOW (never a dead
        screen), then re-engage chase automatically — bounded retries with
        growing backoff, every step announced on the DVR pill. When the
        budget is spent the transport controls stay honest (rewind needs a
        buffer) and the next channel change starts a fresh budget."""
        gen = self._session
        self._chase_fail_count += 1
        self._exit_chase_to_live()
        delays = _CHASE_RETRY_DELAYS
        n = self._chase_fail_count
        if n <= len(delays):
            delay = delays[n - 1]
            self._chase_note(f"Live buffer failed \u2014 retrying in "
                             f"{delay:.0f} s ({n}/{len(delays)})")
            QtCore.QTimer.singleShot(
                int(delay * 1000),
                lambda: self._retry_chase_after_fallback(gen))
        else:
            try:
                log.warning("chase: gave up after %d attempts (last wait "
                            "%.1fs) -- plain live; retry on channel change",
                            n, waited_s)
            except Exception:
                pass
            self._chase_note(
                "Timeshift unavailable on this channel \u2014 rewind "
                "disabled; retries when you change channels",
                clear_after_ms=10000)

    def _retry_chase_after_fallback(self, gen: int):
        """Backed-off automatic chase re-engagement (see
        _fallback_from_chase). Aborts when a newer session owns the player,
        chase already found its way back, or an engagement is in flight."""
        if gen != self._session or self._closing:
            return   # channel changed / view stopped
        if self._mode != "live" or not self._is_dvrable():
            return   # chase already back (e.g. REC engaged it) / not live
        if self.dvr and self.dvr.running:
            return   # an engagement is already in flight
        try:
            log.info("chase: automatic re-engagement (attempt %d)",
                     self._chase_fail_count + 1)
        except Exception:
            pass
        self._engage_chase()

    def _chase_note(self, text: str, clear_after_ms: int = 6000):
        """Chase status on the DVR pill, auto-cleared a few seconds later
        (pass 0 to keep it until something else uses the pill)."""
        if self._closing:
            return
        self._set_dvr_status(text)
        if clear_after_ms:
            QtCore.QTimer.singleShot(
                clear_after_ms,
                lambda: self._set_dvr_status("")
                if self._dvr_status.text() == text else None)

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
        live = (self.current or {}).get("kind") == "live"
        if rec:
            self.vlc.play_at(url, record_path=rec, timeshift=live)
        else:
            self.vlc.play(url, timeshift=live)
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
        """Show/hide the small 'Buffering…' pill (chase buffer filling)."""
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

        ``gen`` is the session generation captured when the engagement
        started. If the channel changed or the view stopped in the meantime,
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
        # no fill pill: the info banner already names the channel, and the
        # brief fill (chase_delay ~5 s) reads as normal startup
        if tries_left > 0:
            QtCore.QTimer.singleShot(
                400, lambda: self._wait_and_enter_chase(gen, tries_left - 1)
            )
        else:
            # Recorder failed (provider blocked it / network) — revert to
            # the plain live stream instead of hanging on a black screen;
            # _fallback_from_chase then re-engages chase automatically.
            try:
                log.warning("chase wait: gave up after %.1fs -- back to "
                            "direct live", waited)
            except Exception:
                pass
            self._fallback_from_chase(waited)

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
        self._chase_fail_count = 0    # chase runs: retry budget re-armed
        self._stall_ticks = 0
        self._set_rate(1.0)
        self._update_control_state()
        # profanity filter / caption overlay: begin reading captions from
        # the buffer now (ONE CCSource serves both)
        if (self._filter_engine.enabled or self._cap_want) \
                and self._cc_source is None and not self._closing:
            self._start_cc_when_buffer(tries_left=25)
        if self._cap_want and not self._cap_fail:
            self._set_cap_on(True)
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

        Aborts silently when the session changed (channel switch / stop)
        so a stale tick can never call play_at. The classic trigger
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
                # Recorder already owns the single connection (chase mode):
                # restart it with the recording file as a second output
                # (buffer keeps growing).
                self._restart_recorder(record=True)
            elif self.current.get("kind") == "live":
                # Fallback direct-live mode (the recorder gave up earlier):
                # recording re-engages the single-connection chase pipeline
                # so the timeline is scrubbable/seekable.
                self._engage_chase()
            else:
                # VOD: unreachable from the UI (REC is swapped for Download
                # on movies/series — see _apply_button_visibility for why)
                # and kept only as a safety net. It re-dials the RAW url
                # and restarts from 0, so it must never run while the
                # caption relay holds the provider connection.
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
            # Chase needs the single-connection recorder regardless: drop
            # only the recording output — the buffer keeps growing and the
            # display player stays where it is. NEVER play the network URL
            # here: that would open a second connection.
            self._restart_recorder(record=False)
        elif self.current and self.current.get("url") and self._mode == "live":
            # Fallback direct-live / VOD mode: the display player carried
            # the record output, restart it as a plain viewer (still one
            # connection).
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
                self.info_overlay, self._dvr_status, self._cap_wid)):
            self.overlay.hide()   # nothing left to show over the video
        # captions may sit lower now that the control bar is gone
        self._layout_overlays()

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

    def _poll_video_size(self):
        """Track the decoded video size; re-layout when it becomes known,
        changes, or goes away with the media.

        The caption overlay anchors to the DISPLAYED picture, which is
        only computable once the video size is known — until then
        _layout_overlays keeps the whole-surface behavior. (0, 0) is the
        "unknown" marker; missing video_size on stub players maps there
        too, so tests/fakes keep working untouched."""
        if self._closing:
            return
        try:
            wh = self.vlc.video_size()
        except Exception:  # noqa: BLE001
            wh = (0, 0)
        if wh != self._video_wh:
            self._video_wh = wh
            self._layout_overlays()
            # A size transition ((0,0) <-> real) brackets the vout swap of a
            # stream switch: VLC destroys and recreates its native window
            # under the translucent overlay, and Windows can leave the old
            # pixels of the layered window behind (ghosted buttons, duplicated
            # slider handles). A synchronous full repaint clears the trails.
            if self.overlay.isVisible():
                self.overlay.repaint()

    def _tick(self):
        # Teardown guard: once stop() has run, no timer may touch VLC —
        # the player instance may already be released.
        if self._closing:
            return
        self._poll_video_size()
        playing = self.vlc.is_playing()
        # Only swap the icon when the state actually flipped: setIcon on a
        # translucent top-level overlay schedules a full recomposition of
        # the layered window, and doing that every tick mid-stream-switch
        # left ghost pixels behind (play/pause drawn on top of each other,
        # duplicated slider handles, stacked mute icons).
        if playing != self._was_playing:
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
            # Self-heal: while chasing, every mode-gated control must be
            # usable — a plain-live tick that ran right before the mode
            # flipped (fallback → successful retry) leaves the transport
            # buttons disabled and the scrubber hidden otherwise.
            for b in (self.btn_back60, self.btn_back10, self.btn_fwd10,
                      self.btn_begin, self.btn_speed, self.btn_live):
                if not b.isEnabled():
                    b.setEnabled(True)
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

        # Live / VOD: the scrubber only appears for VOD (known length).
        # Plain live is the chase-give-up fallback (_fallback_from_chase):
        # there is no buffer behind the playhead, so the rewinds/begin/
        # speed stay honestly DISABLED — the pill explains why, and a retry
        # or the next channel change re-engages chase and re-enables them.
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

        Live TV is always in DVR chase mode (recorder on the single
        connection, playback watching the buffer), so the transport buttons
        (rewinds, begin, speed) are enabled for live AND movies / series —
        the whole file/buffer exists, so both are seekable. LIVE jumps to
        the buffer's write frontier (chase) or skips to the file end (VOD).
        A Download button replaces Record for movies / series."""
        chase = self._mode == "chase"
        vod = self._is_vod()
        for b in (self.btn_back60, self.btn_back10, self.btn_fwd10,
                  self.btn_begin, self.btn_speed):
            b.setEnabled(chase or vod)
        self.btn_live.setEnabled(chase or vod or bool(self.current))
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
            "live": self.btn_live, "rec": self.btn_rec,
            "cc": self.btn_cc, "scale": self.btn_scale,
            "speed": self.btn_speed, "mute": self.btn_mute,
            "volume": self.vol_slider,
        }
        for key, w in widgets.items():
            on = bool(vis.get(key, True)) and key not in compact
            if key == "rec":
                # One slot, deliberately swapped by content kind — NOT just
                # cosmetics. REC on VOD would restart playback through VLC's
                # record output: from position 0, re-encoded, and on the RAW
                # provider URL, which dials a SECOND provider connection
                # whenever playback runs through the caption relay (the
                # account allows one) and breaks the overlay's cue feed.
                # Download covers the same want verbatim, in the background,
                # without touching playback. Evaluated end-to-end and
                # deliberately left as the swap.
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
                             and (any_of("begin", "live", "rec")
                                  or any_of("cc", "scale", "speed")
                                  or any_of("mute", "volume")))
        self.sep2.setVisible(seps_on and
                             any_of("begin", "live", "rec")
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
            f"Playback speed — {rate:g}\u00d7 (live rewind, movies & series)")

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
        # fit <-> crop <-> stretch moves the displayed picture rect: the
        # caption overlay must re-anchor to it immediately
        self._layout_overlays()

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
        exists and quietly turns subtitles off when it doesn't.

        While the caption overlay owns a text track, VLC's own spu stays
        OFF here (double rendering would otherwise flash on ES updates);
        the sticky choice is kept so a fallback re-selects it instantly."""
        if self._closing:
            return
        try:
            if self._cap_on:
                if self.vlc.active_spu() != -1:
                    self.vlc.set_spu(-1)
                self._refresh_spu_button()
                return
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
                    if self._cap_eligible(self._spu_name):
                        # sticky text track matched on the new media — the
                        # overlay takes the rendering back
                        self._engage_caption_overlay()
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
        enabled = bool(tracks) or self._cap_on
        on = self._spu_want != -1 or self._cap_on
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
        if track_id != -1 and self._cap_eligible(name):
            # text track: the app overlay renders it (VLC's spu stays off)
            self._engage_caption_overlay()
        else:
            # Off, or an image/bitmap track VLC must render itself
            self._disengage_caption_overlay()
            try:
                self.vlc.set_spu(self._spu_want)
            except Exception:  # noqa: BLE001
                pass
        self._refresh_spu_button()

    # ---- caption overlay (app-rendered subtitles, one style) ----
    @staticmethod
    def _cap_track_kind(name: str) -> str:
        """Classify a VLC subtitle track by name: 'text' (the overlay can
        render it), 'bitmap' (VLC must render it), 'ass' (VLC on live;
        flattened by the relay on VOD), or 'other'. VLC names MKV SRT
        tracks plainly ('English (United States) - [English]') so 'other'
        is treated as overlay fodder on VOD and left on VLC for live
        (where DVB subs can carry plain names too)."""
        low = (name or "").lower()
        if any(w in low for w in ("dvb", "teletext", "pgs", "bitmap",
                                  "image", "vobsub")):
            return "bitmap"
        if re.search(r"\bass\b|\bssa\b", low):
            return "ass"
        if ("caption" in low or low.startswith("cc") or "608" in low
                or "708" in low or "srt" in low or "subrip" in low
                or "utf8" in low or "text" in low):
            return "text"
        return "other"

    def _cap_eligible(self, name: str) -> bool:
        """Can the overlay render this track? Text always; ASS and plain
        names only on VOD (the relay's MKV parser flattens ASS to text
        there — live has no ASS tracks, so VLC keeps those)."""
        kind = self._cap_track_kind(name)
        return (kind == "text"
                or (kind in ("other", "ass") and self._is_vod()))

    @staticmethod
    def _cap_lang_hint(name: str) -> str:
        """The language word out of a VLC track name, for the parser's
        prefer-language match. VLC names MKV tracks two ways: with the
        track's own name ('English (United States) - [English]') or,
        when the TrackEntry has none, 'Track 2 - [English]' — the bare
        head word there is 'track', so the bracketed language is the
        only usable hint."""
        n = name or ""
        head = n.split("(")[0].split("-")[0].strip().lower()
        hint = head.split()[0] if head.split() else ""
        if not is_language_name(hint):
            hint = ""                       # junk head word ("track") —
            m = re.search(r"\[([^\]]+)\]", n)   # try the bracket instead
            if m:
                words = m.group(1).strip().lower().split()
                if words and is_language_name(words[0]):
                    hint = words[0]
        return hint

    def _engage_caption_overlay(self):
        """The user picked a text subtitle track: render it in the app
        overlay with ONE style. Live: CCSource tails the DVR buffer (zero
        extra connections). VOD: playback routes through the local relay
        so the MKV parser can feed cues (restarts in place when the relay
        isn't up yet)."""
        if self._closing or self._cap_fail:
            return
        self._cap_want = True
        if self._is_dvrable():
            if not find_ccextractor():
                # caption pipeline unavailable: VLC renders this media
                self._cap_fail = True
                self._cap_note("Captions: VLC rendering "
                               "(CCExtractor unavailable)")
                return
            self._set_cap_on(True)
            if self._mode == "chase" and self.dvr:
                self._start_cc_when_buffer(tries_left=75)
            # buffer still filling (or REC pre-chase): _start_chase_now
            # engages the moment chase playback enters
        elif self._is_vod():
            if self._vod_relay is not None:
                self._set_cap_on(True)
                if self._vod_relay.set_prefer_language(
                        self._cap_lang_hint(self._spu_name)):
                    # different language: the old cues in the store are
                    # the wrong language now — drop them (the tap
                    # re-anchors at the frontier immediately and
                    # backfills the window behind it)
                    self._cap_cues.clear()
                # late pick on an already-running relay: verify the tap
                # really has a matching text track (a dead tap or a
                # bitmap-only pick hands rendering back to VLC)
                self._schedule_cap_vod_check()
            else:
                self._restart_through_relay()

    def _disengage_caption_overlay(self):
        """Subtitles Off (or a VLC-rendered track): stop the overlay. The
        live caption reader KEEPS RUNNING — it reads only the local DVR
        buffer (no provider connection) and re-enabling captions is then
        instant instead of paying CCExtractor's catch-up pass again. It is
        torn down on channel change / stop (play_media, _stop_profanity)."""
        self._cap_want = False
        self._set_cap_on(False)

    def _set_cap_on(self, on: bool):
        """Flip who owns caption rendering: the overlay or VLC's spu."""
        on = bool(on) and not self._cap_fail and not self._closing
        if on == self._cap_on:
            return
        self._cap_on = on
        if on:
            self._cap_timer.start()
            try:
                self.vlc.set_spu(-1)   # VLC's own rendering OFF below the
            except Exception:          # overlay (fallback re-enables it)
                pass
        else:
            self._cap_timer.stop()
            self._cap_wid.set_lines([])
        self._refresh_spu_button()

    def _caption_clock_s(self) -> float:
        """The playback position captions must key on: what VLC is
        DISPLAYING right now.

        Chase mode: a TRACKED copy of VLC's clock on the buffer file. The
        raw get_time() on these streams is often garbage broadcast PTS —
        frozen for 5-15 s, or jumping — and keying captions on it made
        captions freeze/repeat with it. The tracked clock snaps to VLC
        whenever the reading is alive and sane (forward moves up to the
        frontier are accepted: a genuine PTS discontinuity moves the cue
        axis too), and otherwise integrates from wall time like _vid_s.

        VOD: the relay's cue times are the file's cluster timecodes — the
        same axis as a healthy get_time(), used raw.
        """
        try:
            ms = self.vlc.get_time()
        except Exception:  # noqa: BLE001
            ms = -1
        raw = ms / 1000.0 if 0 <= ms < 86_400_000 else -1.0
        chase = self._mode == "chase" and self.dvr is not None
        if not chase:
            if raw >= 0.0:
                self._cap_clock_s = raw
                self._cap_raw_s = raw
            return self._cap_clock_s if self._cap_clock_s > 0.0 \
                else self._vid_s
        # chase: tracked clock (garbage-PTS-proof)
        now = time.time()
        frontier = self._frontier_s()
        dt = 0.1 if self._cap_wall <= 0.0 else min(1.0, now - self._cap_wall)
        alive = raw >= 0.0 and raw != self._cap_raw_s
        had_reading = ((self._cap_raw_s is not None
                        and self._cap_raw_s >= 0.0)
                       or self._cap_clock_s > 0.0)
        if alive and 0.0 <= raw <= frontier + 30.0 \
                and raw >= self._cap_clock_s - 10.0:
            self._cap_clock_s = min(raw, frontier)
        elif had_reading:
            # the clock exists but is silent (frozen broadcast PTS),
            # jumped backward hard (garbage), or reads -1 mid-reopen —
            # integrate while actually playing, hold while not
            try:
                playing = self.vlc.is_playing()
            except Exception:  # noqa: BLE001
                playing = True
            if playing and not self._chase_paused:
                self._cap_clock_s = min(frontier,
                                        self._cap_clock_s + dt * self._rate)
        else:
            # no valid VLC reading yet at all (startup): follow the
            # UI-tracked position until one shows up
            self._cap_clock_s = max(0.0, min(self._vid_s, frontier))
        self._cap_raw_s = raw
        self._cap_wall = now
        return self._cap_clock_s

    def _caption_tick(self):
        """100 ms: paint the cue active at the playback position (+ the
        user's delay — pure arithmetic, so the delay applies live)."""
        if self._closing or not self._cap_on:
            return
        try:
            if (self._mode == "chase" and self.dvr) or self._is_vod():
                t = self._caption_clock_s()
            else:
                return
            delay_ms = int(self.config.subtitle_appearance.get(
                "delay_ms", 0) or 0)
            lines = self._cap_cues.text_at(t + delay_ms / 1000.0)
            if lines and self._filter_engine.enabled:
                words = self._filter_engine.words
                lines = [prof_mod.mask_text(ln, words) for ln in lines]
            self._cap_wid.set_lines(lines)
        except Exception:  # noqa: BLE001
            pass

    def _cap_note(self, text: str):
        """Brief on-video note (caption fallbacks etc.)."""
        if self._closing or not self._dvr_status.isHidden():
            return
        self._set_dvr_status(text)
        QtCore.QTimer.singleShot(
            2500, lambda: self._set_dvr_status("")
            if self._dvr_status.text().startswith(text) else None)

    def _cap_source_failed(self, why: str):
        """CCSource died (CCExtractor exit / buffer rotation): hand the
        session to VLC's spu rendering — playback never depends on the
        overlay, and _enforce_spu restores the chosen track by name."""
        if self._closing or self._cap_fail:
            return
        try:
            log.warning("captions: source failed (%s) — VLC renders", why)
        except Exception:
            pass
        self._cap_fail = True
        self._set_cap_on(False)
        self._stop_cc_source()
        self._cap_note("Captions: switched to VLC rendering")

    def _cap_relay_failed(self, why: str):
        """VodRelay's subtitle side died mid-session (tap crash): captions
        hand back to VLC's spu — playback keeps streaming whatever the
        relay has cached. Start-time failures never reach here (the relay
        isn't attached yet); _effective_url handles those."""
        if self._closing or self._cap_fail or self._vod_relay is None:
            return
        try:
            log.warning("captions: relay failed (%s) — VLC renders", why)
        except Exception:
            pass
        self._cap_fail = True
        self._set_cap_on(False)
        try:
            self.vlc.set_spu(self._spu_want)
        except Exception:  # noqa: BLE001
            pass
        self._cap_note("Subtitles: switched to VLC rendering")

    def _restart_through_relay(self):
        """Re-open the current VOD through the local relay so the caption
        overlay can read its subtitle track (ONE connection: the direct
        provider stream is replaced, never duplicated). Playback resumes
        at the current position."""
        if self._closing or not self._is_vod() or self._vod_relay:
            return
        cur = dict(self.current or {})
        if not cur.get("url"):
            return
        try:
            t = self.vlc.get_time() / 1000.0
        except Exception:  # noqa: BLE001
            t = 0.0
        start_at = max(0.0, t - 1.0) if t > 2.0 else 0.0
        self._cap_note("Loading subtitles\u2026")
        # one event-loop turn so the pill paints before the blocking
        # relay start inside play_media
        QtCore.QTimer.singleShot(
            60, lambda: None if self._closing
            else self.play_media(cur, start_at=start_at))

    def _cap_vod_handoff(self, note: str, log_msg: str, *log_args):
        """Give VOD captions back to VLC's own renderer (the overlay can
        offer nothing on this file) and latch the failure for the media."""
        try:
            log.info(log_msg, *log_args)
        except Exception:
            pass
        self._cap_fail = True
        self._set_cap_on(False)
        try:
            self.vlc.set_spu(self._spu_want)
        except Exception:  # noqa: BLE001
            pass
        self._cap_note(note)

    def _schedule_cap_vod_check(self, delay_ms: int = 1500):
        """Arm the post-engagement VOD caption check. The relay's head
        pre-parse usually has parser_tracks ready before playback even
        starts, so ~1.5 s is enough; retries cover a slow streaming head."""
        self._cap_vod_tries = 6
        QtCore.QTimer.singleShot(int(delay_ms), self._cap_vod_check)

    def _cap_vod_check(self):
        """A few seconds after VOD overlay engagement: did the relay's
        parser (MKV or MP4) find a REAL track for the user's pick? Three
        silent-death cases hand the selection back to VLC:
        (a) every subtitle track is bitmap (PGS/VOBSUB) — the overlay can
        never render anything;
        (b) the parser's text-track selection landed on a DIFFERENT
        language than the picked track (VLC names MKV tracks plainly, so
        an "English" pick can be a PGS bitmap while the only text track
        is Arabic — the overlay would sit mute, or worse, subtitle in the
        wrong language);
        (c) the tap never produced ANY track metadata (dead tap after a
        cache rebase, hopeless container) — sitting mute forever helps
        nobody."""
        relay = self._vod_relay
        if (self._closing or not self._cap_on or not self._cap_want
                or relay is None or self._is_dvrable()):
            return
        tracks = getattr(relay, "parser_tracks", None) or {}
        if not tracks:
            # head not parsed yet: retry a few times (a big file over a
            # cold relay can take longer than the first check). While the
            # relay's background startup (tail prefetch on a slow/large
            # file) hasn't finished, the try isn't counted — a 4K rip can
            # legitimately take tens of seconds there.
            starting = hasattr(relay, "_ready") \
                and not relay._ready.is_set() and relay._alive
            if starting:
                QtCore.QTimer.singleShot(3000, self._cap_vod_check)
            elif (self._cap_vod_tries > 0 and not self._closing
                    and self._cap_on and self._cap_want):
                self._cap_vod_tries -= 1
                QtCore.QTimer.singleShot(3000, self._cap_vod_check)
            else:
                self._cap_vod_handoff(
                    "Subtitles: no track data — VLC renders",
                    "captions: relay tap produced no track metadata — "
                    "VLC renders")
            return
        if not any(is_text_codec(c) for c in tracks.values()):
            self._cap_vod_handoff(
                "No restyleable text track — VLC renders",
                "captions: no text track in this file (%s) — VLC renders",
                sorted(set(tracks.values())))
            return
        sel = getattr(relay, "parser_selected", None)
        meta = (getattr(relay, "parser_tracks_meta", None) or {}).get(sel)
        hint = self._cap_lang_hint(self._spu_name)
        if sel is not None and meta and hint and is_language_name(hint) \
                and not lang_matches(hint, meta.get("lang", ""),
                                     meta.get("name", "")):
            self._cap_vod_handoff(
                f"No {hint.capitalize()} text track — VLC renders",
                "captions: picked %r but the only text tracks are %r "
                "— VLC renders", self._spu_name,
                {m.get("lang") for m in
                 (getattr(relay, "parser_tracks_meta", None) or {}).values()
                 if is_text_codec(m.get("codec", ""))})

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
        # "Off" is ALWAYS offered: live caption tracks surface seconds
        # after playback starts (and some channels never list VLC tracks
        # at all — the CC pipeline needs no track id), so a trackless
        # menu must still open with Off + settings
        off = m.addAction("Off")
        off.setCheckable(True)
        off.setChecked(self._spu_want == -1)
        off.triggered.connect(lambda *_, t=-1, n="": self._select_spu(t, n))
        for tid, name in tracks:
            label = name or f"Track {tid}"
            kind = self._cap_track_kind(name)
            if kind == "bitmap":
                label += "  (image \u2014 not adjustable)"
            elif self._cap_eligible(name):
                label += "  (text \u2014 adjustable)"
            elif kind == "ass":
                label += "  (ASS \u2014 VLC rendering)"
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
        # A preview line stands in whenever no cue is active, so style
        # and position changes are visible on the video mid-dialog. The
        # overlay window must be up for it (it may have slept away with
        # the idle controls).
        if not self._overlay_suppressed and not self.overlay.isVisible():
            self.overlay.show()
            self._layout_overlays()
        self._cap_wid.set_preview("Subtitle preview")
        SubtitleDialog(self.config, self._apply_sub_delay,
                       apply_live=self._apply_sub_style_live,
                       parent=self.window()).exec_()
        self._cap_wid.set_preview("")
        if subtitle_instance_args(self.config.subtitle_appearance) != before:
            if self._cap_on:
                # the overlay IS the renderer: repaint is enough — style,
                # size, colors, position all apply instantly, no restart
                self._cap_wid.update()
            else:
                self._reapply_sub_style()

    def _apply_sub_style_live(self, _appearance=None):
        """A style control changed inside the settings dialog: repaint the
        app-rendered captions NOW — the overlay re-reads the config on
        every paint, so font/size/colors/position land mid-drag (the
        dialog already wrote the config). VLC-rendered tracks cannot
        restyle at runtime; the dialog-close path rebuilds the player
        once for those."""
        if self._closing:
            return
        if self._cap_on:
            self._cap_wid.update()

    def _reapply_sub_style(self):
        """Subtitle style args are read once at vlc.Instance() creation —
        so rebuild the player mid-session to apply them without restarting
        the app. Movies/episodes resume at the current position; live
        restarts at the edge (DVR, if on, restarts with it)."""
        self._sub_args_built = subtitle_instance_args(
            self.config.subtitle_appearance)
        cur = dict(self.current or {})
        if self._closing or not cur.get("url"):
            return          # nothing playing: next playback picks them up
        kind = cur.get("kind", "live")
        start_at = 0.0
        if kind in ("vod", "series"):
            try:
                t = self.vlc.get_time() / 1000.0
                if t > 2.0:
                    start_at = t - 1.0
            except Exception:  # noqa: BLE001
                start_at = 0.0
        mute = False
        try:
            mute = self.vlc.is_mute()
        except Exception:  # noqa: BLE001
            pass
        old = self.vlc
        try:
            self.vlc = VLCPlayer(
                timeshift=self.config.timeshift,
                volume=self.config.volume,
                network_caching=self.config.network_caching,
                sub_args=self._sub_args_built,
                spu_delay_ms=int(
                    self.config.subtitle_appearance.get("delay_ms", 0) or 0))
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("sub style: player rebuild failed (%r) — "
                            "keeping the old instance", exc)
            except Exception:
                pass
            self.vlc = old
            return
        self._filter_engine.player = self.vlc
        if mute:
            try:
                self.vlc.set_mute(True)
            except Exception:  # noqa: BLE001
                pass
        try:
            old.stop_and_release()   # close the old media/connection first
        except Exception:  # noqa: BLE001
            pass
        self.play_media(cur, start_at=start_at)
        self._set_dvr_status(
            "Subtitle style applied" +
            ("" if start_at else " (live edge)"))
        QtCore.QTimer.singleShot(
            1800, lambda: self._set_dvr_status("")
            if self._dvr_status.text().startswith("Subtitle style applied")
            else None)

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
        if not self._filter_engine.enabled or self._closing:
            self._stop_profanity()
            return
        self._on_media_for_profanity((self.current or {}).get("kind"))

    def _effective_url(self, url: str, kind: str) -> str:
        """Playback URL, routed through the local splitter for VOD when
        captions are wanted (the overlay's text track and/or the profanity
        filter) — single provider connection; the splitter peels the
        subtitle text and feeds VLC byte-identical data through localhost.
        Falls back to the original URL on any hesitation — playback must
        never depend on it.

        ``self._relay_start_offset`` (set by play_media for resumes and by
        _restart_through_relay for mid-movie engages) marks a RESUME
        session: the relay prefetches only the tail and VLC's own opening
        walk + seek drive the provider stream, so subtitles surface right
        at the switch position."""
        want_caps = self._cap_want and not self._cap_fail
        offset = self._relay_start_offset
        self._relay_start_offset = 0
        if kind not in ("vod", "series") or self._closing \
                or not (want_caps or self.config.profanity.get("enabled")) \
                or not vod_splitter.VOD_SPLITTER_READY:
            return url
        try:
            relay = VodRelay(self)
            local = relay.start(url, USER_AGENT,
                                prefer_language=self._cap_lang_hint(
                                    self._spu_name) or "eng",
                                start_offset=offset)
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("vod splitter: start failed (%r) — direct "
                            "playback", exc)
            except Exception:
                pass
            if want_caps:
                self._cap_fail = True   # no text source: VLC renders
            return url
        if not local:
            try:
                relay.stop()
            except Exception:  # noqa: BLE001
                pass
            if want_caps:
                self._cap_fail = True   # not an MKV etc.: VLC renders
            return url
        relay.cue.connect(self._on_vod_cue)
        relay.failed.connect(self._cap_relay_failed)
        self._vod_relay = relay
        if want_caps:
            self._set_cap_on(True)
            # a few seconds in, verify the file actually HAS a text track
            # for the pick (the check retries while the head streams in)
            self._schedule_cap_vod_check()
        # the evaluation loop — without it the windows pile up but no
        # mute is ever applied
        self._filter_timer.start()
        return local

    def _on_vod_cue(self, start: float, end: float, text: str):
        if self._closing:
            return
        # VOD subtitle tracks are pre-timed — no caption-lag lead
        self._filter_engine.add_cue(start, end, text, lead_s=0.0)
        self._cap_cues.add(start, end, text)

    def _on_media_for_profanity(self, kind: str = None):
        """play_media(): fresh media decides whether the filter engages.

        NOTHING here ever changes playback. Live is ALWAYS in chase mode
        now, so when the filter is on the caption reader simply joins the
        running buffer (started in PARALLEL with the chase fill by
        _engage_chase — re-kicked by _start_chase_now when it isn't up
        yet, e.g. REC engaged chase first). VOD: the splitter was already
        routed in _effective_url BEFORE playback started. The previous
        media's reader was already torn down by play_media's teardown
        (_stop_profanity) — no stop here, or the parallel start above
        would be killed one line after it began.
        """
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
            self._start_cc_when_buffer(tries_left=75)

    def _start_cc_when_buffer(self, tries_left: int = 75):
        """Wait for the DVR buffer to hold data, then start the caption
        reader (~0.4 s poll). Serves BOTH the profanity filter and the
        caption overlay.

        On a long-running buffer the reader JOINS near the playback
        position instead of byte 0: replaying minutes of content costs
        ~1 s of CPU per buffered minute before live cues flow. The exact
        join byte doesn't matter — display times come from the arrival
        anchor (_on_cc_cue), not from CCX's timestamps."""
        if self._closing or tries_left <= 0:
            return
        if self._cc_source is not None or self.dvr is None:
            return
        if not (self._cap_want or self._filter_engine.enabled):
            return   # nobody wants captions anymore (user toggled Off)
        if self.config.chase_delay < 5:
            # captions trail speech by 1-3 s and CCExtractor trails the
            # buffer's write head — a shorter cushion cannot render (or
            # mute) in time. The config UI enforces >= 5; this guards
            # hand-edited settings files.
            if self._filter_engine.enabled:
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
            join = 0
            frontier = self._frontier_s()
            if frontier >= _CC_JOIN_MIN_FRONTIER_S:
                try:
                    size = os.path.getsize(buf)
                except OSError:
                    size = 0
                if size > 188:
                    target_s = max(0.0, self._vid_s - _CC_JOIN_BACK_S)
                    join = int(size * target_s / frontier)
                    join = max(0, min(join - join % 188, size - 188))
            src = CCSource(self)
            src.cue.connect(self._on_cc_cue)
            if hasattr(src, "failed"):
                # any hesitation hands captions back to VLC for this media
                src.failed.connect(self._cap_source_failed)
            if src.start(buf, join_bytes=join):
                self._cc_source = src
                # evaluation loop for the caption windows (see
                # _effective_url: nothing else starts this timer)
                self._filter_timer.start()
                try:
                    log.info("profanity: caption reader on %s "
                             "(join_byte=%d arrival-anchored)", buf, join)
                except Exception:
                    pass
                # no "Profanity filter active" pill at startup — the user
                # asked for a quiet live-TV start (filter status lives in
                # Settings and the tray of the control bar)
                return
        QtCore.QTimer.singleShot(
            400, lambda: self._start_cc_when_buffer(tries_left - 1))

    def _on_cc_cue(self, start: float, end: float, text: str):
        """One live cue arrived — remap it onto the app's clock by ARRIVAL.

        CCExtractor's SRT times live on its own PTS-derived axis, which
        measured 12-32 s ahead of VLC's playback clock with continuous
        drift (the provider's bursts/PTS jumps normalize differently in
        each). So the cue's own timestamps are only used for RELATIVE
        ordering; the absolute position comes from the arrival moment:
        every FRESH cue (advancing no faster than wall time — a catch-up
        burst over old content doesn't qualify) re-anchors

            offset = frontier(now) - _CC_LAG_S - cue_end

        so the newest content sits a pipeline-lag behind the frontier
        and older cues keep their relative spacing. The offset is
        EWMA-smoothed across fresh cues (see _CC_ANCHOR_ALPHA) so burst
        jitter does not wiggle individual captions. Residual error is a
        fraction of the pipeline lag; the subtitle delay setting trims
        taste on top."""
        if self._closing:
            return
        now = time.time()
        last_c = self._cc_last_c
        elapsed = 1.0 if self._cc_last_t <= 0.0 else now - self._cc_last_t
        advance = None if last_c is None else end - last_c
        # Fresh = advancing no faster than a few times wall time. Some
        # providers' caption PTS axis legitimately runs ~2x wall (measured
        # on a 4K channel), which the old advance<=elapsed+5 rule read as
        # an eternal catch-up burst — anchors stopped refreshing and
        # captions drifted late over every silent stretch. Real catch-up
        # replays at 30-100x, far outside even this generous bound.
        fresh = (
            (last_c is None and self._frontier_s() < 20.0)
            or (advance is not None
                and 0.0 < advance <= elapsed * 3.0 + 5.0)
        )
        if last_c is None or end > last_c:
            # baseline for the NEXT cue's advance judgment (never
            # regressed by CCX's duplicate re-emissions)
            self._cc_last_c = end
            self._cc_last_t = now
        if fresh:
            target = self._frontier_s() - _CC_LAG_S - end
            if self._cc_off is None:
                self._cc_off = target   # first anchor lands as-is (fast start)
            else:
                # EWMA: settle on the MEAN pipeline lag instead of jittering
                # cue-to-cue with burst flushes and poll phase
                self._cc_off += (target - self._cc_off) * _CC_ANCHOR_ALPHA
        off = self._cc_off
        if off is None:
            return          # still in a catch-up burst: untrusted times
        start = max(0.0, start + off)
        end = max(start, end + off)
        # arrival-anchored times are already display times: no caption-lag
        # lead for the filter either
        self._filter_engine.add_cue(start, end, text, lead_s=0.0)
        self._cap_cues.add(start, end, text)

    def _stop_cc_source(self):
        """Tear down just the live caption reader (channel change etc.)."""
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

    def _filter_tick(self):
        """100 ms: apply the filter mute for the current playback position.

        The caption clock (_caption_clock_s) is the DISPLAYED position on
        the same timeline the cue windows live on — chase cues share the
        buffer file's PTS axis with VLC's get_time(), VOD cues the file's
        cluster timecodes. A fresh raw read every tick also keeps the mute
        edges unquantized (the 400 ms _vid_s poll would blur them)."""
        if self._closing or not self._filter_engine.enabled \
                or not self._filter_engine.windows:
            return
        if self._mode == "chase" or self._is_vod():
            self._filter_engine.evaluate(self._caption_clock_s())

    def _stop_profanity(self, keep_windows: bool = False):
        """Kill the caption reader / VOD splitter + clear the filter mute.

        While the caption overlay owns those sources (subtitles selected
        on a text track) they must survive: the relay IS the playback URL
        at that point, and the CCSource feeds the overlay, not just the
        filter. play_media/_set_cap_on(False) releases the claim first."""
        if self._vod_relay is not None and not self._cap_on:
            try:
                self._vod_relay.cue.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._vod_relay.stop()
                self._vod_relay.deleteLater()
            except Exception:  # noqa: BLE001
                pass
            self._vod_relay = None
        if not self._cap_on:
            self._stop_cc_source()
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
        # Profanity filter / caption overlay: release the overlay's claim
        # on the caption sources, then kill the ffmpeg extractor and clear
        # the filter mute BEFORE the player goes away (engine.clear()
        # touches VLC).
        try:
            self._set_cap_on(False)
        except Exception:  # noqa: BLE001
            pass
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
        self._video_wh = (0, 0)
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