"""Small threading / UI helpers shared by all screens.

Threading discipline for the whole app: EVERY network call (michaeltv_core
stremio.*, XtreamClient) runs via run_bg() on a background thread, and
results are delivered on the Kivy main thread through
Clock.schedule_once. No screen ever blocks the main thread on the network.
"""

import threading

from kivy.clock import Clock
from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp
from kivy.uix.label import Label


def run_bg(fn, on_result=None, on_error=None):
    """Run fn() on a daemon thread, deliver on the Kivy main thread.

    on_result(result) / on_error(exception) are called via
    Clock.schedule_once, so they may touch widgets freely. A worker that
    raises while on_error is None still surfaces on the console log.
    """
    def wrapper():
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 — marshalled, never lost
            if on_error is not None:
                Clock.schedule_once(lambda dt: on_error(exc))
            else:
                print("mv: background task failed: %r" % (exc,))
            return
        if on_result is not None:
            Clock.schedule_once(lambda dt: on_result(result))

    thread = threading.Thread(target=wrapper, daemon=True)
    thread.start()
    return thread


class Toast(Label):
    """Brief toast-like message with a dark rounded backing."""

    _is_toast = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.size_hint = (None, None)
        self.halign = "center"
        self.valign = "middle"
        self.color = (1, 1, 1, 1)
        self.bold = True
        self.padding = (dp(14), dp(10))
        self.bind(texture_size=self._grow, pos=self._upd_bg, size=self._upd_bg)

    def _grow(self, inst, size):
        self.size = (size[0] + 2 * self.padding[0],
                     max(size[1] + 2 * self.padding[1], dp(38)))
        self._ensure_bg()

    def _ensure_bg(self):
        if getattr(self, "_bg_rect", None) is None:
            with self.canvas.before:
                Color(0.08, 0.08, 0.10, 0.95)
                self._bg_rect = RoundedRectangle(
                    pos=self.pos, size=self.size, radius=[dp(8)] * 4)
        self._upd_bg()

    def _upd_bg(self, *args):
        rr = getattr(self, "_bg_rect", None)
        if rr is not None:
            rr.pos = self.pos
            rr.size = self.size


def toast(anchor, text, seconds=2.5):
    """Show ``text`` centered over ``anchor`` (any widget) for ``seconds``."""
    label = Toast(text=text)
    anchor.add_widget(label)

    def center_it(dt):
        label.center = anchor.center

    Clock.schedule_once(center_it)
    Clock.schedule_once(lambda dt: _fade_out(anchor, label), seconds)
    return label


def _fade_out(anchor, label):
    from kivy.animation import Animation
    anim = Animation(opacity=0, d=0.3)
    anim.bind(on_complete=lambda *_: _remove(anchor, label))
    anim.start(label)


def _remove(anchor, label):
    if label.parent is anchor:
        anchor.remove_widget(label)


def clear_toast(anchor):
    """Remove any toast labels currently parented to ``anchor``."""
    for child in list(anchor.children):
        if getattr(child, "_is_toast", False):
            anchor.remove_widget(child)
