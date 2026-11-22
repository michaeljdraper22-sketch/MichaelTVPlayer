"""Stremio catalog browse: the "Stremio Series" / "Stremio Movies" tabs.

Search Cinemeta straight from the channel panel and play the result with
the SAME stream preference the handoff path uses (rank_streams /
candidate_urls), so a pick here behaves exactly like a stream opened from
Stremio itself.

Text-only on purpose — no posters, no thumbnails. Cinemeta answers a
search in well under a second and a uniform-row QListWidget stays smooth
with the 300+ streams a popular movie lists, so a pick costs one search
plus one addon call.

Activation contract (matches the other browsers):
  * Enter / double-click a title -> play it with the ranked best stream
    (series open their episode list first; an episode then plays the
    same way)
  * right-click -> "Show all streams…" opens the full ranked list, where
    a SINGLE click starts that stream
"""

import logging

from PyQt5 import QtCore, QtWidgets

from .. import stremio
from .worker import AsyncRunner

log = logging.getLogger("mtp")

_GRAY = "#9aa0a6"
_BOLD_14 = "font-weight:bold; font-size:14px;"

# Tab labels: the glyph is what keeps the two tabs apart. ChromeTabBar
# equal-shares the bar and elides, and "Stremio Series"/"Stremio Movies"
# both elide to "Str…" at the panel's usual widths — two identical tabs
# side by side. The app already labels tabs with glyphs (★, ➕).
TAB_SERIES = "\U0001F4FA Stremio Series"     # 📺
TAB_MOVIES = "\U0001F3AC Stremio Movies"     # 🎬


def _stream_row_text(stream: dict) -> str:
    """One line per stream, the facts Stremio's own list shows: the
    quality banner from ``name`` (its first line is the addon, not the
    quality) then the release name and the seed/size line from
    ``title``, with the newlines turned into separators."""
    name = [ln.strip() for ln in str(stream.get("name") or "").splitlines()
            if ln.strip()]
    title = [ln.strip() for ln in str(stream.get("title") or "").splitlines()
             if ln.strip()]
    banner = name[1:] if len(name) > 1 else name
    parts = banner + title
    if not parts:
        hints = stream.get("behaviorHints") or {}
        parts = [str(hints.get("filename") or "Stream")]
    text = "  \u00b7  ".join(parts)
    provider = stremio.provider_of_stream(stream)
    if provider:
        # which debrid pipeline this row would go through — the one fact
        # the release name never says, and the usual reason to pick #2
        text += "   [%s]" % stremio.PROVIDER_LABELS.get(provider, provider)
    return text


