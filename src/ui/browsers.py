"""Channel/movie/series browsers, favorites and custom-channel lists."""

import html
import math
import time
import urllib.parse
from datetime import datetime

from PyQt5 import QtCore, QtWidgets

from ..filters import extract_country
from ..xtream import decode_epg_text
from .worker import AsyncRunner


def episode_title(name, season, episode, ep_title):
    """Compose a series-episode display title. Providers often name the
    episode with the series name (and even the SxEy code) already baked
    in — "4K-NF - Ransom Canyon (2025) (US) - S1E4" — so blindly
    prefixing "{series} - S1E4 " printed the whole thing twice. If the
    episode title already carries the series name, use it as-is."""
    ep_title = (ep_title or "").strip() or f"Episode {episode}"
    name = (name or "").strip()
    if not name or name.lower() in ep_title.lower():
        return ep_title
    compact = ep_title.lower().replace(" ", "")
    if f"s{season}e{episode}".lower() in compact:
        return f"{name} - {ep_title}"
    return f"{name} - S{season}E{episode} {ep_title}"


class BaseBrowser(QtWidgets.QWidget):
    """Base class for the Live / Movies / Series content tabs.

    Subclasses implement fetch_categories / fetch_items / item_display /
    fav_key_of / make_playable. Browsing runs on a background thread.

    Country / region filtering: subclasses set ``country_prefix`` to the
    matching config key prefix ("" for the legacy Live TV keys, "vod_",
    "series_"); both the category dropdown and the item list are then
    filtered by the countries enabled in the Countries dialog (saved in
    settings, so the choice survives restarts).
    """

    media_activated = QtCore.pyqtSignal(dict)
    favorite_changed = QtCore.pyqtSignal()
    RECENT_LABEL = "★ Recently Played"
    # Every browser opens on "All".  Movies / Series set ``big_library``
    # because their full list is a multi-MB download that can take a while —
    # those show a "big list" hint while it loads.
    big_library = False
    country_prefix = None      # None = no country filtering

    def __init__(self, config, client, kind):
        super().__init__()
        self.config = config
        self.client = client
        self.kind = kind
        self.all_items = []
        self._all_cats = None   # full unfiltered category list
        self._playable_mode = False
        self._mode = "cats"
        self.runner = AsyncRunner()
        self.runner.finished.connect(self._on_loaded)
        self._build_ui()
        self._reload_categories()

    # ---- to be overridden ----
    def fetch_categories(self):
        return []

    def fetch_items(self, cat_id):
        return []

    def item_display(self, it):
        return str(it)

    def fav_key_of(self, it):
        return None

    def make_playable(self, it):
        return {"kind": self.kind, "title": self.item_display(it)}

    def _search_activate(self):
        """Enter in the search box: activate the first row of the current
        (filtered) list. Flushes the 250 ms filter debounce first so the
        list matches what was typed. Playback activation moves keyboard
        focus to the player, so Space goes back to pause/play instead of
        typing into the filter."""
        if self._search_timer.isActive():
            self._search_timer.stop()
            self._apply_filter(self.search.text())
        if self.list.count() > 0:
            self._activate(self.list.item(0))

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        # Debounce search: rebuilding a 10k-item list on every keystroke lagged
        # the whole UI.
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(
            lambda: self._apply_filter(self.search.text())
        )
        self.search.textChanged.connect(
            lambda _t: self._search_timer.start()
        )
        # Enter in the search box plays the first filtered row — the row
        # the user is looking at (see _search_activate).
        self.search.returnPressed.connect(self._search_activate)
        self.cat_combo = QtWidgets.QComboBox()
        self.cat_combo.currentIndexChanged.connect(self._on_category)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.cat_combo, 2)
        top.addWidget(self.search, 3)
        layout.addLayout(top)
        self.list = QtWidgets.QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setUniformItemSizes(True)   # big speedup for long lists
        self.list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.list.itemActivated.connect(self._activate)
        self.list.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.list, 1)
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color:#9aa0a6;")
        layout.addWidget(self.status)

    def _reload_categories(self):
        self.status.setText("Loading categories…")
        self._mode = "cats"
        self.runner.run(self.fetch_categories)

    def _on_category(self, _idx):
        if self.cat_combo.count() == 0:
            return
        self._load_current()

    def _load_current(self):
        key = self.cat_combo.itemData(self.cat_combo.currentIndex())
        self.list.clear()
        self.all_items = []
        if key == "recent":
            self._show_playables(
                [r for r in self.config.recents if r.get("kind") == self.kind]
            )
            return
        if key in (None, "all") and self.big_library:
            self.status.setText(
                "Loading the full library… (big list — can take a minute)")
        else:
            self.status.setText("Loading…")
        self._mode = "items"
        cat_arg = None if key in (None, "all") else key
        self.runner.run(self.fetch_items, cat_arg)

    def _on_loaded(self, result):
        ok, val = result
        if ok != "ok":
            self.status.setText(f"Error: {val}")
            return
        if self._mode == "cats":
            self._populate_categories(val or [])
        else:
            self._show_items(val or [])

    # ---- country / region filtering ----
    def _country_allowed(self):
        """Set of enabled country tokens, or None when filtering is off."""
        if self.country_prefix is None or not self._all_cats:
            return None
        try:
            if not getattr(self.config,
                           self.country_prefix + "countries_configured"):
                return None
            allowed = set(getattr(
                self.config, self.country_prefix + "enabled_countries"))
        except AttributeError:
            return None
        return allowed or None

    def _populate_categories(self, cats):
        # Remember the FULL unfiltered list (item-level filtering maps
        # streams back to countries through it), then filter the dropdown.
        self._all_cats = list(cats or [])
        allowed = self._country_allowed()
        if allowed is not None:
            cats = [c for c in self._all_cats
                    if extract_country(c.get("category_name", "")) in allowed]
        self.cat_combo.blockSignals(True)
        self.cat_combo.clear()
        self.cat_combo.addItem(self.RECENT_LABEL, "recent")
        self.cat_combo.addItem("All", "all")
        for cat in cats:
            cid = cat.get("category_id")
            name = cat.get("category_name", str(cid))
            self.cat_combo.addItem(name, cid)
        # Default selection: always "All" (index 1, right after the
        # Recently Played entry) for Live, Movies and Series alike.
        self.cat_combo.setCurrentIndex(1)
        self.cat_combo.blockSignals(False)
        # Always reload explicitly: if the index was already on "All" the
        # currentIndexChanged signal never fires and the list would stay stale
        # (this is what kept unfiltered channels visible).
        self._load_current()

    def _show_items(self, items):
        # Belt-and-suspenders: enforce the country filter at item level too,
        # so unselected countries never appear even in the "All" view.
        allowed = self._country_allowed()
        if allowed is not None:
            good = {
                str(c.get("category_id"))
                for c in self._all_cats
                if extract_country(c.get("category_name", "")) in allowed
            }
            items = [it for it in (items or [])
                     if str(it.get("category_id")) in good]
        self._playable_mode = False
        self.all_items = items or []
        self._apply_filter(self.search.text())

    def _show_playables(self, playables):
        self._playable_mode = True
        text = (self.search.text() or "").strip().lower()
        self.list.clear()
        for p in playables:
            disp = p.get("title", "")
            if text and text not in disp.lower():
                continue
            item = QtWidgets.QListWidgetItem(disp)
            item.setData(QtCore.Qt.UserRole, p)
            self.list.addItem(item)
        self.status.setText(f"{self.list.count()} items")

    def _apply_filter(self, text=""):
        if self._playable_mode:
            self._show_playables(
                [r for r in self.config.recents if r.get("kind") == self.kind]
            )
            return
        text = (text or "").strip().lower()
        # Batch the rebuild: no repaints/layout per inserted row.
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        for it in self.all_items:
            disp = self.item_display(it)
            if text and text not in disp.lower():
                continue
            key = self.fav_key_of(it)
            if key and self.config.is_favorite(key):
                disp = "★ " + disp
            item = QtWidgets.QListWidgetItem(disp)
            item.setData(QtCore.Qt.UserRole, it)
            self.list.addItem(item)
        self.list.setUpdatesEnabled(True)
        self.status.setText(f"{self.list.count()} of {len(self.all_items)} items")

    def _activate(self, wi):
        if not wi:
            return
        data = wi.data(QtCore.Qt.UserRole)
        if self._playable_mode:
            self.media_activated.emit(data)
        else:
            self.media_activated.emit(self.make_playable(data))

    def _context_menu(self, pos):
        wi = self.list.itemAt(pos)
        if not wi:
            return
        data = wi.data(QtCore.Qt.UserRole)
        playable = data if self._playable_mode else self.make_playable(data)
        menu = QtWidgets.QMenu(self)
        act_play = menu.addAction("Play")
        act_fav = None
        if playable.get("fav_key"):
            is_fav = self.config.is_favorite(playable["fav_key"])
            act_fav = menu.addAction(
                "Remove from Favorites" if is_fav else "Add to Favorites"
            )
        chosen = menu.exec_(self.list.viewport().mapToGlobal(pos))
        if chosen == act_play:
            self.media_activated.emit(playable)
        elif chosen == act_fav:
            self.config.toggle_favorite(playable)
            self.favorite_changed.emit()
            self._apply_filter(self.search.text())


