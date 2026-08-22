# -*- coding: utf-8 -*-
"""App-rendered subtitles: ONE style for every text caption source.

Live closed captions (CCExtractor tailing the DVR buffer) and VOD SRT
tracks (the local relay's streaming MKV parser) both arrive as timed cues
on the playback clock. VLC 3 renders CEA-608/708 captions with embedded
colors and absolute caption-grid placement that no libvlc option can
override — so the app draws them itself, in a Qt widget styled live from
``config.subtitle_appearance`` (font/size/colors/outline/background/
position; the delay is arithmetic on the cue clock, so it too is live).

Playback never depends on this overlay: VLC's own spu rendering stays
fully wired and takes over the moment the overlay hesitates (missing
CCExtractor, dead caption source) — see PlayerView._cap_fail.
"""

import re
from bisect import bisect_left
from collections import deque

from PyQt5 import QtCore, QtGui, QtWidgets

# cues kept per source (a 2 h movie carries ~1-3k; a long live session
# similar) — bounded memory, and rewinds still find their captions (the
# cap evicts by ARRIVAL order, so re-received rewound cues stay while
# stale forward-flow ones leave)
_MAX_CUES = 5000
_ROLLUP_LINES = 3          # broadcast roll-up caption window height
_CUE_GRACE_S = 0.25        # hold a cue briefly past its end (anti-flicker)