class StremioCatalogBrowser(QtWidgets.QWidget):
    """A searchable Cinemeta catalog page: movies or series.

    ``kind`` is "movie" or "series"; everything below is shared."""

    media_activated = QtCore.pyqtSignal(dict)
    MIN_CHARS = 2
    SEARCH_DEBOUNCE_MS = 300

    def __init__(self, config, kind, parent=None):
        super().__init__(parent)
        self.config = config
        self.kind = "movie" if str(kind).lower() == "movie" else "series"
        self.results = []
        self._search_id = 0      # drops answers to superseded keystrokes
        self._loaded_query = ""  # the query the rows on screen belong to
        self._activate_when_loaded = False
        self._play_busy = False  # one pick resolution at a time
        self.search_runner = AsyncRunner()
        self.search_runner.finished.connect(self._on_search_loaded)
        self.play_runner = AsyncRunner()
        self.play_runner.finished.connect(self._on_play_loaded)
        self._build_ui()

    # ---- ui ----
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText(
            "Search Stremio movies…" if self.kind == "movie"
            else "Search Stremio shows…")
        self.search.setClearButtonEnabled(True)
        # Same debounce idea as BaseBrowser: Cinemeta is a network search,
        # so one request per settled query instead of one per keystroke.
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(self.SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(
            lambda: self._search(self.search.text()))
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        # Enter in the search box plays the first (filtered) row — the
        # row the user is looking at.
        self.search.returnPressed.connect(self._search_activate)
        layout.addWidget(self.search)

        self.list = QtWidgets.QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setUniformItemSizes(True)
        self.list.setTextElideMode(QtCore.Qt.ElideRight)
        self.list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.list.itemActivated.connect(self._activate)
        self.list.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.list, 1)

        self.status = QtWidgets.QLabel(self._idle_hint())
        self.status.setStyleSheet("color:%s;" % _GRAY)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def _idle_hint(self) -> str:
        return ("Type at least %d characters to search Stremio."
                % self.MIN_CHARS)

    # ---- search ----
    def _search_activate(self):
        """Enter in the search box: play the first row.

        Unlike the local list filters (BaseBrowser), a Cinemeta search is
        a network round trip and cannot be flushed synchronously — pressing
        Enter a keystroke after typing used to hit an empty list and do
        nothing. When the rows on screen do not belong to the current
        query, arm the activation and let it fire the moment the results
        land."""
        query = (self.search.text() or "").strip()
        if self._search_timer.isActive():
            self._search_timer.stop()
            self._search(query)
        if self.list.count() and self._loaded_query == query:
            self._activate(self.list.item(0))
        else:
            self._activate_when_loaded = True

    def _search(self, text):
        text = (text or "").strip()
        self._search_id += 1
        sid = self._search_id
        self._activate_when_loaded = False
        if len(text) < self.MIN_CHARS:
            self.results = []
            self._loaded_query = ""
            self.list.clear()
            self.status.setText(self._idle_hint())
            return
        self.status.setText("Searching\u2026")

        def _work():
            return sid, text, stremio.search_catalog(self.kind, text)

        self.search_runner.run(_work)

    def _on_search_loaded(self, result):
        ok, val = result
        if ok != "ok":
            self.status.setText("Search failed: %s" % val)
            return
        sid, query, hits = val
        if sid != self._search_id:
            return      # a newer query already answered
        self.results = list(hits or [])
        self._loaded_query = query
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        for hit in self.results:
            item = QtWidgets.QListWidgetItem(self._row_text(hit))
            item.setData(QtCore.Qt.UserRole, hit)
            self.list.addItem(item)
        self.list.setUpdatesEnabled(True)
        if self.results:
            # the first row is what Enter plays — show it selected so the
            # keyboard arrows and Enter have an obvious starting point
            self.list.setCurrentRow(0)
        if not self.results:
            self.status.setText("No matches.")
            return
        if self.kind == "movie":
            self.status.setText(
                "%d result(s) \u2014 Enter plays the first; right-click for "
                "the full stream list." % len(self.results))
        else:
            self.status.setText(
                "%d result(s) \u2014 Enter opens the first show's episodes."
                % len(self.results))
        if self._activate_when_loaded:
            self._activate_when_loaded = False
            self._activate(self.list.item(0))

    def _row_text(self, hit):
        name = str(hit.get("name") or "?")
        year = str(hit.get("year") or "")
        return "%s (%s)" % (name, year) if year else name

    # ---- activation ----
    def _activate(self, item):
        if not item:
            return
        hit = item.data(QtCore.Qt.UserRole)
        if not hit:
            return
        if self.kind == "movie":
            self._play_best(hit)
        else:
            self._open_episodes(hit)

    def _play_best(self, hit):
        """Resolve the top-ranked stream in the background, then play it."""
        if self._play_busy:
            return
        self._play_busy = True
        label = self._row_text(hit)
        self.status.setText("Finding the best stream for %s\u2026" % label)
        cur = stremio.catalog_pick(self.kind, hit)
        self.play_runner.run(stremio.best_playable, self.config, cur)

    def _on_play_loaded(self, result):
        self._play_busy = False
        ok, val = result
        if ok != "ok":
            self.status.setText("Could not find a stream: %s" % val)
            return
        if not val:
            self.status.setText(
                "No playable stream found \u2014 right-click to see the "
                "whole list.")
            return
        self.status.setText("Starting %s\u2026" % (val.get("title") or ""))
        self.media_activated.emit(val)

    def _open_episodes(self, hit):
        dlg = StremioEpisodesDialog(self.config, hit, self)
        dlg.media_activated.connect(self.media_activated)
        dlg.exec_()

    def _show_streams(self, cur):
        dlg = StremioStreamsDialog(self.config, cur, parent=self)
        picked = []
        dlg.media_activated.connect(picked.append)
        dlg.exec_()
        if picked:
            self.media_activated.emit(picked[0])

    def _context_menu(self, pos):
        item = self.list.itemAt(pos)
        if not item:
            return
        hit = item.data(QtCore.Qt.UserRole)
        if not hit:
            return
        menu = QtWidgets.QMenu(self)
        if self.kind == "movie":
            act_best = menu.addAction("Play (best stream)")
            act_all = menu.addAction("Show all streams\u2026")
        else:
            # a show has no single stream set: the picker opens episodes
            # AND that episode's full stream list side by side
            act_best = menu.addAction("Episodes & streams\u2026")
            act_all = None
        chosen = menu.exec_(self.list.viewport().mapToGlobal(pos))
        if chosen == act_best:
            self._activate(item)
        elif chosen is not None and chosen == act_all:
            self._show_streams(stremio.catalog_pick(self.kind, hit))

    # ---- external reload (F5 / post-account-save) ----
    def reload(self):
        """Repeat the current search for real — the Stremio tabs have no
        provider library to re-download, only the query on screen."""
        if self._search_timer.isActive():
            self._search_timer.stop()
        self._search(self.search.text())