class LiveBrowser(BaseBrowser):
    # legacy config keys: enabled_countries / countries_configured
    country_prefix = ""

    def fetch_categories(self):
        return self.client.live_categories()

    def fetch_items(self, cat_id):
        # item-level country filtering happens in _show_items (base class)
        return self.client.live_streams(cat_id)

    def item_display(self, it):
        return it.get("name", "")

    def fav_key_of(self, it):
        sid = it.get("stream_id")
        return f"live:{sid}" if sid is not None else None

    def make_playable(self, it):
        sid = it.get("stream_id")
        return {
            "kind": "live",
            "title": it.get("name", ""),
            "url": self.client.live_url(sid),
            "stream_id": sid,
            "fav_key": self.fav_key_of(it),
            "icon": it.get("stream_icon", ""),
        }


class VodBrowser(BaseBrowser):
    # Movie libraries can hold 100k+ items — the "All" fetch is slow, so it
    # gets a long timeout and the "big list" loading hint.
    big_library = True
    country_prefix = "vod_"

    def fetch_categories(self):
        return self.client.vod_categories()

    def fetch_items(self, cat_id):
        # The "All" fetch downloads the whole library: give it a long rope.
        return self.client.vod_streams(
            cat_id, timeout=None if cat_id else 90)

    def item_display(self, it):
        return it.get("name", "")

    def fav_key_of(self, it):
        sid = it.get("stream_id")
        return f"vod:{sid}" if sid is not None else None

    def make_playable(self, it):
        sid = it.get("stream_id")
        ext = it.get("container_extension") or "mp4"
        return {
            "kind": "vod",
            "title": it.get("name", ""),
            "url": self.client.vod_url(sid, ext),
            "fav_key": self.fav_key_of(it),
            "icon": it.get("stream_icon", ""),
        }


