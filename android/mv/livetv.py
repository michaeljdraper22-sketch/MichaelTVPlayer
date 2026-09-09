"""Live TV screen: Xtream categories + channel list.

All Xtream traffic (authenticate / live_categories / live_streams) runs
on worker threads via run_bg; channel URLs are pure string builds
(client.live_url) so the tap handler stays on the main thread.
"""

from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import BooleanProperty
from kivy.uix.asyncimage import AsyncImage
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.screenmanager import Screen

from michaeltv_core.xtream import XtreamClient, normalize_server_url

from .util import run_bg

KV = '''
<LiveTVScreen>:
    canvas.before:
        Color:
            rgba: 0.05, 0.05, 0.06, 1
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
                on_release: app.go("home")
            MTitle:
                text: "Live TV"

        MSpinner:
            id: category
            text: ""
            values: []
            size_hint_y: None
            height: dp(44)
            opacity: 1 if self.values else 0
            disabled: not self.values
            on_text: root.on_category(self.text)

        MLabel:
            id: status
            text: ""
            size_hint_y: None
            height: dp(22)
            padding_x: dp(10)

        ScrollView:
            GridLayout:
                id: channels
                cols: 1
                size_hint_y: None
                height: self.minimum_height
                padding: dp(4), dp(4)
                spacing: dp(2), dp(2)

        BoxLayout:
            id: unconfigured
            orientation: "vertical"
            spacing: dp(10)
            padding: dp(24)
            size_hint_y: None
            height: dp(180)
            opacity: 0
            disabled: True
            MLabel:
                text: "Live TV needs an Xtream provider account.\\n"
                      "Add your server, username and password in Settings."
                halign: "center"
            MButton:
                text: "Open Settings"
                size_hint: None, None
                width: dp(180)
                height: dp(48)
                pos_hint: {"center_x": 0.5}
                on_release: app.go("settings")
'''


class ChannelRow(BoxLayout):
    """One live channel: logo thumb (when the panel sends one) + name."""

    def __init__(self, channel, on_open, **kwargs):
        super().__init__(**kwargs)
        self.orientation = "horizontal"
        self.size_hint_y = None
        self.height = dp(56)
        self.spacing = dp(8)
        self.channel = channel
        self._on_open = on_open

        logo = str(channel.get("logo") or channel.get("stream_icon") or "")
        if logo:
            box = BoxLayout(size_hint_x=None, width=dp(56),
                            padding=(dp(2), dp(2)))
            box.add_widget(AsyncImage(source=logo, allow_stretch=True,
                                      keep_ratio=False))
            self.add_widget(box)
        self.add_widget(Label(
            text=str(channel.get("name") or "?"),
            color=(1, 1, 1, 1), font_size="15sp",
            halign="left", valign="middle",
            shorten=True, shorten_from="right"))

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self._on_open(self.channel)
            return True
        return super().on_touch_down(touch)


class LiveTVScreen(Screen):
    loading = BooleanProperty(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.client = None
        self._sig = None            # creds the current client was built from
        self._categories = []       # [(category_id, name)]

    # ---- lifecycle ----

    def on_enter(self):
        st = self._app().settings
        configured = st.xtream_configured()
        self.ids.unconfigured.opacity = 1 if not configured else 0
        self.ids.unconfigured.disabled = configured
        if not configured:
            self.ids.status.text = ""
            return
        sig = (st.xtream_server.strip(), st.xtream_username.strip(),
               st.xtream_password.strip())
        if sig == self._sig and self._categories:
            return                      # already loaded
        self._sig = sig
        self.client = None
        self._categories = []
        self.ids.category.values = []
        self.ids.category.text = ""
        self.ids.channels.clear_widgets()
        self._connect(sig)

    def _connect(self, sig):
        if self.loading:
            return
        self.loading = True
        self.ids.status.text = "Signing in…"

        def work():
            server, username, password = sig
            client = XtreamClient(normalize_server_url(server),
                                  username, password)
            client.authenticate()
            cats = client.live_categories()
            return client, cats

        run_bg(work, on_result=self._connected,
               on_error=self._connect_failed)

    def _connected(self, result):
        self.loading = False
        client, cats = result
        self.client = client
        self._categories = [
            (str(c.get("category_id")), str(c.get("category_name") or "?"))
            for c in (cats or []) if c.get("category_id") is not None]
        names = [name for _cid, name in self._categories]
        if not names:
            self.ids.status.text = "No live categories on this account"
            return
        self.ids.category.values = names
        self.ids.category.text = names[0]      # triggers on_category()

    def _connect_failed(self, exc):
        self.loading = False
        self.ids.status.text = "Provider error: %s" % (exc,)

    # ---- category / channels ----

    def on_category(self, text):
        if not self.client or not self._categories or not text:
            return
        cid = None
        for category_id, name in self._categories:
            if name == text:
                cid = category_id
                break
        self._load_channels(cid)

    def _load_channels(self, category_id):
        if self._loading_channels():
            return
        self.ids.channels.clear_widgets()
        self.ids.status.text = "Loading channels…"
        client = self.client

        def work():
            return client.live_streams(category_id)

        run_bg(work, on_result=self._channels_ready,
               on_error=self._channels_failed)
        self._channels_job = True

    def _loading_channels(self):
        return bool(getattr(self, "_channels_job", False))

    def _channels_ready(self, streams):
        self._channels_job = False
        grid = self.ids.channels
        grid.clear_widgets()
        for channel in (streams or []):
            if channel.get("stream_id") is None:
                continue
            grid.add_widget(ChannelRow(channel, self._open))
        self.ids.status.text = (
            "%d channel(s)" % len(grid.children) if grid.children
            else "No channels in this category")

    def _channels_failed(self, exc):
        self._channels_job = False
        self.ids.status.text = "Channel load failed: %s" % (exc,)

    def _open(self, channel):
        if not self.client:
            return
        url = self.client.live_url(channel.get("stream_id"), ext="ts")
        self._app().open_player(url, str(channel.get("name") or "Live"),
                                back_to="livetv")

    @staticmethod
    def _app():
        from kivy.app import App
        return App.get_running_app()


Builder.load_string(KV)
