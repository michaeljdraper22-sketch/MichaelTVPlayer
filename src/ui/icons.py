"""Crisp, minimal white line icons (Stremio-style) for the player UI.

Every glyph is pure vector code (QPainter paths at any scale). Each icon is
painted ONCE, antialiased directly at the exact device pixels the screen
will ask for (24 logical px x the screen DPR) and handed to QIcon with its
DPR tag; the app sets AA_UseHighDpiPixmaps so Qt serves that pixmap
1:1 instead of rescaling it on every paint. No intermediate raster is ever
resampled — that was the source of the old grain/softness.
"""

from math import cos, radians, sin

from PyQt5 import QtCore, QtGui, QtWidgets

WHITE = QtGui.QColor(255, 255, 255, 255)
RED = QtGui.QColor(255, 69, 58, 255)
BLUE = QtGui.QColor(10, 132, 255, 255)
GOLD = QtGui.QColor(245, 197, 24, 255)   # catch-up window markers / button

_L = 24          # logical canvas size
_cache = {}


def _screen_dpr() -> float:
    """Device pixel ratio of the primary screen (1.0 when unknown)."""
    try:
        app = QtWidgets.QApplication.instance()
        scr = app.primaryScreen() if app is not None else None
        if scr is not None:
            return max(1.0, float(scr.devicePixelRatio()))
    except Exception:  # noqa: BLE001
        pass
    return 1.0


def _F(x, y):
    return QtCore.QPointF(x, y)


def _icon(key, draw, keep_disabled=False):
    """Render one glyph. ``keep_disabled`` pins the SAME pixmap into the
    icon's Disabled mode (state-indicator glyphs — e.g. the window-download
    button while a download runs — must not gray out; they carry meaning)."""
    dpr = _screen_dpr()
    side = max(8, int(round(_L * dpr)))
    cache_key = (key, side)
    icon = _cache.get(cache_key)
    if icon is not None:
        return icon
    pm = QtGui.QPixmap(side, side)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    p.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
    p.scale(side / _L, side / _L)
    draw(p, WHITE)
    p.end()
    pm.setDevicePixelRatio(dpr)
    if keep_disabled:
        icon = QtGui.QIcon()
        icon.addPixmap(pm, QtGui.QIcon.Normal, QtGui.QIcon.Off)
        icon.addPixmap(pm, QtGui.QIcon.Disabled, QtGui.QIcon.Off)
    else:
        icon = QtGui.QIcon(pm)
    _cache[cache_key] = icon
    return icon


def _pen(p, c, w=2.2):
    pen = QtGui.QPen(c, w)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    p.setPen(pen)
    return pen


def _polyline(p, c, points, w=2.2):
    _pen(p, c, w)
    path = QtGui.QPainterPath(_F(*points[0]))
    for pt in points[1:]:
        path.lineTo(_F(*pt))
    p.drawPath(path)


def _text(p, c, s, rect, size=8):
    p.setPen(c)
    p.setFont(QtGui.QFont("Segoe UI", size, QtGui.QFont.Bold))
    p.drawText(QtCore.QRectF(*rect),
               QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter, s)


# ---- transport ----
def play():
    def draw(p, c):
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(QtGui.QPolygonF(
            [_F(8.2, 5.2), _F(8.2, 18.8), _F(19.8, 12.0)]))
    return _icon("play", draw)


def pause():
    def draw(p, c):
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QtCore.QRectF(7.6, 5.5, 3.4, 13.0), 1.7, 1.7)
        p.drawRoundedRect(QtCore.QRectF(13.0, 5.5, 3.4, 13.0), 1.7, 1.7)
    return _icon("pause", draw)


def _seek_glyph(p, c):
    """Almost-closed circle with an arrowhead running counter-clockwise."""
    _pen(p, c, 2.2)
    p.drawArc(QtCore.QRectF(3.9, 4.4, 16.2, 16.2), 300 * 16, 300 * 16)
    cx, cy, r = 12.0, 12.5, 8.1
    th = radians(240.0)                      # the CCW end of the arc
    ex, ey = cx + r * cos(th), cy - r * sin(th)
    tx, ty = -sin(th), -cos(th)              # travel direction at that end
    nx, ny = cos(th), -sin(th)               # outward normal
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(c)
    p.drawPolygon(QtGui.QPolygonF([
        _F(ex + tx * 3.0, ey + ty * 3.0),    # tip, along the travel direction
        _F(ex + nx * 2.5, ey + ny * 2.5),
        _F(ex - nx * 2.5, ey - ny * 2.5)]))