class SeriesBrowser(BaseBrowser):
    # Series libraries can be as huge as the movie one — long timeout and
    # "big list" hint for the "All" fetch.
    big_library = True
    country_prefix = "series_"

    def fetch_categories(self):
        return self.client.series_categories()

    def fetch_items(self, cat_id):
        # "All" downloads the whole catalogue: give it a long rope.
        return self.client.series(
            cat_id, timeout=None if cat_id else 90)

    def item_display(self, it):
        return it.get("name", "")

    def fav_key_of(self, it):
        sid = it.get("series_id")
        return f"series:{sid}" if sid is not None else None

    def make_playable(self, it):
        return {
            "kind": "series_meta",
            "title": it.get("name", ""),
            "series_id": it.get("series_id"),
            "fav_key": self.fav_key_of(it),
        }

    def _activate(self, wi):
        if not wi:
            return
        data = wi.data(QtCore.Qt.UserRole)
        if self._playable_mode:
            self.media_activated.emit(data)
        else:
            self._open_series(data)

    def _open_series(self, series_item):
        dlg = SeriesEpisodesDialog(self.client, self.config, series_item, self)
        dlg.media_activated.connect(self.media_activated)
        dlg.favorite_changed.connect(self.favorite_changed)
        dlg.exec_()

    def _context_menu(self, pos):
        wi = self.list.itemAt(pos)
        if not wi:
            return
        data = wi.data(QtCore.Qt.UserRole)
        if self._playable_mode:
            playable = data
            menu = QtWidgets.QMenu(self)
            act_play = menu.addAction("Play")
            act_rm = None
            if playable.get("fav_key") and self.config.is_favorite(playable["fav_key"]):
                act_rm = menu.addAction("Remove from Favorites")
            chosen = menu.exec_(self.list.viewport().mapToGlobal(pos))
            if chosen == act_play:
                self.media_activated.emit(playable)
            elif chosen == act_rm:
                self.config.toggle_favorite(playable)
                self.favorite_changed.emit()
        else:
            self._open_series(data)


