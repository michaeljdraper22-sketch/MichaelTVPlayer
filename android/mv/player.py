"""Player screen: Kivy Video with minimal controls.

Flow (on enter): probe_debrid on a worker thread (src/stremio.py:190 —
a 1-byte range GET, 3 s timeout). On success the Video widget starts;
on failure a brief "link dead" message is shown instead. A watchdog
catches the "state says play but nothing ever loaded" failure shape
with a toast-like label.
"""

from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import BooleanProperty
from kivy.uix.screenmanager import Screen
from kivy.uix.slider import Slider
from kivy.uix.video import Video

from michaeltv_core import stremio

from .util import run_bg, toast

STARTUP_GRACE_S = 15.0      # no frames/frames-duration by then -> error
TICK_S = 0.5

KV = '''
<PlayerScreen>:
    canvas.before:
        Color:
            rgba: 0, 0, 0, 1
        Rectangle:
            pos: self.pos
            size: self.size

    BoxLayout:
        orientation: "vertical"

        Toolbar:
            MButton:
                text: "< Back"
                size_hint_x: None
                width: dp(96)
                on_release: root.go_back()
            MTitle:
                id: title_lbl
                text: ""
                shorten: True
                shorten_from: "right"

        BoxLayout:
            padding: 0, dp(6)
            Video:
                id: video
                source: ""
                state: "stop"
                on_eos: root.on_eos()

        BoxLayout:
            orientation: "vertical"
            size_hint_y: None
            height: dp(108)
            padding: dp(10), dp(4)
            spacing: dp(4)

            MLabel:
                id: status
                text: ""
                size_hint_y: None
                height: dp(20)
                italic: True

            BoxLayout:
                orientation: "horizontal"
                spacing: dp(8)
                size_hint_y: None
                height: dp(40)
                MLabel:
                    id: time_cur
                    text: "0:00"
                    size_hint_x: None
                    width: dp(56)
                PosSlider:
                    id: pos
                    min: 0
                    max: 1
                    value: 0
                    on_seek: root.on_seek(args[1])
                MLabel:
                    id: time_left
                    text: "0:00"
                    size_hint_x: None
                    width: dp(56)

            MButton:
                id: play_btn
                text: "Pause"
                size_hint_y: None
                height: dp(44)
                disabled: True
                on_release: root.toggle_play()
'''


class PosSlider(Slider):
    """Position slider that reports scrub start/end; during a scrub the
    tick loop stops overwriting .value so the thumb tracks the finger."""

    scrubbing = BooleanProperty(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.register_event_type("on_seek")

    def on_touch_down(self, touch):
        if super().on_touch_down(touch):
            self.scrubbing = True
            return True
        return False

    def on_touch_move(self, touch):
        return super().on_touch_move(touch) if self.scrubbing else False

    def on_touch_up(self, touch):
        if self.scrubbing:
            self.scrubbing = False
            super().on_touch_up(touch)
            self.dispatch("on_seek", self.value)
            return True
        return super().on_touch_up(touch)

    def on_seek(self, value):
        """Event sink when not bound."""


def keep_screen_on(enable):
    """FLAG_KEEP_SCREEN_ON while playing; no-op off-Android (guarded so
    the module still imports and runs on desktop Kivy)."""
    try:
        from jnius import autoclass          # pyjnius, Android only
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        params = autoclass("android.view.WindowManager$LayoutParams")
        window = activity.getWindow()
        if enable:
            window.addFlags(params.FLAG_KEEP_SCREEN_ON)
        else:
            window.clearFlags(params.FLAG_KEEP_SCREEN_ON)
    except Exception:
        pass


def fmt_time(seconds):
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        seconds = 0
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


class PlayerScreen(Screen):
    url = ""
    title = ""
    back_to = "streams"
    _active_url = None
    _started = False
    _tick = None
    _watchdog = None

    # ---- lifecycle ----

    def on_enter(self):
        self.ids.title_lbl.text = self.title or "Player"
        if self._active_url == self.url and self._started:
            return                      # same playback still loaded
        self._stop_video()
        self._active_url = None
        self._started = False
        self._set_controls(False)
        self.ids.status.text = "Checking link…"
        self.ids.time_cur.text = "0:00"
        self.ids.time_left.text = "0:00"
        self.ids.pos.value = 0
        keep_screen_on(True)
        url = self.url
        run_bg(lambda: stremio.probe_debrid(url),
               on_result=lambda alive: self._probed(alive),
               on_error=lambda exc: self._probe_error(exc))

    def on_leave(self):
        self._stop_video()
        keep_screen_on(False)

    def go_back(self):
        from kivy.app import App
        App.get_running_app().go(self.back_to or "streams")

    # ---- probe / start ----

    def _probed(self, alive):
        if alive:
            self._start()
        else:
            self.ids.status.text = "Link dead — pick another stream."
            toast(self, "Link dead — pick another stream")

    def _probe_error(self, exc):
        self.ids.status.text = "Link check failed: %s" % (exc,)

    def _start(self):
        self._active_url = self.url
        self._started = True
        self.ids.status.text = ""
        video = self.ids.video
        video.source = self.url
        video.state = "play"
        self._set_controls(True)
        if self._tick:
            self._tick.cancel()
        self._tick = Clock.schedule_interval(self._tick_ui, TICK_S)
        if self._watchdog:
            self._watchdog.cancel()
        self._watchdog = Clock.schedule_once(
            lambda dt: self._check_started(), STARTUP_GRACE_S)

    def _check_started(self):
        video = self.ids.video
        if self._active_url and video.state == "play" \
                and not video.texture and not (video.duration or 0):
            self._playback_error(
                "Couldn't play this stream — try another one.")

    def _playback_error(self, text):
        self.ids.status.text = text
        toast(self, text, seconds=3.5)
        self._set_controls(False)

    # ---- controls ----

    def _set_controls(self, enabled):
        self.ids.play_btn.disabled = not enabled
        self.ids.pos.disabled = not enabled
        self.ids.play_btn.text = "Pause" if enabled else "Play"

    def toggle_play(self):
        video = self.ids.video
        if video.state == "play":
            video.state = "pause"
            self.ids.play_btn.text = "Play"
        else:
            video.state = "play"
            self.ids.play_btn.text = "Pause"

    def on_seek(self, value):
        video = self.ids.video
        if self._started:
            video.seek(value)
            self._tick_ui(0)

    def on_eos(self):
        if not (self.ids.video.duration or 0):
            self._playback_error("Playback failed — this link didn't "
                                 "produce any media.")
            return
        self.ids.status.text = "Ended"
        self.ids.play_btn.text = "Play"

    def _tick_ui(self, dt):
        video = self.ids.video
        pos = self.ids.pos
        duration = video.duration or 0
        if duration > 0:
            pos.max = duration
        self.ids.time_cur.text = fmt_time(video.position or 0)
        self.ids.time_left.text = (
            "-" + fmt_time(max(0, duration - (video.position or 0)))
            if duration else "live")
        if not pos.scrubbing and duration > 0:
            pos.value = min(video.position or 0, duration)

    def _stop_video(self):
        if self._tick:
            self._tick.cancel()
            self._tick = None
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        video = self.ids.video
        video.state = "stop"
        try:
            video.unload()
        except Exception:
            pass


Builder.load_string(KV)