def _seek_text(p, c, label):
    _text(p, c, label, (3.0, 5.0, 18.0, 15.0), 7)


def rewind10():
    def draw(p, c):
        _seek_glyph(p, c)
        _seek_text(p, c, "10")
    return _icon("rw10", draw)


def rewind60():
    def draw(p, c):
        _seek_glyph(p, c)
        _seek_text(p, c, "60")
    return _icon("rw60", draw)


def fwd10():
    def draw(p, c):
        p.translate(24, 0)
        p.scale(-1, 1)
        _seek_glyph(p, c)                    # mirrored = clockwise
        p.scale(-1, 1)
        p.translate(-24, 0)
        _seek_text(p, c, "10")
    return _icon("fw10", draw)


# ---- live / dvr / record ----
def live():
    """Jump to the live edge: a 'skip to end' glyph (bar + right triangle).

    White like the other transport buttons — the red dot belongs to the
    RECORD button now, this one used to read as 'recording'.
    """
    def draw(p, c):
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(QtGui.QPolygonF(
            [_F(6.6, 6.0), _F(13.4, 12.2), _F(6.6, 18.4)]))
        p.drawRoundedRect(QtCore.QRectF(15.4, 6.0, 2.6, 12.4), 1.3, 1.3)
    return _icon("live_skip", draw)


def begin():
    """Jump to the beginning: the mirror of the live glyph (bar + LEFT
    triangle) — sits just left of the LIVE button."""
    def draw(p, c):
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(QtGui.QPolygonF(
            [_F(17.4, 6.0), _F(10.6, 12.2), _F(17.4, 18.4)]))
        p.drawRoundedRect(QtCore.QRectF(6.0, 6.0, 2.6, 12.4), 1.3, 1.3)
    return _icon("begin_skip", draw)


def dvr(on):
    def draw(p, c):
        col = BLUE if on else c
        _pen(p, col, 2.2)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawEllipse(QtCore.QRectF(3.9, 4.4, 16.2, 16.2))
        p.drawLine(_F(12, 12.5), _F(12, 7.6))
        p.drawLine(_F(12, 12.5), _F(15.6, 14.2))
    return _icon("dvr" + ("on" if on else "off"), draw)


def rec(on):
    """Record: the classic red dot — filled while recording, a red ring
    when idle, so the button always reads as 'record'."""
    def draw(p, c):
        if on:
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(RED)
            p.drawEllipse(_F(12, 12.5), 5.6, 5.6)
        else:
            _pen(p, RED, 2.2)
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawEllipse(QtCore.QRectF(6.8, 7.3, 10.4, 10.4))
    return _icon("rec" + ("on" if on else "off"), draw)


# ---- captions / subtitles ----
def cc(on):
    """Subtitles: a screen frame with 'CC' inside — blue while a track is
    active (same state colouring as the DVR button)."""
    def draw(p, c):
        col = BLUE if on else c
        _pen(p, col, 2.2)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawRoundedRect(QtCore.QRectF(3.2, 5.2, 17.6, 13.6), 2.4, 2.4)
        _text(p, col, "CC", (3.2, 5.6, 17.6, 12.8), size=9)
    return _icon("cc" + ("on" if on else "off"), draw)


# ---- audio tracks (soundwave bars) ----
def audio():
    """Audio track picker: five rounded soundwave bars — the modern
    audio-track glyph (waveform), deliberately NOT a speaker, so it never
    reads as the mute/volume control beside it."""
    def draw(p, c):
        # own waveform pattern: short-tall-medium-medium-short, centered
        # on the 24px grid, bars drawn with the house round-cap pen
        heights = (7.0, 12.6, 17.8, 10.8, 5.4)
        _pen(p, c, 2.5)
        for i, h in enumerate(heights):
            x = 4.2 + i * 3.9
            y0 = 12.0 - h / 2.0
            p.drawLine(_F(x, y0), _F(x, y0 + h))
    return _icon("audio", draw)