class StremioStreamList(QtWidgets.QWidget):
    """The ranked source list for ONE movie or episode.

    Rows are in rank_streams order — the first is exactly what Enter /
    autoplay would pick (marked ★) — and ONE click starts that stream.
    Shared by the standalone Streams dialog (the player's "all streams"
    button, the movie right-click) and the series picker's right pane, so
    both always show the same list in the same order."""

    media_activated = QtCore.pyqtSignal(dict)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.cur = None
        self.meta = None
        self._seq = 0            # drops answers for a superseded episode
        self._loaded_fav = None  # what the rows on screen belong to
        self._busy = False       # one start resolution at a time
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.header = QtWidgets.QLabel("")
        self.header.setWordWrap(True)
        self.header.setStyleSheet("font-weight:bold; font-size:12px;")
        layout.addWidget(self.header)
        self.list = QtWidgets.QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setUniformItemSizes(True)
        self.list.setTextElideMode(QtCore.Qt.ElideRight)
        # SINGLE click plays: this list exists because the user chose to
        # pick a source themselves instead of letting autoplay choose
        self.list.itemClicked.connect(self._start)
        self.list.itemActivated.connect(self._start)   # Enter / dbl-click
        layout.addWidget(self.list, 1)
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color:%s;" % _GRAY)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.runner = AsyncRunner()
        self.runner.finished.connect(self._on_loaded)
        self.play_runner = AsyncRunner()
        self.play_runner.finished.connect(self._on_play_loaded)

    def load(self, cur, meta=None):
        """Show the ranked streams for ``cur`` (a catalog_pick or a live
        playable — anything carrying stremio_imdb + movie/season/episode)."""
        cur = dict(cur or {})
        self.cur = cur
        self.meta = meta
        self._loaded_fav = None
        self._seq += 1
        seq = self._seq
        if not cur.get("stremio_imdb"):
            self.list.clear()
            self.header.setText("")
            self.status.setText(
                "This stream is still being identified \u2014 no list yet.")
            return
        self.header.setText(str(cur.get("title") or ""))
        self.list.clear()
        self.status.setText("Loading streams\u2026")
        self.runner.run(self._fetch, seq, cur, meta)

    def load_if_new(self, cur, meta=None):
        """load(), but only when ``cur`` is a different item from the one
        the rows currently belong to — arrowing through an episode list
        must not re-issue a fetch per keypress."""
        key = (cur or {}).get("fav_key")
        if key and key == self._loaded_fav and self.list.count():
            return
        self.load(cur, meta)

    def _fetch(self, seq, cur, meta):
        return seq, stremio.ranked_streams(self.config, cur, meta)

    def _on_loaded(self, result):
        ok, val = result
        if ok != "ok":
            self.status.setText("Could not load streams: %s" % val)
            return
        seq, streams = val
        if seq != self._seq:
            return          # a newer episode already answered
        streams = list(streams or [])
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        for idx, stream in enumerate(streams):
            text = _stream_row_text(stream)
            if idx == 0:
                text = "\u2605 " + text      # Enter's pick
            item = QtWidgets.QListWidgetItem(text)
            item.setData(QtCore.Qt.UserRole, stream)
            self.list.addItem(item)
        self.list.setUpdatesEnabled(True)
        self._loaded_fav = (self.cur or {}).get("fav_key")
        if not streams:
            self.status.setText(
                "No playable streams for this title right now.")
            return
        self.list.setCurrentRow(0)
        self.status.setText(
            "%d stream(s) \u2014 click one to play." % len(streams))

    def _start(self, item):
        if not item or self._busy or not self.cur:
            return
        stream = item.data(QtCore.Qt.UserRole)
        if not stream:
            return
        self._busy = True
        self.list.setEnabled(False)
        self.status.setText("Checking that stream\u2026")
        # probe=True: a manually picked row is worth one ranged GET —
        # a dead debrid resolve must say so here, not start a 30 s
        # failure video in the player
        self.play_runner.run(stremio.stream_playable, self.config,
                             dict(self.cur), stream, None, True)

    def _on_play_loaded(self, result):
        ok, val = result
        if ok != "ok" or not val:
            # e.g. the local streaming engine refused to create the
            # torrent — the list stays usable so another row can be tried
            self._busy = False
            self.list.setEnabled(True)
            if ok != "ok":
                self.status.setText("Could not start that stream: %s" % val)
            else:
                self.status.setText(
                    "That stream could not be started \u2014 try another one.")
            return
        self.status.setText("Starting %s\u2026" % (val.get("title") or ""))
        self.media_activated.emit(val)


