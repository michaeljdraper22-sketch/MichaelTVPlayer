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

from PyQt5 import QtCore, QtGui, QtWidgets

# cues kept per source (a 2 h movie carries ~1-3k; a long live session
# similar) — bounded memory, and rewinds still find their captions
_MAX_CUES = 5000
_ROLLUP_LINES = 3          # broadcast roll-up caption window height
_CUE_GRACE_S = 0.25        # hold a cue briefly past its end (anti-flicker)


class CueStore:
    """Time-indexed subtitle cues, shared by every caption source."""

    def __init__(self):
        self.cues = []          # sorted by start
        self._seen = set()

    def clear(self):
        self.cues = []
        self._seen = set()

    def add(self, start: float, end: float, text: str):
        """One cue. Duplicates (relay re-parse after a rebase) drop out."""
        text = (text or "").strip()
        if not text:
            return
        key = (round(float(start), 3), text)
        if key in self._seen:
            return
        self._seen.add(key)
        self.cues.append((float(start), max(float(start), float(end)), text))
        # keep the list sorted even if sources interleave; a source that
        # only ever appends (the common case) pays a cheap already-sorted
        # check
        if len(self.cues) > 1 and self.cues[-2][0] > self.cues[-1][0]:
            self.cues.sort(key=lambda c: c[0])
        if len(self.cues) > _MAX_CUES:
            del self.cues[:len(self.cues) - _MAX_CUES]

    def text_at(self, t: float):
        """Lines to display at content time ``t`` (newest active cue wins —
        roll-up screens repeat the previous lines, so the newest cue IS the
        whole window). Returns [] when nothing is active."""
        t = float(t)
        active = None
        for start, end, text in reversed(self.cues):
            if start <= t <= end + _CUE_GRACE_S:
                active = text
                break
            if start < t - 60.0:
                break           # cues are sorted: everything older is dead
        if active is None:
            return []
        lines = [ln.strip() for ln in active.split("\n") if ln.strip()]
        return lines[-_ROLLUP_LINES:]


class CaptionOverlay(QtWidgets.QWidget):
    """Draws subtitle lines over the video, styled from the config.

    Covers the whole video surface (its parent is the overlay window that
    exactly tracks the video). Style is re-read on every paint, so changes
    in Subtitle settings apply instantly — no player rebuild. Transparent
    for mouse events; paints nothing while no lines are set.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self._lines = []
        self._bottom_inset = 24      # px above the surface bottom (controls)
        self.hide()

    # ---- data ----
    def set_lines(self, lines):
        lines = [ln for ln in (lines or []) if ln]
        if lines == self._lines:
            return
        self._lines = lines
        if lines:
            self.show()
        else:
            self.hide()
        self.update()

    def set_bottom_inset(self, px: int):
        px = max(0, int(px))
        if px != self._bottom_inset:
            self._bottom_inset = px
            self.update()

    # ---- painting ----
    def paintEvent(self, _event):
        if not self._lines or self.height() < 40 or self.width() < 80:
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
        for ln in self._lines:
            wrapped.extend(self._wrap(fm, ln, max_w) or [" "])
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
