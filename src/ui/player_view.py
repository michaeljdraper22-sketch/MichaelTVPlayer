"""Video surface, overlay info, on-video playback controls and DVR rewind."""

import html
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime

from PyQt5 import QtCore, QtGui, QtWidgets

from ..dvr import VlcRecorder
from ..player import VLCPlayer, subtitle_instance_args, USER_AGENT
from .. import profanity as prof_mod
from ..catchup_relay import CatchupRelay
from ..live_cc import CCSource, find_ccextractor, probe_tail_pcr, \
    probe_first_pcr_at
from .. import vod_splitter
from ..vod_splitter import VodRelay
from ..mkv_subs import (is_text_codec, is_language_name, lang_matches,
                        lang_token, track_language_evidence)
from . import browsers
from . import icons as ic
from .caption_overlay import (CaptionOverlay, CueStore, displayed_video_rect)
from .track_panel import NativeDialogCloser, TrackPanel
from .worker import AsyncRunner, FileDownloader

log = logging.getLogger("mtp")


def now_s() -> float:
    """Wall-clock seconds for every timing gate in the playback/caption
    pipeline (dead-reckoned caption clock, caption watchdog, DVR content
    crediting, rescue/reopen cooldowns). Tests drive a virtual clock by
    rebinding this one function — the sanctioned seam; nothing in this
    module may read time.time() directly."""
    return time.time()


def _chase_jump_back_s(lag_ewma):
    """D1 adaptive jump-to-live: how far behind the head LIVE lands. While
    the measured CCX lag L is large, the newest caption is ~L behind the
    head — landing at the true edge (-5 s) means captionless video until
    the pipeline catches up. Land max(5, L+3) behind instead; at normal
    lag (<= _CC_ADAPTIVE_MIN_L_S) the true edge wins (minimal latency).
    Pure: the unit tests and the E2E driver share this formula."""
    if lag_ewma is None or lag_ewma <= _CC_ADAPTIVE_MIN_L_S:
        return _CHASE_SAFETY_S
    return max(_CHASE_SAFETY_S, lag_ewma + _CC_ADAPTIVE_PAD_S)


def _cc_join_byte(size: int, frontier: float, vid_s: float) -> int:
    """D2 near-play join: the CC reader joins the DVR buffer
    ~_CC_JOIN_BACK_S behind the CURRENT playback position at ANY frontier
    (a byte-0 join on a long-running buffer replays minutes of content
    before live cues flow — ~1 s of CPU per buffered minute; the old
    >=90 s frontier gate made a mid-show engage replay from byte 0).
    ``vid_s`` is clamped to the frontier: after a true-edge landing the
    viewer sits PAST the under-credited frontier and the raw ratio would
    overshoot the file tail. Pure: unit-tested directly."""
    if size <= 188 or frontier <= 0.0:
        return 0
    target_s = max(0.0, min(vid_s, frontier) - _CC_JOIN_BACK_S)
    join = int(size * target_s / frontier)
    return max(0, min(join - join % 188, size - 188))


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

# ---- WP2 live-edge wedge cluster (seek-verify-escalate + wedge rescue) ----
# set_time() can NO-OP while VLC sits demuxer-blocked at the tail of the
# growing buffer yet still reports "playing" (measured 2026-08-21: four
# transports in a row left raw pinned at 94.18 s for 7 minutes while the
# state-based revive never fired). Every chase set_time is therefore
# VERIFIED: if raw has not reached the target (content axis) by the
# deadline, the seek escalates to the play_at revive (local buffer file —
# the provider connection is never touched). The deadline grows with jump
# distance (a big demux seek legally takes longer than a small one) and
# escalations back off 5 -> 15 -> 30 s so a flaky verify can never
# reopen-loop.
_SEEK_VERIFY_BASE_S = 1.5
_SEEK_VERIFY_PROP_S = 0.15      # extra verify window per second of |jump|
_SEEK_VERIFY_MAX_S = 6.0
_SEEK_VERIFY_TOL_S = 2.0
_SEEK_ESC_BACKOFF_S = (5.0, 15.0, 30.0)
_SEEK_ESC_DECAY_S = 90.0        # clean verifies for this long decay a strike
# Wedge rescue: raw frozen this long while "playing" AND this much REAL
# content ahead. "Ahead" is measured from the PCR head, not the frontier —
# the frontier under-credits the cold burst (at the true edge
# frontier - current is NEGATIVE: zero rescues all night with the viewer
# 37 s past the frontier) yet over-credits slow trickles.
_WEDGE_FREEZE_S = 8.0
_WEDGE_DATA_AHEAD_S = 10.0
_RAW_MOVE_FRAC = 0.5            # raw "moved" if |d| > max(0.02 s, this
#                              # fraction of rate x tick interval) — a flat
#                              # 0.05 s read legit 0.125x slow-mo as frozen

# Chase re-engagement after a fallback: when a recorder engagement gives
# up (~20 s without data), plain live plays while these bounded, growing
# backoffs retry the buffer — and with it rewind/speed — automatically.
# The budget resets on every channel change (play_media).
_CHASE_RETRY_DELAYS = (10.0, 30.0)

# Live-CC pipeline lag FALLBACK (buffer-tail poll + CCExtractor processing
# + SRT harvest) for when the tail-PCR probe cannot measure the real lag
# (no PCR in the buffer yet, probe failure). Stage 2 measures L live and
# uses this only until the first successful measurement.
_CC_LAG_S = 1.0

# Live anchor smoothing: each flush of the caption stream re-derives the
# CCX->app offset, but per-flush estimates jitter with the pipeline (burst
# flushes, poll phase). An EWMA settles captions on the MEAN lag instead
# of wiggling cue-to-cue. Stage 3 raised this from 0.35: the anchor
# decision is now DEFERRED to one clean sample per arrival flush (see
# _cc_flush_pending), so the sample rate fell ~3x while its per-sample
# skew shrank (newest-cue probes only) — 0.50 keeps the same smoothing
# per second while converging ~1.7x faster. The stage-2 matrices showed
# a persistent -0.4 s signed innovation (anchor trailing its target)
# across every phase; the faster EWMA halves that residual.
_CC_ANCHOR_ALPHA = 0.50

# ---- stage-2 single-axis caption timing (transport-aware, self-healing) ----
# Video frames play 1:1 with wall time no matter what the timestamp numbers
# do, so the caption clock dead-reckons on ONE app-owned content axis and
# never mixes axes. Absolute snaps to VLC's raw get_time are gone: only
# outlier-rejected DELTA nudges fold in (raw advancing ~rate x wall within
# _CC_SYNC_TOL_S agrees that frames kept playing; bigger jumps are PTS
# renumbering — the clock ignores them and remembers the divergence).
_CC_SYNC_TOL_S = 2.0        # |raw delta - rate x wall delta| accepted as a nudge
_CC_STALL_FREEZE_S = 6.0    # raw frozen this long while "playing" = underrun stall
_CC_REBASE_S = 4.0          # anchor correction beyond this snaps (no EWMA crawl)
# WP3 robust anchor-snap: a large gap alone is NOT snap evidence — the
# 2026-08-21 corpus showed most >4 s snaps were single-batch noise spikes
# round-tripping within 20 s (35-50 per session), each slamming the whole
# stored timeline. A REAL correction is either huge, PERSISTENT, or lands
# on a STABLE target (the pre-WP3 scenario-f contract: a wrong forced
# rebase must snap back as soon as the true target re-asserts itself —
# its target is rock-stable while the anchor sits 6 s away).
_CC_REBASE_HARD_S = 8.0     # beyond this the snap is immediate (real jump)
_CC_REBASE_CONFIRM_N = 2    # consecutive out-of-band batches confirm a snap
_CC_REBASE_STABLE_S = 0.8   # |target - prev_target| <= this = stable target
_CC_LAG_ALPHA = 0.35        # EWMA over the measured CCX lag L (per-batch).
#                              # WP3 retune (sync_stage3_retune.py over the
#                              # pinned 2026-08-21 corpus, 249 batches):
#                              # 0.18 trailed regime swings by p95 13.1 s
#                              # on ramps; 0.35 halves it (7.4) AND cuts the
#                              # steady-state error (p95 2.16 -> 1.49). The
#                              # fancier candidates LOST to the plain faster
#                              # EWMA: per-batch speech-skew noise (p95
#                              # 3.6 s) dwarfs the ramp signal, so
#                              # derivative lead and adaptive-alpha terms
#                              # amplified noise (ramp p95 14.6 / steady
#                              # 2.4-2.6).
_CC_LAG_MAX_S = 240.0       # sanity bound on a single L sample (the 4K
#                              # channel measured L>130 s one session —
#                              # 90 froze the EWMA while the true lag
#                              # grew past it)
# WP3 store coherence: stored cues keep their PIN-TIME positions. Each
# cue is placed at edge - L_est when it arrives — its true position to
# within the tracker trail — and NO later small correction improves that
# (the harness's raw-window ground truth shows whole-store shifts on
# small corrections only drag correctly-pinned cues with the swing: an
# L oscillation of +-12 s displaced 40-s-old cues by ~8 s p95). The
# store moves ONLY on a rebase snap (a confirmed real correction), which
# slides every window by the full delta — coherent before and after.
_CC_ADAPTIVE_MIN_L_S = 8.0  # D1 adaptive jump-to-live: while the measured
_CC_ADAPTIVE_PAD_S = 3.0    # CCX lag L exceeds this, LIVE lands
#                              # max(_CHASE_SAFETY_S, L+3) behind the head
#                              # (captions exist that close to the edge —
#                              # landing at the true edge means captionless
#                              # video until the pipeline catches up); true
#                              # edge (-5 s) at normal lag
_CC_EDGE_ALPHA = 0.50       # edge pull toward the PCR head (this CDN
#                              # bursts: 0.1x for tens of seconds, then
#                              # 30 s of content at once — wall dead-
#                              # reckoning alone swings 5-8 s off between
#                              # probes, and every correction snapped the
#                              # anchor; the PCR head is the truth)
_CC_EDGE_SNAP_S = 2.5       # hard edge resync when the PCR head says we're off
_CC_EDGE_PROBE_MS = 2000    # periodic head probe (caption-silent stretches)
_CC_D_ALPHA = 0.30          # EWMA of (raw - clock) — the axis divergence
# (c) freeze-aware clock: a 0.2x-delivery night feeds sub-6-s freeze/thaw
# cycles (the continuous-freeze stall branch never engages) while wall time
# runs 1:1 — integrating wall through those trickles ran the caption clock
# 8.5 s ahead of the frames actually on screen (2026-08-21). The tick keeps
# a rolling raw-vs-wall window; when raw advanced less than this fraction
# of rate x wall while "playing", the clocks HOLD (and the anti-lead clamp
# bounds any residue).
_CC_TRICKLE_WIN_S = 3.0
_CC_TRICKLE_RATIO = 0.3
_CC_LEAD_MAX_S = 1.0        # the caption clock may never lead VLC's raw
#                              # position by more than this while playing
#                              # (raw is the displayed truth)
# Caption-stopped watchdog: cues still arriving (within _CC_WATCH_CUE_S) but
# NO window intersecting the clock for _CC_WATCH_GAP_S means the anchor
# diverged from the stored windows — re-derive it from the newest cue.
# The arrival window spans CCX's burst-parse stalls (a landed 30-s CDN
# burst takes the parser ~20-30 s of silence) — safe because the instant-L
# derivation in _cc_watchdog_fire yields ~0 during genuine speech pauses,
# so widening it cannot fire on those.
_CC_WATCH_CUE_S = 20.0
_CC_WATCH_GAP_S = 5.0
_CC_WATCH_COOLDOWN_S = 8.0

# Live-CC engage: CCExtractor joins the DVR buffer this many seconds behind
# the current playback position (see _start_cc_when_buffer / _cc_join_byte)
# instead of replaying the whole file — the arrival anchor absorbs the exact
# placement. Kept small: CCX chews 4K HEVC slower than real time, so every
# skipped second is that much faster to first caption. D2: the join is
# position-based at ANY frontier (the old >=90 s gate replayed byte 0 on a
# mid-show engage).
_CC_JOIN_BACK_S = 8.0

# VOD profanity mute-lead: movies were measured to miss mutes by ~0.5 s
# (the word already audible as the window opened). The word-position
# estimate inside a cue is proportional to character share, and tracks
# run their dialogue slightly ahead of the estimate — so the VOD filter
# path opens each mute window this many seconds EARLY. VOD ONLY: live
# cues are arrival-anchored to display times already.
_VOD_MUTE_LEAD_S = 0.4

# Playback speeds offered by the speed button (chase mode / VOD).
# Capped at 4x: VLC mutes the audio output entirely above ~4x playback
# speed ("fast forward 5x goes silent").
_SPEEDS = (0.125, 0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)

# ---- stage-1 live-caption sync diagnosis (measurement only) ----
# Every timing-axis sample / decision lands on the "mtp.sync" logger
# (own DEBUG file when MTP_SYNC_LOG is set — see logging_setup). All call
# sites are guarded by _SYNC_ON, so a normal run pays one boolean check.
synclog = logging.getLogger("mtp.sync")
_SYNC_ON = bool(os.environ.get("MTP_SYNC_LOG"))

# Window flags for the on-video overlay layer (see PlayerView.__init__):
# Tool = frameless helper window with no taskbar entry. It is OWNED by the
# main window — Windows keeps an owned window above its owner (so it can
# float over the native video HWND) but NOT above other applications: a
# foreground app's windows and the (topmost) taskbar cover it normally.
# (The previous Qt.ToolTip flags made it WS_EX_TOPMOST on Windows, so
# controls/captions painted over other apps' windows and the taskbar
# whenever the player was buried behind them.) WindowDoesNotAcceptFocus +
# WA_ShowWithoutActivating keep keyboard shortcuts on the main window
# while the on-video controls stay clickable.
_OVERLAY_WIN_FLAGS = QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint
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
/* NOTE on hit plates: the overlay is a per-pixel-alpha window and WINDOWS
   ROUTES CLICKS BY PIXEL ALPHA — a fully transparent pixel passes the
   click through to the video below, even inside a button's rect.  Without
   a plate only the glyph's own strokes were clickable (measured: the
   scale button's whole 42 px hit-tested as VideoSurface).  The plate must
   ALSO be invisible: the earlier white rgba(255,255,255,3) plates showed
   as (3,3,3) gray tiles on dark video.  rgba(63,63,63,2) is the fix:
   premultiplied it stores (0,0,0,2) — zero contribution over black, at
   most a 2-step dip on pure white — while the nonzero alpha keeps every
   pixel clickable (verified with WindowFromPoint: the overlay is hit at
   stored alpha 1-3, transparent pixels fall through).  QSS parsing traps
   rule out the neighbors: alpha 1 parses as FLOAT 1.0 = fully opaque,
   and sub-1% percentages misparse — keep an integer >= 2.  Keep the
   plate a COLOR, not pure black: alpha-1 black plates once rendered as
   solid boxes on some setups. */
#ovButton { background-color: rgba(63,63,63,2); border: none;
            border-radius: 5px; color: #ffffff; font-size: 13px; }
#ovButton:hover { background-color: rgba(255,255,255,45); }
#ovButton:pressed { background-color: rgba(255,255,255,95); }

/* ---- on-video playback controls (float over the video, no box) ---- */
#ctlOverlay { background: transparent; }
#ctlOverlay QWidget { background: transparent; }
#ctlOverlay QToolButton { background-color: rgba(63,63,63,2);
                          border: none; border-radius: 6px; }
#ctlOverlay QToolButton:hover { background-color: rgba(255,255,255,45); }
#ctlOverlay QToolButton:pressed { background-color: rgba(255,255,255,95); }
#ctlSep { color: rgba(255,255,255,70); background: transparent; }
#ctlTimeLabel { color: #ffffff; background: transparent; font-size: 12px;
                font-weight: 600; }
