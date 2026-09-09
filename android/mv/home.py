"""Home screen: combined movie + series search over Cinemeta."""

import re

from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import BooleanProperty
from kivy.uix.asyncimage import AsyncImage
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.screenmanager import Screen

from michaeltv_core import stremio

from .util import run_bg

# Interleave nothing — movies first, then series, each best-first (the
# Cinemeta search order); capped so one broad query can't build a
# thousand rows.
MAX_PER_KIND = 25

KV = '''
<HomeScreen>:
    canvas.before:
        Color:
            rgba: 0.05, 0.05, 0.06, 1
        Rectangle:
            pos: self.pos
            size: self.size

    BoxLayout:
        orientation: "vertical"

        Toolbar:
            MTitle:
                text: "MichaelTV"
            Widget:
            MButton:
                text: "Live TV"
                on_release: app.go("livetv")
            MButton:
                text: "Settings"
                on_release: app.go("settings")

        BoxLayout:
            orientation: "horizontal"
            size_hint_y: None
            height: dp(48)
            padding: dp(8), dp(4)
            spacing: dp(8)
            MInput:
                id: search_input
                hint_text: "Search movies & series…"
                multiline: False
                write_tab: False
                on_text_validate: root.do_search()
            MButton:
                id: search_btn
                text: "Search"
                size_hint_x: None
                width: dp(96)
                on_release: root.do_search()

        MLabel:
            id: status
            text: ""
            size_hint_y: None
            height: dp(22)
            padding_x: dp(10)

        ScrollView:
            GridLayout:
                id: results
                cols: 1
                size_hint_y: None
                height: self.minimum_height
                padding: dp(4), dp(4)
                spacing: dp(2), dp(2)
'''


def _catalog_search(catalog, query):
    """One Cinemeta catalog search (worker thread context).

    NOTE: this calls michaeltv_core.stremio._catalog_search directly —
    the shared helper behind the public search_movies()/search_series()
    — because the public series wrapper drops poster/year, and the home
    list wants thumbnails for series too. michaeltv_core is a verbatim,
    pinned copy of src/, so the helper's shape is frozen with this app.
    """
    return stremio._catalog_search(catalog, query)


def unified_search(query):
    """Combined movie + series results, best-first per kind (worker).

    Items: {kind, id, name, year, poster}. Mapping mirrors the public
    search_movies()/search_series() field rules exactly (type filter,
    year via the (19|20)dd regex on releaseInfo/year).
    """
    year_re = re.compile(r"(?:19|20)\d{2}")

    out = []
    for catalog, kind in (("movie", "movie"), ("series", "series")):
        for m in _catalog_search(catalog, query):
            if m.get("type") not in (None, kind):
                continue
            if not (m.get("id") and m.get("name")):
                continue
            ym = year_re.match(str(m.get("releaseInfo")
                                   or m.get("year") or ""))
            out.append({
                "kind": kind,
                "id": str(m["id"]),
                "name": str(m["name"]),
                "year": ym.group(0) if ym else "",
                "poster": str(m.get("poster") or ""),
            })
            if sum(1 for r in out if r["kind"] == kind) >= MAX_PER_KIND:
                break
    return out


class ResultRow(BoxLayout):
    """One search hit: poster thumb + name + year/type."""

    def __init__(self, item, on_open, **kwargs):
        super().__init__(**kwargs)
        self.orientation = "horizontal"
        self.size_hint_y = None
        self.height = dp(84)
        self.spacing = dp(8)
        self.item = item
        self._on_open = on_open

        thumb_box = BoxLayout(size_hint_x=None, width=dp(60),
                              padding=(dp(2), dp(2)))
        self.thumb = AsyncImage(source=item.get("poster") or "",
                                allow_stretch=True, keep_ratio=False)
        thumb_box.add_widget(self.thumb)
        self.add_widget(thumb_box)

        text_box = BoxLayout(orientation="vertical", padding=(0, dp(4)))
        kind_label = "Movie" if item["kind"] == "movie" else "Series"
        sub = " · ".join(p for p in (item.get("year") or "", kind_label) if p)
        text_box.add_widget(Label(
            text=item["name"], color=(1, 1, 1, 1),
            font_size="16sp", bold=True, halign="left", valign="middle",
            shorten=True, shorten_from="right"))
        text_box.add_widget(Label(
            text=sub, color=(0.6, 0.6, 0.65, 1),
            font_size="13sp", halign="left", valign="middle"))
        self.add_widget(text_box)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self._on_open(self.item)
            return True
        return super().on_touch_down(touch)


class HomeScreen(Screen):
    searching = BooleanProperty(False)

    def do_search(self):
        if self.searching:
            return                      # double-tap guard
        query = (self.ids.search_input.text or "").strip()
        if not query:
            return
        self.searching = True
        self.ids.search_btn.disabled = True
        self.ids.search_btn.text = "…"
        self.ids.status.text = "Searching…"
        self.ids.results.clear_widgets()
        run_bg(lambda: unified_search(query),
               on_result=lambda rows: self._show(query, rows),
               on_error=lambda exc: self._failed(query, exc))

    def _set_idle(self):
        self.searching = False
        self.ids.search_btn.disabled = False
        self.ids.search_btn.text = "Search"

    def _show(self, query, rows):
        self._set_idle()
        grid = self.ids.results
        grid.clear_widgets()
        for item in rows:
            grid.add_widget(ResultRow(item, self._open))
        if rows:
            movies = sum(1 for r in rows if r["kind"] == "movie")
            self.ids.status.text = (
                "%d movie(s), %d series" % (movies, len(rows) - movies))
        else:
            self.ids.status.text = "Nothing found for %r" % query

    def _failed(self, query, exc):
        self._set_idle()
        self.ids.status.text = "Search failed: %s" % (exc,)

    def _open(self, item):
        app = self._app()
        if app:
            app.open_detail(item)

    @staticmethod
    def _app():
        from kivy.app import App
        return App.get_running_app()


Builder.load_string(KV)
