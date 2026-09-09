"""Settings screen: addon URL list, resolution preference, size demote,
Xtream credentials — persisted to settings.json on Save."""

from kivy.lang import Builder
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.screenmanager import Screen
from kivy.uix.textinput import TextInput

from .settings import RES_PREFS
from .util import toast

SHARED_KV = '''
<MHeader@Label>:
    color: 0.7, 0.7, 0.75, 1
    bold: True
    font_size: "15sp"
    size_hint_y: None
    height: dp(28)
    canvas.before:
        Color:
            rgba: 0.25, 0.25, 0.3, 1
        Rectangle:
            pos: self.x, self.y - dp(2)
            size: self.width, dp(1)

<MAddButton@Button>:
    text: "+ Add addon"
    size_hint_y: None
    height: dp(44)
    background_normal: ""
    background_down: ""
    background_color: 0.13, 0.2, 0.14, 1
    color: 1, 1, 1, 1
    bold: True
'''

KV = '''
<SettingsScreen>:
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
            Widget:
            MButton:
                text: "Save"
                size_hint_x: None
                width: dp(96)
                on_release: root.save()

        ScrollView:
            GridLayout:
                id: form
                cols: 1
                size_hint_y: None
                height: self.minimum_height
                padding: dp(10), dp(8)
                spacing: dp(4), dp(6)

                MHeader:
                    text: "Stream addons (priority order)"

                MLabel:
                    text: ("First addon wins ties. Paste a debrid-enabled "
                           "addon URL (with your key) so torrent streams "
                           "become playable direct links.")
                    font_size: "13sp"
                    color: 0.6, 0.6, 0.65, 1
                    size_hint_y: None
                    height: dp(58)
                    padding_x: dp(2)

                GridLayout:
                    id: addon_slots
                    cols: 1
                    size_hint_y: None
                    height: self.minimum_height
                    spacing: dp(4), dp(6)

                MAddButton:
                    on_release: root.add_addon_row("")

                MHeader:
                    text: "Resolution preference"
                MSpinner:
                    id: resolution
                    text: "1080"
                    values: []
                    size_hint_y: None
                    height: dp(44)

                MHeader:
                    text: "Demote streams larger than (GB, 0 = never)"
                MInput:
                    id: size_demote
                    text: "25"
                    multiline: False
                    input_filter: "int"
                    size_hint_y: None
                    height: dp(44)

                MHeader:
                    text: "Xtream / Live TV"
                MInput:
                    id: xt_server
                    hint_text: "Server URL (http://panel.example.com)"
                    multiline: False
                    write_tab: False
                    size_hint_y: None
                    height: dp(44)
                MInput:
                    id: xt_user
                    hint_text: "Username"
                    multiline: False
                    write_tab: False
                    size_hint_y: None
                    height: dp(44)
                MInput:
                    id: xt_pass
                    hint_text: "Password"
                    multiline: False
                    write_tab: False
                    password: True
                    size_hint_y: None
                    height: dp(44)

                Widget:
                    size_hint_y: None
                    height: dp(12)
'''


class AddonRow(BoxLayout):
    """One addon URL input + remove button."""

    def __init__(self, value, on_remove, **kwargs):
        super().__init__(**kwargs)
        self.orientation = "horizontal"
        self.size_hint_y = None
        self.height = dp(44)
        self.spacing = dp(6)
        self.input = TextInput(
            text=value or "", multiline=False, write_tab=False,
            size_hint_x=0.8, background_normal="",
            background_color=(0.14, 0.14, 0.17, 1),
            foreground_color=(1, 1, 1, 1), cursor_color=(1, 1, 1, 1))
        btn = Button(text="X", size_hint_x=None, width=dp(44),
                     background_normal="", background_down="",
                     background_color=(0.25, 0.12, 0.12, 1), color=(1, 1, 1, 1))
        btn.bind(on_release=lambda _b, row=self: on_remove(row))
        self.add_widget(self.input)
        self.add_widget(btn)


class SettingsScreen(Screen):

    def on_enter(self):
        st = self._app().settings
        slots = self.ids.addon_slots
        slots.clear_widgets()
        for base in st.addons:
            self._append_addon_row(slots, base)
        if not slots.children:
            self._append_addon_row(slots, "")

        self.ids.resolution.values = list(RES_PREFS)
        self.ids.resolution.text = (
            st.resolution_pref if st.resolution_pref in RES_PREFS
            else "1080")
        self.ids.size_demote.text = str(st.size_demote_gb)
        self.ids.xt_server.text = st.xtream_server
        self.ids.xt_user.text = st.xtream_username
        self.ids.xt_pass.text = st.xtream_password

    def add_addon_row(self, value):
        self._append_addon_row(self.ids.addon_slots, value)

    def _append_addon_row(self, slots, value):
        row = AddonRow(value, self.remove_addon_row)
        slots.add_widget(row)

    def remove_addon_row(self, row):
        if row.parent:
            row.parent.remove_widget(row)

    def save(self):
        app = self._app()
        addons = []
        # GridLayout.children is reverse-visual-order (bottom first);
        # the DISPLAY order is the addons' priority, so walk top-down.
        for row in reversed(self.ids.addon_slots.children):
            base = (row.input.text or "").strip().rstrip("/")
            if base.endswith("/manifest.json"):
                base = base[: -len("/manifest.json")].rstrip("/")
            if base.startswith(("http://", "https://")) \
                    and base not in addons:
                addons.append(base)
        try:
            size = int(self.ids.size_demote.text or "0")
        except ValueError:
            size = 25
        app.settings.replace({
            "addons": addons,
            "resolution_pref": self.ids.resolution.text or "1080",
            "size_demote_gb": max(0, min(500, size)),
            "xtream_server": self.ids.xt_server.text.strip(),
            "xtream_username": self.ids.xt_user.text.strip(),
            "xtream_password": self.ids.xt_pass.text.strip(),
        })
        toast(self, "Settings saved")

    @staticmethod
    def _app():
        from kivy.app import App
        return App.get_running_app()


Builder.load_string(SHARED_KV)
Builder.load_string(KV)