# Characters that render as NOTHING (bidi embedding/isolate controls,
# zero-width space, BOM, LRM/RLM marks, soft hyphen, word joiner) —
# rippers pad subtitle lines with them. A line made only of these (plus
# NBSP) painted a background box with no text ("the box shows up but
# it's empty"). Soft hyphen and LRM/RLM joined the list after the same
# report came back: a line of only U+00AD or U+200E survives a plain
# strip() yet paints nothing.
_INVIS_RE = re.compile(
    "[\u00ad\u200b\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
# Zero-width JOINERS (U+200C/200D) are stripped only for the paintable
# test: Arabic shaping needs them INSIDE words, so they stay in the
# emitted text — but a line made only of them (padding again) paints
# nothing and must drop.
_JOINS_RE = re.compile("[\u200c\u200d\xa0\\s]")


def visible_lines(text: str) -> list:
    """Cue text -> lines that would actually paint something."""
    out = []
    for ln in (text or "").split("\n"):
        ln = _INVIS_RE.sub("", ln).replace("\xa0", " ").strip()
        if ln and _JOINS_RE.sub("", ln):
            out.append(ln)
    return out


def displayed_video_rect(video_wh, mode: str, surface_w: int, surface_h: int):
    """Where the decoded video paints inside the surface: (x, y, w, h).

    Pure geometry (no Qt types) so the caption anchoring is testable.
    ``fit`` letterboxes/pillarboxes the whole picture inside the surface,
    centered — the rect is then smaller than the surface.  ``crop`` and
    ``stretch`` cover the entire surface (zoomed past the picture's edges
    or distorted onto them).  An unknown size ((0, 0) — nothing decoded
    yet, video_get_size before playback) also maps to the full surface,
    so callers keep the historic whole-surface behavior until the real
    size arrives."""
    vw = int(video_wh[0] or 0)
    vh = int(video_wh[1] or 0)
    sw, sh = int(surface_w), int(surface_h)
    if (vw <= 0 or vh <= 0 or sw <= 0 or sh <= 0
            or mode in ("crop", "stretch")):
        return 0, 0, max(0, sw), max(0, sh)
    scale = min(sw / vw, sh / vh)
    dw = max(1, int(vw * scale + 0.5))
    dh = max(1, int(vh * scale + 0.5))
    return (sw - dw) // 2, (sh - dh) // 2, dw, dh


class CueStore:
    """Time-indexed subtitle cues, shared by every caption source."""

    def __init__(self):
        self.cues = []          # sorted by start
        self._seen = set()      # (round(start, 3), text) of STORED cues
        self._order = deque()   # (start, text) in arrival order — the
        #                         eviction queue (rewinds re-receive cues)
        self._max_span = 0.0    # upper bound on any stored end - start

    def clear(self):
        self.cues = []
        self._seen = set()
        self._order.clear()
        self._max_span = 0.0

    def add(self, start: float, end: float, text: str):
        """One cue. Duplicates (relay re-parse after a rebase) drop out —
        but only while the cue is actually STORED: eviction prunes the
        dedupe memory with it, so a rewind that re-receives evicted cues
        re-enters them instead of painting nothing."""
        text = (text or "").strip()
        if not text or not visible_lines(text):
            return      # nothing paintable (padding-only lines) — skip
        key = (round(float(start), 3), text)
        if key in self._seen:
            return
        self._seen.add(key)
        s = float(start)
        e = max(s, float(end))
        self.cues.append((s, e, text))
        self._order.append((s, text))
        if e - s > self._max_span:
            self._max_span = e - s
        # keep the list sorted even if sources interleave; a source that
        # only ever appends (the common case) pays a cheap already-sorted
        # check
        if len(self.cues) > 1 and self.cues[-2][0] > self.cues[-1][0]:
            self.cues.sort(key=lambda c: c[0])
        while len(self.cues) > _MAX_CUES:
            self._evict_one()

    def _evict_one(self):
        """Drop the oldest-ARRIVED cue — and its dedupe key with it.

        Evicting by arrival rather than by start is what keeps rewinds
        alive: a re-received old cue is a fresh arrival, so it stays
        (the oldest stale arrival leaves instead), while the forward
        live/VOD flow — where arrival order IS start order — evicts
        exactly the cues the old front-truncate did."""
        if not self._order:
            return
        s, txt = self._order.popleft()
        i = bisect_left(self.cues, (s,))    # first cue with start >= s
        while i < len(self.cues) and self.cues[i][0] <= s:
            if self.cues[i][2] == txt:      # (start, text) is unique
                del self.cues[i]
                self._seen.discard((round(s, 3), txt))
                return
            i += 1

    def shift(self, delta: float):
        """Move every stored window by ``delta`` content seconds (same
        direction). A snap-and-rebase of the live arrival anchor moves the
        CCX->app offset by whole seconds; shifting the already-mapped cues
        with it keeps the store's timeline coherent, so a scrub back after
        a rebase shows captions placed where they actually play."""
        if not self.cues or not delta:
            return
        self.cues = [(max(0.0, s + delta), max(0.0, e + delta), txt)
                     for s, e, txt in self.cues]
        self._seen = {(round(s, 3), txt) for s, _e, txt in self.cues}
        self._order = deque((s, txt) for s, _e, txt in self.cues)
        # the max(0.0, ...) clamp can only shrink windows, so _max_span
        # remains a valid upper bound

    def text_at(self, t: float):
        """Lines to display at content time ``t``. Overlap policy: the
        NEWEST covering cue wins — the one with the greatest start, ties
        broken by latest arrival (roll-up screens repeat the previous
        lines, so the newest cue IS the whole window). Returns [] when
        nothing is active.

        The backward scan breaks early only once no OLDER cue could
        still be active: cues are sorted by start but ENDS are not (a
        song or a description can run minutes past newer cues' starts),
        and every stored window fits within ``_max_span``, so a cue
        starting before ``t - grace - _max_span`` cannot reach ``t``.
        The old fixed 60 s horizon abandoned such still-active long cues
        mid-display."""
        t = float(t)
        active = None
        cutoff = t - _CUE_GRACE_S - self._max_span
        for start, end, text in reversed(self.cues):
            if start <= t <= end + _CUE_GRACE_S:
                active = text
                break
            if start < cutoff:
                break       # sorted by start: older windows cannot reach t
        if active is None:
            return []
        return visible_lines(active)[-_ROLLUP_LINES:]


class CaptionOverlay(QtWidgets.QWidget):
    """Draws subtitle lines over the video, styled from the config.

    PlayerView._layout_overlays sizes/positions this widget onto the
    DISPLAYED picture rect (see displayed_video_rect — letterboxed movies
    get a smaller overlay than the surface, so captions scale and anchor
    with the picture, not the widget).  Style is re-read on every paint,
    so changes in Subtitle settings apply instantly — no player rebuild.
    Transparent for mouse events; paints nothing while no lines are set
    (a preview line may stand in while the settings dialog is open).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self._lines = []
        self._preview = ""       # sample line while the settings dialog
        #                          is open (see set_preview)
        self._bottom_inset = 24      # px above the picture bottom (controls)
        self.hide()

    # ---- data ----
    def set_lines(self, lines):
        lines = [ln for ln in (lines or []) if ln]
        if lines == self._lines:
            return
        self._lines = lines
        self._sync_visibility()
        self.update()

    def set_preview(self, text):
        """A sample line painted whenever no cue is active — the settings
        dialog sets it so style/position tweaks are visible on the video
        even mid-silence. Real cues always take precedence."""
        text = (text or "").strip()
        if text == self._preview:
            return
        self._preview = text
        self._sync_visibility()
        self.update()

    def _sync_visibility(self):
        self.setVisible(bool(self._lines or self._preview))

    def set_bottom_inset(self, px: int):
        px = max(0, int(px))
        if px != self._bottom_inset:
            self._bottom_inset = px
            self.update()

    # ---- painting ----
    def paintEvent(self, _event):
        lines = self._lines or ([self._preview] if self._preview else [])
        if not lines or self.height() < 40 or self.width() < 80:
            return
        ap = self._appearance() or {}
        w, h = self.width(), self.height()

        # font: config size is px at 1080p and scales with the video
        px = self._font_px(ap, h)
        font = QtGui.QFont(ap.get("font") or "")
        font.setPixelSize(px)
        font.setWeight(QtGui.QFont.Normal)
        fm = QtGui.QFontMetrics(font)

        # wrap lines to ~90% of the surface width
        max_w = int(w * 0.9)
        wrapped = []
        for ln in lines:
            wrapped.extend(self._wrap(fm, ln, max_w))
        if not wrapped:
            return

        # geometry: stack bottom-up above (inset + position raise)
        pos = int(ap.get("pos_pct", 0) or 0)
        bottom = self._bottom_inset + int(h * 0.04) \
            + int((pos / 100.0) * h * 0.5)
        bottom = min(bottom, int(h * 0.9))
        line_h = fm.height()
        block_h = line_h * len(wrapped)
        y = h - bottom - block_h
        if y < int(h * 0.02):
            y = int(h * 0.02)

        # colors
        text = self._color(ap.get("text_color"), "#FFFFFF", 255)
        outline_on = bool(ap.get("outline_enabled", True))
        outline = self._color(ap.get("outline_color"), "#000000", 255)
        # VLC's thickness unit: 4 ("normal") ≈ 6% of the font height
        thick = max(1, round(int(ap.get("outline_thickness", 4) or 4)
                             / 4.0 * px * 0.06))
        bg_on = bool(ap.get("bg_enabled"))
        bg = self._color(ap.get("bg_color"), "#000000",
                         round(max(0, min(100,
                                          int(ap.get("bg_opacity", 50) or 0)))
                               * 2.55))

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
        painter.setFont(font)
        for i, ln in enumerate(wrapped):
            lw = fm.horizontalAdvance(ln)
            x = (w - lw) // 2
            ly = y + i * line_h
            if bg_on:
                painter.fillRect(
                    x - thick * 2, ly + fm.ascent() - fm.height() + 2,
                    lw + thick * 4, fm.height(),
                    bg)
            if outline_on:
                path = QtGui.QPainterPath()
                path.addText(QtCore.QPointF(x, ly + fm.ascent()), font, ln)
                stroke = QtGui.QPen(outline, thick * 2)
                stroke.setJoinStyle(QtCore.Qt.RoundJoin)
                stroke.setCapStyle(QtCore.Qt.RoundCap)
                painter.setPen(stroke)
                painter.drawPath(path)
                painter.setPen(QtCore.Qt.NoPen)
                painter.drawPath(path)
                painter.fillPath(path, text)
            else:
                painter.setPen(QtGui.QPen(text))
                painter.drawText(QtCore.QPointF(x, ly + fm.ascent()), ln)
        painter.end()

    # ---- helpers ----
    def _appearance(self) -> dict:
        cfg = self._cfg_getter
        try:
            return cfg() if cfg else {}
        except Exception:  # noqa: BLE001
            return {}

    _cfg_getter = None

    def bind_config(self, getter):
        """Provide a callable returning the current subtitle_appearance."""
        self._cfg_getter = getter

    @staticmethod
    def _font_px(ap: dict, surface_h: int) -> int:
        size = int(ap.get("size", 0) or 0)
        if size > 0:
            # config px are at 1080p; scale with the actual video height
            return max(9, round(size * max(surface_h, 120) / 1080.0))
        # Auto: VLC sizes subtitles from the video height (~5%)
        return max(9, round(surface_h * 0.05))

    @staticmethod
    def _wrap(fm: QtGui.QFontMetrics, line: str, max_w: int) -> list:
        """Word-wrap one caption line to ``max_w`` px."""
        words = line.split()
        if not words:
            return []
        out = []
        cur = ""
        for word in words:
            cand = word if not cur else cur + " " + word
            if fm.horizontalAdvance(cand) <= max_w or not cur:
                cur = cand
            else:
                out.append(cur)
                cur = word
        if cur:
            out.append(cur)
        return out

    @staticmethod
    def _color(hex_color, fallback: str, alpha: int) -> QtGui.QColor:
        col = QtGui.QColor(str(hex_color) or fallback)
        if not col.isValid():
            col = QtGui.QColor(fallback)
        col.setAlpha(max(0, min(255, int(alpha))))
        return col