# ---- video scaling (two arrows stretching diagonally apart) ----
def scale():
    def draw(p, c):
        # corner brackets reach further toward the middle, and the diagonal
        # arrow shafts run well past the bracket — the old short stubs read
        # as "nubby"
        _polyline(p, c, [(4.6, 4.6), (9.8, 4.6)])
        _polyline(p, c, [(4.6, 4.6), (4.6, 9.8)])
        _polyline(p, c, [(9.8, 4.6), (5.2, 9.2)])
        _polyline(p, c, [(4.6, 9.8), (9.2, 5.2)])
        _polyline(p, c, [(19.4, 19.4), (14.2, 19.4)])
        _polyline(p, c, [(19.4, 19.4), (19.4, 14.2)])
        _polyline(p, c, [(14.2, 19.4), (18.8, 14.8)])
        _polyline(p, c, [(19.4, 14.2), (14.8, 18.8)])
    return _icon("scale", draw)


# ---- playback speed (speedometer) ----
def speed():
    def draw(p, c):
        _pen(p, c, 2.2)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawArc(QtCore.QRectF(4.4, 5.4, 15.2, 15.2), 0, 180 * 16)
        _pen(p, c, 2.0)
        p.drawLine(_F(4.4, 13.0), _F(6.2, 13.0))
        p.drawLine(_F(19.6, 13.0), _F(17.8, 13.0))
        _pen(p, c, 2.4)
        p.drawLine(_F(12, 13.0), _F(16.8, 8.9))
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c)
        p.drawEllipse(_F(12, 13.0), 1.9, 1.9)
    return _icon("speed", draw)


# ---- volume ----
def volume(on):
    def draw(p, c):
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(QtGui.QPolygonF([
            _F(4.2, 9.6), _F(7.8, 9.6), _F(12.2, 5.8),
            _F(12.2, 19.2), _F(7.8, 14.4), _F(4.2, 14.4)]))
        if on:
            _pen(p, c, 2.1)
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawArc(QtCore.QRectF(9.6, 9.1, 6.8, 6.8), -50 * 16, 100 * 16)
            p.drawArc(QtCore.QRectF(6.8, 6.3, 12.4, 12.4), -50 * 16, 100 * 16)
        else:
            _pen(p, c, 2.2)
            p.drawLine(_F(16.2, 9.8), _F(21.0, 15.6))
            p.drawLine(_F(21.0, 9.8), _F(16.2, 15.6))
    return _icon("vol" + ("on" if on else "off"), draw)


# ---- menu / panel / fullscreen chrome ----
def menu():
    def draw(p, c):
        _pen(p, c, 2.2)
        p.drawLine(_F(5, 7.2), _F(19, 7.2))
        p.drawLine(_F(5, 12.0), _F(19, 12.0))
        p.drawLine(_F(5, 16.8), _F(19, 16.8))
    return _icon("menu", draw)


def fullscreen():
    def draw(p, c):
        _polyline(p, c, [(9.4, 4.6), (4.6, 4.6), (4.6, 9.4)])
        _polyline(p, c, [(14.6, 4.6), (19.4, 4.6), (19.4, 9.4)])
        _polyline(p, c, [(19.4, 14.6), (19.4, 19.4), (14.6, 19.4)])
        _polyline(p, c, [(4.6, 14.6), (4.6, 19.4), (9.4, 19.4)])
    return _icon("fullscreen", draw)


def panel_collapse():
    """Sidebar icon with a chevron — 'hide the channel list'."""
    def draw(p, c):
        _pen(p, c, 2.2)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawRoundedRect(QtCore.QRectF(3.6, 5.0, 16.8, 14.0), 2.5, 2.5)
        p.drawLine(_F(9.4, 5.0), _F(9.4, 19.0))
        _polyline(p, c, [(16.4, 9.4), (13.6, 12.0), (16.4, 14.6)])
    return _icon("panel_collapse", draw)


def panel_expand():
    """Big chevron-right — floating 'show the channel list' button."""
    def draw(p, c):
        _polyline(p, c, [(9.6, 6.4), (16.4, 12.0), (9.6, 17.6)], 2.6)
    return _icon("panel_expand", draw)