class SeriesEpisodesDialog(QtWidgets.QDialog):
    media_activated = QtCore.pyqtSignal(dict)
    favorite_changed = QtCore.pyqtSignal()

    def __init__(self, client, config, series, parent=None):
        super().__init__(parent)
        self.client = client
        self.config = config
        # Favorited series arrive as {"kind": "series_meta", "title": ...}
        # without a "name" key — normalize so titles/windows always work.
        self.series = dict(series or {})
        self.series.setdefault("name", self.series.get("title", "Series"))
        self.setWindowTitle(self.series.get("name", "Series"))
        self.resize(660, 620)

        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel(self.series.get("name", ""))
        title.setStyleSheet("font-weight:bold; font-size:14px;")
        self.plot = QtWidgets.QLabel("")
        self.plot.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self.plot)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Episode", "Title"])
        self.tree.itemActivated.connect(self._on_double)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context)
        layout.addWidget(self.tree, 1)

        self.status = QtWidgets.QLabel("Loading episodes…")
        self.status.setStyleSheet("color:#666;")
        layout.addWidget(self.status)

        self.runner = AsyncRunner()
        self.runner.finished.connect(self._on_loaded)
        self.runner.run(self.client.series_info, series.get("series_id"))

    def _on_loaded(self, result):
        ok, val = result
        if ok != "ok":
            self.status.setText(f"Error: {val}")
            return
        val = val if isinstance(val, dict) else {}
        info = val.get("info", {}) or {}
        if info.get("plot"):
            self.plot.setText(info.get("plot"))
        self.tree.clear()

        def _num(v):
            try:
                return int(str(v).strip())
            except (TypeError, ValueError):
                return 0

        # Modern Xtream shape: episodes live in a dict keyed by season
        # number  {"episodes": {"1": [ep, ...], "2": [...]}} — the "seasons"
        # array is only metadata (names/covers), it has NO episode lists.
        # Older panels embedded them as seasons[]["episode"]; support both.
        seasons_meta = val.get("seasons") or []
        meta_names = {}
        for sm in seasons_meta:
            if sm.get("season_number") is not None:
                meta_names[str(sm.get("season_number"))] = (
                    sm.get("name") or f"Season {sm.get('season_number')}")

        rows = []          # (season_no, label, episodes)
        ep_map = val.get("episodes")
        if isinstance(ep_map, dict) and ep_map:
            for key, eps in ep_map.items():
                label = meta_names.get(str(key), f"Season {key}")
                rows.append((_num(key), label,
                             sorted(eps or [], key=lambda e: _num(
                                 e.get("episode_num")))))
        else:
            for sm in seasons_meta:
                eps = sm.get("episode") or []
                if not eps:
                    continue
                no = sm.get("season_number", "?")
                rows.append((_num(no), sm.get("name") or f"Season {no}",
                             sorted(eps, key=lambda e: _num(
                                 e.get("episode_num")))))
        rows.sort(key=lambda r: r[0])

        total = 0
        for no, label, eps in rows:
            root = QtWidgets.QTreeWidgetItem([label, ""])
            self.tree.addTopLevelItem(root)
            for ep in eps:
                epnum = ep.get("episode_num", "")
                title = ep.get("title", "") or f"Episode {epnum}"
                node = QtWidgets.QTreeWidgetItem(
                    [f"S{no} E{epnum}", title])
                node.setData(0, QtCore.Qt.UserRole, {
                    "series_name": self.series.get("name", ""),
                    "season": no,
                    "episode": epnum,
                    "title": title,
                    "id": ep.get("id"),
                    "container_extension": ep.get("container_extension", "mp4"),
                    "info": ep.get("info", {}),
                })
                root.addChild(node)
                total += 1
            root.setExpanded(True)
        if not rows or not total:
            self.status.setText("No episodes found.")
            return
        self.status.setText(f"{len(rows)} season(s), {total} episode(s)")

    def _make_playable(self, ep):
        return {
            "kind": "series",
            "title": episode_title(ep["series_name"], ep["season"],
                                   ep["episode"], ep["title"]),
            "url": self.client.series_url(ep["id"], ep.get("container_extension", "mp4")),
            "fav_key": f"episode:{ep['id']}",
            "icon": (ep.get("info") or {}).get("movie_image", ""),
            # identity for "play next episode": the next fetch re-reads
            # series_info and walks the ordered season/episode list
            "series_id": self.series.get("series_id"),
            "series_name": ep.get("series_name", ""),
            "season": ep.get("season"),
            "episode": ep.get("episode"),
        }

    def _on_double(self, item, _col):
        data = item.data(0, QtCore.Qt.UserRole)
        if not data:
            return
        playable = self._make_playable(data)
        self.config.add_recent(playable)
        self.media_activated.emit(playable)
        self.accept()

    def _context(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        data = item.data(0, QtCore.Qt.UserRole)
        if not data:
            return
        playable = self._make_playable(data)
        menu = QtWidgets.QMenu(self)
        act_play = menu.addAction("Play")
        is_fav = self.config.is_favorite(playable["fav_key"])
        act_fav = menu.addAction("Remove from Favorites" if is_fav else "Add to Favorites")
        chosen = menu.exec_(self.tree.viewport().mapToGlobal(pos))
        if chosen == act_play:
            self.config.add_recent(playable)
            self.media_activated.emit(playable)
            self.accept()
        elif chosen == act_fav:
            self.config.toggle_favorite(playable)
            self.favorite_changed.emit()


def _epg_text(s: str) -> str:
    """Base64 + URL/HTML encoded EPG text -> readable string."""
    text = decode_epg_text(s or "")
    try:
        text = urllib.parse.unquote(text)
    except Exception:  # noqa: BLE001
        pass
    return html.unescape(text)


class CatchupBrowser(BaseBrowser):
    """Catch-Up tab: live channels whose provider archive is enabled
    (tv_archive == 1).  Activating a channel opens the archive picker
    (CatchupPickerDialog) listing its past programs; picking one plays the
    provider's recorded broadcast via the timeshift URL."""

    # NO country filter (country_prefix None): the archive list is small
    # (~1k channels) and the archive-capable channels cluster in country
    # groups the Live filter may have deselected — sharing it rendered
    # this tab empty on a filtered config (measured: 1014 archive
    # channels, 0 visible with a 4K/8K/US Live filter).
    country_prefix = None

    def fetch_categories(self):
        return self.client.live_categories()

    def fetch_items(self, cat_id):
        # only archive-capable channels have anything to catch up on
        return [c for c in (self.client.live_streams(cat_id) or [])
                if str(c.get("tv_archive")) == "1"]

    def item_display(self, it):
        dur = it.get("tv_archive_duration")
        base = it.get("name", "")
        if dur:
            return f"{base}  ({dur}d)"
        return base

    def fav_key_of(self, it):
        sid = it.get("stream_id")
        return f"catchup:{sid}" if sid is not None else None

    def make_playable(self, it):
        return {
            "kind": "catchup_channel",
            "title": it.get("name", ""),
            "stream_id": it.get("stream_id"),
            "archive_days": it.get("tv_archive_duration"),
            "fav_key": self.fav_key_of(it),
            "icon": it.get("stream_icon", ""),
        }

    def _activate(self, wi):
        if wi:
            self._open_picker(wi.data(QtCore.Qt.UserRole))

    def _open_picker(self, channel):
        if not channel:
            return
        # favorited channels arrive as catchup_channel playables without the
        # raw provider fields — carry what we need through
        chan = dict(channel or {})
        chan.setdefault("name", chan.get("title", ""))
        chan.setdefault("stream_id", chan.get("stream_id"))
        dlg = CatchupPickerDialog(self.client, self.config, chan, self)
        dlg.media_activated.connect(self.media_activated)
        dlg.favorite_changed.connect(self.favorite_changed)
        dlg.exec_()

    def _context_menu(self, pos):
        wi = self.list.itemAt(pos)
        if not wi:
            return
        data = wi.data(QtCore.Qt.UserRole)
        if self._playable_mode:
            playable = data
            menu = QtWidgets.QMenu(self)
            act_play = menu.addAction("Play")
            act_rm = None
            if playable.get("fav_key") and self.config.is_favorite(playable["fav_key"]):
                act_rm = menu.addAction("Remove from Favorites")
            chosen = menu.exec_(self.list.viewport().mapToGlobal(pos))
            if chosen == act_play:
                self.media_activated.emit(playable)
            elif chosen == act_rm:
                self.config.toggle_favorite(playable)
                self.favorite_changed.emit()
        else:
            playable = self.make_playable(data)
            menu = QtWidgets.QMenu(self)
            act_open = menu.addAction("Browse catch-up programs")
            act_fav = None
            if playable.get("fav_key"):
                is_fav = self.config.is_favorite(playable["fav_key"])
                act_fav = menu.addAction(
                    "Remove from Favorites" if is_fav else "Add to Favorites")
            chosen = menu.exec_(self.list.viewport().mapToGlobal(pos))
            if chosen == act_open:
                self._open_picker(data)
            elif chosen == act_fav:
                self.config.toggle_favorite(playable)
                self.favorite_changed.emit()
                self._apply_filter(self.search.text())


class CatchupPickerDialog(QtWidgets.QDialog):
    """Archive program picker for one catch-up channel: the provider's EPG
    data table filtered to programs that already started (the recorded
    window), grouped by local day, newest first.  Double-click (or the
    context menu) plays the program from its beginning."""

    media_activated = QtCore.pyqtSignal(dict)
    favorite_changed = QtCore.pyqtSignal()

    def __init__(self, client, config, channel, parent=None):
        super().__init__(parent)
        self.client = client
        self.config = config
        self.channel = dict(channel or {})
        self.channel.setdefault("name", self.channel.get("title", "Channel"))
        self.setWindowTitle(f"Catch-Up — {self.channel.get('name', 'Channel')}")
        self.resize(720, 640)

        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel(self.channel.get("name", ""))
        title.setStyleSheet("font-weight:bold; font-size:14px;")
        days = self.channel.get("archive_days") or self.channel.get("tv_archive_duration")
        sub = QtWidgets.QLabel(
            f"Recorded programs ({days} day archive)" if days
            else "Recorded programs")
        sub.setStyleSheet("color:#9aa0a6;")
        layout.addWidget(title)
        layout.addWidget(sub)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Time", "Program", "Length"])
        self.tree.itemActivated.connect(self._on_double)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context)
        layout.addWidget(self.tree, 1)

        self.status = QtWidgets.QLabel("Loading programs…")
        self.status.setStyleSheet("color:#666;")
        layout.addWidget(self.status)

        self.runner = AsyncRunner()
        self.runner.finished.connect(self._on_loaded)
        self.runner.run(self.client.epg_table, self.channel.get("stream_id"))

    # ---- helpers ----
    @staticmethod
    def _ts(v):
        try:
            n = int(str(v).strip())
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    def _on_loaded(self, result):
        ok, val = result
        if ok != "ok":
            self.status.setText(f"Error: {val}")
            return
        now = time.time()
        days = self._ts(self.channel.get("archive_days")
                        or self.channel.get("tv_archive_duration")) or 3
        # programs that already started, within the archive window
        rows = []
        for e in val or []:
            st = self._ts(e.start_timestamp)
            sp = self._ts(e.stop_timestamp)
            if st is None:
                continue
            if sp is None:
                sp = st + 1800
            if st >= now or st < now - (days * 86400 + 7200):
                continue
            rows.append((st, sp, e))
        rows.sort(key=lambda r: r[0], reverse=True)

        self.tree.clear()
        groups = {}
        order = []
        for st, sp, e in rows:
            day = datetime.fromtimestamp(st).date()
            if day not in groups:
                groups[day] = []
                order.append(day)
            groups[day].append((st, sp, e))
        total = 0
        for day in order:
            delta = (datetime.now().date() - day).days
            if delta == 0:
                label = "Today"
            elif delta == 1:
                label = "Yesterday"
            else:
                label = day.strftime("%A, %b %d")
            root = QtWidgets.QTreeWidgetItem([label, "", ""])
            f = root.font(0)
            f.setBold(True)
            root.setFont(0, f)
            self.tree.addTopLevelItem(root)
            for st, sp, e in groups[day]:
                t0 = datetime.fromtimestamp(st)
                t1 = datetime.fromtimestamp(sp)
                dur = max(1, int(round((sp - st) / 60.0)))
                in_progress = sp > now
                disp = _epg_text(e.title) or "Program"
                col0 = t0.strftime("%H:%M") + "\u2013" + t1.strftime("%H:%M")
                col2 = (f"{dur // 60}:{dur % 60:02d}"
                        + ("  (in progress)" if in_progress else ""))
                node = QtWidgets.QTreeWidgetItem([col0, disp, col2])
                node.setData(0, QtCore.Qt.UserRole, {
                    "stream_id": self.channel.get("stream_id"),
                    "channel": self.channel.get("name", ""),
                    "icon": self.channel.get("stream_icon",
                                             self.channel.get("icon", "")),
                    "title": disp,
                    "start": st,
                    "stop": sp,
                    "description": _epg_text(e.description),
                })
                root.addChild(node)
                total += 1
            root.setExpanded(day == order[0])
        if not total:
            self.status.setText("No recorded programs available.")
            return
        self.status.setText(f"{total} program(s)")

    def _make_playable(self, prog):
        dur_min = max(1, math.ceil((prog["stop"] - prog["start"]) / 60.0))
        return {
            "kind": "catchup",
            "title": f"{prog['channel']} \u2014 {prog['title']}",
            "url": self.client.timeshift_url(
                prog["stream_id"], prog["start"], dur_min),
            "stream_id": prog["stream_id"],
            "utc_start": prog["start"],
            "utc_end": prog["stop"],
            "channel": prog["channel"],
            "program": prog["title"],
            "fav_key": f"catchup:{prog['stream_id']}:{prog['start']}",
            "icon": prog.get("icon", ""),
        }

    def _on_double(self, item, _col):
        data = item.data(0, QtCore.Qt.UserRole)
        if not data:
            return
        playable = self._make_playable(data)
        self.config.add_recent(playable)
        self.media_activated.emit(playable)
        self.accept()

    def _context(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        data = item.data(0, QtCore.Qt.UserRole)
        if not data:
            return
        playable = self._make_playable(data)
        menu = QtWidgets.QMenu(self)
        act_play = menu.addAction("Play")
        is_fav = self.config.is_favorite(playable["fav_key"])
        act_fav = menu.addAction("Remove from Favorites" if is_fav
                                 else "Add to Favorites")
        chosen = menu.exec_(self.tree.viewport().mapToGlobal(pos))
        if chosen == act_play:
            self.config.add_recent(playable)
            self.media_activated.emit(playable)
            self.accept()
        elif chosen == act_fav:
            self.config.toggle_favorite(playable)
            self.favorite_changed.emit()


class PlayableListWidget(QtWidgets.QWidget):
    """Reusable list of "playable" dicts (used by Favorites and Custom tabs)."""

    media_activated = QtCore.pyqtSignal(dict)
    favorite_changed = QtCore.pyqtSignal()

    def __init__(self, config, get_items, can_remove=False, parent=None):
        super().__init__(parent)
        self.config = config
        self.get_items = get_items
        self.can_remove = can_remove

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.search, 1)
        refresh_btn = QtWidgets.QPushButton("⟳")
        refresh_btn.setFixedWidth(34)
        refresh_btn.clicked.connect(self.refresh)
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        self.list = QtWidgets.QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.list.itemActivated.connect(self._activate)
        self.list.customContextMenuRequested.connect(self._context)
        layout.addWidget(self.list, 1)

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color:#9aa0a6;")
        layout.addWidget(self.status)

    def refresh(self, *_):
        text = (self.search.text() or "").strip().lower()
        self.list.clear()
        for p in self.get_items() or []:
            disp = p.get("title", "")
            if text and text not in disp.lower():
                continue
            item = QtWidgets.QListWidgetItem(disp)
            item.setData(QtCore.Qt.UserRole, p)
            self.list.addItem(item)
        self.status.setText(f"{self.list.count()} items")

    def _activate(self, wi):
        if wi:
            self.media_activated.emit(wi.data(QtCore.Qt.UserRole))

    def _context(self, pos):
        wi = self.list.itemAt(pos)
        if not wi:
            return
        playable = wi.data(QtCore.Qt.UserRole)
        menu = QtWidgets.QMenu(self)
        act_play = menu.addAction("Play")
        act_rm = menu.addAction("Remove") if self.can_remove else None
        chosen = menu.exec_(self.list.viewport().mapToGlobal(pos))
        if chosen == act_play:
            self.media_activated.emit(playable)
        elif chosen == act_rm:
            self._remove(playable)

    def _remove(self, playable):
        self.config.toggle_favorite(playable)
        self.favorite_changed.emit()
        self.refresh()


