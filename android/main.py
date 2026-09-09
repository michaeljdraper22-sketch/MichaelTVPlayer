"""MichaelTV for Android — app entry point.

The whole Stremio/Xtream brain lives in michaeltv_core/ (a verbatim
copy of the Windows app's src/, synced by android/prepare_core.py).
This package is only the Kivy UI around it.

Run python android/prepare_core.py FIRST (locally or in CI) so that
michaeltv_core/ exists next to this file before anything imports it.
"""

from kivy.app import App
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.uix.screenmanager import ScreenManager, SlideTransition

from mv.settings import Settings
from mv.home import HomeScreen
from mv.detail import DetailScreen
from mv.streams import StreamsScreen
from mv.player import PlayerScreen
from mv.settings_screen import SettingsScreen
from mv.livetv import LiveTVScreen

# Shared dark-theme widget styles used by every screen's KV. Loaded in
# build() before any screen is instantiated.
SHARED_KV = '''
<Toolbar@BoxLayout>:
    orientation: "horizontal"
    size_hint_y: None
    height: dp(50)
    padding: dp(4)
    spacing: dp(6)
    canvas.before:
        Color:
            rgba: 0.09, 0.09, 0.11, 1
        Rectangle:
            pos: self.pos
            size: self.size

<MButton@Button>:
    background_normal: ""
    background_down: ""
    background_color: 0.15, 0.15, 0.18, 1
    color: 1, 1, 1, 1
    bold: True
    font_size: "15sp"

<MInput@TextInput>:
    background_normal: ""
    background_active: ""
    background_color: 0.14, 0.14, 0.17, 1
    foreground_color: 1, 1, 1, 1
    cursor_color: 1, 1, 1, 1
    padding: dp(10), dp(10)

<MLabel@Label>:
    color: 0.85, 0.85, 0.88, 1

<MTitle@Label>:
    color: 1, 1, 1, 1
    bold: True
    font_size: "17sp"

<MSpinner@Spinner>:
    background_normal: ""
    background_down: ""
    background_color: 0.15, 0.15, 0.18, 1
    color: 1, 1, 1, 1
    bold: True

<MPoster@AsyncImage>:
    allow_stretch: True
    keep_ratio: True
'''


class MichaelTVApp(App):
    def build(self):
        self.title = "MichaelTV"
        self.settings = Settings.load(self.user_data_dir)
        Window.clearcolor = (0.05, 0.05, 0.06, 1)
        Builder.load_string(SHARED_KV)

        sm = ScreenManager(transition=SlideTransition())
        sm.add_widget(HomeScreen(name="home"))
        sm.add_widget(DetailScreen(name="detail"))
        sm.add_widget(StreamsScreen(name="streams"))
        sm.add_widget(PlayerScreen(name="player"))
        sm.add_widget(SettingsScreen(name="settings"))
        sm.add_widget(LiveTVScreen(name="livetv"))
        return sm

    # ---- navigation ----

    def go(self, name):
        if self.root.current != name:
            self.root.current = name

    def open_detail(self, item):
        detail = self.root.get_screen("detail")
        detail.item = item
        self.go("detail")

    def open_streams(self, payload):
        streams = self.root.get_screen("streams")
        streams.payload = payload
        streams._loaded_key = None          # force a fresh load
        self.go("streams")

    def open_player(self, url, title, back_to="streams"):
        player = self.root.get_screen("player")
        player.url = url
        player.title = title
        player.back_to = back_to or "streams"
        self.go("player")


if __name__ == "__main__":
    MichaelTVApp().run()