def download():
    """Download: an arrow dropping into a tray (VOD replacement for DVR)."""
    def draw(p, c):
        _polyline(p, c, [(12.0, 4.2), (12.0, 13.0)])
        _polyline(p, c, [(7.9, 9.5), (12.0, 13.6), (16.1, 9.5)])
        _polyline(p, c, [(4.7, 16.6), (4.7, 19.4), (19.3, 19.4), (19.3, 16.6)])
    return _icon("download", draw)


def download_window(color=None, keep_disabled=False):
    """Catch-up window download: a timeline segment between two in-point
    markers with the download arrow below it — mirrors the two gold < >
    markers the button drops onto the scrubber. WHITE at rest like every
    other control glyph; the GOLD variant marks the engaged states (window
    markers active, download in flight)."""
    gold = color is not None and color != WHITE

    def draw(p, c):
        col = color or c
        # the timeline with the selected segment highlighted
        _polyline(p, col, [(3.0, 5.2), (21.0, 5.2)], 2.0)
        _polyline(p, col, [(6.6, 2.8), (6.6, 7.6), (17.4, 7.6), (17.4, 2.8)], 2.0)
        # the download arrow beneath
        _polyline(p, col, [(12.0, 10.6), (12.0, 16.6)])
        _polyline(p, col, [(8.9, 13.8), (12.0, 16.9), (15.1, 13.8)])
        _polyline(p, col, [(6.0, 19.6), (6.0, 21.4), (18.0, 21.4), (18.0, 19.6)], 2.0)
    key = "download-window-gold" if gold else "download-window"
    if keep_disabled:
        key += "-keep"
    return _icon(key, draw, keep_disabled=keep_disabled)


# ---- autoplay next / play next ----
# Both glyphs are direct translations of the user's mockups (Screenshot
# 2026-08-28 105013 for autoplay, 121619 for play next).
def autoplay(on):
    """Autoplay-next toggle, co-designed with the user: a smaller
    OUTLINED play triangle with a coil wave beside it — straight
    vertical strokes joined by semicircular U-turns (bottom, top,
    bottom), a short start nub under the triangle's right side, and a
    final leg rising to the height of the top U's peak. OFF strikes an
    X over it, the mute-button convention for 'disabled'."""
    def draw(p, c):
        _pen(p, c, 2.2)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawPolygon(QtGui.QPolygonF(
            [_F(3.5, 9.0), _F(3.5, 17.0), _F(10.0, 13.0)]))
        # K*r control offsets give circular semicircles from cubics
        path = QtGui.QPainterPath(_F(9.2, 17.0))
        path.lineTo(9.2, 18.0)                                          # start nub
        path.cubicTo(_F(9.2, 20.667), _F(13.2, 20.667), _F(13.2, 18.0))  # U bottom
        path.lineTo(13.2, 11.0)                                         # up
        path.cubicTo(_F(13.2, 8.333), _F(17.2, 8.333), _F(17.2, 11.0))   # U top
        path.lineTo(17.2, 18.0)                                         # down
        path.cubicTo(_F(17.2, 20.667), _F(21.2, 20.667), _F(21.2, 18.0))  # U bottom
        path.lineTo(21.2, 9.0)                                          # end at U-peak height
        p.drawPath(path)
        if not on:
            _pen(p, c, 2.2)
            p.drawLine(_F(3.9, 6.4), _F(21.7, 20.4))
            p.drawLine(_F(21.7, 6.4), _F(3.9, 20.4))
    return _icon("autoplay8" + ("on" if on else "off"), draw)


def play_next():
    """Play next (next episode / next channel), per the user's mockup: a
    chunky SOLID right-pointing triangle with a SOLID rounded-cap bar on
    its heel — the skip-forward glyph, no frame."""
    def draw(p, c):
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(QtGui.QPolygonF(
            [_F(1.8, 5.4), _F(1.8, 18.6), _F(17.8, 12.0)]))
        p.drawRoundedRect(QtCore.QRectF(19.4, 5.4, 2.6, 13.2), 1.3, 1.3)
    return _icon("play_next4", draw)


def check():
    """Checkmark — the selected row marker in the track picker panel."""
    def draw(p, c):
        _polyline(p, c, [(5.4, 12.6), (10.1, 17.2), (18.8, 7.4)], 2.6)
    return _icon("check", draw)
