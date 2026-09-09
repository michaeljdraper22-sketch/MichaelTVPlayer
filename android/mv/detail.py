"""Detail screen: poster/title; season+episode picker for series,
"Find streams" action."""

from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import BooleanProperty, DictProperty
from kivy.uix.screenmanager import Screen

from michaeltv_core import stremio

from .util import run_bg

KV = '''
<DetailScreen>:
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
                id: find_btn
                text: "Find streams"
                disabled: True
                size_hint_x: None
                width: dp(150)
                on_release: root.find_streams()

        BoxLayout:
            orientation: "horizontal"
            spacing: dp(10)
            padding: dp(10), dp(6)
            size_hint_y: None
            height: dp(150)
            MPoster:
                id: poster
                source: ""
                size_hint_x: None
                width: dp(100)
            BoxLayout:
                orientation: "vertical"
                spacing: dp(4)
                MLabel:
                    id: title_lbl
                    text: ""
                    font_size: "20sp"
                    bold: True
                    halign: "left"
                    valign: "middle"
                MLabel:
                    id: sub_lbl
                    text: ""
                    font_size: "14sp"
                    color: 0.6, 0.6, 0.65, 1
                    halign: "left"
                    valign: "top"

        MSpinner:
            id: season_spinner
            text: ""
            values: []
            size_hint_y: None
            height: dp(44)
            opacity: 0 if not self.values else 1
            disabled: not self.values
            on_text: root.on_season(self.text)

        MLabel:
            id: status
            text: ""
            size_hint_y: None
            height: dp(22)
            padding_x: dp(10)

        ScrollView:
            GridLayout:
                id: episodes
                cols: 1
                size_hint_y: None
                height: self.minimum_height
                padding: dp(4), dp(4)
                spacing: dp(2), dp(2)
'''


class DetailScreen(Screen):
    item = DictProperty({})
    loading = BooleanProperty(False)

    # ---- lifecycle ----

    def on_enter(self):
        self._episode_rows = []   # buttons of the current season
        item = self.item or {}
        self.ids.title_lbl.text = item.get("name") or ""
        sub = " · ".join(p for p in (item.get("year") or "",
                                     "Movie" if item.get("kind") == "movie"
                                     else "Series") if p)
        self.ids.sub_lbl.text = sub
        self.ids.poster.source = item.get("poster") or ""
        self.ids.episodes.clear_widgets()
        self.ids.season_spinner.values = []
        self.ids.season_spinner.text = ""
        self._episodes = []            # [(season, episode, name)]
        self._selected = None
        self.ids.find_btn.disabled = True
        if item.get("kind") == "movie":
            self.ids.find_btn.disabled = False
            self.ids.status.text = ""
        else:
            self._load_meta()

    def _load_meta(self):
        if self.loading:
            return
        self.loading = True
        self.ids.status.text = "Loading episodes…"
        imdb = self.item.get("id") or ""
        run_bg(lambda: stremio.series_meta(imdb),
               on_result=self._meta_ready,
               on_error=self._meta_failed)

    def _meta_ready(self, meta):
        self.loading = False
        eps = []
        for v in (meta or {}).get("videos") or []:
            try:
                s = int(str(v.get("season", 0)).strip() or 0)
                e = int(str(v.get("episode", 0)).strip() or 0)
            except (TypeError, ValueError):
                continue
            if s >= 0 and e >= 1:
                eps.append((s, e, str(v.get("name") or "")))
        eps.sort(key=lambda t: (t[0], t[1]))
        self._episodes = eps
        seasons = sorted({s for s, _e, _n in eps})
        if not seasons:
            self.ids.status.text = "No episode list found for this series"
            return
        spinner = self.ids.season_spinner
        spinner.values = [
            ("Specials" if s == 0 else "Season %d" % s) for s in seasons]
        self._seasons = seasons
        spinner.text = spinner.values[0]      # triggers on_season()

    def _meta_failed(self, exc):
        self.loading = False
        self.ids.status.text = "Could not load episodes: %s" % (exc,)

    # ---- season / episode selection ----

    def on_season(self, text):
        if not getattr(self, "_seasons", None) or not text:
            return
        try:
            season = self._seasons[list(self.ids.season_spinner.values)
                                   .index(text)]
        except ValueError:
            return
        self._selected = None
        self.ids.find_btn.disabled = True
        grid = self.ids.episodes
        grid.clear_widgets()
        self._episode_rows = []
        for s, e, name in self._episodes:
            if s != season:
                continue
            row = self._make_episode_row(s, e, name)
            self._episode_rows.append(row)
            grid.add_widget(row)
        if not self._episode_rows:
            self.ids.status.text = "No episodes in %s" % text

    def _make_episode_row(self, season, episode, name):
        from kivy.uix.button import Button
        label = "S%02dE%02d · %s" % (season, episode, name or "")
        row = Button(
            text=label,
            size_hint_y=None, height=dp(46),
            halign="left", shorten=True, shorten_from="right",
        )
        row.ep_key = (season, episode)
        row.bind(on_release=lambda _b: self.select_episode(*row.ep_key))
        row.background_normal = ""
        row.background_down = ""
        row.background_color = (0.12, 0.12, 0.15, 1)
        row.color = (0.75, 0.75, 0.8, 1)
        return row

    def select_episode(self, season, episode):
        self._selected = (season, episode)
        self.ids.find_btn.disabled = False
        for row in self._episode_rows:
            sel = row.ep_key == (season, episode)
            row.bold = sel
            row.color = (1, 1, 0.7, 1) if sel else (0.75, 0.75, 0.8, 1)

    # ---- action ----

    def find_streams(self):
        item = self.item or {}
        if not item.get("id"):
            return
        season = episode = 0
        if item.get("kind") != "movie":
            if not self._selected:
                return
            season, episode = self._selected
        from kivy.app import App
        App.get_running_app().open_streams({
            "kind": item.get("kind"),
            "imdb": item["id"],
            "season": season,
            "episode": episode,
            "title": item.get("name") or "",
        })


Builder.load_string(KV)
