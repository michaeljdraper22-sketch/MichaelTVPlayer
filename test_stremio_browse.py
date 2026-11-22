# -*- coding: utf-8 -*-
"""Offline regression test for the Stremio catalog browse tabs.

Covers the whole pick path without a network, a VLC instance or an IPTV
account:
  [1] Cinemeta search shaping (year for BOTH catalogs, type filter)
  [2] episode_map ordering (specials LAST, sorted, junk skipped)
  [3] catalog_pick identity shapes (the field contract the player needs)
  [4] ranked_streams == rank_streams order + the season-pack veto
  [5] stream_playable URL resolution: debrid chain / plain url / local
      server fallback / unplayable -> None
  [6] best_playable picks the head and falls through a refused head
  [7] the tab: debounced search, Enter activates the first row, series
      open the episode dialog, right-click offers the stream list
  [8] the two dialogs: episode tree activation and SINGLE-click play
  [9] MainWindow wiring: reload_all reaches both Stremio tabs, and a
      catalog playable takes the window over exactly like a handoff
 [10] the tab-index migration for the two inserted tabs

Run:  .venv\\Scripts\\python.exe test_stremio_browse.py   (sets QT_QPA_PLATFORM itself)
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from src import stremio  # noqa: E402
from src.ui import stremio_browse as sb  # noqa: E402
from src.config import Config  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402

PASS = []
FAIL = []

# module-level: a QApplication whose Python reference is dropped gets
# garbage-collected, taking the whole widget stack with it
app = None


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def pump(ms=40):
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec_()
    if app is not None:
        app.processEvents()


def wait_for(pred, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        pump(20)
        if pred():
            return True
    return False


def make_cfg(**over):
    """A Config with fake learned debrid pipelines and no disk path
    (save() is a no-op because path is None)."""
    cfg = Config({}, None)
    cfg.stremio_provider_bases = {
        "premiumize": "https://torrentio.strem.fun/resolve/premiumize/PKEY/",
        "torbox": "https://torrentio.strem.fun/resolve/torbox/TKEY/",
    }
    for key, val in over.items():
        setattr(cfg, key, val)
    return cfg


META = {"name": "Adventure Time", "poster": "", "videos": [
    {"season": 0, "episode": 1, "name": "Webisode"},
    {"season": 1, "episode": 1, "name": "Slumber Party Panic"},
    {"season": 1, "episode": 2, "name": "Trouble in Lumpy Space"},
    {"season": 2, "episode": 1, "name": "It Came from the Nightosphere"},
    {"season": None, "episode": 4, "name": "junk row"},
]}


def stream(quality, file_name, **extra):
    """A Torrentio-shaped entry: name = addon + quality banner, title =
    torrent name / FILE name / seeds+size (the file name is line 2 —
    _resolve_file_name and the pack veto both read it from there)."""
    s = {"name": "Torrentio\n%s" % quality,
         "title": "%s\n%s\n\U0001F464 20 \U0001F4BE 1.5 GB \u2699\ufe0f X"
                  % (file_name.replace(".mkv", ""), file_name),
         "_addon": "https://a"}
    s.update(extra)
    if not s.get("url") and not s.get("infoHash"):
        # real addon entries always carry one of the two — without it
        # _stream_usable filters the row out before ranking
        s["infoHash"] = "%040x" % (abs(hash(file_name)) % 16 ** 40)
        s["fileIdx"] = 0
    return s


def main():
    global app
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    # [1] search shaping ---------------------------------------------------
    print("[1] Cinemeta search shaping")
    raw = [
        {"id": "tt1", "name": "The Office", "type": "series",
         "releaseInfo": "2005-2013"},
        {"id": "tt2", "name": "The Office", "type": "series",
         "releaseInfo": "2001-2003"},
        {"id": "tt9", "name": "Not A Show", "type": "movie"},
        {"id": "tt3", "name": "", "type": "series"},
    ]
    orig_catalog = stremio._catalog_search
    stremio._catalog_search = lambda cat, q: raw if cat == "series" else []
    try:
        hits = stremio.search_catalog("series", "the office")
    finally:
        stremio._catalog_search = orig_catalog
    check("series search keeps both year variants",
          [h.get("year") for h in hits] == ["2005", "2001"])
    check("series search drops wrong-type hits",
          all(h["id"] != "tt9" for h in hits))
    check("series search drops nameless hits",
          all(h["id"] != "tt3" for h in hits))
    check("search_catalog keeps the type field",
          hits[0]["type"] == "series")

    # [2] episode map ------------------------------------------------------
    print("[2] episode_map ordering")
    stremio._meta_cache["ttADV"] = META
    rows = stremio.episode_map("ttADV")
    check("specials (season 0) sort LAST",
          [s for s, _ in rows] == [1, 2, 0])
    check("episodes ascend inside a season",
          rows[0][1] == [(1, "Slumber Party Panic"),
                         (2, "Trouble in Lumpy Space")])
    check("videos without a season/episode are skipped",
          all(e != 4 for _s, eps in rows for e, _t in eps))
    check("empty imdb -> empty map", stremio.episode_map("") == [])

    # [3] catalog_pick shapes ---------------------------------------------
    print("[3] catalog_pick identity contract")
    cur_ep = stremio.catalog_pick("series", {"id": "ttADV",
                                             "name": "Adventure Time"},
                                  1, 2, meta=META)
    check("episode title carries show + SxxExx + episode name",
          cur_ep["title"] == "Adventure Time \u2014 S01E02 Trouble in "
                             "Lumpy Space")
    check("episode fav_key matches _adjacent_playable's shape",
          cur_ep["fav_key"] == "stremio:ttADV:1:2")
    check("episode pick is NOT a movie", not cur_ep.get("movie"))
    check("episode identity fields present",
          (cur_ep["stremio_imdb"], cur_ep["season"], cur_ep["episode"],
           cur_ep["series_name"]) == ("ttADV", 1, 2, "Adventure Time"))
    cur_mv = stremio.catalog_pick(
        "movie", {"id": "tt0133093", "name": "The Matrix", "year": "1999"})
    check("movie pick is marked movie", cur_mv.get("movie") is True)
    check("movie title keeps the year",
          cur_mv["title"] == "The Matrix (1999)")
    check("movie fav_key is stable per title",
          cur_mv["fav_key"] == "stremio:tt0133093")

    # [4] ranked_streams ---------------------------------------------------
    print("[4] ranked_streams order + pack veto")
    cfg = make_cfg(stremio_resolution_pref="1080")
    plain = [
        stream("480p", "Show.S01E02.480p.mkv"),
        stream("1080p", "Show.S01E02.1080p.mkv"),
        stream("720p", "Show.S01E02.720p.mkv"),
    ]
    orig_addon = stremio.addon_streams
    stremio.addon_streams = lambda c, i, s, e: list(plain)
    try:
        ranked = stremio.ranked_streams(cfg, cur_ep, META)
    finally:
        stremio.addon_streams = orig_addon
    check("preference order wins (1080 first)",
          "1080p" in ranked[0]["name"])
    check("same order as rank_streams",
          [s["name"] for s in ranked]
          == [s["name"] for s in stremio.rank_streams(cfg, plain, cur_ep)])

    lie = stream("1080p", "Show.S01E02.Slumber.Party.Panic.1080p.mkv")
    good = stream("720p", "Show.S01E02.Trouble.in.Lumpy.Space.720p.mkv")
    stremio.addon_streams = lambda c, i, s, e: [lie, good]
    try:
        ranked = stremio.ranked_streams(cfg, cur_ep, META)
    finally:
        stremio.addon_streams = orig_addon
    check("a pack file titled as ANOTHER episode is vetoed",
          all("Slumber" not in s["title"] for s in ranked))
    check("the honest candidate survives", len(ranked) == 1)

    orig_mov = stremio.addon_movie_streams
    seen = {}
    def _fake_movie(c, imdb):
        seen["imdb"] = imdb
        return [stream("Torrentio\n1080p", "The.Matrix.1999.1080p.mkv\n")]

    stremio.addon_movie_streams = _fake_movie
    try:
        mranked = stremio.ranked_streams(cfg, cur_mv)
    finally:
        stremio.addon_movie_streams = orig_mov
    check("movie picks query the MOVIE catalog", seen.get("imdb")
          == "tt0133093")
    check("movie list is playable", len(mranked) == 1)
    check("no imdb -> empty list",
          stremio.ranked_streams(cfg, {"title": "x"}) == [])

    # [5] stream_playable --------------------------------------------------
    print("[5] stream_playable URL resolution")
    torrent = stream("Torrentio\n1080p", "Show.S01E02.1080p.mkv\n",
                     infoHash="A" * 40, fileIdx=3)
    p = stremio.stream_playable(cfg, cur_ep, torrent)
    check("torrent-only build goes through a learned debrid pipeline",
          p and p["url"].startswith(
              "https://torrentio.strem.fun/resolve/premiumize/PKEY/"))
    check("built link carries the hash and file index",
          ("a" * 40 in p["url"]) and "/3/" in p["url"])
    check("playable is kind=stremio", p["kind"] == "stremio")
    check("identity rides along",
          (p["stremio_imdb"], p["season"], p["episode"], p["fav_key"])
          == ("ttADV", 1, 2, "stremio:ttADV:1:2"))
    check("torrent identity recorded for the dead-link walk",
          (p["info_hash"], p["file_idx"]) == ("a" * 40, 3))

    pref = make_cfg(stremio_resolution_pref="1080",
                    stremio_provider_pref="torbox")
    p = stremio.stream_playable(pref, cur_ep, torrent)
    check("the preferred provider leads the chain",
          p["url"].startswith(
              "https://torrentio.strem.fun/resolve/torbox/TKEY/"))

    plain_s = stream("Addon\n", "x.mkv", url="https://cdn.example/x.mkv")
    p = stremio.stream_playable(cfg, cur_ep, plain_s)
    check("a plain url is used as-is",
          p["url"] == "https://cdn.example/x.mkv")

    resolve_s = {"url": "https://torrentio.strem.fun/resolve/torbox/TKEY/"
                        + "B" * 40 + "/File.S01E02.mkv/0/File.S01E02.mkv",
                 "name": "Torrentio\n1080p", "title": "x\n"}
    p = stremio.stream_playable(cfg, cur_ep, resolve_s)
    check("a listed resolve link is used as its addon built it",
          p["url"] == resolve_s["url"])
    check("the other learned provider rides along as the fallback",
          any("/resolve/premiumize/" in u for u in
              stremio.candidate_urls(cfg, cur_ep, resolve_s)))

    prem_s = {"url": "https://torrentio.strem.fun/resolve/premiumize/PKEY/"
                     + "B" * 40 + "/File.S01E02.mkv/0/File.S01E02.mkv",
              "name": "Torrentio\n1080p", "title": "x\n"}
    p = stremio.stream_playable(pref, cur_ep, prem_s)
    check("an explicit provider preference leads the chain",
          "/resolve/torbox/" in p["url"])
    check("the listed link is still the second candidate",
          stremio.candidate_urls(pref, cur_ep, prem_s)[1] == prem_s["url"])

    # local server fallback: no learned pipelines at all
    bare = Config({}, None)
    bare.data["stremio_provider_bases"] = {}
    created = {}

    class FakeServer:
        def __init__(self, base=""):
            self.base = base

        def create(self, info_hash, trackers=()):
            created["hash"] = info_hash
            return created.get("ok", True)

        def play_url(self, info_hash, file_idx):
            return "http://127.0.0.1:11470/%s/%d" % (info_hash, file_idx)

    orig_server = stremio.StreamingServer
    stremio.StreamingServer = FakeServer
    try:
        p = stremio.stream_playable(bare, cur_ep, torrent)
        check("no pipeline -> the local streaming engine serves it",
              p and p["url"].startswith("http://127.0.0.1:11470/"))
        check("local engine got the hash", created.get("hash") == "a" * 40)
        created["ok"] = False
        check("a refused create -> None",
              stremio.stream_playable(bare, cur_ep, torrent) is None)
        dead = {"name": "x", "title": "y"}
        check("a stream with neither url nor hash -> None",
              stremio.stream_playable(bare, cur_ep, dead) is None)
    finally:
        stremio.StreamingServer = orig_server

    # [5b] the click-time liveness probe --------------------------------
    print("[5b] probe=True on a manually picked stream")
    probed = []
    orig_probe = stremio.probe_debrid

    def _probe(url, timeout_s=3.0, report=None):
        probed.append(url)
        return "premiumize" not in url      # premiumize 502s, torbox serves
    stremio.probe_debrid = _probe
    try:
        p = stremio.stream_playable(cfg, cur_ep, torrent)
        check("probe=False never probes (Enter must start instantly)",
              not probed and p and "/resolve/premiumize/" in p["url"])
        p = stremio.stream_playable(cfg, cur_ep, torrent, None, True)
        check("probe=True skips the dead provider",
              p and "/resolve/torbox/" in p["url"])
        check("...after checking them in order", len(probed) == 2)

        probed[:] = []
        stremio.probe_debrid = lambda u, timeout_s=3.0, report=None: (
            probed.append(u) or False)
        check("every pipeline dead -> None (the picker keeps the list)",
              stremio.stream_playable(cfg, cur_ep, torrent, None, True)
              is None)

        probed[:] = []
        stremio.probe_debrid = lambda u, timeout_s=3.0, report=None: (
            probed.append(u) or True)
        check("throttled/alive -> the first candidate",
              stremio.stream_playable(cfg, cur_ep, torrent, None, True)
              ["url"].startswith(
                  "https://torrentio.strem.fun/resolve/premiumize/"))
        probed[:] = []
        local = {"name": "x", "title": "y", "infoHash": "A" * 40,
                 "fileIdx": 0}
        orig_server = stremio.StreamingServer

        class Srv:
            def __init__(self, base=""):
                pass

            def create(self, info_hash, trackers=()):
                return True

            def play_url(self, info_hash, file_idx):
                return "http://127.0.0.1:11470/%s/%d" % (info_hash, file_idx)
        stremio.StreamingServer = Srv
        try:
            p = stremio.stream_playable(bare, cur_ep, local, None, True)
        finally:
            stremio.StreamingServer = orig_server
        check("the local engine is never probed (it serves its own)",
              not probed and p and p["url"].startswith(
                  "http://127.0.0.1:11470/"))
    finally:
        stremio.probe_debrid = orig_probe

    # [6] best_playable ----------------------------------------------------
    print("[6] best_playable")
    calls = []

    def _fake_rank(c, cur, meta=None):
        return [{"infoHash": "C" * 40, "fileIdx": 0, "name": "a",
                 "title": "one.mkv"},
                {"url": "https://cdn.example/second.mkv",
                 "name": "b", "title": "two.mkv"}]

    orig_rank = stremio.ranked_streams
    stremio.ranked_streams = _fake_rank
    try:
        p = stremio.best_playable(cfg, cur_ep, META)
        check("head stream wins when it resolves",
              p["url"].startswith("https://torrentio.strem.fun/resolve/"))
        # make the head unplayable: no url, hash and no chain in a bare cfg
        bare2 = Config({}, None)
        bare2.data["stremio_provider_bases"] = {}
        orig_server2 = stremio.StreamingServer

        class Refuse:
            def __init__(self, base=""):
                pass

            def create(self, info_hash, trackers=()):
                calls.append(info_hash)
                return False
        stremio.StreamingServer = Refuse
        try:
            p2 = stremio.best_playable(bare2, cur_ep, META)
        finally:
            stremio.StreamingServer = orig_server2
        check("a refused head falls through to the next stream",
              p2 and p2["url"] == "https://cdn.example/second.mkv")
    finally:
        stremio.ranked_streams = orig_rank

    # [7] the tab ----------------------------------------------------------
    print("[7] StremioCatalogBrowser")
    hit_mv = {"id": "tt0133093", "name": "The Matrix", "year": "1999",
              "poster": ""}
    orig_search = stremio.search_catalog
    stremio.search_catalog = lambda kind, q: ([hit_mv] if kind == "movie"
                                              else [])
    try:
        tab = sb.StremioCatalogBrowser(make_cfg(), "movie")
        tab.search.setText("matrix")          # debounced
        got = wait_for(lambda: tab.list.count() == 1, timeout=3.0)
        check("search results land after the debounce", got)
        check("row shows name (year)",
              tab.list.item(0).text() == "The Matrix (1999)")

        played = []
        tab.media_activated.connect(played.append)
        orig_best = stremio.best_playable
        stremio.best_playable = lambda c, cur, meta=None: {
            "kind": "stremio", "url": "https://x/1", "title": cur["title"],
            "fav_key": cur["fav_key"], "stremio_imdb": cur["stremio_imdb"]}
        try:
            tab.search.returnPressed.emit()   # Enter in the search box
            check("Enter plays the first row",
                  wait_for(lambda: len(played) == 1, timeout=3.0))
        finally:
            stremio.best_playable = orig_best
        check("the emitted playable is the movie pick",
              played and played[0]["stremio_imdb"] == "tt0133093")

        # right-click -> full stream list carries the movie identity
        shown = []

        class FakeStreams(QtWidgets.QDialog):
            media_activated = QtCore.pyqtSignal(dict)

            def __init__(self, config, cur, meta=None, parent=None):
                super().__init__(parent)
                shown.append(dict(cur))

            def exec_(self):
                return 0

        orig_streams = sb.StremioStreamsDialog
        sb.StremioStreamsDialog = FakeStreams
        try:
            tab._show_streams(stremio.catalog_pick("movie", hit_mv))
        finally:
            sb.StremioStreamsDialog = orig_streams
        check("right-click opens the list for THAT movie",
              shown and shown[0]["stremio_imdb"] == "tt0133093")
    finally:
        stremio.search_catalog = orig_search

    # series tab: Enter opens the episode dialog with the right hit
    print("[7b] series tab activation")
    hit_series = {"id": "ttADV", "name": "Adventure Time", "year": "2010"}
    stremio.search_catalog = lambda kind, q: ([hit_series]
                                              if kind == "series" else [])
    opened = []

    class FakeEpisodes(QtWidgets.QDialog):
        media_activated = QtCore.pyqtSignal(dict)

        def __init__(self, config, hit, parent=None):
            super().__init__(parent)
            opened.append(dict(hit))

        def exec_(self):
            return 1

    orig_dialog = sb.StremioEpisodesDialog
    sb.StremioEpisodesDialog = FakeEpisodes
    try:
        stab = sb.StremioCatalogBrowser(make_cfg(), "series")
        stab.search.setText("adventure time")
        wait_for(lambda: stab.list.count() == 1, timeout=3.0)
        stab.search.returnPressed.emit()
        check("Enter on a show opens its episode list",
              bool(opened) and opened[0]["id"] == "ttADV")
    finally:
        sb.StremioEpisodesDialog = orig_dialog
        stremio.search_catalog = orig_search

    # [8] the two-pane series picker ---------------------------------------
    print("[8] two-pane episodes + streams picker")
    orig_epmap = stremio.episode_map
    orig_meta = stremio.series_meta
    orig_ranked = stremio.ranked_streams
    stremio.episode_map = lambda imdb: [
        (1, [(1, "Slumber Party Panic"), (2, "Trouble in Lumpy Space")]),
        (0, [(1, "Webisode")])]
    stremio.series_meta = lambda imdb: META
    ep_streams = [
        stream("1080p", "Show.S01E01.1080p.mkv"),
        stream("720p", "Show.S01E01.720p.mkv"),
    ]
    seen_cur = []

    def _fake_ranked(cfg, cur, meta=None):
        seen_cur.append(cur.get("fav_key"))
        return list(ep_streams)
    stremio.ranked_streams = _fake_ranked
    try:
        dlg = sb.StremioEpisodesDialog(make_cfg(), hit_series)
        check("episode tree loads",
              wait_for(lambda: dlg.tree.topLevelItemCount() > 0, 3.0))
        check("season headers are labelled",
              dlg.tree.topLevelItem(0).text(0) == "Season 1")
        check("specials header labelled", dlg.tree.topLevelItem(1).text(0)
              == "Specials")
        season1 = dlg.tree.topLevelItem(0)
        check("episode rows carry SxxExx",
              season1.child(0).text(0) == "S1 E1")

        # the right pane fills from the initial selection, nothing plays
        check("opening loads the first episode's streams",
              wait_for(lambda: dlg.streams.list.count() == 2, 3.0))
        check("...for THAT episode",
              seen_cur and seen_cur[-1] == "stremio:ttADV:1:1")
        check("the pane names the episode",
              "S01E01" in dlg.streams.header.text())
        check("the head row is marked as Enter's pick",
              dlg.streams.list.item(0).text().startswith("\u2605"))
        check("opening played nothing",
              not dlg.streams._busy and not dlg._busy)

        # highlighting another episode swaps the pane's list
        dlg.tree.setCurrentItem(season1.child(1))
        check("highlighting an episode loads ITS streams",
              wait_for(lambda: seen_cur[-1] == "stremio:ttADV:1:2", 3.0))
        check("the pane renames too",
              "S01E02" in dlg.streams.header.text())

        # a single click in the pane plays that stream
        picked = []
        dlg.media_activated.connect(picked.append)
        orig_sp = stremio.stream_playable
        stremio.stream_playable = lambda c, cur, st, server=None, probe=False: {
            "kind": "stremio", "url": "https://x/pane",
            "title": cur["title"], "fav_key": cur["fav_key"]}
        try:
            dlg.streams.list.itemClicked.emit(dlg.streams.list.item(1))
            check("a single click in the pane plays that stream",
                  wait_for(lambda: len(picked) == 1, 3.0))
        finally:
            stremio.stream_playable = orig_sp
        check("it played the HIGHLIGHTED episode (S01E02)",
              picked and "S01E02" in picked[0]["title"])

        # Enter / double-click on an episode still plays its best stream
        dlg2 = sb.StremioEpisodesDialog(make_cfg(), hit_series)
        wait_for(lambda: dlg2.tree.topLevelItemCount() > 0, 3.0)
        s1 = dlg2.tree.topLevelItem(0)
        picked2 = []
        dlg2.media_activated.connect(picked2.append)
        orig_best = stremio.best_playable
        stremio.best_playable = lambda c, cur, meta=None: {
            "kind": "stremio", "url": "https://x/ep", "title": cur["title"],
            "fav_key": cur["fav_key"]}
        try:
            dlg2._activate(s1.child(1))        # Enter / double-click
            check("Enter on an episode plays the best stream",
                  wait_for(lambda: len(picked2) == 1, 3.0))
            dlg2._activate(s1)                 # a season header
            pump(200)
            check("a season header does not play", len(picked2) == 1)
        finally:
            stremio.best_playable = orig_best
        check("the episode identity is right",
              "S01E02" in picked2[0]["title"])
    finally:
        stremio.episode_map = orig_epmap
        stremio.series_meta = orig_meta
        stremio.ranked_streams = orig_ranked

    print("[8b] standalone stream list dialog")
    streams3 = [
        stream("Torrentio\n1080p", "A.1080p.mkv\n\U0001F464 10"),
        stream("Torrentio\n720p", "B.720p.mkv\n\U0001F464 4"),
        {"name": "Torrentio\n4k", "title": "C.4k.mkv\n",
         "url": "https://torrentio.strem.fun/resolve/torbox/TKEY/"
                + "D" * 40 + "/C.4k.mkv/0/C.4k.mkv"},
    ]
    stremio.ranked_streams = lambda c, cur, meta=None: list(streams3)
    try:
        sdlg = sb.StremioStreamsDialog(make_cfg(), cur_mv)
        check("stream list loads",
              wait_for(lambda: sdlg.streams.list.count() == 3, 3.0))
        check("the head row is marked as Enter's pick",
              sdlg.streams.list.item(0).text().startswith("\u2605"))
        check("rows are single-line (no raw newlines)",
              "\n" not in sdlg.streams.list.item(0).text())
        check("the debrid provider is shown when known",
              "TorBox" in sdlg.streams.list.item(2).text())
        check("status invites a single click",
              "click one to play" in sdlg.streams.status.text())
        check("the standalone dialog hides its inner header",
              sdlg.streams.header.isHidden())

        chosen = []
        sdlg.media_activated.connect(chosen.append)
        orig_sp = stremio.stream_playable
        picked_streams = []

        def _fake_sp(c, cur, st, server=None, probe=False):
            picked_streams.append(st)
            return {"kind": "stremio", "url": "https://x/pick",
                    "title": cur["title"], "fav_key": cur["fav_key"]}
        stremio.stream_playable = _fake_sp
        try:
            # SINGLE click on row 2 (the user's manual override)
            sdlg.streams.list.itemClicked.emit(sdlg.streams.list.item(1))
            check("a single click starts that stream",
                  wait_for(lambda: len(chosen) == 1, 3.0))
        finally:
            stremio.stream_playable = orig_sp
        check("the CLICKED row was the one resolved",
              picked_streams and "B.720p" in picked_streams[0]["title"])

        sdlg2 = sb.StremioStreamsDialog(make_cfg(), cur_mv)
        wait_for(lambda: sdlg2.streams.list.count() == 3, 3.0)
        orig_sp = stremio.stream_playable
        stremio.stream_playable = lambda c, cur, st, server=None, probe=False: None
        try:
            sdlg2.streams.list.itemClicked.emit(sdlg2.streams.list.item(0))
            ok = wait_for(lambda: "try another" in sdlg2.streams.status.text(),
                          3.0)
            check("a stream that cannot start keeps the list usable",
                  ok and sdlg2.streams.list.isEnabled())
        finally:
            stremio.stream_playable = orig_sp
    finally:
        stremio.ranked_streams = orig_ranked

    # [9] MainWindow wiring ------------------------------------------------
    print("[9] MainWindow wiring")

    class StubTab:
        def __init__(self):
            self.reloads = 0

        def reload(self):
            self.reloads += 1

        def _reload_categories(self, force=False):
            pass

    class StubFav:
        def refresh(self):
            pass

    class StubCountries:
        def _load(self, force=False):
            pass

    class Shim:
        pass

    shim = Shim()
    shim.fav_tab = StubFav()
    shim.countries_dialog = StubCountries()
    for attr in ("live_tab", "vod_tab", "series_tab", "catchup_tab"):
        setattr(shim, attr, StubTab())
    shim.stremio_series_tab = StubTab()
    shim.stremio_movie_tab = StubTab()
    MainWindow.reload_all(shim)
    check("reload_all refreshes the Stremio Series tab",
          shim.stremio_series_tab.reloads == 1)
    check("reload_all refreshes the Stremio Movies tab",
          shim.stremio_movie_tab.reloads == 1)

    # the two tab labels must stay APART once ChromeTabBar elides them:
    # "Stremio Series"/"Stremio Movies" both collapse to "Str…"
    from src.ui.main_window import ChromeTabBar
    fm = QtGui.QFontMetrics(ChromeTabBar().font())
    el_s = fm.elidedText(sb.TAB_SERIES, QtCore.Qt.ElideRight, 68)
    el_m = fm.elidedText(sb.TAB_MOVIES, QtCore.Qt.ElideRight, 68)
    check("Stremio tab labels are distinct", sb.TAB_SERIES != sb.TAB_MOVIES)
    check("...and stay distinct when the bar elides them (%r vs %r)"
          % (el_s, el_m), el_s != el_m)

    class FakePlayer(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.played = []

        def play_media(self, playable, start_at=0.0):
            self.played.append(playable)

        def set_panel_hidden(self, on):
            pass

        def setFocus(self, *a, **k):
            pass

    class Win(QtWidgets.QMainWindow):
        _apply_channels = MainWindow._apply_channels
        _save_splitter_sizes = MainWindow._save_splitter_sizes

        def __init__(self):
            super().__init__()
            self.config = Config({}, None)
            self.player_view = FakePlayer()
            self.splitter = QtWidgets.QSplitter()
            self.tabs = QtWidgets.QTabWidget()
            self.splitter.addWidget(self.tabs)
            self.splitter.addWidget(QtWidgets.QWidget())
            self.setCentralWidget(self.splitter)
            self.splitter.setSizes([300, 500])
            self._channels_hidden = False
            self._zen = False
            self.btn_hide_channels = QtWidgets.QToolButton()
            self.act_chan = QtWidgets.QAction("Hide channel list", self)

    win = Win()
    catalog_playable = {"kind": "stremio", "title": "The Matrix (1999)",
                        "url": "https://torrentio.strem.fun/resolve/"
                               "torbox/TKEY/" + "E" * 40 + "/m.mkv/0/m.mkv",
                        "fav_key": "stremio:tt0133093", "movie": True,
                        "stremio_imdb": "tt0133093"}
    MainWindow.play(win, dict(catalog_playable), 0.0)
    check("a catalog pick reaches the player",
          win.player_view.played
          and win.player_view.played[0]["stremio_imdb"] == "tt0133093")
    check("it takes the window over like a handoff",
          win._channels_hidden is True)

    # [11] the playing-view "all streams" button --------------------------
    print("[11] choose-stream button on the playing movie")
    from src.ui.player_view import PlayerView

    def mk_view(cur):
        view = PlayerView(Config({}, None))
        view._closing = False
        view._attach_done = True
        view.current = cur
        view.show()
        view._update_control_state()
        view._apply_button_visibility()
        rec = []

        def _fake_play(p, s=0.0):
            rec.append((p.get("fav_key"), s, p.get("url")))
            view.current = p
        view.play_media = _fake_play
        return view, rec

    movie_cur = dict(catalog_playable)
    v, rec = mk_view(movie_cur)
    check("button visible on a movie picked from the tab",
          not v.btn_stream_list.isHidden())
    check("button enabled (identity is known)", v.btn_stream_list.isEnabled())
    check("the S button is still there too", not v.btn_stream.isHidden())

    # a live channel has nothing to switch between
    v2, _rec2 = mk_view({"kind": "live", "title": "L", "url": "http://x",
                         "fav_key": "live:1"})
    check("hidden on live TV", v2.btn_stream_list.isHidden())
    check("disabled on live TV", not v2.btn_stream_list.isEnabled())

    # a handoff before its identity resolved: no list to show yet
    handoff = {"kind": "stremio", "title": "Stremio stream",
               "url": "http://127.0.0.1:11470/" + "F" * 40 + "/0",
               "fav_key": "stremio:ffffffffffffffff:0",
               "info_hash": "F" * 40, "file_idx": 0}
    v3, _rec3 = mk_view(handoff)
    check("hidden while a handoff's identity is still resolving",
          v3.btn_stream_list.isHidden())
    notes = []
    v3.show_info = lambda text, **kw: notes.append(text)
    # a disabled button swallows the click entirely — that is the point —
    # so exercise the shortcut path, which is the one that can still ask
    v3._choose_stream_clicked()
    check("the shortcut explains why there is no list yet",
          any("identifying" in n.lower() for n in notes))

    # identity lands -> the button appears on the same view
    v3.current.update({"stremio_imdb": "tt0133093", "movie": True,
                       "movie_name": "The Matrix", "year": "1999"})
    v3._update_control_state()
    v3._apply_button_visibility()
    check("appears once the identity is known",
          not v3.btn_stream_list.isHidden())

    # clicking opens the ranked list for THAT item and resumes on pick
    opened = []

    class FakeStreams(QtWidgets.QDialog):
        media_activated = QtCore.pyqtSignal(dict)

        def __init__(self, config, cur, meta=None, parent=None):
            super().__init__(parent)
            opened.append(dict(cur))

        def exec_(self):
            self.media_activated.emit(dict(pick))
            return 1

    pick = {"kind": "stremio", "url": "https://cdn.example/other.mkv",
            "title": "The Matrix (1999)",
            "fav_key": movie_cur["fav_key"], "movie": True,
            "stremio_imdb": "tt0133093"}
    orig_dlg = sb.StremioStreamsDialog
    sb.StremioStreamsDialog = FakeStreams
    try:
        v.vlc.get_time = lambda: 313000        # 5:13 into the movie
        v.vlc.get_length = lambda: 8000000
        v.btn_stream_list.click()
    finally:
        sb.StremioStreamsDialog = orig_dlg
    check("the list opens for the playing movie",
          opened and opened[0]["stremio_imdb"] == "tt0133093")
    check("the picked row starts", bool(rec) and rec[0][2]
          == "https://cdn.example/other.mkv")
    check("...resuming where playback stood (313 s)",
          rec and abs(rec[0][1] - 313.0) < 0.01)

    # a different item would start from the top instead
    v4, rec4 = mk_view(dict(movie_cur))
    other = dict(pick, fav_key="stremio:tt9999999:1:1",
                 url="https://cdn.example/other2.mkv")
    pick = other
    sb.StremioStreamsDialog = FakeStreams
    try:
        v4.vlc.get_time = lambda: 313000
        v4.vlc.get_length = lambda: 8000000
        v4.btn_stream_list.click()
    finally:
        sb.StremioStreamsDialog = orig_dlg
    check("a different episode starts from 0",
          rec4 and rec4[0][1] == 0.0)
    for view in (v, v2, v3, v4):
        view.close()

    # [10] tab-index migration --------------------------------------------
    print("[10] saved-tab migration")
    from src import config as config_mod

    def load_with(settings):
        tmp = tempfile.mkdtemp(prefix="mtp_cfg_")
        folder = os.path.join(tmp, config_mod.APP_NAME)
        os.makedirs(folder)
        with open(os.path.join(folder, "settings.json"), "w",
                  encoding="utf-8") as fh:
            import json
            json.dump(settings, fh)
        old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = tmp
        try:
            return config_mod.Config.load()
        finally:
            if old is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old

    cfg1 = load_with({"last_tab": 4, "_catchup_tab_migrated": True})
    check("Favorites (4) shifts to 6", cfg1.last_tab == 6)
    check("the migration marker is written",
          cfg1.data.get("_stremio_tabs_migrated") is True)
    cfg2 = load_with({"last_tab": 3})
    check("an old config passes through BOTH migrations (3 -> 6)",
          cfg2.last_tab == 6)
    cfg3 = load_with({"last_tab": 2, "_catchup_tab_migrated": True})
    check("Series (2) never shifts", cfg3.last_tab == 2)
    cfg4 = load_with({"last_tab": 3, "_catchup_tab_migrated": True,
                      "_stremio_tabs_migrated": True})
    check("an already-migrated config is untouched", cfg4.last_tab == 3)

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