class FavoritesTab(PlayableListWidget):
    def __init__(self, config, parent=None):
        super().__init__(config, lambda: config.favorites, can_remove=True, parent=parent)


class CustomTab(PlayableListWidget):
    def __init__(self, config, parent=None):
        super().__init__(config, lambda: config.custom_channels, can_remove=True, parent=parent)

    def _remove(self, playable):
        chans = self.config.data.setdefault("custom_channels", [])
        chans[:] = [c for c in chans if c.get("fav_key") != playable.get("fav_key")]
        self.config.save()
        self.refresh()

    def add_channel_dialog(self, parent=None):
        dlg = QtWidgets.QDialog(parent)
        dlg.setWindowTitle("Add Custom Channel")
        form = QtWidgets.QFormLayout(dlg)
        name = QtWidgets.QLineEdit()
        url = QtWidgets.QLineEdit()
        url.setPlaceholderText("http://.../stream.m3u8   rtsp://...   http://.../stream.ts")
        form.addRow("Name:", name)
        form.addRow("Stream URL:", url)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec_() == QtWidgets.QDialog.Accepted and url.text().strip():
            item = self.config.add_custom_channel(
                name.text().strip() or url.text().strip(), url.text().strip()
            )
            self.refresh()
            self.media_activated.emit(item)