#ctlOverlay QSlider { background-color: rgba(63,63,63,2); }

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
"""


class JumpSlider(QtWidgets.QSlider):
    """QSlider where a CLICK anywhere on the groove jumps straight to that
    point (standard QSlider only page-steps). A click also enters drag
    mode, so you can keep holding and fine-tune.

    ``_win_mode`` (set by PlayerView while a catch-up download window is
    being selected) reroutes clicks: instead of seeking, the click emits
    ``win_picked`` with the clicked value — the view moves the NEAREST
    gold window marker there."""

    win_picked = QtCore.pyqtSignal(int)

    def mousePressEvent(self, ev):
        if (ev.button() == QtCore.Qt.LeftButton
                and getattr(self, "_win_mode", False)):
            st = self.style()
            handle = st.pixelMetric(QtWidgets.QStyle.PM_SliderLength,
                                    None, self)
            span = max(1, self.width() - handle)
            x = ev.pos().x() - handle // 2
            frac = min(1.0, max(0.0, x / span))
            val = self.minimum() + round(
                (self.maximum() - self.minimum()) * frac)
            val = max(self.minimum(), min(self.maximum(), val))
            self.win_picked.emit(val)
            ev.accept()
            return
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


# gold used by the catch-up download-window markers + button
_WIN_GOLD = QtGui.QColor(245, 197, 24, 255)
_WIN_GOLD_HEX = "#f5c518"
_WIN_GAP_MS = 1000          # smallest selectable window (1 s)


class WinMarker(QtWidgets.QWidget):
    """One gold < / > catch-up download-window handle on the scrubber.

    Pure view widget: paints the gold chevron (filled when selected) plus
    a guide line through the groove, and reports clicks/drags as
    slider-local x positions — PlayerView owns the millisecond values and
    the clamping."""

    clicked = QtCore.pyqtSignal()
    drag_moved = QtCore.pyqtSignal(int)   # slider-local x while dragging

    def __init__(self, left_pointing: bool, parent=None):
        super().__init__(parent)
        self.left_pointing = left_pointing
        self.selected = False
        self._dragging = False
        self.setFixedSize(16, 24)
        self.setCursor(QtCore.Qt.SizeHorCursor)
        self.setToolTip("Drag me, click to select me, then adjust with "
                        "the \u2190/\u2192 arrow keys")

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self.clicked.emit()
            self._dragging = True
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._dragging:
            self.drag_moved.emit(ev.pos().x() + self.x())
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._dragging = False
        ev.accept()

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        gold = _WIN_GOLD
        glyph = QtCore.Qt.black if self.selected else gold
        if self.selected:
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(gold)
            p.drawRoundedRect(QtCore.QRectF(0.5, 0.5, 15.0, 16.5), 3.0, 3.0)
        else:
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor(245, 197, 24, 40))
            p.drawRoundedRect(QtCore.QRectF(0.5, 0.5, 15.0, 16.5), 3.0, 3.0)
        pen = QtGui.QPen(glyph, 2.4)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        p.setPen(pen)
        if self.left_pointing:      # "<" marks the window START
            p.drawPolyline(QtGui.QPolygonF([
                QtCore.QPointF(10.4, 3.4), QtCore.QPointF(4.8, 8.4),
                QtCore.QPointF(10.4, 13.4)]))
        else:                        # ">" marks the window END
            p.drawPolyline(QtGui.QPolygonF([
                QtCore.QPointF(5.6, 3.4), QtCore.QPointF(11.2, 8.4),
                QtCore.QPointF(5.6, 13.4)]))
        # guide line through the groove below the glyph
        pen = QtGui.QPen(gold, 1)
        p.setPen(pen)
        p.drawLine(QtCore.QPointF(8.0, 14.0), QtCore.QPointF(8.0, self.height()))


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
    # live TV "Play next": PlayerView has no channel list — MainWindow
    # resolves the next channel in the Live tab's current list
    request_next_channel = QtCore.pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.client = None
        self.current = None
        # Paint the themed background on EVERY expose.  Without this the
        # widget paints nothing of its own (no styled background, no
        # autofill), so strips newly exposed by a splitter drag — where the
        # channel panel / handle used to sit — kept the stale backing-store
        # pixels forever: the "ghost list" trails over the video.  (The
        # opaque black VideoSurface child covers this everywhere except
        # the exact expose strips.)
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
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
        self._raw_change_wall = 0.0   # wall time raw last CHANGED (wedge
        #                              # detector: frozen-while-playing)
        self._seek_verify = None      # WP2 (a): armed by _chase_seek —
        #                              # (target, vlc_t, deadline, armed_at);
        #                              # _tick verifies raw landed, else
        #                              # escalates to the play_at revive
        self._seek_esc_strikes = 0    # escalation backoff ladder position
        self._seek_esc_ok_at = 0.0    # earliest next escalation (now_s axis)
        self._seek_esc_clean = 0.0    # last clean verify (strike decay)
        self._raw_win = []            # WP2 (c): rolling (now, raw) window
        self._trickle_hold = False    # frames trickling: clocks hold
        self._cap_raw_clock = None    # caption clock at raw's last CHANGE
        #                              # (fold expectation baseline)
        self._tick_t = None           # wall clock of the previous _tick
        self._live_paused = False     # paused in plain LIVE mode (timeshift)
        self._scale_mode = config.scale_mode   # fit | stretch | crop
        self._video_wh = (0, 0)  # decoded video size (see _poll_video_size)
        self._downloading = False     # a VOD download is in flight
        self._compact_level = -1      # control-row compaction step (see _fit_ctl)
        self._compact_hidden = set()  # buttons hidden by compaction
        self._in_fit_ctl = False      # _fit_ctl re-entrancy guard
        self._resize_burst_t0 = None  # wall time a resize burst started
        self._was_playing = False     # for the audio re-apply on transitions
        self._scrub_on = False        # scrubber row currently shown (VOD/chase)
        self._popup_open = False      # a ctl popup menu is open (don't hide)
        self._info_sticky = False     # now-playing banner tied to the
        #                             # control show/hide cycle (see
        #                             # show_info / _wake / _sleep)
        self._spu_want = -1           # DESIRED subtitle track id (-1 = off)
        self._spu_name = ""           # its name — re-matched after media opens
        self._spu_ui = None           # (enabled, on, name) last painted on btn_cc
        # audio tracks (mirror of the spu stack). _audio_name empty = AUTO
        # mode: no user pick — English is preferred by default; a non-empty
        # name is the CURRENT program's pick, re-matched on player swaps
        # and cleared by play_media (picks are per-program — every show
        # starts at Auto/English).
        self._audio_want = None       # DESIRED audio track id (None = auto)
        self._audio_name = ""         # the pick's name ("" = auto mode)
        self._audio_ui = None         # (sticky, label, n) last painted tooltip
        self._audio_auto_tid = None   # English id last auto-logged (log guard)
        # profanity filter (live TV: captions from the DVR buffer + engine)
        self._cc_source = None        # live closed-caption reader
        self._vod_relay = None        # VOD splitter (single-connection)
        self._catchup_relay = None    # catch-up range proxy (scrub-ability)
        self._catchup_local_url = ""  # the relay URL VLC plays (rescue path)
        self._cu_raw_wall = 0.0       # catch-up watchdog: wall time raw last
        #                              # moved (freeze detection)
        self._cu_raw_moved = False    # raw has advanced at least once this
        #                              # media (never rescue on a never-moving
        #                              # clock — garbage timestamps)
        self._cap_relay_gen = 0       # session the attached relay belongs
        self._vod_raw_wall = 0.0      # VOD stall watchdog: wall time raw last
        #                              # moved (series/movies, like catch-up)
        self._vod_rescues = 0         # rescues this media (cap: 2)
        self._cap_relay_gen = 0       # session the attached relay belongs
        self._relay_start_offset = 0  # byte offset for the NEXT relay start
        #                                 # (resume / mid-movie subtitle
        #                                 # engage — consumed by
        #                                 # _effective_url)
        # caption overlay: app-rendered subtitles, one style for every
        # text source (live CC via CCExtractor, VOD SRT via the relay)
        self._cap_cues = CueStore()   # every cue, both sources
        self._cap_tick_errs = set()   # distinct _caption_tick errors
        #                              # already logged (once-per-error)
        self._cap_on = False          # the overlay owns caption rendering
        self._cap_want = False        # user picked a text track (sticky)
        self._cap_fail = False        # source dead this media: VLC renders
        self._cap_vod_tries = 0       # _cap_vod_check retries (head parse)
        self._cap_clock_s = 0.0       # caption timing clock (VLC display
        #                              # position; see _caption_clock_s)
        self._cap_raw_s = None        # last raw get_time() seen by the
        #                              # caption clock (freeze/jump guard)
        self._cap_raw_wall = 0.0      # wall time that raw reading first
        #                              # appeared (delta validation window)
        self._cap_wall = 0.0          # wall time of the last caption-clock
        #                              # update (integration dt)
        self._cap_backlog_s = None    # live-edge backlog in content seconds
        #                              # (edge = clock + backlog; None until
        #                              # a transport event seeds it)
        self._cap_div_s = 0.0         # EWMA of (raw get_time - dead-reckoned
        self._cap_div_ok = False      # clock): the axis divergence used to
        #                              # convert content-axis seek targets
        #                              # into VLC set_time numbers
        # live-CC arrival anchor: the newest FRESH cue is pinned at
        # edge - L on the app's content axis (L = the MEASURED CCX lag
        # from the tail-PCR probe; the old hardcoded 1 s was wrong by
        # 1.5-2 orders of magnitude). CCX's own cue times only carry
        # RELATIVE ordering; the offset below maps them onto the axis.
        self._cc_off = None           # live cue offset, CCX s -> app s
        self._cc_last_c = None        # end-time of the last anchored cue
        self._cc_last_t = 0.0         # wall time of the last anchor
        self._cc_pend = None          # deferred anchor (end, head_rel):
        #                              # applied once per arrival batch by
        #                              # _cc_flush_pending (see _on_cc_cue)
        self._cc_prev_target = None   # last anchor target (WP3 stable-
        #                              # target snap evidence — see the
        #                              # _CC_REBASE_* constants)
        self._cc_oob_run = 0          # consecutive out-of-band anchor gaps
        self._cc_stash = []           # cues that arrived before the first
        #                              # anchor: stored once an offset exists
        self._cc_lag = None           # EWMA of the measured CCX lag L
        self._cc_head_pcr = None      # (pcr_s, wall) of the last throttled
        #                              # tail-PCR probe of the write head
        self._cc_join_byte = 0        # buffer byte CCExtractor joined at
        self._cc_join_app_s = 0.0     # content-axis position of that byte
        #                              # (refined from PCR+bytes for
        #                              # mid-session joins; 0 for cold ones)
        # caption-stopped watchdog (see _caption_tick)
        self._cc_last_arrival = 0.0   # wall time ANY live cue arrived
        self._cc_last_active = 0.0    # wall time a window last hit the clock
        self._cc_last_watchfire = 0.0 # watchdog cooldown
        # periodic PCR-head probe: keeps the dead-reckoned live edge
        # calibrated even through caption-silent stretches (this CDN
        # bursts, so the edge must follow the head, not the wall clock)
        self._cc_edge_timer = QtCore.QTimer(self)
        self._cc_edge_timer.setInterval(_CC_EDGE_PROBE_MS)
        self._cc_edge_timer.timeout.connect(self._cc_edge_probe_tick)
        # stage-1 sync diagnosis bookkeeping (see mtp.sync logger)
        self._sync_last_show_t = None  # wall time the overlay last painted
        self._sync_pcr_join = None     # (pid, pcr_s) at CCX's join byte
        self._sync_pcr_join_tries = 0
        self._sync_credit_s = 0.0      # wall time credited to content clock
        self._sync_capped_s = 0.0      # wall time thrown away by 15 s cap
        self._sync_timer = QtCore.QTimer(self)
        self._sync_timer.setInterval(5000)
        self._sync_timer.timeout.connect(self._sync_tick)
        if _SYNC_ON:
            self._sync_timer.start()
        self._cap_timer = QtCore.QTimer(self)
        self._cap_timer.setInterval(100)
        self._cap_timer.timeout.connect(self._caption_tick)
        self.runner = AsyncRunner()
        self.runner.finished.connect(self._on_epg)
        # next-episode / next-program lookups (Play next button + the
        # autoplay-on-end path) — its own runner so a slow series_info
        # fetch can never clobber the EPG display
        self._next_runner = AsyncRunner()
        self._next_runner.finished.connect(self._on_next_fetched)
        self._eof_next_done = False   # one autoplay shot per media
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

        # Stremio-style picker card (dark glass, checkmark on the selected
        # row) shared by EVERY control-bar popup — audio tracks, subtitles,
        # scale and speed — same stacking rules as the other overlays:
        # child of the overlay window, above the native video HWND, no
        # focus. The opener button toggles it (same button = close), a
        # 1 s refresh keeps the audio list filling while VLC surfaces
        # late track lists.
        self._ctl_panel = TrackPanel(self.overlay)
        self._ctl_panel.picked.connect(self._on_ctl_panel_picked)
        self._ctl_panel.closed.connect(self._ctl_panel_closed)
        self._ctl_panel_btn = None     # the button that opened the card
        self._ctl_panel_pick = None    # row-click callback for that open
        self._ctl_panel_refresh = None  # 1 s content refresher, if any
        self._ctl_panel_timer = QtCore.QTimer(self)
        self._ctl_panel_timer.setInterval(1000)
        self._ctl_panel_timer.timeout.connect(self._on_ctl_panel_tick)

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

        # gold < > download-window markers live ON the slider (hidden until
        # a catch-up window selection starts).  The slider reports clicks
        # while in window mode; PlayerView converts pixels <-> milliseconds.
        self.slider.win_picked.connect(self._on_win_slider_click)
        self.slider.installEventFilter(self)
        self._win_markers = {
            "start": WinMarker(True, self.slider),
            "end": WinMarker(False, self.slider),
        }
        for _side, _m in self._win_markers.items():
            _m.clicked.connect(lambda s=_side: self._win_select(s))
            _m.drag_moved.connect(lambda x, s=_side: self._win_drag(s, x))
            _m.hide()
        self._win_sel = False          # window-select mode active?
        self._win_sel_side = None      # "start" | "end" (arrow keys move it)
        self._win_start_ms = 0
        self._win_end_ms = 0

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
                              "Download this video to the downloads folder")
        # catch-up programs get the WINDOW download instead of REC/DL: the
        # first press drops two gold < > markers on the time bar (drag /
        # arrow-key them, click the bar to place), the second press
        # downloads exactly that stretch of the recording. The glyph is
        # WHITE at rest like every other button and only turns GOLD while
        # a window selection is live or a download is in flight.
        self.btn_win = ctl_btn(ic.download_window(),
                               "Download a time window of this program",
                               checkable=True)
        self.btn_win.setStyleSheet(
            "QToolButton:checked { background: rgba(245,197,24,52);"
            " border: 1px solid #f5c518; border-radius: 4px; }")
        self.btn_rec = ctl_btn(ic.rec(False),
                               "Record this channel to a file "
                               "(Settings > Recording folder)", checkable=True)
        self.sep2 = ctl_sep()
        # subtitles: opens the track menu (enabled once the stream's
        # subtitle tracks are discovered — movies/series almost always carry
        # SRT language tracks, live channels occasionally carry DVB ones)
        self.btn_cc = ctl_btn(ic.cc(False), "Subtitles (C)")
        # right-click = track/style menu (left-click is the on/off toggle)
        self.btn_cc.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.btn_cc.customContextMenuRequested.connect(
            lambda _pos: self._subs_panel())
        # audio track picker: opens the track menu (multi-language streams
        # list their dubs; enabled always — Auto works without tracks)
        self.btn_audio = ctl_btn(ic.audio(), "Audio tracks (A)")
        self.btn_scale = ctl_btn(ic.scale(), "Video scaling "
                                             "(fit / stretch / crop)")
        # a touch wider than the rest: the scale glyph is wide and its hit
        # area felt imprecise at the standard 34 px
        self.btn_scale.setFixedSize(42, 30)
        self.btn_speed = ctl_btn(ic.speed(), "Playback speed "
                                              "(live rewind, movies & series)")
        # autoplay-next toggle (series episodes / catch-up programs): ON by
        # default — the natural end of an episode rolls straight into the
        # next one, and a season finale into the next season's first
        # episode. Live from app open (sticky preference, like CC/scale/
        # speed — it only flips the setting until a stream plays). OFF
        # strikes an X through the glyph, mute-button style.
        self.btn_auto = ctl_btn(ic.autoplay(self.config.autoplay_next),
                                "Autoplay next episode", checkable=True)
        self.btn_auto.setChecked(self.config.autoplay_next)
        # play next: next episode for series, the next recorded program for
        # catch-up, the next channel for live TV (movies get neither)
        self.btn_next = ctl_btn(ic.play_next(), "Play next (N)")
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
                  self.btn_dl, self.btn_win, self.btn_rec, self.sep2,
                  self.btn_cc, self.btn_audio, self.btn_scale, self.btn_speed,
                  self.btn_auto, self.btn_next, self.sep3,
                  self.btn_mute, self.vol_slider):
            rl.addWidget(w)
        ctl_lay.addWidget(row)
        # Pre-stream (nothing playing yet): the transport buttons, play and
        # the audio picker can do nothing, and REC has no stream to record,
        # so they start grayed out. CC, scaling AND SPEED stay LIVE: all
        # three pick defaults the next stream starts with (sticky English
        # captions / remembered scale mode / the speed choice now applies
        # to whatever starts playing — see _set_rate).
        # _update_control_state() re-enables per mode once media plays.
        for b in (self.btn_back60, self.btn_back10, self.btn_play,
                  self.btn_fwd10, self.btn_begin, self.btn_live,
                  self.btn_rec, self.btn_audio,
                  self.btn_next):
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
        # Belt-and-suspenders for the Qt.Tool overlay window (see
        # _OVERLAY_WIN_FLAGS): ownership already keeps it under other apps'
        # windows, but hide it outright on focus loss anyway so a stale
        # show() path can never float controls/captions over a foreground
        # app (or over the taskbar) even for a frame.
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
        self.btn_audio.clicked.connect(self._audio_menu)
        self.btn_scale.clicked.connect(self._scale_menu)
        self.btn_speed.clicked.connect(self._speed_menu)
        self.btn_auto.toggled.connect(self._on_autoplay_toggled)
        self.btn_next.clicked.connect(self._play_next_clicked)
        self.btn_mute.toggled.connect(self._on_mute)
        self.btn_dl.clicked.connect(self._start_download)
        self.btn_win.clicked.connect(self._on_win_btn)
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
        # Splitter/window resize settle: rebuild the native video window's
        # content once dragging stops (see _recompose_video_surface).
        self._ghost_fix_timer = QtCore.QTimer(self)
        self._ghost_fix_timer.setSingleShot(True)
        self._ghost_fix_timer.setInterval(180)
        self._ghost_fix_timer.timeout.connect(self._recompose_video_surface)
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
        now = now_s()
        if self._ghost_fix_timer.isActive() and self._resize_burst_t0 \
                and now - self._resize_burst_t0 > 0.15:
            # long continuous drag: clear the accumulating ghost tiles
            # inline (nothing is playing on a black surface — invisible);
            # safe: no layout surgery, just a hide/repaint/show cycle
            self._recompose_video_surface()
            self._resize_burst_t0 = now
        else:
            if not self._ghost_fix_timer.isActive():
                self._resize_burst_t0 = now
        super().resizeEvent(event)
        self._layout_overlays()
        # Video aspect/crop is re-applied debounced (see _scale_timer):
        # never block the GUI thread with libvlc calls mid-drag.
        self._scale_timer.start()
        # Native video window ghost cure, also debounced (see
        # _recompose_video_surface): runs once the drag settles.
        self._ghost_fix_timer.start()

    def _recompose_video_surface(self):
        """Clear the tiled "ghost" echoes a splitter drag leaves on the video.

        The video surface is a NATIVE child window; Windows fills strips a
        resize exposes with nothing (NULL class brush), so the DWM keeps
        compositing whatever was on screen there before — tiled echoes of
        the channel list / splitter handle over the black video area.  With
        nothing playing no repaint ever covers them.  Hide the surface,
        force a synchronous repaint of the window (which also erases every
        stale backing-store pixel underneath the hidden surface), then show
        it again with clean content.  While a stream plays, the video
        frames repaint the whole area every few milliseconds — nothing to
        do.  Runs debounced after a resize burst settles, and inline every
        ~0.35 s during a long drag.
        """
        if self._closing:
            return
        try:
            playing = False
            try:
                playing = bool(self.vlc.is_playing())
            except Exception:  # noqa: BLE001
                pass
            if playing:
                return
            self.surface.hide()
            self.window().repaint()
            self.surface.show()
        except Exception:  # noqa: BLE001
            pass

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
        # WINDOWED exception: when fit mode leaves a bottom black bar and
        # the player is not fullscreen, the captions park INSIDE that bar
        # (the professional-player placement) instead of over the
        # picture's bottom edge — the widget then covers the whole
        # surface and paints its text into the bar (see set_bar_top).
        vx, vy, vw, vh = displayed_video_rect(
            self._video_wh, self._scale_mode, g.width(), g.height())
        bar_top = None
        if self._cap_bar_enabled() and self._scale_mode == "fit" \
                and g.height() - (vy + vh) >= 48:
            bar_top = vy + vh
        if bar_top is not None:
            cap = QtCore.QRect(g.left(), g.top(), g.width(), g.height())
        else:
            cap = QtCore.QRect(g.left() + vx, g.top() + vy, vw, vh)
        if self._cap_wid.geometry() != cap:
            self._cap_wid.setGeometry(cap)
        self._cap_wid.set_bar_top(bar_top)
        if not self.ctl.isHidden():
            # the control bar spans the whole SURFACE bottom — captions
            # must clear it. Over the picture the inset is measured from
            # the PICTURE bottom, so a letterboxed picture (bottom above
            # the bar) needs less of it; in bar mode it is measured from
            # the surface bottom (= the overlay's own bottom edge).
            bar_rect_top = g.height() - self.ctl.height() - 10
            if bar_top is None:
                self._cap_wid.set_bottom_inset(
                    max(24, (vy + vh) - bar_rect_top + 4))
            else:
                self._cap_wid.set_bottom_inset(
                    max(0, g.height() - bar_rect_top + 4))
        else:
            self._cap_wid.set_bottom_inset(24 if bar_top is None else 0)

    def _cap_bar_enabled(self) -> bool:
        """Park captions in the bottom letterbox bar? (windowed only —
        fullscreen keeps the classic over-the-picture placement; the
        setting lives in Subtitle settings so it can be turned off.)"""
        if self._fullscreen:
            return False
        try:
            return bool(self.config.subtitle_appearance.get("prefer_bar",
                                                            True))
        except Exception:  # noqa: BLE001
            return True

    def set_client(self, client):
        self.client = client

    # ---- content kinds ----
    def _is_vod(self) -> bool:
        """A movie, series episode or catch-up program: the whole recording
        already exists server-side, so it is seekable/scrubbable without
        any DVR machinery."""
        return bool(self.current
                    and self.current.get("kind") in ("vod", "series",
                                                     "catchup"))

    def _is_catchup(self) -> bool:
        """A provider catch-up (archive) program — window-downloadable."""
        return bool(self.current and self.current.get("kind") == "catchup")

    def _catchup_dur_ms(self) -> int:
        """Known recording length (EPG start -> stop).  VLC cannot compute
        a duration for these raw TS streams, so the scrubber is seeded
        from the program window instead of get_length()."""
        cur = self.current or {}
        try:
            return max(0, int(cur.get("utc_end") or 0)
                       - int(cur.get("utc_start") or 0)) * 1000
        except (TypeError, ValueError):
            return 0

    def _catchup_seek_to(self, target_ms: float):
        """Catch-up seek on the byte-fraction axis: time-based set_time
        lands imprecisely on indexless TS, but the relay is fully
        range-seekable and VLC maps fractions to exact byte ranges."""
        dur = self._catchup_dur_ms()
        if dur <= 0:
            return
        frac = max(0.0, min(0.999, float(target_ms) / dur))
        self.vlc.set_position(frac)
        self._vid_s = min(dur / 1000.0, max(0.0, float(target_ms) / 1000.0))
        self._cu_raw_wall = now_s()   # a fresh seek must not look like a stall

    # Catch-up stall watchdog: the provider's timeshift backend kills
    # sibling connections mid-body (see CatchupRelay's _RESUME_* policy),
    # and the relay's byte-exact resume bridges almost every kill — but if
    # retries run out (or the whole relay died) VLC is left frozen/ended
    # forever: catch-up has NO chase machinery to notice. Reopen the
    # stream at the tracked position when the clock stops advancing
    # mid-program.
    _CU_FREEZE_S = 12.0      # raw frozen this long while "playing" = rescue
    _CU_STOP_TICKS = 4       # not-playing ticks this many = rescue
    _CU_END_MARGIN_S = 15.0  # this close to the program end, stopping is
    #                          # natural (recording ran out) — never rescue

    # VOD (series/movies) stall watchdog — same disease as catch-up's
    # (provider/relay connection dies mid-episode, VLC freezes on the last
    # frame, pause/play do nothing and autoplay never fires because the
    # player never reaches "ended"; seen live on 2026-08-29 autoplay of
    # Adventure Time S3E19: log silent from 12:47 to the 18:58 close).
    _VOD_FREEZE_S = 30.0     # raw frozen this long while playing = rescue
    _VOD_MAX_RESCUES = 2     # then give up (logged at ERROR = auto-report)

    def _vod_stall_watchdog(self, now, playing, raw_s, dur_s, raw_moved):
        if (self._closing or self._live_paused or self._seeking
                or not self.current
                or self.current.get("kind") not in ("series", "movie",
                                                    "vod")):
            self._vod_raw_wall = now
            return
        if raw_moved:
            self._vod_raw_wall = now
            return
        if not playing or self._vid_s >= dur_s - self._CU_END_MARGIN_S:
            self._vod_raw_wall = now   # near-end/natural stop isn't a stall
            return
        if now - self._vod_raw_wall < self._VOD_FREEZE_S:
            return
        self._vod_raw_wall = now
        cur = dict(self.current)
        pos_s = max(0.0, self._vid_s)
        from .. import feedback
        feedback.stat("vod_stalls")
        if self._vod_rescues >= self._VOD_MAX_RESCUES:
            log.error("VOD stalled twice at %.0fs and gave up: %r "
                      "(pause/play dead, autoplay blocked)", pos_s,
                      cur.get("title"))
            return
        self._vod_rescues += 1
        log.error("VOD stall rescue %d: clock frozen at %.0fs — reopening "
                  "'%s'", self._vod_rescues, pos_s, cur.get("title"))
        feedback.crumb("VOD stall rescue at %.0fs: %r"
                       % (pos_s, cur.get("title")))
        # cap the resume position a little before the freeze point so the
        # reopened stream doesn't land straight back on the dead edge
        self.play_media(cur, start_at=max(0.0, pos_s - 3.0))

    def _catchup_watchdog(self, now, playing, raw_s, dur_s, raw_moved):
        if (self._closing or self._live_paused or self._seeking
                or not self.current):
            self._cu_raw_wall = now
            return
        if raw_moved:
            self._cu_raw_wall = now
            self._cu_raw_moved = True
        elif not playing:
            self._cu_raw_wall = now   # an idle player has no clock to watch
        self._stall_ticks = 0 if playing else self._stall_ticks + 1
        near_end = self._vid_s >= dur_s - self._CU_END_MARGIN_S
        frozen = (playing and self._cu_raw_moved
                  and self._cu_raw_wall > 0.0
                  and now - self._cu_raw_wall > self._CU_FREEZE_S)
        stopped = (self._stall_ticks >= self._CU_STOP_TICKS
                   and self._vid_s > 5.0)
        if near_end or not (frozen or stopped):
            return
        if now - self._last_reopen < _REOPEN_COOLDOWN_S:
            return
        freeze_s = max(0.0, now - self._cu_raw_wall) if frozen else 0.0
        # _vid_s kept integrating through the freeze — hand the rescue the
        # position the picture actually froze on
        pos_ms = max(0.0, self._vid_s - freeze_s) * 1000.0
        self._rescue_catchup(pos_ms, "frozen" if frozen else "stopped")

    def _rescue_catchup(self, pos_ms: float, why: str):
        """Reopen the catch-up stream where it stalled (watchdog fire)."""
        self._last_reopen = now_s()
        self._stall_ticks = 0
        self._cu_raw_wall = now_s()
        self._cu_raw_moved = False
        self._cu_rescues = getattr(self, "_cu_rescues", 0) + 1
        cur = self.current or {}
        if self._cu_rescues >= 2 and cur.get("kind") == "catchup" \
                and getattr(self, "client", None):
            # Repeat failures on this very stream: the catch-up URL FORM
            # itself may be wrong for this panel (panels support only one
            # of the two families — see XtreamClient.timeshift_url).
            # Flip legacy/modern and replay from scratch via the relay.
            try:
                dur_min = max(1, math.ceil(
                    (cur.get("utc_end", 0) - cur.get("utc_start", 0))
                    / 60.0)) or 60
                cur = dict(cur)
                cur["url"] = self.client.timeshift_url(
                    cur["stream_id"], cur["utc_start"], dur_min)
                self._catchup_local_url = ""
                self.current = cur
                self.play_media(cur, start_at=max(0.0, pos_ms / 1000.0))
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("catchup form-flip replay failed: %r", exc)
        url = self._catchup_local_url \
            or (self.current or {}).get("url", "")
        if not url:
            return
        try:
            log.warning("catchup rescue: stream %s at %.1fs — reopening",
                        why, pos_ms / 1000.0)
        except Exception:
            pass
        self.vlc.play(url, timeshift=False)
        sess = self._session

        def _reseek():
            # set_position before VLC finished opening is a silent no-op —
            # re-apply the position once (and again a beat later) the media
            # is actually playing
            if self._closing or self._session != sess \
                    or not self._is_catchup():
                return
            self._catchup_seek_to(pos_ms)
        QtCore.QTimer.singleShot(1500, _reseek)
        QtCore.QTimer.singleShot(3000, _reseek)

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

    def show_info(self, title: str, epg: str = "", sticky: bool = False):
        """Show the now-playing banner.

        ``sticky=True`` (media start/switch): the banner stays for as long
        as the playback controls are on screen and hides with them; after
        that it only comes back when the user moves the cursor to the very
        top of the video (see _wake) and leaves again when the controls
        sleep. ``sticky=False`` (transient messages, EPG refresh) keeps the
        old 4.5 s auto-hide — and while a sticky banner is up it only
        refreshes the text instead of restarting a timer."""
        if epg:
            self._last_epg = epg
        elif title and not epg and self._last_epg:
            epg = self._last_epg
        if sticky:
            self._info_sticky = True
        elif self._info_sticky:
            self.info_overlay.set_info(title, epg)   # refresh text only
            return
        self.info_overlay.set_info(title, epg)
        self.info_overlay.show()
        if (not self._immersive and not self.overlay.isVisible()
                and not self._overlay_suppressed):
            self.overlay.show()   # the info banner lives in the overlay window
        self._layout_overlays()
        if not sticky:
            self.info_timer.start()

    def _resurface_info(self):
        """Bring the now-playing banner back for another control-cycle
        (cursor dragged to the very top of the video while the controls
        are awake)."""
        if not self.current:
            return
        title = self.current.get("title", "")
        self.show_info(title, self._last_epg, sticky=True)

    def hide_info(self):
        self.info_overlay.hide()
        self._info_sticky = False

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
        try:
            from .. import feedback
            feedback.crumb("play %s %r" % (kind, title))
            feedback.usage("play_" + kind)
        except Exception:
            pass
        self._last_epg = ""
        self.show_info(title, sticky=True)
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
        self._cap_tick_errs.clear()   # a new media logs its own errors
        # fresh live-CC arrival anchor + tracked caption clock per media
        self._cc_off = None
        self._cc_last_c = None
        self._cc_last_t = 0.0
        self._cc_pend = None
        self._cc_prev_target = None
        self._cc_oob_run = 0
        self._cc_stash = []
        self._cc_lag = None
        self._cc_head_pcr = None
        self._cc_join_byte = 0
        self._cc_join_app_s = 0.0
        self._cc_last_arrival = 0.0
        self._cc_last_active = 0.0
        self._cc_last_watchfire = 0.0
        self._cap_clock_s = 0.0
        self._cap_raw_s = None
        self._cap_raw_wall = 0.0
        self._cap_raw_clock = None
        self._cap_backlog_s = None
        self._cap_div_s = 0.0
        self._cap_div_ok = False
        self._sync_pcr_join = None      # new media -> new CCX join reference
        self._sync_pcr_join_tries = 0
        self._sync_credit_s = 0.0
        self._sync_capped_s = 0.0
        if _SYNC_ON:
            if _SYNC_ON:
                synclog.info("MEDIA kind=%s title=%r", kind, title)
        self._cap_wall = 0.0
        self.btn_rec.blockSignals(True)
        self.btn_rec.setChecked(False)
        self.btn_rec.setIcon(ic.rec(False))
        self.btn_rec.blockSignals(False)
        # a half-finished download-window selection never carries into the
        # next program
        self._win_cancel(silent=True)
        self._mode = "live"
        self._chase_paused = False
        self._dvr_t0 = None
        self._dvr_base = 0.0
        self._reset_dvr_clock()
        self._stall_ticks = 0
        self._last_reopen = 0.0
        self._reopen_last_at = -1e9
        self._reopen_last_t = 0.0
        self._reopen_repeats = 0
        self._cu_raw_wall = 0.0
        self._cu_raw_moved = False
        self._chase_started = False
        # fresh chase budget: a channel that gave up on the buffer gets to
        # try again from scratch (pending retry timers die on the session
        # bump at the top of play_media)
        self._chase_fail_count = 0
        self._seek_verify = None
        self._seek_esc_strikes = 0
        self._seek_esc_ok_at = 0.0
        self._seek_esc_clean = 0.0
        self._raw_win = []
        self._trickle_hold = False
        self._dvr_status.hide()
        self._scrub_on = False
        self._vid_s = 0.0
        self._eof_next_done = False   # re-arm autoplay-next for this media
        self._cu_rescues = 0         # re-arm catch-up rescue/form-flip
        if (self.current or {}).get("url") != playable.get("url"):
            self._vod_rescues = 0     # same-media rescue reopens keep the cap
        self._vod_raw_wall = now_s()
        self._last_raw = None
        self._raw_change_wall = 0.0
        self._video_wh = (0, 0)   # next media's size is unknown until the
                                  # tick polls it (captions re-anchor then)
        self._live_paused = False
        # Audio track picks are PER-PROGRAM: every load starts at Auto
        # (English default). Unlike subtitles, a manual pick must never
        # follow the user into the next program — this provider's rips
        # share identical track names across the catalog, so a sticky
        # pick would re-select the same language on every later show
        # (the pick still survives player swaps and chase reopens within
        # THIS program via _enforce_audio's name re-match).
        self._audio_want = None
        self._audio_name = ""
        self._audio_auto_tid = None
        # Playback speed is STICKY: a pick made before (or during) a stream
        # applies to whatever starts next — never reset to 1x on switch.
        # _poke_rate re-applies it once the fresh player has its media.
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
        self._poke_rate()
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

    # ---- play next / autoplay next ----
    def _on_autoplay_toggled(self, on):
        self.config.autoplay_next = bool(on)
        self.btn_auto.setIcon(ic.autoplay(on))
        self.btn_auto.setToolTip("Autoplay next episode — ON"
                                 if on else "Autoplay next episode — OFF")

    def _play_next_clicked(self):
        cur = self.current
        if self._closing or not cur:
            return
        if cur.get("kind") == "live":
            self.request_next_channel.emit()
            return
        if cur.get("kind") not in ("series", "catchup") or not self.client:
            return
        if cur.get("kind") == "series" and cur.get("series_id") is None:
            self.show_info("No episode list for this item")
            return
        self.show_info("Finding next\u2026")
        base = cur.get("fav_key")
        self._next_runner.run(lambda: (base, self._fetch_next(cur)))

    @staticmethod
    def _nn(v):
        """Tolerant int() for provider episode/season numbers."""
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return 0

    def _fetch_next(self, cur):
        """Worker-thread lookup: the playable AFTER ``cur``, or None.
        Series walks the full ordered season list (season finale -> the
        next season's first episode); catch-up takes the earliest recorded
        program that starts after the current one."""
        if cur.get("kind") == "series":
            return self._fetch_series_next(cur)
        if cur.get("kind") == "catchup":
            return self._fetch_catchup_next(cur)
        return None

    def _fetch_series_next(self, cur):
        info = self.client.series_info(cur["series_id"]) or {}
        eps = []
        ep_map = info.get("episodes")
        if isinstance(ep_map, dict) and ep_map:
            # modern shape: {"episodes": {"1": [...], "2": [...]}}
            for key, lst in ep_map.items():
                for e in sorted(lst or [],
                                key=lambda e: self._nn(e.get("episode_num"))):
                    eps.append((self._nn(key),
                                self._nn(e.get("episode_num")), e))
        else:
            for sm in info.get("seasons") or []:
                for e in sm.get("episode") or []:
                    eps.append((self._nn(sm.get("season_number")),
                                self._nn(e.get("episode_num")), e))
        eps.sort(key=lambda t: (t[0], t[1]))
        season = self._nn(cur.get("season"))
        episode = self._nn(cur.get("episode"))
        idx = next((i for i, (s, n, _e) in enumerate(eps)
                    if s == season and n == episode), None)
        if idx is None or idx + 1 >= len(eps):
            return None
        s, n, e = eps[idx + 1]
        name = cur.get("series_name") or ""
        return {
            "kind": "series",
            "title": browsers.episode_title(
                name, s, n, e.get("title")),
            "url": self.client.series_url(e.get("id"),
                                          e.get("container_extension", "mp4")),
            "fav_key": f"episode:{e.get('id')}",
            "icon": (e.get("info") or {}).get("movie_image", ""),
            "series_id": cur.get("series_id"),
            "series_name": name,
            "season": s,
            "episode": n,
        }

    def _fetch_catchup_next(self, cur):
        started_after = int(cur.get("utc_start") or 0)
        if not started_after or cur.get("stream_id") is None:
            return None
        now = time.time()
        best = None
        for e in self.client.epg_table(cur["stream_id"]) or []:
            try:
                st = int(str(e.start_timestamp).strip())
                sp = int(str(e.stop_timestamp).strip())
            except (TypeError, ValueError):
                continue
            # only programs that already started (they exist in the
            # archive) and begin after the current one
            if st <= started_after or st > now:
                continue
            if best is None or st < best[0]:
                best = (st, sp, e)
        if best is None:
            return None
        st, sp, e = best
        if sp <= st:
            sp = st + 1800
        title = _decode(e.title) or "Program"
        dur_min = max(1, math.ceil((sp - st) / 60.0))
        return {
            "kind": "catchup",
            "title": f"{cur.get('channel', '')} \u2014 {title}",
            "url": self.client.timeshift_url(cur["stream_id"], st, dur_min),
            "stream_id": cur["stream_id"],
            "utc_start": st,
            "utc_end": sp,
            "channel": cur.get("channel", ""),
            "program": title,
            "fav_key": f"catchup:{cur['stream_id']}:{st}",
            "icon": cur.get("icon", ""),
        }

    def _on_next_fetched(self, result):
        ok, val = result
        if self._closing or ok != "ok":
            if not self._closing:
                try:
                    log.warning("next-episode lookup failed: %s", val)
                except Exception:
                    pass
                self.show_info("Could not look up the next item")
            return
        base, nxt = val
        cur = self.current or {}
        if cur.get("fav_key") != base:
            return          # the user moved on while the lookup ran
        if not nxt:
            self.show_info("No more episodes in this series"
                           if cur.get("kind") == "series"
                           else "No later programs in the archive")
            return
        self._start_next(nxt)

    def _start_next(self, nxt):
        """Switch playback to the next item through the same bookkeeping
        MainWindow.play() does (recents + last_channel)."""
        self.config.add_recent(nxt)
        self.config.data["last_channel"] = nxt
        self.config.save()
        self.play_media(nxt)
        # move the browser tab's blue selection to the new episode/program
        try:
            win = self.window()
            if hasattr(win, "_select_playing"):
                win._select_playing(nxt)
        except Exception:  # noqa: BLE001
            pass

    def _maybe_autoplay_next(self, playing, length_ms, raw_ms):
        """Natural end of a series episode / catch-up program: roll into
        the next item when the autoplay toggle is on."""
        if self._eof_next_done or self._closing:
            return
        cur = self.current or {}
        if cur.get("kind") not in ("series", "catchup"):
            return
        if not self.btn_auto.isChecked() or length_ms <= 0:
            return
        if self._seeking or self._live_paused or self._win_sel \
                or self._downloading:
            return
        ended = False
        try:
            ended = self.vlc.state_name() == "ended"
        except Exception:  # noqa: BLE001
            pass
        if not ended:
            # indexless catch-up TS may never report "ended" — the clock
            # sitting at the end with playback stopped is the same signal
            if playing or raw_ms <= 0 or raw_ms < length_ms - 1500:
                return
        self._eof_next_done = True
        self.show_info("Up next\u2026")
        base = cur.get("fav_key")
        self._next_runner.run(lambda: (base, self._fetch_next(cur)))

    # ---- controls ----
    def _toggle_pause(self):
        if not self.current:
            return          # nothing loaded (pre-stream Space key)
        if self._mode == "chase":
            # Watching the buffer: a plain file pause — the recorder keeps
            # filling the file, so this is a flawless pause of live TV.
            try:
                down = self.vlc.state_name() in ("ended", "stopped", "error")
            except Exception:  # noqa: BLE001
                down = False
            if self._chase_paused:
                # resume — unless the player died at the file edge meanwhile
                self._sync_transport("resume")
                if down:
                    cur = self.vlc.get_time()
                    at = self._cap_content_for_raw(cur / 1000.0) \
                        if cur >= 0 else self._frontier_s()
                    self._chase_seek(at, resume=True)
                else:
                    self._chase_paused = False
                    self.vlc.resume()
            elif down:
                # stalled at the edge (not user-paused): play revives it
                self._sync_transport("revive-resume")
                cur = self.vlc.get_time()
                at = self._cap_content_for_raw(cur / 1000.0) \
                    if cur >= 0 else self._frontier_s()
                self._chase_seek(at, resume=True)
            else:
                self._sync_transport("pause")
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
        now = now_s()
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
            if _SYNC_ON:
                synclog.info("DVRSTART base=%.2f size=%d",
                             self._dvr_base, size)
            return
        if size > self._dvr_size:
            # growth since the last GROWTH sighting confirms the interval
            # between them was recorded. The cap only guards against
            # pathological stalls — VLC flushes the sout file in 2-4 s
            # bursts, so a tight cap here under-counted real content and
            # made jump-to-live land far behind the true edge.
            if self._dvr_last_growth is not None:
                gap = max(0.0, now - self._dvr_last_growth)
                credit = min(15.0, gap)
                if _SYNC_ON:
                    self._sync_credit_s += credit
                    if gap > 15.0:
                        self._sync_capped_s += gap - 15.0
                        synclog.info(
                            "DVRCLAMP gap=%.2f capped_away=%.2f "
                            "cum_credit=%.1f cum_capped=%.1f fr=%.1f",
                            gap, gap - 15.0, self._sync_credit_s,
                            self._sync_capped_s, self._frontier_s())
                self._dvr_content_s += credit
            self._dvr_last_growth = now
        self._dvr_size = size
        self._dvr_tick_t = now

    def _head_ahead_s(self, current: float):
        """WP2 (b): REAL content ahead of the viewer, for the wedge
        rescue. Primary source is the PCR head (reliable in both frontier
        failure directions — the frontier under-credits the cold burst, so
        at the true edge ``frontier - current`` is NEGATIVE and the old
        rescue was structurally unreachable; it also over-credits slow
        trickles). Falls back to the legacy frontier gap while the PCR /
        join pins are unavailable (no CC pipeline yet, mid-session join
        not refined, probe failure) — exactly today's behavior."""
        head = self._cc_head_pcr
        if head is not None and self._sync_pcr_join is not None \
                and self._cc_join_app_s is not None:
            head_rel = head[0] - self._sync_pcr_join[1]
            return (head_rel + self._cc_join_app_s) - current
        return self._frontier_s() - current

    def _trickle_test(self, now: float, playing: bool) -> bool:
        """WP2 (c): True when raw advanced less than _CC_TRICKLE_RATIO of
        rate x wall over the rolling window while "playing" — frames are
        trickling in (0.2x-delivery nights), so the caption clock and
        _vid_s must HOLD rather than integrate wall time (measured 8.5 s
        of clock lead through sub-6-s freeze/thaw cycles)."""
        if not playing or self._chase_paused or len(self._raw_win) < 2:
            return False
        t0, r0 = self._raw_win[0]
        raw = self._raw_win[-1][1]
        if r0 is None or raw is None:
            return False
        wall = now - t0
        if wall < _CC_TRICKLE_WIN_S * 0.8:
            return False          # window not full yet
        if raw <= r0:
            return True           # nothing advanced at all while "playing"
        return (raw - r0) < _CC_TRICKLE_RATIO * self._rate * wall

    def _raw_win_rate(self, now: float) -> float:
        """Measured raw advance per wall second over the rolling window —
        the delivered-data rate while frames trickle (the viewer at the
        edge consumes exactly what arrives). Clamped to the playback
        rate: raw cannot legitimately advance faster."""
        if len(self._raw_win) < 2:
            return self._rate
        t0, r0 = self._raw_win[0]
        rn = self._raw_win[-1][1]
        if r0 is None or rn is None or now <= t0:
            return self._rate
        rate = (rn - r0) / (now - t0)
        return max(0.0, min(rate, self._rate))

    # ---- stage-1 sync diagnosis helpers (measurement only) ----

    def _sync_raw_s(self) -> float:
        """Raw VLC get_time() in seconds (-1 when unreadable). Read-only."""
        try:
            ms = self.vlc.get_time()
        except Exception:  # noqa: BLE001
            ms = -1
        return ms / 1000.0 if 0 <= ms < 86_400_000 else -1.0

    def _sync_probe_head_pcr(self):
        """(pid, pcr_s) of the newest PCR in the DVR buffer tail."""
        buf = None
        try:
            if self.dvr:
                buf = self.dvr.buffer_file()
        except Exception:  # noqa: BLE001
            buf = None
        if not buf:
            return None, None
        return probe_tail_pcr(buf)

    def _sync_transport(self, tag: str, target=None, extra: str = ""):
        """Transport decision trace: commanded target, raw get_time before
        the call and ~1 s after (did the player actually move there?)."""
        if self._closing or not _SYNC_ON:
            return
        synclog.info("XPORT %s target=%s raw_pre=%.2f fr=%.2f cc=%.2f "
                     "vid=%.2f rate=%.2f %s",
                     tag, "-" if target is None else "%.2f" % target,
                     self._sync_raw_s(), self._frontier_s(), self._cap_clock_s,
                     self._vid_s, self._rate, extra)

        def _after():
            if self._closing:
                return
            synclog.info("XPORT1s %s raw=%.2f fr=%.2f cc=%.2f vid=%.2f",
                         tag, self._sync_raw_s(), self._frontier_s(),
                         self._cap_clock_s, self._vid_s)
        QtCore.QTimer.singleShot(1000, _after)

    def _sync_probe_join_at(self, join: int):
        """Pin the PCR at CCExtractor's join byte — the zero point of CCX's
        own cue axis. Retried a few times (a just-created buffer may not
        have flushed a PCR past the join byte yet). Stage 2 needs this on
        EVERY run (L is measured live), not only with sync logging."""
        if self._closing or self._cc_source is None:
            return
        buf = None
        try:
            if self.dvr:
                buf = self.dvr.buffer_file()
        except Exception:  # noqa: BLE001
            buf = None
        if not buf:
            return
        pid, pcr = probe_first_pcr_at(buf, join)
        if pcr is not None:
            self._sync_pcr_join = (pid, pcr)
            if _SYNC_ON:
                synclog.info("PCRJOIN byte=%d pid=%s pcr=%.3f",
                             join, pid, pcr)
        elif self._sync_pcr_join_tries < 20:
            self._sync_pcr_join_tries += 1
            QtCore.QTimer.singleShot(500,
                                     lambda: self._sync_probe_join_at(join))

    def _sync_tick(self):
        """5 s snapshot of every timing axis + the CCX lag estimate."""
        if self._closing or not _SYNC_ON:
            return
        if not (self._mode == "chase" and self.dvr):
            return
        now = now_s()
        raw = self._sync_raw_s()
        fr = self._frontier_s()
        cc = self._cap_clock_s
        pid, pcr = self._sync_probe_head_pcr()
        head_rel = lag = None
        if pcr is not None and self._sync_pcr_join is not None:
            head_rel = pcr - self._sync_pcr_join[1]
            if self._cc_last_c is not None:
                lag = head_rel - self._cc_last_c
        fsize = feedpos = -1
        if self._cc_source is not None:
            feedpos = int(getattr(self._cc_source, "_ts_pos", -1) or -1)
        try:
            if self.dvr and self.dvr.file_path:
                fsize = os.path.getsize(self.dvr.file_path)
        except OSError:
            pass
        synclog.info(
            "TICK fr=%.2f cc=%.2f raw=%.2f vid=%.2f playing=%s "
            "started=%s stall=%d | d_fr_cc=%+.2f "
            "d_fr_raw=%+.2f d_cc_raw=%+.2f | off=%s ccx_end=%s "
            "since_show=%s since_cue=%s | pcr=%.3f pid=%s head_rel=%s "
            "ccx_lag=%s | credit=%.1f capped=%.1f | fsize=%d feed=%d "
            "feed_behind=%d | backlog=%s edge=%s div=%s lag_ewma=%s",
            fr, cc, raw, self._vid_s,
            int(self.vlc.is_playing()), int(self._chase_started),
            self._stall_ticks,
            fr - cc, fr - raw, cc - raw,
            "-" if self._cc_off is None else "%.2f" % self._cc_off,
            "-" if self._cc_last_c is None else "%.2f" % self._cc_last_c,
            "-" if self._sync_last_show_t is None
            else "%.1f" % (now - self._sync_last_show_t),
            "-" if self._cc_last_t <= 0.0 else "%.1f" % (now - self._cc_last_t),
            -1.0 if pcr is None else pcr, "-" if pid is None else pid,
            "-" if head_rel is None else "%.2f" % head_rel,
            "-" if lag is None else "%.2f" % lag,
            self._sync_credit_s, self._sync_capped_s, fsize, feedpos,
            max(0, fsize - feedpos),
            "-" if self._cap_backlog_s is None
            else "%.2f" % self._cap_backlog_s,
            "%.2f" % self._cap_edge_s(),
            ("%.2f" % self._cap_div_s) if self._cap_div_ok else "-",
            "-" if self._cc_lag is None else "%.2f" % self._cc_lag)


    def _safe_seek_target(self, target: float) -> float:
        """Clamp a chase seek so it can never land past real data. The
        bound is the (PCR-calibrated) live edge, not the frontier — the
        frontier under-credits the cold burst by 20-35 s of content that
        the buffer really holds; frontier+60 caps pathological edge
        over-estimates (a seek past EOF would stall-loop the watchdog)."""
        edge = self._cap_edge_s()
        frontier = self._frontier_s()
        # Stale-axis guard: the edge is dead-reckoned off the caption
        # clock. If that clock has not run for seconds (the caption /
        # filter machinery down), a FROZEN edge must not veto data the
        # frontier has already confirmed on disk — it used to clamp every
        # rescue reopen and loop-breaker escape back to the buffer start,
        # which is what made the wedge repeat the same few seconds
        # forever. The frontier only ever confirms bytes that exist, so
        # it is the safe floor while the edge is stale.
        if edge < frontier and now_s() - self._cap_wall > 5.0:
            edge = frontier
        limit = min(edge, frontier + 60.0) - _CHASE_SAFETY_S
        return max(0.0, min(max(0.0, target), limit))

    # ---- stage-2 single-axis caption timing helpers ----

    def _cap_edge_s(self) -> float:
        """Best content-axis estimate of the live write head: the
        dead-reckoned clock plus backlog (PCR-calibrated), falling back to
        the frontier before a transport event seeds the backlog."""
        if self._cap_backlog_s is not None:
            return self._cap_clock_s + self._cap_backlog_s
        return self._frontier_s()

    def _cap_vlc_time_for(self, content_s: float) -> float:
        """Content-axis position -> VLC set_time NUMBER. When the raw axis
        has diverged from the dead-reckoned clock (broadcast PTS
        renumbering), the same content lives at a different number inside
        VLC; seeks must use that number or they land in the wrong place."""
        return content_s + self._cap_div_s if self._cap_div_ok else content_s

    def _cap_content_for_raw(self, raw_s: float) -> float:
        """VLC get_time number -> content-axis position (inverse map)."""
        return raw_s - self._cap_div_s if self._cap_div_ok else raw_s

    def _cap_seed_transport(self, target_s: float, jump_live: bool = False):
        """A transport event defines the caption clock BY CONSTRUCTION:
        the set_time target IS where playback lands, so the dead-reckoned
        clock seeds there and the raw-delta baseline restarts. The
        live-edge backlog reseeds with it: jump-to-live lands
        max(_CHASE_SAFETY_S, L+3) behind the edge (D1); any other seek
        PRESERVES the dead-reckoned edge (the recorder kept writing while
        the viewer was elsewhere) and measures the new backlog against
        it."""
        if not (self._mode == "chase" and self.dvr is not None):
            return
        prev_edge = None
        if self._cap_backlog_s is not None:
            prev_edge = self._cap_clock_s + self._cap_backlog_s
        # The landing gap IS the new backlog — for jump-to-live included
        # (D1's adaptive landing is max(5, L+3) behind the head; seeding the
        # constant 5 double-counted the gap and pushed every anchor pin
        # hot). prev_edge keeps the pre-seek edge estimate, so the backlog
        # tracks the real gap even when _chase_seek's clamps moved the
        # target off the nominal one.
        if prev_edge is not None:
            self._cap_backlog_s = max(_CHASE_SAFETY_S,
                                      prev_edge - float(target_s))
        else:
            self._cap_backlog_s = max(_CHASE_SAFETY_S,
                                      self._frontier_s() - float(target_s))
        if _SYNC_ON:
            synclog.info("SEED target=%.2f jump=%s backlog=%.2f edge=%.2f",
                         target_s, jump_live, self._cap_backlog_s,
                         float(target_s) + self._cap_backlog_s)
        self._cap_clock_s = float(target_s)
        self._cap_raw_s = None
        self._cap_raw_wall = 0.0
        self._cap_raw_clock = None
        self._cap_wall = now_s()

    def _cc_probe_head_pcr(self):
        """Absolute PCR at the buffer's write head, throttled to ~4/s
        (probe cost ~20 ms; cue bursts arrive faster). The value lives on
        the renumber-immune PCR content axis."""
        now = now_s()
        if self._cc_head_pcr is not None \
                and now - self._cc_head_pcr[1] < 0.25:
            return self._cc_head_pcr[0]
        buf = None
        try:
            if self.dvr:
                buf = self.dvr.buffer_file()
        except Exception:  # noqa: BLE001
            buf = None
        if not buf:
            return None
        _pid, pcr = probe_tail_pcr(buf)
        if pcr is not None:
            self._cc_head_pcr = (pcr, now)
        return pcr

    def _cc_refine_join_app(self, head_pcr: float):
        """After a MID-SESSION CCX join: derive the join byte's
        content-axis position from the TS byte rate measured on the
        join->head segment (seconds per byte = (head_pcr - join_pcr) /
        bytes written since). Byte-0 joins are exactly 0 — nothing to
        derive; without this a mid-session join's head_rel would sit tens
        of seconds below the app axis and drag every anchor with it."""
        if self._cc_join_app_s is not None or self._cc_join_byte <= 0 \
                or self._sync_pcr_join is None or self.dvr is None \
                or not self.dvr.file_path:
            return
        try:
            size = os.path.getsize(self.dvr.file_path)
        except OSError:
            return
        seg_s = head_pcr - self._sync_pcr_join[1]
        seg_b = size - self._cc_join_byte
        if seg_s > 5.0 and seg_b > 188:
            self._cc_join_app_s = max(0.0,
                                      self._cc_join_byte * seg_s / seg_b)
            if _SYNC_ON:
                synclog.info("JOINAPP byte=%d app=%.2f (seg %.2fs/%dB)",
                             self._cc_join_byte, self._cc_join_app_s,
                             seg_s, seg_b)

    def _cc_calibrate_edge(self, head_rel: float) -> float:
        """Keep the dead-reckoned live edge honest: the PCR head plus the
        join byte's content position IS the write head, so pull the edge
        toward it — gently for drift (wall dead-reckoning error, provider
        rate wobble), hard when they come apart (cold-burst under-credit,
        recorder restart hiccups). Anchors pin at edge - L, so this is
        what keeps mapped cues at their true content positions.

        Returns the SNAP correction applied (0.0 for a gentle pull): a
        landed CDN burst moves the head AND CCX's lag by the same amount,
        so callers advance the lag estimate with it — otherwise the pin
        (edge - L) runs hot for the several seconds the lag EWMA needs to
        catch up, and every burst forced a whole-store rebase."""
        if self._cc_join_app_s is None:
            return 0.0    # mid-session join not yet placed — wait for the
            #               # join-app refinement (a wrong constant here would
            #               # drag every anchor with it)
        target = head_rel + self._cc_join_app_s
        if self._cap_backlog_s is None:
            self._cap_backlog_s = max(_CHASE_SAFETY_S,
                                      target - self._cap_clock_s)
            return 0.0
        err = target - (self._cap_clock_s + self._cap_backlog_s)
        if abs(err) > _CC_EDGE_SNAP_S:
            self._cap_backlog_s += err
            if _SYNC_ON:
                synclog.info("EDGESNAP err=%+.2f edge->%.2f", err, target)
            return err
        self._cap_backlog_s += err * _CC_EDGE_ALPHA
        return 0.0

    def _cc_rebase(self, target_off: float, why: str):
        """Snap-and-rebase: set the CCX->app offset immediately (no EWMA
        crawl) AND slide every stored cue/filter window by the same delta
        so the store's timeline stays coherent — a scrub back after the
        rebase shows captions placed where they actually play."""
        shift = target_off - (self._cc_off if self._cc_off is not None
                              else 0.0)
        self._cc_off = target_off
        self._cc_oob_run = 0
        self._cap_cues.shift(shift)
        self._filter_engine.shift_windows(shift)
        if _SYNC_ON:
            synclog.info("REBASE why=%s shift=%+.2f off=%.2f",
                         why, shift, target_off)

    def _cc_edge_probe_tick(self):
        """2 s: re-probe the write head and pull the live edge onto it
        (see _cc_calibrate_edge — the head bursts, the wall clock lies)."""
        if self._closing or self._cc_source is None \
                or not (self._mode == "chase" and self.dvr):
            return
        head_pcr = self._cc_probe_head_pcr()
        if head_pcr is None or self._sync_pcr_join is None:
            return
        self._cc_refine_join_app(head_pcr)
        self._cc_calibrate_edge(head_pcr - self._sync_pcr_join[1])

    def _seek_ms(self, ms):
        if self._mode == "chase" and self.dvr:
            # Skip relative to what's DISPLAYED (the content-axis clock),
            # not to _vid_s — after PTS renumbering those disagree. The
            # max() guards against a clock that is somehow stale: it must
            # never drag a rewind target BEHIND the tracked position (the
            # frozen-clock bug sent Rewind 60 straight to the beginning).
            base = max(self._cap_clock_s, self._vid_s) \
                if self._cap_clock_s > 0.0 else self._vid_s
            self._chase_seek(base + ms / 1000.0)
            return
        if self._is_catchup():
            # indexless TS: seek on the byte-fraction axis
            self._catchup_seek_to(self._vid_s * 1000.0 + ms)
            return
        # Live / VOD: normal seek (works for VOD; live streams ignore it).
        self.vlc.seek_ms(ms)

    def _jump_begin(self):
        """The inverse of LIVE: restart playback at the very beginning of
        the DVR buffer (or of the movie)."""
        if self._mode == "chase" and self.dvr:
            self._set_rate(1.0)
            self._sync_transport("jump_begin", 0.0)
            self._chase_seek(0.0, resume=True)
        elif self._is_catchup():
            self._catchup_seek_to(0.0)
        elif self.vlc.get_length() > 0:
            self.vlc.set_time(0)

    def _chase_seek(self, target_s: float, resume: bool = False,
                    jump_live: bool = False):
        """Seek within the chase buffer — and revive a dead player.

        ``set_time()`` is a NO-OP once VLC ran into the end of the growing
        buffer file and stopped, which used to leave every rewind / FF /
        LIVE / play press dead until the watchdog noticed. When the display
        player is down (ended/stopped/error) the buffer file is reopened AT
        the target instead (~half a second) — a local file operation, so
        the single 8kstrong connection is never touched.

        Targets arrive on the app's CONTENT axis; the set_time number is
        converted via the measured axis divergence (broadcast PTS
        renumbering moves VLC's numbers without moving the content).
        ``jump_live=True`` targets the live edge (dead-reckoned +
        PCR-calibrated head) instead of the frontier-clamped position —
        the frontier under-credits the cold burst by 20-35 s of content;
        with D1's adaptive landing the target sits max(5, L+3) behind
        the edge while the measured CCX lag L is large.

        WP2 (a): every set_time is VERIFIED — a wedged player (still
        "playing", demuxer blocked at the buffer tail) silently no-ops
        it. _arm_seek_verify arms the check; _verify_seek (in _tick)
        escalates to the play_at revive on a no-op.
        """
        if not (self._mode == "chase" and self.dvr):
            return
        target_s = float(target_s)
        if jump_live:
            back = _chase_jump_back_s(self._cc_lag)
            target = max(0.0, min(self._cap_edge_s() - back,
                                  self._frontier_s() + 120.0))
        else:
            target = self._safe_seek_target(target_s)
        vlc_t = 0.0 if target <= 0.0 else self._cap_vlc_time_for(target)
        try:
            log.info("chase seek: target=%.1fs safe=%.1fs vlc=%.1fs "
                     "frontier=%.1fs edge=%.1fs div=%s%s",
                     target_s, target, vlc_t, self._frontier_s(),
                     self._cap_edge_s(),
                     "-" if not self._cap_div_ok
                     else "%.1f" % self._cap_div_s,
                     " jump_live" if jump_live else "")
        except Exception:
            pass
        self._sync_transport("chase_seek" if not jump_live else "jump_edge",
                             target_s,
                             extra="safe=%.2f vlc=%.2f resume=%s"
                             % (target, vlc_t, resume))
        try:
            down = self.vlc.state_name() in ("ended", "stopped", "error")
        except Exception:  # noqa: BLE001
            down = False
        if not down:
            raw_pre = self._sync_raw_s()
            self.vlc.set_time(int(vlc_t * 1000))
            self._vid_s = target
            # the set_time target defines the caption clock BY CONSTRUCTION
            # (and reseeds the live-edge backlog)
            self._cap_seed_transport(target, jump_live=jump_live)
            self._arm_seek_verify(target, vlc_t, raw_pre)
            if resume and self._chase_paused:
                self._chase_paused = False
                self.vlc.resume()
            return
        self._chase_revive(target, vlc_t,
                           "chase_seek" if not jump_live else "jump_live")

    def _arm_seek_verify(self, target: float, vlc_t: float,
                         raw_pre: float):
        """(a) Arm the set_time verification: raw must reach the target
        (content axis) by the deadline — target-proportional, since VLC
        may legally take >1.5 s on a big demux jump. Last-wins: a newer
        seek supersedes a pending verify (user intent = latest)."""
        if not (self._mode == "chase" and self.dvr):
            self._seek_verify = None
            return
        jump = abs(vlc_t - (raw_pre if raw_pre >= 0.0 else vlc_t))
        now = now_s()
        deadline = now + min(_SEEK_VERIFY_MAX_S,
                             _SEEK_VERIFY_BASE_S
                             + _SEEK_VERIFY_PROP_S * jump)
        self._seek_verify = (float(target), float(vlc_t), deadline, now)

    def _verify_seek(self, now: float, raw: float):
        """(a) Verify the armed set_time landed; escalate to the play_at
        revive on a confirmed no-op. Escalations are backoff-gated (a
        flaky verify must never reopen-loop) and share the reopen
        cooldown. Clean verifies decay the strike ladder."""
        v = self._seek_verify
        if v is None:
            if self._seek_esc_strikes \
                    and now - self._seek_esc_clean > _SEEK_ESC_DECAY_S:
                self._seek_esc_strikes = 0
            return
        target, vlc_t, deadline, armed_at = v
        if raw >= 0.0 and abs(self._cap_content_for_raw(raw)
                              - target) <= _SEEK_VERIFY_TOL_S:
            self._seek_verify = None
            self._seek_esc_clean = now
            return
        if now < deadline or self._chase_paused:
            return
        self._seek_verify = None
        if now < self._seek_esc_ok_at \
                or now - self._last_reopen < _REOPEN_COOLDOWN_S:
            return
        n = min(self._seek_esc_strikes, len(_SEEK_ESC_BACKOFF_S) - 1)
        self._seek_esc_strikes += 1
        self._seek_esc_ok_at = now + _SEEK_ESC_BACKOFF_S[n]
        self._chase_revive(target, vlc_t, "seek-escalate")
        if _SYNC_ON:
            synclog.info("SEEKESC target=%.2f vlc=%.2f raw=%.2f "
                         "waited=%.2f strike=%d",
                         target, vlc_t, raw, now - armed_at,
                         self._seek_esc_strikes)

    def _chase_revive(self, target: float, vlc_t: float, why: str):
        """Reopen the buffer AT a target position on a wedged/down player
        — set_time is a no-op in those states. Local file operation, so
        the single provider connection is never touched. Shared by
        _chase_seek's down-revive, the seek-verify escalation, and
        available to the wedge rescue. A revive always resumes: the
        intent is to get playback going again."""
        buf = self.dvr.buffer_file() if self.dvr else None
        if not buf:
            return False
        try:
            log.warning("chase revive (%s): player wedged/down — "
                        "play_at %.1fs", why, target)
        except Exception:
            pass
        self._chase_paused = False
        self._chase_started = False   # re-armed by the first playing tick
        self._stall_ticks = 0
        self._last_reopen = now_s()
        self._vid_s = target
        self._seek_verify = None
        self._cap_seed_transport(target)
        self.vlc.play_at(buf, vlc_t)
        self._poke_audio()
        self._poke_rate()
        return True

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
        if self._closing or (self._mode != "chase" and not self._is_vod()):
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
            edge = self._cap_edge_s()
            try:
                log.info("jump to live edge: target=%.1fs frontier=%.1fs "
                         "(L=%s)", max(0.0, edge - _chase_jump_back_s(
                             self._cc_lag)),
                         self._frontier_s(),
                         "-" if self._cc_lag is None
                         else "%.1f" % self._cc_lag)
            except Exception:
                pass
            self._sync_transport("jump_live", edge)
            self._chase_seek(edge, resume=True, jump_live=True)
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
                # (_chase_seek clamps and revives a stalled player); the
                # slider speaks VLC's raw-axis numbers — convert to the
                # content axis before clamping
                self._chase_seek(
                    self._cap_content_for_raw(self.slider.value() / 1000.0))
            elif self._is_catchup():
                # indexless TS: the byte-fraction axis is the reliable one
                self._catchup_seek_to(self.slider.value())
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
        self._dvr_t0 = now_s()
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
            self._last_reopen = now_s()
            # a (re)open AT a position is a transport event: it seeds the
            # caption clock and the live-edge backlog by construction
            self._cap_seed_transport(target)
            self.vlc.play_at(buf, self._cap_vlc_time_for(target))
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
            if _SYNC_ON:
                synclog.info("REBASE base=%.2f run_credit=%.1f run_capped=%.1f",
                             self._dvr_base, self._sync_credit_s,
                             self._sync_capped_s)
                self._sync_credit_s = 0.0
                self._sync_capped_s = 0.0
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
        waited = 0.0 if self._dvr_t0 is None else (now_s() - self._dvr_t0)
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
        self._set_rate(self._rate)    # sticky speed applies to this stream too
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
        # revive where the viewer actually WAS. The caption clock is the
        # display truth when it is LIVE (it holds through VLC freezes via
        # its stall detection), but a caption pipeline that died with the
        # old channel leaves it FROZEN at a stale position — reopening
        # there every cooldown was the "short permanent loop" after a
        # play-next channel switch (same few seconds repeating until the
        # user changed channels). Take the FURTHEST of the two clocks:
        # _vid_s always advanced while frames played, and a stale caption
        # clock can then never drag playback backwards.
        at = max(self._cap_clock_s, self._vid_s)
        # Loop-breaker: repeated rescues at (nearly) the same anchor mean
        # the player wedges the instant it lands there — after 3 in a row,
        # jump to fresh data near the live edge instead of reopening at
        # the same spot forever.
        noww = now_s()
        if at <= getattr(self, "_reopen_last_at", -1e9) + 2.0 \
                and noww - getattr(self, "_reopen_last_t", 0.0) < 60.0:
            self._reopen_repeats = getattr(self, "_reopen_repeats", 0) + 1
        else:
            self._reopen_repeats = 0
        self._reopen_last_at = at
        self._reopen_last_t = noww
        if self._reopen_repeats >= 3:
            self._reopen_repeats = 0
            at = self._safe_seek_target(self._frontier_s()
                                        - self.config.chase_delay)
            try:
                log.warning("chase reopen: same-anchor loop broken — "
                            "jumping near live edge (%.1fs)", at)
            except Exception:
                pass
        target = self._safe_seek_target(at)
        try:
            log.warning("chase reopen: play_at %.1fs (frontier=%.1fs "
                        "was_at=%.1fs)", target, self._frontier_s(),
                        self._vid_s)
        except Exception:
            pass
        self._chase_paused = False
        self._chase_started = False
        self._set_rate(1.0)
        self._seek_verify = None   # the reopen supersedes any pending verify
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
                try:
                    from .. import feedback
                    feedback.stat("recording_failures")
                    feedback.crumb("record failed: no folder/stream")
                except Exception:
                    pass
                log.error("recording failed to start: %s",
                          "no record folder chosen"
                          if not folder else "nothing playable")
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
            try:
                from .. import feedback
                feedback.usage("record")
                feedback.crumb("record -> %s" % os.path.basename(
                    self._rec_path))
            except Exception:
                pass
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
    def _dl_folder(self):
        """Where downloads land (Settings > Download folder).  Asks once
        and remembers when unset."""
        folder = self.config.download_folder
        if not folder or not os.path.isdir(folder):
            folder = QtWidgets.QFileDialog.getExistingDirectory(
                self, "Choose where downloads are saved")
            if folder:
                self.config.download_folder = folder
                self.config.save()
        return folder or ""

    def _start_download(self):
        """Save the original movie/episode file to the downloads folder.
        Unlike REC (which re-records the decode), this copies the provider's
        bytes verbatim in a background thread."""
        if self._downloading or not self._is_vod():
            return
        if not (self.current and self.current.get("url")):
            return
        folder = self._dl_folder()
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
        self.btn_win.setEnabled(self._is_catchup())
        self.btn_win.setIcon(ic.download_window())
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
                and self._dvr_status.text().startswith(
                    ("Download", "Enable", "Stream length"))):
            self._set_dvr_status(None)

    # ---- catch-up download window (gold < > markers on the scrubber) ----
    def _on_win_btn(self):
        """The window button: FIRST press drops the two gold < > markers on
        the time bar, SECOND press confirms and downloads the selected
        stretch (Esc cancels)."""
        if self._closing:
            return
        if self._win_sel:
            self._win_confirm()
        elif self._is_catchup():
            self._win_engage()

    def _win_engage(self):
        if not self.config.control_buttons.get("timebar", True):
            # the time bar setting is off — there is nothing to mark
            self._set_dvr_status(
                "Enable the time bar (Settings \u25b8 Playback controls) "
                "to pick a download window")
            QtCore.QTimer.singleShot(4000, self._hide_dl_pill)
            return
        self._set_scrub_visible(True)
        if self.slider.maximum() <= 0:
            self._set_dvr_status("Stream length unknown yet \u2014 try again "
                                 "in a moment")
            QtCore.QTimer.singleShot(4000, self._hide_dl_pill)
            return
        self._win_sel = True
        self.btn_win.setChecked(True)
        self.btn_win.setIcon(ic.download_window(ic.GOLD))
        self.btn_win.setToolTip("Confirm download window (Esc cancels)")
        # < lands at the current position, > at the end of the recording:
        # drag/nudge < back to 0 for the whole program
        end = self.slider.maximum()
        start = int(max(0.0, min(self._vid_s * 1000.0, end - _WIN_GAP_MS)))
        self._win_start_ms = start
        self._win_end_ms = end
        self.slider._win_mode = True
        for m in self._win_markers.values():
            m.show()
        self._win_reposition()
        self._win_select("start")
        self._wake()

    def _win_cancel(self, silent: bool = False):
        """Leave window-select mode (cancel / channel change / teardown)."""
        if not self._win_sel:
            return
        self._win_sel = False
        self._win_sel_side = None
        self.slider._win_mode = False
        for m in self._win_markers.values():
            m.selected = False
            m.update()
            m.hide()
        self.btn_win.blockSignals(True)
        self.btn_win.setChecked(False)
        self.btn_win.blockSignals(False)
        if not self._downloading:
            # a download in flight keeps the gold "active" icon
            self.btn_win.setIcon(ic.download_window())
        self.btn_win.setToolTip("Download a time window of this program")
        if not silent:
            self._set_dvr_status(None)
            self._wake()

    def _win_cancel_if_active(self) -> bool:
        """Esc: eat the key only when a window selection was live."""
        if self._win_sel:
            self._win_cancel()
            return True
        return False

    def _win_confirm(self):
        a, b = int(self._win_start_ms), int(self._win_end_ms)
        self._win_cancel(silent=True)
        if b - a < _WIN_GAP_MS:
            return
        self._start_window_download(a, b)

    def _win_select(self, side):
        """One marker selected at a time — the arrow keys nudge that one."""
        if not self._win_sel:
            return
        self._win_sel_side = side
        for s, m in self._win_markers.items():
            m.selected = (s == side)
            m.update()
        self._win_update_pill()
        self._wake()

    def _win_nudge(self, ms):
        if not self._win_sel:
            return
        if self._win_sel_side == "end":
            self._win_end_ms = int(max(self._win_start_ms + _WIN_GAP_MS,
                                       min(self.slider.maximum(),
                                           self._win_end_ms + ms)))
        else:
            self._win_start_ms = int(max(0, min(
                self._win_end_ms - _WIN_GAP_MS, self._win_start_ms + ms)))
        self._win_reposition()
        self._win_update_pill()
        self._wake()

    def _win_drag(self, side, x):
        """Marker drag: x is slider-local pixels (WinMarker reports it)."""
        if not self._win_sel:
            return
        self._win_sel_side = side
        val = self._value_for_x(x)
        if side == "end":
            self._win_end_ms = int(max(self._win_start_ms + _WIN_GAP_MS,
                                       min(self.slider.maximum(), val)))
        else:
            self._win_start_ms = int(max(0, min(
                self._win_end_ms - _WIN_GAP_MS, val)))
        for s, m in self._win_markers.items():
            m.selected = (s == side)
        self._win_reposition()
        self._win_update_pill()

    def _on_win_slider_click(self, val):
        """Click on the time bar while selecting: the NEAREST marker jumps
        there and becomes the selected one."""
        if not self._win_sel:
            return
        if abs(val - self._win_start_ms) <= abs(val - self._win_end_ms):
            self._win_start_ms = int(max(
                0, min(self._win_end_ms - _WIN_GAP_MS, val)))
            self._win_select("start")
        else:
            self._win_end_ms = int(max(
                self._win_start_ms + _WIN_GAP_MS,
                min(self.slider.maximum(), val)))
            self._win_select("end")
        self._win_reposition()

    def _win_update_pill(self):
        a, b = self._win_start_ms, self._win_end_ms
        glyph = "<" if self._win_sel_side == "start" else ">"
        self._set_dvr_status(
            f"Download window {glyph}  {_fmt(a)} \u2013 {_fmt(b)} "
            f"({_fmt(b - a)})  \u2014  drag or click the gold markers, "
            f"\u2190/\u2192 nudge the selected one; press the gold window "
            f"button again to download, Esc cancels")

    def _win_reposition(self):
        if not self._win_sel:
            return
        h = max(self.slider.height(), 20)
        for side, m in self._win_markers.items():
            v = self._win_start_ms if side == "start" else self._win_end_ms
            x = int(round(self._x_for_value(v)))
            m.setGeometry(x - m.width() // 2, 0, m.width(), h)
            m.raise_()

    # pixel <-> ms mapping, mirroring JumpSlider's own click math
    def _slider_metrics(self):
        st = self.slider.style()
        handle = st.pixelMetric(QtWidgets.QStyle.PM_SliderLength,
                                None, self.slider)
        return handle, max(1, self.slider.width() - handle)

    def _x_for_value(self, v) -> float:
        lo = self.slider.minimum()
        hi = max(1, self.slider.maximum())
        handle, span = self._slider_metrics()
        frac = min(1.0, max(0.0, (v - lo) / float(hi - lo)))
        return handle // 2 + frac * span

    def _value_for_x(self, x) -> int:
        lo = self.slider.minimum()
        hi = max(1, self.slider.maximum())
        handle, span = self._slider_metrics()
        frac = min(1.0, max(0.0, (x - handle // 2) / float(span)))
        return int(lo + round((hi - lo) * frac))

    def seek_or_nudge(self, sec, nudge_s=None):
        """Arrow keys: with the download-window markers active they nudge
        the selected gold marker (1 s steps by default, coarser via
        Shift/Ctrl); otherwise they seek playback by ``sec`` seconds."""
        if self._win_sel:
            step = nudge_s if nudge_s is not None else max(1, min(10, abs(sec)))
            self._win_nudge(int(step * 1000) * (1 if sec >= 0 else -1))
        else:
            self._seek_ms(int(sec * 1000))

    def _start_window_download(self, a_ms: int, b_ms: int):
        """Download the selected [a, b) window of the catch-up recording.
        The provider's timeshift endpoint honors arbitrary start times and
        durations, so the window is simply a SECOND timeshift URL —
        verbatim bytes, no re-encode, playback untouched."""
        cur = self.current or {}
        if self._downloading:
            return
        if not (self.client and cur.get("stream_id") is not None
                and cur.get("utc_start") is not None):
            self._set_dvr_status("Window download unavailable for this "
                                 "stream")
            QtCore.QTimer.singleShot(4000, self._hide_dl_pill)
            return
        folder = self._dl_folder()
        if not folder:
            return
        utc_a = int(cur["utc_start"]) + a_ms // 1000
        dur_min = max(1, math.ceil((b_ms - a_ms) / 60000.0))
        url = self.client.timeshift_url(cur["stream_id"], utc_a, dur_min)
        t0 = datetime.fromtimestamp(utc_a)
        t1 = datetime.fromtimestamp(utc_a + (b_ms - a_ms) // 1000)
        safe = re.sub(r"[^\w\-.]+", "_",
                      (cur.get("title") or "catchup")).strip("._")[:60]
        safe = safe or "catchup"
        path = os.path.join(
            folder, f"{safe}_{t0.strftime('%H%M')}-{t1.strftime('%H%M')}.ts")
        n = 1
        while os.path.exists(path):        # never clobber an earlier download
            path = os.path.join(
                folder,
                f"{safe}_{t0.strftime('%H%M')}-{t1.strftime('%H%M')} ({n}).ts")
            n += 1
        self._downloading = True
        self.btn_dl.setEnabled(False)
        self.btn_win.setEnabled(False)
        # gold while the download runs — pinned into the icon's disabled
        # mode so the state survives the button being greyed out
        self.btn_win.setIcon(
            ic.download_window(ic.GOLD, keep_disabled=True))
        self._set_dvr_status("Downloading window\u2026")
        self._dl = FileDownloader(self)
        self._dl.progress.connect(self._on_dl_progress)
        self._dl.finished.connect(self._on_dl_finished)
        self._dl.start(url, path)
        try:
            log.info("window download start: %s -> %s", url, path)
        except Exception:
            pass

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
        if obj is self.slider and et == QtCore.QEvent.Resize \
                and self._win_sel:
            # the bar resized (window resize / compact fit): the gold
            # markers follow their millisecond positions
            self._win_reposition()
        elif et in (QtCore.QEvent.MouseMove, QtCore.QEvent.HoverMove,
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
                # _on_focus_changed): keep the overlay hidden — showing an
                # owned Qt.Tool window while another app owns the
                # foreground could raise it over that app's windows for a
                # frame, and a cursor passing over the app's exposed area
                # behind them must not surface the controls over e.g.
                # Chrome.
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
            # fresh-from-hidden: flush any stale backing-store pixels the
            # vout swap of a stream switch left behind (ghost buttons)
            self.overlay.repaint()
        # now-playing banner: after its first control-cycle it only comes
        # back when the cursor is dragged to the VERY TOP of the video
        try:
            pos = QtGui.QCursor.pos()
            local = self.surface.mapFromGlobal(pos)
            top_band = max(28, self.surface.height() // 12)
            if (self.current and 0 <= local.x() < self.surface.width()
                    and 0 <= local.y() <= top_band
                    and not self.info_overlay.isVisible()):
                self._resurface_info()
        except Exception:  # noqa: BLE001
            pass
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
        if self._win_sel:
            # mid download-window selection: the markers + pill must stay
            # on screen — hiding the bar would strand the interaction
            self.hide_timer.start()
            return
        self.ctl.hide()
        self._btn_showpanel.hide()
        # the sticky now-playing banner leaves with the controls
        if self._info_sticky:
            self.info_overlay.hide()
            self._info_sticky = False
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
            # slider handles — e.g. a doubled play-next glyph right after a
            # play-next channel switch, or leftover control plates after a
            # series-episode -> movie transition). Repaint UNCONDITIONALLY:
            # when the overlay is hidden at that instant (controls asleep
            # mid-switch) the stale pixels simply live in its backing store
            # until the next _wake() paints them over the video — the
            # isVisible() guard was exactly how those ghosts survived.
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
        # Audio tracks: same story (defaults re-applied by VLC on media
        # opens / ES updates, lists arriving late) — plus the English
        # default for streams that carry more than one language.
        self._enforce_audio()
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
            now = now_s()
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
            # stall for 5-15 s at a time. The cap allows the TRUE-edge
            # landing zone past the frontier (the buffer really holds that
            # cold-burst content; see _safe_seek_target).
            pos_cap = frontier + 60.0
            sane = 0.0 <= raw <= frontier + 60.0
            # meaningful-movement clock for the stuck-player rescue: a
            # wedged player's get_time can oscillate by 1 ms — that must
            # NOT count as movement (it kept the rescue disarmed for
            # minutes on a frozen true-edge landing). The threshold is
            # RATE-AWARE: at 0.125x slow-mo raw advances ~0.05 s per tick,
            # which a flat 0.05 read as frozen (and the rescue would have
            # reopened legit slow-mo every cooldown).
            move_min = max(0.02, _RAW_MOVE_FRAC * dt * self._rate)
            if sane and self._last_raw is not None \
                    and abs(raw - self._last_raw) > move_min:
                self._raw_change_wall = now
            if (sane and raw != self._last_raw
                    and abs(raw - self._vid_s) <= 3.0):
                self._vid_s = min(raw, pos_cap)
            elif playing and not self._chase_paused and not self._seeking \
                    and not self._trickle_hold:
                self._vid_s = min(pos_cap, self._vid_s + dt * self._rate)
            self._last_raw = raw
            # (c) freeze-aware clock: keep the rolling raw-vs-wall window
            # fed (in _tick, which always runs in chase — captions off
            # must not disable it)
            self._raw_win.append((now, raw if sane else None))
            while self._raw_win and now - self._raw_win[0][0] \
                    > _CC_TRICKLE_WIN_S:
                del self._raw_win[0]
            self._trickle_hold = self._trickle_test(now, playing)
            # Advance the content-axis clock HERE, unconditionally: it is
            # not just caption timing anymore — _seek_ms's rewind base,
            # _jump_live's edge and _safe_seek_target's clamp all lean on
            # it. It used to run only from _caption_tick/_filter_tick, so
            # with captions off (and the profanity filter windowless) the
            # clock stayed frozen at its last transport seed: a Rewind 60
            # computed base=seed-60 → clamped to 0 → "jumps to the
            # beginning", and every rescue reopen was clamped back there
            # by the frozen edge → the short loop (2026-08-27 report).
            # The integrator is dt-keyed on _cap_wall, so an extra cadence
            # alongside the 100 ms caption/filter ticks double-counts
            # nothing.
            self._caption_clock_s()
            current = self._vid_s
            # ---- stuck-player rescue (merged watchdogs) ----
            # VLC at the ragged edge of the growing buffer file can end up
            # ENDED (state) or demuxer-blocked while still "playing"; in
            # both states set_time is a NO-OP and only reopening the
            # buffer revives it. Two signatures, one rescue:
            #   a) not playing for 3+ ticks (classic end-of-file stop)
            #   b) raw frozen ~8+ s (jitter-proofed: sub-frame oscillation
            #      must not refresh the timer) while REAL content exists
            #      ahead — measured from the PCR head, because the frontier
            #      is negative for a viewer past the under-credited frontier
            #      (the 2026-08-21 wedge: zero rescues all night) and
            #      over-credits slow trickles. Falls back to the legacy
            #      frontier test while the PCR pins are unavailable.
            if (not playing and not self._chase_paused
                    and self._chase_started):
                self._stall_ticks += 1
            else:
                self._stall_ticks = 0
            raw_frozen = (self._raw_change_wall > 0.0
                          and now - self._raw_change_wall > _WEDGE_FREEZE_S)
            head_ahead = None
            if raw_frozen and playing:
                if self._cc_head_pcr is None \
                        or now - self._cc_head_pcr[1] > 2.0:
                    self._cc_probe_head_pcr()   # ~20 ms, on suspicion only
                head_ahead = self._head_ahead_s(current)
            if (not self._chase_paused
                    and now - self._last_reopen > _REOPEN_COOLDOWN_S
                    and self.dvr.buffer_file()
                    and ((self._stall_ticks >= 3 and self._chase_started
                          and not playing and raw >= 0)
                         or (raw_frozen and playing and head_ahead is not None
                             and head_ahead > _WEDGE_DATA_AHEAD_S))):
                self._last_reopen = now
                try:
                    log.warning("chase rescue: playing=%s raw=%.1f "
                                "frozen=%.1fs frontier=%.1fs "
                                "head_ahead=%s — reopening",
                                playing, raw,
                                now - self._raw_change_wall
                                if self._raw_change_wall else 0.0,
                                frontier,
                                "-" if head_ahead is None
                                else "%.1f" % head_ahead)
                except Exception:
                    pass
                self._stall_ticks = 0
                self._reopen_chase(gen)
                return
            # (a) verify the last set_time landed; escalate on a no-op
            self._verify_seek(now, raw)
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
            # The threshold is SPEED-AWARE (at 4x the gap shrinks fast, so
            # a fixed threshold let VLC hit the file end before the next
            # tick could reset the rate), and measured against the TRUE
            # edge: with D1's adaptive landing (and on cold-burst nights)
            # the viewer legitimately sits past the under-credited
            # frontier, where the old frontier gap was meaningless.
            if (self._rate > 1.0 and playing and not self._chase_paused
                    and self._cap_edge_s() - current
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
        now = now_s()
        self._tick_t = now
        length = self.vlc.get_length()
        raw = self.vlc.get_time()
        if length <= 0 and self._is_catchup():
            # VLC cannot size these indexless TS streams; the program
            # window IS the length (the scrubber + markers rely on it)
            length = self._catchup_dur_ms()
        vod = length > 0
        if vod != self._scrub_on:
            self._scrub_on = vod
            for b in (self.btn_back60, self.btn_back10, self.btn_fwd10,
                      self.btn_begin):
                b.setEnabled(vod)
            self.btn_win.setEnabled(
                vod and self._is_catchup() and not self._downloading)
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
            raw_moved = (raw_s >= 0.0 and self._last_raw is not None
                         and abs(raw_s - self._last_raw) > 0.02)
            if (0.0 <= raw_s <= length / 1000.0 + 5.0
                    and raw_s != self._last_raw
                    and abs(raw_s - self._vid_s) <= 3.0):
                self._vid_s = min(raw_s, length / 1000.0)
            elif playing and not self._live_paused and not self._seeking:
                self._vid_s = min(length / 1000.0, self._vid_s + dt)
            self._last_raw = raw_s
            if not self._seeking:
                self._set_scrub(int(length), int(self._vid_s * 1000))
            if self._is_catchup():
                self._catchup_watchdog(now, playing, raw_s,
                                       length / 1000.0, raw_moved)
            elif self.current.get("kind") in ("series", "movie", "vod"):
                self._vod_stall_watchdog(now, playing, raw_s,
                                         length / 1000.0, raw_moved)
            self._maybe_autoplay_next(playing, length, raw)
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
            # the marker pixel<->ms mapping changed with the range
            if self._win_sel:
                self._win_reposition()
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
        # Pre-stream (no media yet) the transport group stays enabled: the
        # speed pick is sticky and applies to whatever stream starts next.
        live_prestream = self.current is None
        for b in (self.btn_back60, self.btn_back10, self.btn_fwd10,
                  self.btn_begin, self.btn_speed):
            b.setEnabled(chase or vod or live_prestream)
        self.btn_live.setEnabled(chase or vod or bool(self.current))
        # play/pause + audio need SOMETHING loaded; REC needs the DVR
        # recorder of a live chase stream (VOD/catch-up swap the slot to
        # Download/Window) — never disable it mid-recording
        self.btn_play.setEnabled(bool(self.current))
        self.btn_audio.setEnabled(bool(self.current))
        kind = (self.current or {}).get("kind")
        # autoplay is a sticky preference, not a stream action: selectable
        # (and hoverable) from app open, before anything plays — flipping
        # it pre-stream just sets what the next series/catch-up will do.
        self.btn_auto.setEnabled(True)
        self.btn_next.setEnabled(kind in ("live", "series", "catchup"))
        self.btn_rec.setEnabled(chase or self.btn_rec.isChecked())
        self.btn_dl.setEnabled(vod and not self._downloading)
        self.btn_win.setEnabled(self._is_catchup() and not self._downloading)
        self._scrub_on = chase
        self._set_scrub_visible(chase)
        if self.current is not None and not chase and not vod:
            self._set_rate(1.0)
        self._apply_button_visibility()
        self._apply_scale()
        self._poke_audio()
        self._refresh_spu_button()
        self._refresh_audio_button()

    # ---- per-button visibility (Settings ▸ Playback controls…) ----
    def apply_button_visibility(self):
        self._apply_button_visibility()

    def _apply_button_visibility(self):
        vis = self.config.control_buttons
        compact = self._compact_hidden
        vod = self._is_vod()
        catchup = self._is_catchup()
        widgets = {
            "back60": self.btn_back60, "back10": self.btn_back10,
            "play": self.btn_play, "fwd10": self.btn_fwd10,
            "begin": self.btn_begin,
            "live": self.btn_live, "rec": self.btn_rec,
            "cc": self.btn_cc, "audio": self.btn_audio,
            "scale": self.btn_scale,
            "speed": self.btn_speed, "mute": self.btn_mute,
            "volume": self.vol_slider,
            "autoplay": self.btn_auto, "playnext": self.btn_next,
        }
        kind = (self.current or {}).get("kind")
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
                # without touching playback. Catch-up programs get the
                # WINDOW download button instead (pick a start/end stretch
                # of the recording). Evaluated end-to-end and deliberately
                # left as the swap.
                w.setVisible(on and not vod)
                self.btn_dl.setVisible(on and vod and not catchup)
                self.btn_win.setVisible(on and catchup)
            elif key == "autoplay":
                # series episodes + catch-up programs only (never movies,
                # never live TV — there is no "next" to roll into)
                w.setVisible(on and kind in ("series", "catchup"))
            elif key == "playnext":
                # "play next channel" on live TV, "play next episode" on
                # series / catch-up; hidden for movies and pre-stream
                w.setVisible(on and kind in ("live", "series", "catchup"))
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
        # button show/hide mid-stream-switch (series -> movie swaps the
        # rec slot etc.) re-composes the layered overlay window; a
        # synchronous repaint keeps no stale glyph pixels behind. Skipped
        # during _fit_ctl's compaction ladder (it re-runs this per level).
        if self.overlay.isVisible() and not self._in_fit_ctl:
            self.overlay.repaint()

    # ---- control-bar popup cards (speed / scale / audio / subtitles) ----
    def _open_ctl_panel(self, btn, header, rows, on_pick, refresh=None):
        """Toggle-aware card opener for the control-bar buttons: the SAME
        button closes its card; a different button swaps the content (the
        click-outside closer already guarantees one card at a time).

        Fully exception-trapped with logging: these run inside clicked
        slots, where PyQt swallows a raise into a stderr that does not
        exist in the windowed exe — without this trap a failing open is
        a button that silently does nothing and leaves no log line."""
        try:
            if self._closing:
                return
            if self._ctl_panel.isVisible():
                same_button = self._ctl_panel_btn is btn
                self._ctl_panel.close_panel(
                    "toggle" if same_button else "swap")
                if same_button:
                    return          # plain toggle close
                # (close_panel's closed signal already cleared the opener
                # refs — fall through and re-open for the NEW button)
            self._ctl_panel_btn = btn
            self._ctl_panel_pick = on_pick
            self._ctl_panel_refresh = refresh
            self._ctl_panel.set_rows(rows, header=header)
            self._ctl_panel.popup(btn)
            self._popup_open = True      # keep the controls on while picking
            if refresh:
                self._ctl_panel_timer.start()
            try:
                log.info("ctl panel: open %s (%d rows)", header, len(rows))
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            try:
                log.exception("ctl panel: open failed (%s)", header)
            except Exception:  # noqa: BLE001
                pass
            # still unwind to a sane state: a half-open card or a running
            # refresh timer must never wedge the control bar
            try:
                self._ctl_panel_timer.stop()
                self._ctl_panel.close_panel("open-error")
            except Exception:  # noqa: BLE001
                pass

    def _on_ctl_panel_picked(self, row):
        cb = self._ctl_panel_pick
        if cb is None:
            return
        try:
            cb(row)
        except Exception:  # noqa: BLE001
            try:
                log.exception("ctl panel: pick failed (%r)", row.get("id"))
            except Exception:  # noqa: BLE001
                pass

    def _on_ctl_panel_tick(self):
        if self._ctl_panel_refresh is not None:
            try:
                self._ctl_panel_refresh()
            except Exception:  # noqa: BLE001
                # the 1 s refresh timer is a slot too — same silent-death
                # hole as the openers; log and stop rather than spam
                try:
                    log.exception("ctl panel: refresh failed — timer stopped")
                except Exception:  # noqa: BLE001
                    pass
                self._ctl_panel_timer.stop()

    def _ctl_panel_closed(self):
        """Card hid (pick / click outside / Escape / toggle): resume the
        normal control auto-hide cycle. (_ctl_panel_pick deliberately
        survives — a row pick closes the card BEFORE its callback runs,
        e.g. Subtitle settings opens its modal with the card already
        gone.)"""
        self._ctl_panel_timer.stop()
        self._ctl_panel_btn = None
        self._ctl_panel_refresh = None
        self._popup_open = False
        if not self._closing:
            self._wake()

    def _speed_menu(self):
        if not self.btn_speed.isEnabled():
            return
        rows = [{"id": s, "main": f"{s:g}\u00d7",
                 "checked": abs(s - self._rate) < 1e-9}
                for s in _SPEEDS]
        self._open_ctl_panel(
            self.btn_speed, "SPEED", rows,
            lambda row: self._set_rate(row["id"]))

    def _set_rate(self, rate):
        rate = max(0.125, min(5.0, float(rate)))
        if self.current is not None and self._mode != "chase" \
                and not self._is_vod():
            rate = 1.0   # plain-live fallback has no speed control; a
            #              PRE-stream pick (current is None) is kept and
            #              applied when the next stream starts
        self._sync_transport("rate", None, extra="rate=%g" % rate)
        self._rate = rate
        try:
            self.vlc.set_rate(rate)
        except Exception:  # noqa: BLE001
            pass
        self.btn_speed.setToolTip(
            f"Playback speed — {rate:g}\u00d7 (live rewind, movies & series)")

    def _scale_menu(self):
        rows = [{"id": mode, "main": label,
                 "checked": self._scale_mode == mode}
                for mode, label in (("fit", "Fit (letterbox)"),
                                    ("stretch", "Stretch to fill"),
                                    ("crop", "Crop to fill"))]
        self._open_ctl_panel(
            self.btn_scale, "VIDEO", rows,
            lambda row: self._set_scale_mode(row["id"]))

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
        is dormant (dimmed, unclickable) until a stream starts, then live
        for the rest of the session."""
        try:
            tracks = self.vlc.spu_tracks()
        except Exception:  # noqa: BLE001
            tracks = []
        enabled = bool(tracks) or self._cap_on
        on = self._spu_want != -1 or self._cap_on
        streaming = self.current is not None
        state = (enabled, on, streaming, self._spu_name if on else "")
        if state == self._spu_ui:
            return
        self._spu_ui = state
        self.btn_cc.setEnabled(streaming)
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
            log.info("subtitles: select id=%d name=%r", track_id,
                     self._spu_name or "Off")
        except Exception:  # noqa: BLE001
            pass
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

        Chase mode: a DEAD-RECKONED clock on ONE app-owned content axis.
        Every transport event (_chase_seek's set_time target) defines the
        clock BY CONSTRUCTION; between transports it advances wall x rate
        while playing and holds while paused/stalled. Raw get_time() is
        never snapped to — its absolute numbers lie (broadcast PTS
        renumbering, normalization glitches) — but its DELTAS are folded
        in when they agree frames played ~1:1 with wall (outlier-rejected
        nudge), keeping the clock locked to VLC's real timeline. Only a
        loose sanity bound applies.

        VOD: the relay's cue times are the file's cluster timecodes — the
        same axis as a healthy get_time(), used raw. None of the chase
        logic runs here.
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
        # chase: dead-reckoned, delta-locked (garbage-absolute-proof)
        now = now_s()
        frontier = self._frontier_s()
        dt = 0.0 if self._cap_wall <= 0.0 \
            else max(0.0, min(2.0, now - self._cap_wall))
        try:
            playing = self.vlc.is_playing()
        except Exception:  # noqa: BLE001
            playing = True
        playing = playing and not self._chase_paused
        prev_clock = self._cap_clock_s
        branch = reject = ""
        # startup fallback: no transport seed and no reading yet — follow
        # the UI-tracked position until one shows up (captured BEFORE the
        # integration below, which would otherwise lift the zero clock
        # past the <= 0 check)
        no_clock_yet = self._cap_raw_s is None and prev_clock <= 0.0

        # a plausible reading frozen for many seconds while "playing" is
        # a buffer underrun at the edge, not playback: the frames have
        # stopped even though VLC still reports playing
        frozen_for = 0.0
        if raw >= 0.0 and self._cap_raw_s == raw and self._cap_raw_wall > 0.0:
            frozen_for = now - self._cap_raw_wall
        stalled = playing and frozen_for > _CC_STALL_FREEZE_S
        # (c) freeze-aware: sub-stall freeze/thaw cycles (0.2x-delivery
        # nights) never trip the continuous-freeze branch — hold the clock
        # on the tick's rolling-window verdict instead (see _trickle_test)
        trickling = playing and self._trickle_hold

        # wall dead-reckoning: frames play 1:1 with wall time
        wall_adv = dt * self._rate \
            if (playing and not stalled and not trickling) else 0.0
        fold = 0.0
        raw_changed = False
        if raw >= 0.0 and raw != self._cap_raw_s:
            raw_changed = True
            if self._cap_raw_s is not None and self._cap_raw_s >= 0.0 \
                    and self._cap_raw_wall > 0.0:
                d_raw = raw - self._cap_raw_s
                d_wall = max(0.0, now - self._cap_raw_wall)
                # expectation = how far the CLOCK believes frames played
                # since raw last moved (its integrations across the span,
                # plus this tick's pending one). The old wall-based
                # expectation made every thaw after a held stretch (stall,
                # trickle, pause) read as a renumber.
                if self._cap_raw_clock is not None:
                    expected = (prev_clock - self._cap_raw_clock) + wall_adv
                else:
                    expected = d_wall * self._rate if playing else 0.0
                residual = d_raw - expected
                if abs(residual) <= _CC_SYNC_TOL_S:
                    # raw advanced ~rate x wall: frames really played —
                    # fold the small residual in (drift/jitter correction)
                    fold = residual
                    branch = "fold"
                else:
                    # PTS renumbering / garbage jump: frames keep playing
                    # 1:1, the clock stays — remember the axis divergence
                    # so transport targets convert to VLC's numbers
                    self._cap_div_s = raw - (prev_clock + wall_adv)
                    self._cap_div_ok = True
                    branch = "renum"
                    reject = "jump(%+.2f)" % residual
            else:
                branch = "base"       # first reading after a seed
            self._cap_raw_s = raw
            self._cap_raw_wall = now
        elif raw < 0.0:
            branch = "noraw"
        elif not playing:
            branch = "hold"
        elif stalled:
            branch = "stall"
        elif trickling:
            branch = "trickle"
        else:
            branch = "integ"

        self._cap_clock_s = prev_clock + wall_adv + fold
        if no_clock_yet:
            # no transport seed and no reading yet (startup): follow the
            # UI-tracked position until one shows up
            self._cap_clock_s = max(0.0, min(self._vid_s, frontier))
            branch = "seed"
        if branch == "fold":
            d_now = raw - self._cap_clock_s
            if self._cap_div_ok:
                self._cap_div_s += (d_now - self._cap_div_s) * _CC_D_ALPHA
            else:
                self._cap_div_s = d_now
                self._cap_div_ok = True
        # loose sanity bound only — real correctness comes from transport
        # seeds and outlier-rejected deltas, never absolute snaps
        self._cap_clock_s = max(0.0, min(self._cap_clock_s, frontier + 120.0))
        # (c) anti-lead clamp: raw is what's ON SCREEN — the clock may lag
        # it (delivery trickle, underrun) but must never lead it beyond the
        # fold granularity, or captions paint ahead of the frozen frames
        # (measured 8.5 s lead on the 2026-08-21 0.17x night). Skipped
        # while a seek verify is pending: the SEEDED clock is the truth
        # there, and raw is about to jump to it.
        if playing and raw >= 0.0 and self._seek_verify is None:
            lead_cap = self._cap_content_for_raw(raw) + _CC_LEAD_MAX_S
            if self._cap_clock_s > lead_cap:
                self._cap_clock_s = max(0.0, lead_cap)
        # fold baseline stored AFTER all clock mutations this tick (clamp,
        # seeding) — it is the clock's position AT this reading
        if raw_changed:
            self._cap_raw_clock = self._cap_clock_s

        # live-edge backlog: the write head advances with DELIVERED data —
        # wall x rate while playback consumes 1:1, raw's MEASURED rate
        # while frames trickle (a 0.2x night must not grow the backlog 5x
        # too fast: the PCR edge-snap would sawtooth and drag every anchor
        # pin with it). Paused playback still grows it 1:1 (trickling is
        # False — the viewer genuinely falls behind a live feed).
        if self._cap_backlog_s is not None and prev_clock > 0.0:
            growing = self._dvr_last_growth is None \
                or (now - self._dvr_last_growth) < 8.0
            if growing:
                adv = self._raw_win_rate(now) * dt if trickling else dt
                self._cap_backlog_s = max(
                    0.0, self._cap_backlog_s + adv
                    - (self._cap_clock_s - prev_clock))
        if _SYNC_ON:
            synclog.info(
                "CLOCK %s raw=%.2f prev=%.2f new=%.2f d=%+.3f fr=%.2f "
                "dt=%.3f rate=%.2f paused=%s backlog=%s edge=%.2f div=%s%s",
                branch, raw, prev_clock, self._cap_clock_s,
                self._cap_clock_s - prev_clock, frontier, dt, self._rate,
                self._chase_paused,
                "-" if self._cap_backlog_s is None
                else "%.2f" % self._cap_backlog_s,
                self._cap_clock_s + (self._cap_backlog_s or 0.0),
                ("%.2f" % self._cap_div_s) if self._cap_div_ok else "-",
                (" reject=" + reject) if reject else "")
        self._cap_wall = now
        return self._cap_clock_s

    def _caption_tick(self):
        """100 ms: paint the cue active at the playback position shifted
        by the user's delay (positive = LATER — pure arithmetic, so the
        delay applies live)."""
        if self._closing or not self._cap_on:
            return
        try:
            chase = self._mode == "chase" and self.dvr
            if chase or self._is_vod():
                if chase:
                    # the deferred arrival-batch anchor lands here (see
                    # _cc_flush_pending) — before the paint decision
                    self._cc_flush_pending()
                t = self._caption_clock_s()
            else:
                return
            delay_ms = int(self.config.subtitle_appearance.get(
                "delay_ms", 0) or 0)
            # POSITIVE delay = show cues LATER, like every other path
            # (config.py's wording, the +/- tooltip, VLC's spu delay via
            # video_set_spu_delay): query the store delay seconds in the
            # PAST, which holds each cue back by the delay.
            lines = self._cap_cues.text_at(t - delay_ms / 1000.0)
            if lines and _SYNC_ON:
                blank = now_s() - (self._sync_last_show_t
                                        or now_s())
                if blank > 2.0:
                    synclog.info("PAINT after %.1f s blank (t=%.2f)",
                                 blank, t)
                self._sync_last_show_t = now_s()   # captions ARE painting
            if chase:
                now = now_s()
                if lines:
                    self._cc_last_active = now
                elif self._cc_off is not None:
                    # Caption-stopped watchdog: cues keep arriving but no
                    # window has intersected the clock for a while — the
                    # anchor diverged from the stored windows (see
                    # _cc_watchdog_fire)
                    if (self._cc_last_arrival > 0.0
                            and now - self._cc_last_arrival
                            < _CC_WATCH_CUE_S
                            and self._cc_last_active > 0.0
                            and now - self._cc_last_active
                            > _CC_WATCH_GAP_S
                            and now - self._cc_last_watchfire
                            > _CC_WATCH_COOLDOWN_S):
                        self._cc_watchdog_fire(now)
            if lines and self._filter_engine.enabled:
                words = self._filter_engine.words
                lines = [prof_mod.mask_text(ln, words) for ln in lines]
            self._cap_wid.set_lines(lines)
        except Exception as exc:  # noqa: BLE001
            # keep the 100 ms caption timer alive whatever happens, but
            # never swallow errors SILENTLY: log each DISTINCT error once
            # (identical repeats suppressed; a new error logs once more)
            key = "%s:%s" % (type(exc).__name__, exc)
            if key not in self._cap_tick_errs:
                if len(self._cap_tick_errs) >= 64:
                    self._cap_tick_errs.clear()   # bound the memory
                self._cap_tick_errs.add(key)
                try:
                    log.warning("captions: tick failed (%s: %s) — "
                                "identical errors now suppressed",
                                type(exc).__name__, exc)
                except Exception:  # noqa: BLE001
                    pass

    def _cc_watchdog_fire(self, now: float):
        """Cues arrived within the last seconds yet NO window intersected
        the clock for > _CC_WATCH_GAP_S: the stored windows and the clock
        came apart (anchor divergence). Snap-derive the anchor from the
        newest cue (edge - L - end) and rebase the stored windows with
        it, so display resumes on the newest content within seconds.
        L here is the INSTANTANEOUS head-probe lag (head_rel - cue end),
        not the EWMA: the watchdog fires exactly when something jumped
        (burst, wedge), which is when the EWMA is mid-transient and would
        land the first correction short — the measured two-fire sequences
        that stretched stops past the contract. The instant lag also
        makes speech-pause derivations ~0 (edge grows, last cue doesn't,
        L_now grows to match — target stays put), so the small-shift
        suppression below stays honest."""
        gap = now - self._cc_last_active if self._cc_last_active > 0.0 \
            else 0.0
        self._cc_last_watchfire = now
        self._cc_last_active = now
        if self._cc_last_c is None:
            return
        lag = None
        head_pcr = self._cc_probe_head_pcr()
        if head_pcr is not None and self._sync_pcr_join is not None:
            lag = (head_pcr - self._sync_pcr_join[1]) - self._cc_last_c
            if not (0.0 <= lag <= _CC_LAG_MAX_S):
                lag = None
        if lag is None:
            lag = self._cc_lag if self._cc_lag is not None else _CC_LAG_S
        target = self._cap_edge_s() - lag - self._cc_last_c
        shift = target - (self._cc_off if self._cc_off is not None else 0.0)
        if abs(shift) < 1.0:
            return    # normal speech gap, not divergence — nothing to fix
        # WP3 data-limited guard: the clock sits far PAST the newest
        # delivered cue's pinned position — the caption for what is on
        # screen has not left the pipeline yet (provider lag exceeds the
        # viewer's backlog). No rebase can cover it (the pin is still ~L
        # behind the clock); rebasing only drags the stored region — the
        # harness measured a ~1-2 s whole-store slam every cooldown
        # through a 1->60 L ramp (~50 rebases, scrubbed regions left ~9 s
        # off). Wait for delivery to catch up instead.
        newest_end = self._cc_last_c + (self._cc_off or 0.0)
        if self._cap_clock_s - newest_end > _CC_WATCH_CUE_S:
            return
        try:
            log.warning("captions: no window hit the clock for %.1f s "
                        "while cues kept arriving — anchor rebased "
                        "(shift %+.1f s)", gap, shift)
        except Exception:
            pass
        self._cc_rebase(target, "watchdog")

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
        if self._closing or self._cap_fail or self._vod_relay is None \
                or not self._cap_relay_live():
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
        if sel is None:
            # Text tracks exist but the English-default policy matched none
            # of them (all labeled non-English, none unlabeled): captions
            # stay OFF. Deliberately NOT the VLC handoff — that re-enabled
            # VLC's own track selection, which then rendered the non-English
            # track (the "subtitles default to a foreign language" bug).
            metas = getattr(relay, "parser_tracks_meta", None) or {}
            langs = sorted({str(m.get("lang") or m.get("name") or "?")
                            for m in metas.values()})
            try:
                log.info("captions: no English text track (langs=%s) — "
                         "captions off", langs)
            except Exception:
                pass
            self._cap_fail = True
            self._set_cap_on(False)
            self._spu_want = -1
            self._spu_name = ""
            self._refresh_spu_button()
            self._cap_note("Captions: no English text track on this file")
            return
        meta = (getattr(relay, "parser_tracks_meta", None) or {}).get(sel)
        hint = self._cap_lang_hint(self._spu_name)
        # An EVIDENCE-FREE selection (unlabeled track) is the ladder's
        # assumed-English pick: acceptable for an English hint, no matter
        # what lang_matches says about empty lang/name (a mismatch handoff
        # there would send a perfectly good default track back to VLC).
        if sel is not None and meta and hint and is_language_name(hint) \
                and not lang_matches(hint, meta.get("lang", ""),
                                     meta.get("name", "")) \
                and not (lang_token(hint) == "eng"
                         and not track_language_evidence(
                             meta.get("lang", ""),
                             meta.get("name", ""))):
            self._cap_vod_handoff(
                f"No {hint.capitalize()} text track — VLC renders",
                "captions: picked %r but the only text tracks are %r "
                "— VLC renders", self._spu_name,
                {m.get("lang") for m in
                 (getattr(relay, "parser_tracks_meta", None) or {}).values()
                 if is_text_codec(m.get("codec", ""))})

    def _cycle_spu(self):
        """C key: Off -> English-first track -> ... -> Off. English is the
        default language: when the stream has an English track, the first
        press lands on it instead of whatever track happens to be first."""
        if self._closing:
            return
        try:
            tracks = self.vlc.spu_tracks()
        except Exception:  # noqa: BLE001
            return
        if not tracks:
            return
        tracks = self._english_first(tracks)
        ids = [-1] + [tid for tid, _ in tracks]
        names = {tid: name for tid, name in tracks}
        try:
            idx = ids.index(self._spu_want)
        except ValueError:
            idx = 0
        nxt = ids[(idx + 1) % len(ids)]
        self._select_spu(nxt, names.get(nxt, ""))
        self._flash_spu(nxt, names.get(nxt, ""))

    def _recommended_spu(self, tracks):
        """(id, name) of the track one-click captions should start: the
        first English-named track, else the first unlabeled one (no
        language evidence = assumed English, the same ladder the relay
        parser uses). None when every track is labeled non-English —
        auto-showing a foreign track is exactly the default the
        English-default policy forbids, so the button opens the menu
        and lets the user pick deliberately."""
        tracks = self._english_first(list(tracks))
        tid, name = tracks[0]
        if self._is_english_name(name) \
                or not track_language_evidence("", name):
            return tid, name
        for tid, name in tracks[1:]:
            if self._is_english_name(name) \
                    or not track_language_evidence("", name):
                return tid, name
        return None

    def _subs_menu(self):
        """CC button: with subtitles OFF, one click STARTS the recommended
        English captions (the same English-first pick the C key makes) —
        no menu, no fuss. With subtitles already ON (or when no track can
        auto-start), the click opens the track/style card. Right-click
        opens the card from either state."""
        if not self._closing and self._spu_want == -1 and not self._cap_on:
            try:
                tracks = self.vlc.spu_tracks()
            except Exception:  # noqa: BLE001
                tracks = []
            pick = self._recommended_spu(tracks) if tracks else None
            if pick is not None:
                self._select_spu(pick[0], pick[1])
                self._flash_spu(pick[0], pick[1])
                return
            if not tracks and self._is_dvrable() and not self._cap_fail:
                # live channel whose captions never surface as VLC
                # tracks (the CC pipeline needs no track id): engaging
                # the overlay directly still gives one-click captions
                # (a missing CCExtractor disengages with a note inside)
                self._spu_name = "English CC"
                self._engage_caption_overlay()
                return
        self._subs_panel()

    def _subs_panel(self):
        """The track/style card (right-click on the CC button, left-click
        once captions are on, and the left-click fallback when no track
        can auto-start)."""
        if self._closing:
            return
        try:
            tracks = self.vlc.spu_tracks()
        except Exception:  # noqa: BLE001
            tracks = []
        # "Off" is ALWAYS offered: live caption tracks surface seconds
        # after playback starts (and some channels never list VLC tracks
        # at all — the CC pipeline needs no track id), so a trackless
        # card must still open with Off + settings
        rows = [{"id": -1, "name": "", "main": "Off",
                 "checked": self._spu_want == -1}]
        for tid, name in tracks:
            kind = self._cap_track_kind(name)
            if kind == "bitmap":
                sub = "image \u2014 not adjustable"
            elif self._cap_eligible(name):
                sub = "text \u2014 adjustable"
            elif kind == "ass":
                sub = "ASS \u2014 VLC rendering"
            else:
                sub = ""
            rows.append({"id": tid, "name": name, "main": name or
                         f"Track {tid}", "sub": sub,
                         "checked": tid == self._spu_want})
        rows.append({"sep": True})
        rows.append({"id": "settings", "main": "Subtitle settings\u2026"})
        self._open_ctl_panel(
            self.btn_cc, "SUBTITLES", rows, self._on_spu_row_picked)

    def _on_spu_row_picked(self, row):
        rid = row.get("id")
        if rid == "settings":
            self._open_sub_settings()
        else:
            self._select_spu(rid, row.get("name", "") or "")

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
        dlg = SubtitleDialog(self.config, self._apply_sub_delay,
                             apply_live=self._apply_sub_style_live,
                             parent=self.window())
        closer = None
        if NativeDialogCloser is not None:
            # click outside the dialog = done (settings apply live, so a
            # dismiss finalizes exactly like OK)
            closer = NativeDialogCloser(dlg)
            QtWidgets.QApplication.instance() \
                .installNativeEventFilter(closer)
        try:
            dlg.exec_()
        finally:
            if closer is not None:
                try:
                    QtWidgets.QApplication.instance() \
                        .removeNativeEventFilter(closer)
                except Exception:  # noqa: BLE001
                    pass
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
        dialog already wrote the config). Re-layout too: the black-bar
        placement toggle is geometry, not paint. VLC-rendered tracks
        cannot restyle at runtime; the dialog-close path rebuilds the
        player once for those."""
        if self._closing:
            return
        if self._cap_on:
            self._layout_overlays()
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
        if kind in ("vod", "series", "catchup"):
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

    # ---- audio tracks (embedded stream tracks) ----
    @staticmethod
    def _is_english_name(name: str) -> bool:
        """Does a VLC track NAME carry the English language? Word-matched
        through the shared alias table, so 'English', 'Track 2 - [English]'
        and 'en' all qualify while 'Audio 1' (no language evidence) and
        'Spanish' do not."""
        return lang_matches("english", "", name or "")

    @classmethod
    def _english_first(cls, tracks: list) -> list:
        """[(id, name), ...] with the FIRST English-named track moved to
        the front (stable otherwise) — cycle order and menus start at
        English when the stream has one, per the English-default policy."""
        for i, (_, name) in enumerate(tracks):
            if cls._is_english_name(name):
                if i:
                    tracks = [tracks[i]] + tracks[:i] + tracks[i + 1:]
                break
        return tracks

    def _enforce_audio(self):
        """Re-assert the audio-track choice against the CURRENT media.

        Runs from every tick for the same reasons as _enforce_spu: VLC
        re-selects a stream's own default track on media opens and ES
        updates, fresh players after hung-stop swaps lose the selection,
        and the track list only exists once VLC has parsed the elementary
        streams. Two modes:

        auto  (no user pick — _audio_name empty): default English. When
              the current track's name doesn't word-match English and the
              stream HAS an English track, switch to it. Streams without
              one keep VLC's own selection — audio is never disabled.
        pick  (menu / A-key choice): re-matched by NAME across player
              swaps and chase reopens within THIS program; play_media
              clears the pick, so every program starts at Auto/English
              (a wrong pick must never follow the user into the next
              show). If the name vanishes entirely mid-program the
              selector falls back to auto mode (never silence)."""
        if self._closing:
            return
        try:
            tracks = self.vlc.audio_tracks()
            if tracks:
                names = dict(tracks)
                active = self.vlc.active_audio()
                if not self._audio_name:
                    if not self._is_english_name(names.get(active, "")):
                        for tid, name in tracks:
                            if self._is_english_name(name):
                                if tid != self._audio_auto_tid:
                                    self._audio_auto_tid = tid
                                    try:
                                        log.info("audio: defaulting to "
                                                 "English track %r "
                                                 "(was %r)", name,
                                                 names.get(active, "?"))
                                    except Exception:
                                        pass
                                self.vlc.set_audio(tid)
                                break
                elif self._audio_want not in names:
                    # id unknown here: re-match by name (an EMPTY track
                    # list never reaches this point — it skips the block)
                    match = None
                    for tid, name in tracks:
                        if name and name == self._audio_name:
                            match = (tid, name)
                            break
                    if match is None:
                        low = self._audio_name.lower()
                        for tid, name in tracks:
                            if name and low in name.lower():
                                match = (tid, name)
                                break
                    if match is None:
                        # pick gone on this media: back to auto (English)
                        self._audio_want = None
                        self._audio_name = ""
                    else:
                        self._audio_want, self._audio_name = match
                        self.vlc.set_audio(self._audio_want)
                elif active != self._audio_want:
                    self.vlc.set_audio(self._audio_want)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_audio_button()

    def _refresh_audio_button(self):
        """Tooltip-only state on the audio button — this runs every tick
        and the icon is state-neutral, so nothing repaints unless the
        label changed. The button is ALWAYS clickable: with no tracks the
        menu still opens with Auto."""
        try:
            n = len(self.vlc.audio_tracks())
        except Exception:  # noqa: BLE001
            n = 0
        label = self._audio_name or "Auto (English)"
        state = (bool(self._audio_name), label, n)
        if state == self._audio_ui:
            return
        self._audio_ui = state
        self.btn_audio.setToolTip(
            f"Audio tracks \u2014 {label} (A)" if n else "Audio tracks (A)")

    def _select_audio(self, track_id, name: str = ""):
        """User picked an audio track from the menu (None/-1 = Auto)."""
        try:
            tid = None if track_id is None else int(track_id)
        except (TypeError, ValueError):
            tid = None
        if tid is None or tid < 1:
            self._audio_want = None
            self._audio_name = ""
        else:
            self._audio_want = tid
            self._audio_name = name or ""
            self.vlc.set_audio(self._audio_want)
        self._refresh_audio_button()

    def _cycle_audio(self):
        """A key: Auto -> English-first track -> ... -> Auto."""
        if self._closing:
            return
        try:
            tracks = self.vlc.audio_tracks()
        except Exception:  # noqa: BLE001
            return
        if not tracks:
            return
        names = dict(tracks)
        ids = [None] + [tid for tid, _ in self._english_first(tracks)]
        try:
            idx = ids.index(self._audio_want if self._audio_name else None)
        except ValueError:
            idx = 0
        nxt = ids[(idx + 1) % len(ids)]
        self._select_audio(nxt, names.get(nxt, ""))
        self._flash_audio(nxt, names.get(nxt, ""))

    def _audio_menu(self):
        """Waveform button: opens (or toggles closed) the Stremio-style
        audio track picker card above the button."""
        if self._closing:
            return
        self._open_ctl_panel(
            self.btn_audio, "AUDIO", self._refresh_audio_rows(),
            lambda row: self._select_audio(row["id"], row.get("name", "")),
            refresh=self._refresh_audio_rows)

    def _refresh_audio_rows(self) -> list:
        """Rebuild the picker rows from the current track list — runs on
        open and once a second while open (track lists can arrive seconds
        into playback). Returns them for the opener."""
        try:
            tracks = self.vlc.audio_tracks()
        except Exception:  # noqa: BLE001
            tracks = []
        auto_on = not self._audio_name
        rows = [{
            "id": None, "name": "", "main": "Auto",
            "sub": "English when available", "checked": auto_on,
        }]
        # one checkmark only: Auto in auto mode (whatever track the English
        # default / VLC landed on), the pick itself in pick mode
        for tid, name in self._english_first(tracks):
            main, sub = self._audio_row_label(name, tid)
            rows.append({
                "id": tid, "name": name, "main": main, "sub": sub,
                "checked": bool(self._audio_name) and tid == self._audio_want,
                "tip": name or f"Track {tid}",
            })
        if not tracks:
            # tracks surface seconds after playback starts — say so rather
            # than showing a card with nothing but Auto (the 1 s refresh
            # fills the list the moment they arrive)
            rows.append({
                "id": "empty", "name": "", "main": "No audio tracks yet",
                "sub": "this stream is still loading\u2026", "enabled": False,
            })
        if self._ctl_panel.isVisible():
            self._ctl_panel.set_rows(rows, header="AUDIO")
        return rows

    @staticmethod
    def _audio_row_label(name: str, tid) -> tuple:
        """VLC's raw track name -> (main, sub) for the picker row: the
        language word as the big label, the leftover qualifier as the dim
        sub-label ('Track 2 - [English]' -> 'English'; 'English (United
        States)' -> 'English' + 'United States')."""
        raw = (name or "").strip()
        if not raw:
            return f"Track {tid}", ""
        m = re.search(r"\[([^\]]+)\]", raw)
        if m:
            return m.group(1).strip(), ""
        m = re.match(r"([^(]+?)\s*\((.+)\)\s*$", raw)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        if " - " in raw:
            head, tail = raw.rsplit(" - ", 1)
            tail = tail.strip()
            if tail and not tail.isdigit():
                return tail, head.strip()
        return raw, ""

    def _flash_audio(self, track_id, name: str):
        """Brief on-video confirmation while cycling with the keyboard."""
        if self._closing or not self._dvr_status.isHidden():
            return   # the pill is busy with DVR start-up info
        text = name if track_id else "Auto (English)"
        self._set_dvr_status(f"Audio: {text}")
        QtCore.QTimer.singleShot(1200,
                                 lambda: self._set_dvr_status("")
                                 if self._dvr_status.text() ==
                                 f"Audio: {text}" else None)


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
        self._filter_engine.whole_cue = bool(prof.get("whole_cue"))
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
        """Playback URL.  Catch-up goes through the local RANGE relay (the
        provider's timeshift responses carry a malformed Accept-Ranges
        header, which leaves VLC's stream non-seekable — no scrub bar, no
        seeks; the relay re-serves the bytes with correct range headers).
        VOD with captions wanted routes through the local splitter relay
        (single provider connection; the splitter peels the subtitle text
        and feeds VLC byte-identical data through localhost).  Both fall
        back to the original URL on any hesitation — playback must never
        depend on them.

        ``self._relay_start_offset`` (set by play_media for resumes and by
        _restart_through_relay for mid-movie engages) marks a RESUME
        session: the relay prefetches only the tail and VLC's own opening
        walk + seek drive the provider stream, so subtitles surface right
        at the switch position."""
        offset = self._relay_start_offset
        self._relay_start_offset = 0
        if kind == "catchup" and url and url.startswith("http") \
                and not self._closing:
            return self._start_catchup_relay(url) or url
        want_caps = self._cap_want and not self._cap_fail
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
        self._cap_relay_gen = self._session   # stale-delivery guard mark
        if want_caps:
            self._set_cap_on(True)
            # a few seconds in, verify the file actually HAS a text track
            # for the pick (the check retries while the head streams in)
            self._schedule_cap_vod_check()
        # the evaluation loop — without it the windows pile up but no
        # mute is ever applied
        self._filter_timer.start()
        return local

    # ---- catch-up relay (localhost range proxy: scrub-ability) ----
    def _start_catchup_relay(self, url: str) -> str:
        """Serve the timeshift stream locally with correct range headers;
        '' on failure (caller plays the provider URL directly)."""
        try:
            relay = CatchupRelay()
            local = relay.start(url, USER_AGENT)
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("catchup relay: failed (%r) — direct playback",
                            exc)
            except Exception:
                pass
            return ""
        if not local:
            return ""
        self._catchup_relay = relay
        self._catchup_local_url = local
        return local

    def _stop_catchup_relay(self):
        relay = self._catchup_relay
        self._catchup_relay = None
        self._catchup_local_url = ""
        if relay is not None:
            try:
                relay.stop()
            except Exception:  # noqa: BLE001
                pass

    def _cap_relay_live(self) -> bool:
        """Stale-delivery guard for the VOD relay's queued cue/failed
        signals.

        VodRelay emits from worker threads over queued connections, and
        play_media's teardown (disconnect + stop + store clear + session
        bump) races emissions that were already in flight — ``failed``
        isn't disconnected at all — so a delivery from the PREVIOUS
        media's relay can land after the new media attached its own
        relay (stray caption, phantom profanity-mute window, cap_fail
        latched against a healthy relay). Accept a delivery only from
        the relay the CURRENT media attached; with no sender (direct
        call, or a delivery whose sender was already destroyed) fall
        back to the session marker set at attach time."""
        try:
            snd = self.sender()
        except Exception:  # noqa: BLE001
            snd = None
        if snd is not None:
            return snd is self._vod_relay
        return self._cap_relay_gen == self._session

    def _on_vod_cue(self, start: float, end: float, text: str):
        if self._closing or not self._cap_relay_live():
            return
        # VOD subtitle tracks are pre-timed — no caption-lag lead, but the
        # measured ~0.5 s late-mute trim applies (see _VOD_MUTE_LEAD_S)
        self._filter_engine.add_cue(start, end, text,
                                    lead_s=_VOD_MUTE_LEAD_S)
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

        On ANY buffer the reader JOINS near the playback position
        (``_cc_join_byte``) instead of byte 0: replaying buffered content
        costs ~1 s of CPU per buffered minute before live cues flow — and
        with D2 the old >=90 s frontier gate is gone, so a mid-show
        engage at a small frontier joins near the playhead too. The exact
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
            # D2: join near the playback position at ANY frontier — a
            # mid-show engage must never replay the buffer from byte 0
            try:
                size = os.path.getsize(buf)
            except OSError:
                size = 0
            join = _cc_join_byte(size, self._frontier_s(), self._vid_s)
            src = CCSource(self)
            src.cue.connect(self._on_cc_cue)
            if hasattr(src, "failed"):
                # any hesitation hands captions back to VLC for this media
                src.failed.connect(self._cap_source_failed)
            if src.start(buf, join_bytes=join):
                self._cc_source = src
                # Join bookkeeping: the PCR at the join byte pins CCX's
                # axis origin — every L measurement and edge calibration
                # keys off it (see _on_cc_cue). Mid-session joins also
                # need the join byte's content position (refined from the
                # first head probe); byte-0 joins are exactly 0.
                self._cc_join_byte = int(join)
                self._cc_join_app_s = 0.0 if join <= 0 else None
                self._sync_pcr_join = None
                self._sync_pcr_join_tries = 0
                if _SYNC_ON:
                    synclog.info("CCSTART buf=%s join_byte=%d",
                                 buf, join)
                QtCore.QTimer.singleShot(
                    50, lambda j=join: self._sync_probe_join_at(j))
                # periodic head probe: edge calibration between cues
                self._cc_edge_timer.start()
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
        """One live cue arrived — place it at its TRUE content position.

        CCExtractor's SRT times are zero-based from its join byte and the
        provider's caption axis drifts against the video axis (stage 1
        measured 0-129 s and nonstationary), so a cue's own numbers only
        carry RELATIVE ordering. The absolute position comes from two
        live measurements: the tail-PCR probe gives the write head, and
        L = head - cue_end is CCX's true lag — so the newest FRESH cue
        pins at

            edge - L        (edge = dead-reckoned head, PCR-calibrated)

        which is exactly where that cue's speech sits in the buffer.

        The anchor decision itself is DEFERRED to the next caption tick
        (``_cc_flush_pending``): CCX flushes its SRT in multi-second
        bursts, and every cue inside a burst passes the per-cue
        freshness test — anchoring per-cue let a burst's stale interior
        cues snap-rebase the anchor by a full batch width (the live
        matrix's "queue-skew" innovation tail, and a mid-pause store
        shift that blanked captions at resume). Corrections beyond
        _CC_REBASE_S snap only when the evidence is real (huge,
        persistent, or a stable target — see _cc_flush_pending); smaller
        ones EWMA-settle, and stored cues keep their pin-time positions
        (the store re-coheres through rebase snaps only)."""
        if self._closing:
            return
        now = now_s()
        self._cc_last_arrival = now
        off_before = self._cc_off
        last_c = self._cc_last_c
        elapsed = 1.0 if self._cc_last_t <= 0.0 else now - self._cc_last_t
        advance = None if last_c is None else end - last_c
        # Fresh = advancing no faster than a few times wall time. Some
        # providers' caption PTS axis legitimately runs ~2x wall (measured
        # on a 4K channel), which a tighter bound read as an eternal
        # catch-up burst — anchors stopped refreshing and captions drifted
        # late over every silent stretch. Real catch-up replays at
        # 30-100x, far outside even this generous bound.
        fresh = (
            (last_c is None and self._frontier_s() < 20.0)
            or (advance is not None
                and 0.0 < advance <= elapsed * 3.0 + 5.0)
        )
        if fresh:
            reason = "fresh"
        elif last_c is None:
            reason = "first-cue-fr>=20"
        elif advance is None:
            reason = "no-advance-info"
        elif advance <= 0.0:
            reason = "dup/regress"
        else:
            reason = "catchup(adv=%.1f>%.1f)" % (advance, elapsed * 3.0 + 5.0)
        if last_c is None or end > last_c:
            # baseline for the NEXT cue's advance judgment (never
            # regressed by CCX's duplicate re-emissions)
            self._cc_last_c = end
            self._cc_last_t = now
        # -- calibrate the dead-reckoned edge on one head probe --
        head_pcr = self._cc_probe_head_pcr()
        head_rel = None
        if head_pcr is not None and self._sync_pcr_join is not None:
            head_rel = head_pcr - self._sync_pcr_join[1]
            self._cc_refine_join_app(head_pcr)
            # a landed CDN burst moves the head and the lag together —
            # keep the pin (edge - L) stable across it
            snap = self._cc_calibrate_edge(head_rel)
            if snap and self._cc_lag is not None:
                self._cc_lag += snap
        if fresh:
            # deferred anchor (see the docstring): the caption tick
            # applies ONE decision per arrival batch, written by its
            # newest cue (last writer wins)
            self._cc_pend = (end, head_rel)
        off = self._cc_off
        if _SYNC_ON:
            lag = self._cc_lag
            mapped_end = None if off is None else end + off
            synclog.info(
                "CUE cx=[%.2f..%.2f] adv=%s el=%.2f %s | off=%s "
                "fr=%.2f cc=%.2f raw=%.2f lead=%s | pcr=%.3f "
                "head_rel=%s lag_ewma=%s | q=%.60s",
                start, end,
                "-" if advance is None else "%.2f" % advance,
                elapsed, reason,
                "-" if off is None else "%.2f" % off,
                self._frontier_s(), self._cap_clock_s, self._sync_raw_s(),
                "-" if mapped_end is None
                else "%+.2f" % (mapped_end - self._cap_clock_s),
                -1.0 if head_pcr is None else head_pcr,
                "-" if head_rel is None else "%.2f" % head_rel,
                "-" if lag is None else "%.2f" % lag,
                (text or "").replace("\n", " / "))
        if off is None:
            # no anchor yet (catch-up burst / pre-first-flush): stash the
            # cue — _cc_flush_pending stores the whole stash the moment
            # the first anchor pins the offset
            self._cc_stash.append((start, end, text))
            if len(self._cc_stash) > 300:
                del self._cc_stash[:150]
            return
        start = max(0.0, start + off)
        end = max(start, end + off)
        # arrival-anchored times are already display times: no caption-lag
        # lead for the filter either
        self._filter_engine.add_cue(start, end, text, lead_s=0.0)
        self._cap_cues.add(start, end, text)

    def _cc_flush_pending(self):
        """Apply the deferred anchor decision (see _on_cc_cue): one per
        arrival batch — the batch's newest cue was the last writer, so
        its target is the only one that lands. Takes ONE clean lag
        sample per batch from that cue (interior cues' samples carried
        the whole batch width and inflated the EWMA).

        The pin uses the FRESH sample, not the EWMA: smoothing L first
        and pinning second compounds the EWMA's transient — a delivery
        pause through a fast L jump left the first post-pause pin 10.6 s
        off (65% of the accumulated jump; p3_repro_osc.py). The anchor's
        own a=0.5 EWMA on the target provides the smoothing; the L EWMA
        (WP3: 0.18 -> 0.35) serves D1 landing and diagnostics."""
        if self._cc_pend is None or self._closing:
            return
        end, head_rel = self._cc_pend
        self._cc_pend = None
        lag_now = None
        if head_rel is not None:
            lag_now = head_rel - end
            if 0.0 <= lag_now <= _CC_LAG_MAX_S:
                if self._cc_lag is None:
                    self._cc_lag = lag_now
                else:
                    self._cc_lag += (lag_now - self._cc_lag) * _CC_LAG_ALPHA
            else:
                lag_now = None
        lag = self._cc_lag
        pin_lag = lag_now if lag_now is not None \
            else (lag if lag is not None else _CC_LAG_S)
        target = self._cap_edge_s() - pin_lag - end
        if _SYNC_ON:
            synclog.info("ANCHOR pend_end=%.2f L=%s pinL=%s target=%.2f "
                         "off=%s", end,
                         "-" if lag is None else "%.2f" % lag,
                         "-" if lag_now is None else "%.2f" % lag_now,
                         target,
                         "-" if self._cc_off is None
                         else "%.2f" % self._cc_off)
        if self._cc_off is None:
            self._cc_off = target   # first anchor lands as-is (fast start)
            self._cc_prev_target = target
            self._cc_oob_run = 0
        else:
            gap = target - self._cc_off
            prev = self._cc_prev_target
            self._cc_prev_target = target
            if abs(gap) > _CC_REBASE_S:
                # WP3 robust snap: a big gap alone is not snap evidence
                # (sample spikes round-tripped the store 35-50x per
                # session on the 2026-08-21 corpus). Snap only when the
                # correction is REAL: huge outright, persistent across
                # batches, or the target itself is stable — a lone spike
                # MOVES the target, a genuine correction (incl. the
                # snap-back after a wrong forced rebase) re-asserts a
                # steady one.
                self._cc_oob_run += 1
                stable = prev is not None \
                    and abs(target - prev) <= _CC_REBASE_STABLE_S
                if abs(gap) > _CC_REBASE_HARD_S \
                        or self._cc_oob_run >= _CC_REBASE_CONFIRM_N \
                        or stable:
                    self._cc_rebase(target, "anchor-snap")
                    gap = 0.0        # fully applied by the snap
            else:
                self._cc_oob_run = 0
            if gap:
                # EWMA: settle on the MEAN pipeline lag instead of jittering
                # cue-to-cue with burst flushes and poll phase. Stored cues
                # keep their pin-time positions (see the WP3 note above the
                # constants): they are already at their true windows to
                # within the tracker trail, and the store re-coheres only
                # through rebase snaps.
                self._cc_off += gap * _CC_ANCHOR_ALPHA
        if self._cc_stash:
            # cues that arrived before the first anchor (or during a
            # catch-up burst) become placeable now: same axis, so the
            # offset maps them straight to their true positions — a cold
            # join then keeps its first screens scrubbable
            off = self._cc_off
            for ss, se, stx in self._cc_stash:
                ms = max(0.0, ss + off)
                me = max(ms, se + off)
                self._filter_engine.add_cue(ms, me, stx, lead_s=0.0)
                self._cap_cues.add(ms, me, stx)
            self._cc_stash = []

    def _stop_cc_source(self):
        """Tear down just the live caption reader (channel change etc.)."""
        try:
            self._cc_edge_timer.stop()
        except Exception:  # noqa: BLE001
            pass
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
        self._stop_catchup_relay()
        if self._vod_relay is not None and not self._cap_on:
            try:
                self._vod_relay.cue.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                # `failed` was never disconnected here — a stale delivery
                # used to latch _cap_fail against the NEXT media's relay
                self._vod_relay.failed.disconnect()
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
        took it, or the window was minimized): the owned Qt.Tool overlay
        window already sinks below the foreground app, but hiding outright
        guarantees no stale show() path can paint it over other apps."""
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
        self._win_cancel(silent=True)
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
        self._seek_verify = None
        self._seek_esc_strikes = 0
        self._seek_esc_ok_at = 0.0
        self._seek_esc_clean = 0.0
        self._raw_win = []
        self._trickle_hold = False
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
            self._ctl_panel_timer.stop()
            self._ctl_panel.close_panel("teardown")
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
        mods = event.modifiers()
        if key == QtCore.Qt.Key_Escape \
                and self._ctl_panel.isVisible():
            self._ctl_panel.close_panel("Escape")
        elif key == QtCore.Qt.Key_Escape and self._win_sel:
            self._win_cancel()
        elif key == QtCore.Qt.Key_Space:
            self._toggle_pause()
        elif key in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right,
                     QtCore.Qt.Key_Up, QtCore.Qt.Key_Down):
            sign = -1 if key in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Down) else 1
            if self._win_sel:
                # marker nudging: plain 1 s, Shift 10 s, Ctrl 60 s
                nudge = 6000 if mods & QtCore.Qt.ControlModifier \
                    else 1000 if not mods & QtCore.Qt.ShiftModifier else 10000
                self._win_nudge(sign * nudge)
            elif key in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right):
                self._seek_ms(sign * 10000)
            else:
                self._seek_ms(sign * 60000)
        elif key == QtCore.Qt.Key_M:
            self.btn_mute.toggle()
        elif key == QtCore.Qt.Key_C:
            self._cycle_spu()
        elif key == QtCore.Qt.Key_A:
            self._cycle_audio()
        elif key == QtCore.Qt.Key_N:
            self._play_next_clicked()
        else:
            super().keyPressEvent(event)