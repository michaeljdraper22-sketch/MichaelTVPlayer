"""Streams screen: ranked stream list for a movie or episode.

Worker flow: addon_movie_streams / addon_streams (network) then
rank_streams (the shared English-preference + resolution + size +
provider ordering) — both take the Settings object directly, since it
implements the config facade stremio.py touches:

    stremio.addon_streams(config, imdb, season, episode)   (src :1035)
    stremio.addon_movie_streams(config, imdb)              (src :1045)
    stremio.rank_streams(config, streams, cur=None)        (src :1140)

Playability (rank_streams' usable() test, src :1169): entries with a
direct "url" play in-app; infoHash+fileIdx entries need a Stremio
streaming server (impossible on Android v1) and are greyed out.
"""

import re

from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import BooleanProperty, DictProperty
from kivy.uix.button import Button
from kivy.uix.screenmanager import Screen

from michaeltv_core import stremio

from .util import run_bg

TORRENT_NOTE = ("needs debrid-enabled addon URL "
                "(torrent entries can't play on Android v1)")

# Mirrors stremio._stream_parts' regexes (src :1055-1059) so the row
# detail line shows what the ranking actually scored.
_RES_RE = re.compile(r"\b(2160|1440|1080|720|480|360)(?:p|i)?\b", re.I)
_RES_ALIAS_RE = re.compile(r"\b(4k|8k|uhd)\b", re.I)
_SIZE_RE = re.compile(r"\U0001F4BE\s*([\d.]+)\s*(GB|MB)", re.I)

KV = '''
<StreamsScreen>:
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
                on_release: root.go_back()
            MTitle:
                id: title_lbl
                text: ""
                shorten: True
                shorten_from: "right"

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


def fetch_ranked(settings, payload):
    """Worker: merged addon streams, ranked best-first (network here)."""
    imdb = payload.get("imdb") or ""
    if payload.get("kind") == "movie":
        streams = stremio.addon_movie_streams(settings, imdb)
    else:
        streams = stremio.addon_streams(
            settings, imdb, int(payload.get("season") or 0),
            int(payload.get("episode") or 0))
    return stremio.rank_streams(settings, streams)


def stream_main_text(stream):
    """First human-readable line: the release name."""
    text = " ".join(str(stream.get(k) or "") for k in ("name", "title"))
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return "stream"


def stream_detail_text(stream):
    """Resolution · size · addon host, from the same text the ranker
    parses (name/title + behaviorHints.filename)."""
    text = " ".join(str(stream.get(k) or "") for k in ("name", "title"))
    text += " " + str(((stream.get("behaviorHints") or {})
                       .get("filename")) or "")
    res = ""
    m = _RES_RE.search(text)
    if m:
        res = m.group(1) + "p"
    elif _RES_ALIAS_RE.search(text):
        res = "2160p"
    size = ""
    m = _SIZE_RE.search(text)
    if m:
        gb = float(m.group(1)) / (1024.0 if m.group(2).upper() == "MB" else 1.0)
        size = ("%.1f GB" % gb) if gb >= 1 else "%.0f MB" % (gb * 1024)
    host = ""
    addon = str(stream.get("_addon") or "")
    if addon.startswith(("http://", "https://")):
        host = addon.split("//", 1)[1].split("/", 1)[0]
    return " · ".join(p for p in (res, size, host) if p)


class StreamRow(Button):
    """One stream entry; disabled+greyed when torrent-only."""

    def __init__(self, stream, on_play, **kwargs):
        main = stream_main_text(stream)
        detail = stream_detail_text(stream)
        playable = bool(stream.get("url"))
        if not playable:
            text = "[color=#8a8a92]%s\n%s[/color]" % (
                main.replace("[", "(").replace("]", ")"),
                TORRENT_NOTE)
        else:
            text = "%s\n[color=#9a9aa2]%s[/color]" % (
                main.replace("[", "(").replace("]", ")"), detail)
        super().__init__(
            text=text, markup=True,
            size_hint_y=None, height=dp(64),
            halign="left", valign="middle",
            **kwargs)
        self.stream = stream
        self.background_normal = ""
        self.background_down = ""
        if playable:
            self.background_color = (0.12, 0.12, 0.15, 1)
            self.color = (1, 1, 1, 1)
            self.bind(on_release=lambda _b: on_play(stream))
        else:
            self.disabled = True
            self.background_color = (0.08, 0.08, 0.10, 1)
            self.disabled_color = (0.45, 0.45, 0.5, 1)


class StreamsScreen(Screen):
    payload = DictProperty({})
    loading = BooleanProperty(False)

    def on_enter(self):
        payload = self.payload or {}
        title = payload.get("title") or "Streams"
        if payload.get("kind") != "movie":
            title += " S%02dE%02d" % (int(payload.get("season") or 0),
                                      int(payload.get("episode") or 0))
        self.ids.title_lbl.text = title
        if self.loading:
            return                      # worker already running
        key = (payload.get("kind"), payload.get("imdb"),
               int(payload.get("season") or 0), int(payload.get("episode") or 0))
        if getattr(self, "_loaded_key", None) == key and \
                self.ids.results.children:
            return                      # returning from the player
        self._loaded_key = key
        self.ids.results.clear_widgets()
        self.loading = True
        self.ids.status.text = "Finding streams…"
        app = self._app()
        run_bg(lambda: fetch_ranked(app.settings, payload),
               on_result=self._show,
               on_error=self._failed)

    def go_back(self):
        self._app().go("detail")

    def _show(self, ranked):
        self.loading = False
        grid = self.ids.results
        grid.clear_widgets()
        playable = 0
        for stream in ranked:
            grid.add_widget(StreamRow(stream, self._play))
            playable += 1 if stream.get("url") else 0
        if ranked:
            note = "" if playable else " (all torrent — %s)" % TORRENT_NOTE
            self.ids.status.text = "%d stream(s)%s" % (len(ranked), note)
        else:
            self.ids.status.text = "No streams found"

    def _failed(self, exc):
        self.loading = False
        self.ids.status.text = "Stream search failed: %s" % (exc,)

    def _play(self, stream):
        self._app().open_player(stream.get("url") or "",
                                 stream_main_text(stream),
                                 back_to="streams")

    @staticmethod
    def _app():
        from kivy.app import App
        return App.get_running_app()


Builder.load_string(KV)