class StremioStreamsDialog(QtWidgets.QDialog):
    """The whole ranked stream list for one movie/episode, standalone.

    One SINGLE click starts that stream (right-click -> this dialog ->
    click is the manual override). The first row is exactly what Enter
    would have played."""

    media_activated = QtCore.pyqtSignal(dict)

    def __init__(self, config, cur, meta=None, parent=None):
        super().__init__(parent)
        self.cur = dict(cur or {})
        heading = str(self.cur.get("title") or "Streams")
        self.setWindowTitle("Streams \u2014 %s" % heading)
        self.resize(780, 620)
        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel(heading)
        title.setStyleSheet(_BOLD_14)
        title.setWordWrap(True)
        layout.addWidget(title)
        hint = QtWidgets.QLabel(
            "Ranked by your stream preferences. Click a stream to play it.")
        hint.setStyleSheet("color:%s;" % _GRAY)
        layout.addWidget(hint)
        self.streams = StremioStreamList(config)
        self.streams.header.hide()      # the dialog heading already says it
        layout.addWidget(self.streams, 1)
        self.streams.media_activated.connect(self._on_picked)
        self.streams.load(self.cur, meta)

    def _on_picked(self, playable):
        self.media_activated.emit(playable)
        self.accept()


class StremioEpisodesDialog(QtWidgets.QDialog):
    """Two-pane picker for one Stremio show.

    Left: Cinemeta's season/episode tree. Right: the FULL ranked stream
    list for the episode highlighted on the left — Stremio web's own
    series flow. A single click on a stream plays it; Enter / double-click
    on an episode plays its best stream. Nothing plays until one of those
    happens."""

    media_activated = QtCore.pyqtSignal(dict)

    def __init__(self, config, hit, parent=None):
        super().__init__(parent)
        self.config = config
        self.hit = dict(hit or {})
        self.meta = {}
        self._busy = False
        name = str(self.hit.get("name") or "Series")
        year = str(self.hit.get("year") or "")
        heading = name + (" (%s)" % year if year else "")
        self.setWindowTitle(heading)
        self.resize(1020, 660)

        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel(heading)
        title.setStyleSheet(_BOLD_14)
        layout.addWidget(title)
        self.plot = QtWidgets.QLabel("")
        self.plot.setWordWrap(True)
        self.plot.setStyleSheet("color:%s;" % _GRAY)
        layout.addWidget(self.plot)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Episode", "Title"])
        self.tree.itemActivated.connect(self._activate)
        # highlighting an episode is what fills the stream pane: the user
        # browses the sources without committing to anything
        self.tree.currentItemChanged.connect(self._on_current)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        split.addWidget(self.tree)
        self.streams = StremioStreamList(config)
        self.streams.media_activated.connect(self._on_stream_picked)
        split.addWidget(self.streams)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 660])
        layout.addWidget(split, 1)

        self.status = QtWidgets.QLabel("Loading episodes\u2026")
        self.status.setStyleSheet("color:%s;" % _GRAY)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.runner = AsyncRunner()
        self.runner.finished.connect(self._on_loaded)
        self.play_runner = AsyncRunner()
        self.play_runner.finished.connect(self._on_play_loaded)
        imdb = str(self.hit.get("id") or "")
        self.runner.run(self._load_episodes, imdb)

    @staticmethod
    def _load_episodes(imdb):
        # episode_map already reads the cached meta; returning it keeps
        # every later catalog_pick() free of even a cached call
        return stremio.episode_map(imdb), stremio.series_meta(imdb)

    def _on_loaded(self, result):
        ok, val = result
        if ok != "ok":
            self.status.setText("Could not load episodes: %s" % val)
            return
        rows, meta = val if isinstance(val, tuple) else ([], {})
        self.meta = meta or {}
        plot = str(self.meta.get("description") or "").strip()
        if plot:
            self.plot.setText(plot[:300] + ("\u2026" if len(plot) > 300
                                            else ""))
        self.tree.clear()
        total = 0
        for season, episodes in rows or []:
            label = "Specials" if int(season) == 0 else "Season %d" % season
            root = QtWidgets.QTreeWidgetItem([label, ""])
            self.tree.addTopLevelItem(root)
            for episode, ep_title in episodes:
                node = QtWidgets.QTreeWidgetItem(
                    ["S%d E%d" % (season, episode), ep_title])
                node.setData(0, QtCore.Qt.UserRole, {
                    "season": int(season), "episode": int(episode),
                    "title": ep_title,
                })
                root.addChild(node)
                total += 1
            root.setExpanded(True)
        if not total:
            self.status.setText("No episodes found for this show.")
            return
        # the first episode's streams load as part of opening, so the
        # right pane is never blank (and Enter has something to play)
        first = self.tree.topLevelItem(0)
        if first is not None and first.childCount():
            self.tree.setCurrentItem(first.child(0))
        self.tree.setFocus(QtCore.Qt.OtherFocusReason)
        self.status.setText(
            "%d season(s), %d episode(s) \u2014 highlight an episode for "
            "its streams; click one to play, or Enter for the best pick."
            % (len(rows), total))

    # ---- streams follow the highlighted episode ----
    def _on_current(self, item, _prev=None):
        data = item.data(0, QtCore.Qt.UserRole) if item else None
        if not data:
            return          # a season header — keep the pane as it was
        self.streams.load_if_new(self._cur_for(data), self.meta)

    def _cur_for(self, data):
        return stremio.catalog_pick(
            "series", self.hit, data.get("season"), data.get("episode"),
            meta=self.meta)

    def _on_stream_picked(self, playable):
        self.media_activated.emit(playable)
        self.accept()

    # ---- Enter / double-click an episode: its best stream ----
    def _activate(self, item, _col=0):
        data = item.data(0, QtCore.Qt.UserRole) if item else None
        if not data:
            return          # a season header
        self._play(data)

    def _play(self, data):
        if self._busy:
            return
        season = int(data.get("season") or 0)
        episode = int(data.get("episode") or 0)
        cur = self._cur_for(data)
        self._busy = True
        self.status.setText("Finding the best stream for S%02dE%02d\u2026"
                            % (season, episode))
        self.play_runner.run(stremio.best_playable, self.config, cur,
                             self.meta)

    def _on_play_loaded(self, result):
        self._busy = False
        ok, val = result
        if ok != "ok":
            self.status.setText("Could not find a stream: %s" % val)
            return
        if not val:
            self.status.setText(
                "No playable stream for that episode \u2014 the stream list "
                "on the right shows what the addons returned.")
            return
        self.status.setText("Starting %s\u2026" % (val.get("title") or ""))
        self.media_activated.emit(val)
        self.accept()

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        data = item.data(0, QtCore.Qt.UserRole) if item else None
        if not data:
            return
        menu = QtWidgets.QMenu(self)
        act_best = menu.addAction("Play (best stream)")
        act_all = menu.addAction("Open the stream list in its own window")
        chosen = menu.exec_(self.tree.viewport().mapToGlobal(pos))
        if chosen == act_best:
            self._play(data)
        elif chosen is not None and chosen == act_all:
            dlg = StremioStreamsDialog(self.config, self._cur_for(data),
                                       meta=self.meta, parent=self)
            picked = []
            dlg.media_activated.connect(picked.append)
            dlg.exec_()
            if picked:
                self.media_activated.emit(picked[0])
                self.accept()
