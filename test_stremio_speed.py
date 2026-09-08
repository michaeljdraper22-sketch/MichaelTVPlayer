# -*- coding: utf-8 -*-
"""Offscreen regression test for the fast-debrid-handoff package:

[1] stremio.probe_debrid against a local HTTP server: 206/200/redirect
    read as alive; 502 / connection-refused / hang read as dead — and a
    hang returns within the requested timeout, not the default 3 s.
    The probe must ask for ONE byte (Range: bytes=0-0), never the body.
[2] _stremio_guard_tick alive rule: only playing/paused or >5 s of
    tracked playback counts as alive. The opening-hang shape (VLC stuck
    in 'opening', is_playing() True — live-seen 2026-09-02 16:01, 55 s
    dead screen) must now fall back after the 10 s budget; a mid-play
    stall (>5 s played) must NOT restart the stream from zero.
[3] _on_stremio_probe: a dead probe switches to the local-server torrent
    immediately; an alive probe, a stale fav_key, an already-done
    fallback, or a VLC that already started all leave it alone.
[4] stremio._find_catalog: candidates are scored by word overlap with a
    60% floor — the 2026-09-03 20:57 incident (Cinemeta's search ranked
    'Real Time with Bill Maher' above 'Adventure Time'; the old first-
    ANY-shared-word rule matched Maher off the single word 'Time',
    retitled the stream and killed every episode button + autoplay)
    can't reproduce regardless of search ordering. Plus the 2026-09-04
    cleaner regression: junk tokens strip as WHOLE tokens only, so 'DV'
    can no longer eat the middle of 'Adventure' ('A enture Time' was a
    query no title could word-match, so every Adventure Time lookup honestly
    missed — prev/next/autoplay dead all day).
[4b] 2026-09-05 11:11: server-URL identity must honor the URL's
    file_idx. A debrid-stall fallback on a 278-file 'Complete Series'
    pack (real order: S10E13 first, S01E01 last) let the stats name-scan
    grab files[0]'s marker — the playing S03E22 got retitled as the
    series finale S10E13, ⏮ targeted S10E12 Gumbaldia and next/autoplay
    died. torrent_names now surfaces stats files[file_idx] as the played
    name; a markerless played file resolves as a movie from its OWN
    name; the stats-less path prefers the idx-scoped .torrent cache
    over the flattened global scan; _torrent_metainfo picks
    files[file_idx] over largest-file.
[4c] 2026-09-07 13:01: the streams cache is an OrderedDict. The v1.5.20
    review gave the 12-entry streams cache FIFO eviction via
    dict.popitem(last=False) — but _streams_cache was a PLAIN dict,
    whose popitem() takes no arguments, so the first lookup that needed
    to evict (Bluey binge, cache full at 12) died with
    TypeError('dict.popitem() takes no keyword arguments') BEFORE the
    insert, leaving the cache pinned at 12 and every later new-episode
    lookup dying the same way: autoplay reported 'nothing found' from
    then on. The cache must evict oldest-inserted and keep serving.
[4d] 2026-09-08 10:18: a year LEFT of the episode marker must not
    reach the series query. A torrentio resolve URL embedded a
    Sonarr-style file name ('Bluey (2018) - S01E50 - …mkv'); the
    cleaner's cut + end-strip left the year as a query word
    ('Bluey (2018'), no series title carries it, and the 60% floor
    failed EVERY lookup — open identity, autoplay and the next-click
    all 'nothing found' while S01E51 sat one episode away.
    strip_year=True on the release-head query; movie/canonical names
    keep their years.

[5] wiring: play_media kicks the probe with the guard.

Run:  .venv\\Scripts\\python.exe test_stremio_speed.py   (sets QT_QPA_PLATFORM itself)
"""
import inspect
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src import stremio  # noqa: E402
from src.config import Config  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else " FAIL ") + name)


app = QtWidgets.QApplication(sys.argv)


def main():
    print("[1] stremio.probe_debrid: liveness of a debrid resolve link")
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            seen["range"] = self.headers.get("Range")
            if self.path.startswith("/ok206"):
                self.send_response(206)
                self.send_header("Content-Range", "bytes 0-0/1000")
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(b"x")
            elif self.path.startswith("/ok200"):
                body = b"y" * 10          # range ignored: full 200 answer
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/redir"):
                self.send_response(302)
                self.send_header("Location", "/ok200")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path.startswith("/bad502"):
                body = b"upstream error"
                self.send_response(502)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/hang"):
                time.sleep(5.0)
                self.send_response(200)
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(b"z")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]

    check("206 with range honored -> alive",
          stremio.probe_debrid(base + "/ok206", timeout_s=3.0) is True)
    check("probe asks for exactly one byte (Range: bytes=0-0)",
          seen.get("range") == "bytes=0-0")
    check("200 full answer (range ignored) -> alive",
          stremio.probe_debrid(base + "/ok200", timeout_s=3.0) is True)
    check("redirect followed -> alive",
          stremio.probe_debrid(base + "/redir", timeout_s=3.0) is True)
    check("502 -> dead",
          stremio.probe_debrid(base + "/bad502", timeout_s=3.0) is False)
    t0 = time.monotonic()
    hang_dead = stremio.probe_debrid(base + "/hang", timeout_s=0.8)
    took = time.monotonic() - t0
    check("hang -> dead within the requested timeout",
          hang_dead is False and took < 3.0)
    # a port with no listener: the refused shape
    closed = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = closed.server_address[1]
    closed.server_close()
    check("connection refused -> dead",
          stremio.probe_debrid("http://127.0.0.1:%d/x" % port,
                               timeout_s=2.0) is False)
    srv.shutdown()

    print("[2] guard alive rule: real playback or nothing")
    view = PlayerView(Config({}, None))

    def arm(state_name, playing, vid_s, age_s=11.0, done=False):
        """One guard tick against a faked VLC; returns how many times the
        fallback began."""
        fired = []
        view.current = {"kind": "stremio", "fav_key": "stremio:abc:0",
                        "title": "T", "url": base + "/ok206"}
        view._closing = False
        view._vid_s = vid_s
        view._stremio_fallback_done = done
        view._stremio_guard_t0 = pv_mod.now_s() - age_s
        view.vlc.state_name = lambda: state_name
        view.vlc.is_playing = lambda: playing
        view._begin_stremio_fallback = lambda: fired.append(1)
        view._stremio_guard_tick()
        app.processEvents()
        return fired

    check("opening-hang (is_playing() True, 0 s played) -> fallback fires",
          len(arm("opening", True, 0.0)) == 1)
    check("same shape inside the 10 s budget -> still waiting",
          len(arm("opening", True, 0.0, age_s=4.0)) == 0)
    check("playing -> alive, no fallback",
          len(arm("playing", True, 600.0)) == 0)
    check("paused -> alive, no fallback",
          len(arm("paused", False, 600.0)) == 0)
    check("502 shape (ended, 0 s played) -> fallback fires",
          len(arm("ended", False, 0.0)) == 1)
    check("played >5 s then ended -> alive (EOF paths own it)",
          len(arm("ended", False, 120.0)) == 0)
    check("mid-play buffering stall -> alive (no restart from zero)",
          len(arm("buffering", True, 120.0)) == 0)
    check("fallback already spent -> nothing left to try",
          len(arm("ended", False, 0.0, done=True)) == 0)

    print("[3] probe result handler")
    def probe_case(alive, state_name="opening", fav="stremio:abc:0",
                   done=False, vid_s=0.0):
        fired = []
        view.current = {"kind": "stremio", "fav_key": fav,
                        "title": "T", "url": base + "/ok206"}
        view._closing = False
        view._vid_s = vid_s
        view._stremio_fallback_done = done
        view.vlc.state_name = lambda: state_name
        view.vlc.is_playing = lambda: False
        view._begin_stremio_fallback = lambda: fired.append(1)
        view._stremio_guard.start()
        view._on_stremio_probe(("ok", ("stremio:abc:0", alive, 1.5)))
        app.processEvents()
        return fired, view._stremio_guard.isActive()

    fired, guard_on = probe_case(False)
    check("dead probe -> early fallback + guard disarmed",
          len(fired) == 1 and guard_on is False)
    fired, guard_on = probe_case(True)
    check("alive probe -> no fallback, guard stays armed as backstop",
          len(fired) == 0 and guard_on is True)
    fired, _ = probe_case(False, fav="stremio:other:0")
    check("stale fav_key (user moved on) -> no fallback", len(fired) == 0)
    fired, _ = probe_case(False, state_name="playing")
    check("VLC already started (beat the probe home) -> no fallback",
          len(fired) == 0)
    fired, _ = probe_case(False, done=True)
    check("fallback already spent -> no second try", len(fired) == 0)

    print("[4] _find_catalog: best-overlap with a 60% word floor")
    metas = [
        {"id": "tt0350448", "name": "Real Time with Bill Maher"},
        {"id": "tt15248880", "name": "Adventure Time: Fionna & Cake"},
        {"id": "tt1305826", "name": "Adventure Time"},
    ]

    def search(results):
        return lambda q: list(results)

    # the incident shape exactly: Maher ranked FIRST, sharing 'Time'
    hit = stremio._find_catalog(search(metas), "Adventure Time")
    check("Maher-first ordering still matches Adventure Time",
          hit and hit["id"] == "tt1305826")
    # the wrong hit must not win even when the true title is ABSENT
    hit = stremio._find_catalog(
        search([metas[0], {"id": "tt0496", "name": "Tim Allen"}]),
        "Adventure Time")
    check("single shared word can never match (honest miss)",
          hit is None)
    # the incident's other file-name form: year left in the cleaned name
    hit = stremio._find_catalog(search(metas), "Adventure Time 2008")
    check("year-bearing query still matches Adventure Time",
          hit and hit["id"] == "tt1305826")
    # a one-word show must still hit (the old 'Silo' dead end)
    hit = stremio._find_catalog(
        search([{"id": "tt1212", "name": "Silo"},
                {"id": "tt3434", "name": "Silicon Valley"}]), "Silo")
    check("single-word name still matches", hit and hit["id"] == "tt1212")
    # canonical titles may drop a query word: 2 of 3 words is enough
    hit = stremio._find_catalog(
        search([{"id": "tt7142", "name": "Star Trek: The Next Generation"},
                {"id": "tt7143", "name": "Star Trek: Deep Space Nine"}]),
        "Star Trek TNG")
    check("partial canonical title (2 of 3 words) still matches",
          hit and hit["id"] == "tt7142")
    # the true match beats a same-overlap imposter by search order
    hit = stremio._find_catalog(
        search([{"id": "tt1", "name": "Time Bandits"},
                {"id": "tt2", "name": "Adventure Time"}]),
        "Adventure Time")
    check("full-overlap candidate beats partial regardless of order",
          hit and hit["id"] == "tt2")
    # movies flow through the same matcher
    movie_hit = stremio._find_catalog(
        search([{"id": "tt0001", "name": "The Time Machine",
                 "releaseInfo": "2002"},
                {"id": "tt0086", "name": "The Terminator",
                 "releaseInfo": "1984"}]),
        "The Terminator 1984")
    check("movie matcher picks the right title word-set",
          movie_hit and movie_hit["id"] == "tt0086")

    # ---- the 2026-09-04 field regression: _JUNK_RES's codec alternation
    # had no word boundaries, so the 'DV' token ate the middle of
    # 'Adventure' — clean_show_name turned every Adventure Time release
    # into the query 'A enture Time', a string no catalog title can ever
    # word-match, and the strict scorer (correctly) refused everything:
    # identity, prev/next and autoplay logged 'nothing found' all day
    # (player.log 2026-09-04 12:42-13:06, 5 misses in a row)
    release = ("Adventure.Time.S03E24.Ghost.Princess.1080p.HMAX.WEBRip."
               "DD.2.0.H.265.-EDGE2020.mkv")
    cleaned = stremio.clean_show_name(release)
    check("cleaner keeps 'Adventure' whole (DV strips as a token only)",
          cleaned == "Adventure Time")
    cleaned = stremio.clean_show_name(
        "Marvels.Multiverse.of.Madness.2022.1080p.WEBRip.x265.mkv")
    check("cleaner keeps 'Multiverse' whole (MULTI strips as a token only)",
          "Multiverse" in cleaned.split())
    cleaned = stremio.clean_movie_name(
        "Everything.Everywhere.All.At.Once.2022.2160p.BluRay.MULTI.DV."
        "HEVC.mkv")
    check("movie cleaner still strips whole MULTI/DV tokens",
          cleaned == "Everything Everywhere All At Once 2022")
    seen = []

    def recording_search(q):
        seen.append(q)
        return list(metas)

    # resolve_identity's exact call shape: clean first, then match
    hit = stremio._find_catalog(recording_search,
                                stremio.clean_show_name(release))
    check("incident release name resolves to Adventure Time",
          bool(hit) and hit["id"] == "tt1305826")
    check("searched query carries the whole word 'Adventure'",
          any("adventure" in q.lower().split() for q in seen))

    print("[4b] 2026-09-05 11:11: server-stats names must honor "
          "file_idx")
    # The incident: a debrid stall fell back to the local-server URL
    # /03727f0b.../73 of a 278-file 'Complete Series S01-S10' TiZU pack.
    # The pack's files[] order is bizarre (S10E13 at index 0, S01E01 at
    # 277 — the offsets confirm it is the torrent's real order), and the
    # stats scan took the FIRST S/E-marked name it walked past: S10E13.
    # The playing S03E22 got retitled as the series finale, ⏮ resolved
    # to S10E12 Gumbaldia and next/autoplay died (the finale has no
    # next). The next-episode click survived only because the correct
    # S03E23 lookahead had been prefetched BEFORE the bad identity
    # stomped cur (player.log 11:11:40-42, 11:23:16).
    PACK = ("Adventure.Time.2010.S01-S10.Complete.Series.1080p.BluRay."
            "EAC3.AV1-TiZU")
    F_S10E13 = "Adventure.Time.S10E13.1080p.BluRay.EAC3.AV1-TiZU.mkv"
    F_S03E22 = "Adventure.Time.S03E22.1080p.BluRay.EAC3.AV1-TiZU.mkv"
    F_MOVIE = ("Everything.Everywhere.All.At.Once.2022.1080p.BluRay."
               "MULTI.DV.HEVC.mkv")
    OTHER_TORRENT_FILE = ("Adventure Time (2008) - S08E19 - Jelly Beans "
                          "Have Power (1080p BluRay x265 ImE).mkv")

    def pack_files(played_name):
        files = [{"path": "p\\Season 10\\" + F_S10E13, "name": F_S10E13,
                  "length": 866189846, "offset": 0}]
        for i in range(1, 73):
            files.append({"path": "p\\Season 1\\f%d.mkv" % i,
                          "name": "filler.%d.mkv" % i, "length": 10 ** 6,
                          "offset": 10 ** 6 * i})
        files.append({"path": "p\\Season 3\\" + played_name,
                      "name": played_name, "length": 215183704,
                      "offset": 20273191820})
        return files

    class FakePackServer(stremio.StreamingServer):
        def __init__(self, played_name, with_stats=True):
            super().__init__("")
            self.played_name = played_name
            self.with_stats = with_stats

        def stats(self, info_hash, file_idx):
            if not self.with_stats:
                return None
            return {"infoHash": info_hash, "name": PACK,
                    "files": pack_files(self.played_name)}

        def _global_stats(self):
            return {"activeTorrents": [
                {"infoHash": "other", "name": OTHER_TORRENT_FILE,
                 "files": [{"name": OTHER_TORRENT_FILE}]}]}

    saved = (stremio.find_series, stremio.find_movie,
             stremio.series_meta, stremio._torrent_metainfo)
    try:
        stremio.find_series = \
            lambda name: (("tt1305826", "Adventure Time")
                          if "adventure" in name.lower() else None)
        stremio.find_movie = lambda q, year="": (
            {"id": "tt6652842", "name": "Everything Everywhere All at Once",
             "year": "2022", "poster": ""}
            if "everything everywhere" in q.lower() else None)
        stremio.series_meta = lambda imdb: {"name": "Adventure Time",
                                            "videos": [
            {"season": 3, "episode": 22, "name": "Paper Pete"},
            {"season": 3, "episode": 21, "name": "Marceline's Closet"},
        ]}
        stremio._torrent_metainfo = lambda h, file_idx=-1: None

        srv_url = ("http://127.0.0.1:11470/"
                   "03727f0bdebfd938dc6d56986ae7dc45429416dd/73")

        # the incident verbatim: identity of server URL file 73
        ident = stremio.resolve_identity(
            srv_url, FakePackServer(F_S03E22))
        check("pack URL /73 resolves to S03E22, not files[0] S10E13",
              ident and ident.get("season") == 3
              and ident.get("episode") == 22
              and ident.get("stremio_imdb") == "tt1305826")
        check("identity file_name is the played file's name",
              ident and ident.get("file_name") == F_S03E22)

        # torrent_names exposes the played file first
        played, names = FakePackServer(F_S03E22).torrent_names(
            "03727f0bdebfd938dc6d56986ae7dc45429416dd", 73)
        check("torrent_names returns files[73] as the played name",
              played == F_S03E22 and names[0] == F_S03E22)

        # a MOVIE file inside the pack: no marker on the played file —
        # the movie path must use its own name, never a sibling episode
        ident = stremio.resolve_identity(
            srv_url, FakePackServer(F_MOVIE))
        check("markerless pack file resolves as the right movie",
              ident and ident.get("movie") is True
              and ident.get("stremio_imdb") == "tt6652842"
              and ident.get("file_name") == F_MOVIE)

        # no per-torrent stats: the idx-scoped .torrent cache must beat
        # the flattened global scan (whose marker-bearing names belong
        # to OTHER torrents — the S08E19 junk above)
        stremio._torrent_metainfo = \
            lambda h, file_idx=-1: (PACK, F_S03E22)
        ident = stremio.resolve_identity(
            srv_url, FakePackServer(F_S03E22, with_stats=False))
        check("stats-less server URL prefers idx-picked metainfo over "
              "global-scan junk",
              ident and ident.get("season") == 3
              and ident.get("episode") == 22)

        # _torrent_metainfo itself: files[file_idx] beats largest-file
        def benc(obj):
            if isinstance(obj, dict):
                return b"d" + b"".join(
                    benc(k) + benc(v) for k, v in obj.items()) + b"e"
            if isinstance(obj, list):
                return b"l" + b"".join(benc(v) for v in obj) + b"e"
            if isinstance(obj, int):
                return b"i%de" % obj
            return b"%d:%s" % (len(obj), obj)

        torrent_blob = benc({b"info": {
            b"name": b"at-pack",
            b"files": [
                {b"length": 900, b"path": [b"Season 10",
                                            b"S10E13.mkv"]},
                {b"length": 100, b"path": [b"Season 3",
                                           b"S03E22.mkv"]},
            ]}})

        class FakeResp:
            status_code = 200
            content = torrent_blob

        saved_sess = stremio._session
        try:
            stremio._torrent_metainfo = saved[3]   # the real one
            stremio._session = type("S", (), {
                "get": staticmethod(lambda url, **kw: FakeResp())})()
            tname, fname = stremio._torrent_metainfo("a" * 40, 1)
            check("metainfo picks the entry AT file_idx",
                  fname == "S03E22.mkv")
            _, fname = stremio._torrent_metainfo("a" * 40)
            check("metainfo keeps largest-file default without an idx",
                  fname == "S10E13.mkv")
            _, fname = stremio._torrent_metainfo("a" * 40, 99)
            check("metainfo falls back to largest on a bad idx",
                  fname == "S10E13.mkv")
        finally:
            stremio._session = saved_sess
    finally:
        (stremio.find_series, stremio.find_movie,
         stremio.series_meta, stremio._torrent_metainfo) = saved

    print("[4c] 2026-09-07 13:01: streams cache evicts without dying "
          "at the 12-entry boundary")
    saved_qa = stremio._query_addon
    saved_ab = stremio._addon_bases
    try:
        stremio._streams_cache.clear()   # the PRODUCTION object — the
        # plain-dict bug lives in its type, so it must not be rebound
        bases = ("http://addon.fake",)
        stremio._addon_bases = lambda cfg: bases
        queries = []

        def fake_qa(base, url):
            queries.append(url)
            ep = int(url.rsplit(":", 1)[1].split(".")[0])
            return [{"title": "S01E%02d" % ep, "url": "http://x/%d" % ep}]

        stremio._query_addon = fake_qa
        cfg = Config({}, None)

        def key(ep):
            return ("tt7678620", 1, ep, bases)

        # the binge state at 13:01 exactly: 12 episodes already queried
        # (current + lookahead + prevlook over five watched episodes)
        for ep in range(1, 13):
            stremio._streams_cache[key(ep)] = (time.monotonic(), [])
        try:
            streams = stremio.addon_streams(cfg, "tt7678620", 1, 13)
            check("the 13th lookup does NOT die on popitem",
                  streams == [{"title": "S01E13", "url": "http://x/13"}])
        except Exception as exc:   # noqa: BLE001
            check("the 13th lookup does NOT die on popitem "
                  "(%r)" % exc, False)
            streams = None
        c = stremio._streams_cache
        check("cache stays bounded at 12", len(c) == 12)
        check("oldest-inserted entry (ep1) was the eviction victim",
              key(1) not in c and key(2) in c)
        check("the new episode is cached", key(13) in c)
        # the wedge made EVERY later lookup die the same way; one more
        # must succeed too
        try:
            streams = stremio.addon_streams(cfg, "tt7678620", 1, 14)
            check("the 14th lookup works as well",
                  streams == [{"title": "S01E14", "url": "http://x/14"}])
        except Exception as exc:   # noqa: BLE001
            check("the 14th lookup works as well (%r)" % exc, False)
        n = len(queries)
        try:
            stremio.addon_streams(cfg, "tt7678620", 1, 14)
        except Exception:      # noqa: BLE001 — pre-fix: pops again, dies
            pass
        check("a re-ask is served from the cache, not the addon",
              len(queries) == n)
    finally:
        stremio._streams_cache.clear()
        stremio._query_addon = saved_qa
        stremio._addon_bases = saved_ab

    print("[4d] 2026-09-08 10:18: a year LEFT of the episode marker "
          "must not reach the series query")
    # The incident: the morning's first debrid stream stalled, the
    # re-picked torbox stream relaunched as a torrentio resolve URL
    # embedding a Sonarr-style file name — 'Bluey (2018) - S01E50 -
    # Shaun (1080p DSNP WEB-DL x265 Garshasp).mkv'. clean_show_name
    # cut at the S/E marker, the end-strip peeled the ')' but left the
    # year as a query word ('Bluey (2018'), no series title carries
    # it, and the 60% floor failed EVERY lookup: the open-time
    # identity (10:10:39), the autoplay fire (10:18:01) and the next
    # click (10:18:28) all logged 'found no show' + 'nothing found'
    # while S01E51 sat one episode away.
    INCIDENT_URL = ("https://torrentio.strem.fun/resolve/torbox/"
                    "1f7be332-5fea-4aa8-8814-46eabea63736/"
                    "4443778978da9a14f1a873a6e0e7159c0dd7e490/"
                    "Bluey%20(2018)%20-%20S01E50%20-%20Shaun%20(1080p"
                    "%20DSNP%20WEB-DL%20x265%20Garshasp).mkv/49/Bluey"
                    "%20(2018)%20-%20S01E50%20-%20Shaun%20(1080p%20DSNP"
                    "%20WEB-DL%20x265%20Garshasp).mkv")
    saved_fs, saved_sm = stremio.find_series, stremio.series_meta
    queries = []
    try:
        def fake_find_series(q):
            queries.append(q)
            return ("tt7678620", "Bluey") if q == "Bluey" else None

        stremio.find_series = fake_find_series
        stremio.series_meta = lambda imdb: {
            "name": "Bluey",
            "videos": [{"season": 1, "episode": 50, "name": "Shaun"},
                       {"season": 1, "episode": 51,
                        "name": "Daddy Putdown"}]}
        ident = stremio.resolve_identity(
            INCIDENT_URL, stremio.StreamingServer(""))
        check("incident URL resolves to Bluey S01E50",
              bool(ident) and ident["stremio_imdb"] == "tt7678620"
              and ident["season"] == 1 and ident["episode"] == 50)
        check("episode title comes back with the identity",
              bool(ident) and ident.get("episode_name") == "Shaun")
        check("catalog was asked for 'Bluey', not 'Bluey (2018'",
              queries and queries[-1] == "Bluey")
        # the scorer itself was never the bug: with the right show in
        # the results, the un-fixed query shape still honestly misses
        # (2 query words, 60% floor needs 2, no title carries '2018')
        metas = [{"id": "tt7678620", "name": "Bluey"},
                 {"id": "tt999", "name": "The Bluey Show"}]
        hit = stremio._find_catalog(lambda q: list(metas), "Bluey (2018")
        check("the un-fixed query shape still misses (fix is in the "
              "cleaning, not the scorer)", hit is None)
        # the morning's FIRST url (debridio, year RIGHT of the marker)
        # resolved pre-fix and must keep resolving
        queries.clear()
        ident = stremio.resolve_identity(
            "https://addon.debridio.com/play/series/premiumize/0889be7c"
            "bed8417f47e58540afea55c0/tdgs5gcukfk2fcc3/3fa123ad17042b"
            "783436796ce7212ca24fbb8655/Bluey.S01E50.2018.2160p.WEB-DL"
            ".H265.AAC-BlackTV.mp4", stremio.StreamingServer(""))
        check("debridio dotted name (year right of marker) still resolves",
              bool(ident) and ident["episode"] == 50
              and queries == ["Bluey"])
        # defaults pinned: strip_year is release-head-only
        check("clean_show_name default keeps canonical title years",
              stremio.clean_show_name("Space: 1999") == "Space: 1999")
        check("movie cleaner keeps the release year",
              stremio.clean_movie_name(
                  "Everything.Everywhere.All.At.Once.2022.2160p.BluRay."
                  "MULTI.DV.HEVC.mkv")
              == "Everything Everywhere All At Once 2022")
        try:
            dotted = stremio.clean_show_name(
                "Bluey.2018.S01E50.1080p.DSNP.WEB-DL.x265",
                strip_year=True)
        except TypeError as exc:   # pre-fix: the kwarg does not exist
            dotted = "kwarg gone: %r" % exc
        check("bare year left of the marker strips too (dotted form)",
              dotted == "Bluey")
    finally:
        stremio.find_series, stremio.series_meta = saved_fs, saved_sm

    print("[4e] 2026-09-08 12:05: franchise sequels — the release YEAR "
          "must break the title tie")
    # The incident: the Spider-Man.2.2004 torbox handoff resolved as
    # 'Spider-Man (2002)' (the banner said so, twice) because the word
    # filter counts only words >2 chars — 'Spider Man 2' cannot tell
    # 'Spider-Man' (2002) from 'Spider-Man 2' (2004) and Cinemeta's
    # unstable order decided. The next-stream walk then queried the
    # WRONG MOVIE's stream list ('current stream not in the ranked
    # list — starting from the top') and switched playback to a 6 GB
    # Spider-Man 1 file mid-viewing (read by the user as 'cut several
    # minutes off the beginning').
    saved_sm2 = stremio.search_movies
    saved_cd = stremio._content_disposition
    try:
        qs = stremio._movie_queries(
            "Spider-Man.2.2004.UHD.BluRay.2160p.TrueHD.Atmos.7.1.DV.HEVC."
            "HYBRID.REMUX-FraMeSToR.mkv")
        check("incident name heads to 'Spider Man 2' + year 2004",
              bool(qs) and qs[0] == ("Spider Man 2", "2004"))

        def adversarial(q):
            # the 2002 ORIGINAL ranked first — the tie that lost live
            return [
                {"id": "tt0145487", "name": "Spider-Man",
                 "releaseInfo": "2002"},
                {"id": "tt0316654", "name": "Spider-Man 2",
                 "releaseInfo": "2004"},
                {"id": "tt1872181", "name": "The Amazing Spider-Man 2",
                 "releaseInfo": "2014"},
            ]
        stremio.search_movies = adversarial
        hit = stremio.find_movie("Spider Man 2", "2004")
        check("year breaks the sequel tie (2002 ranked first)",
              hit and hit["id"] == "tt0316654")
        hit = stremio.find_movie("Spider Man 2")
        check("no year -> search order still decides (the honest miss)",
              hit and hit["id"] == "tt0145487")

        # end-to-end on the incident's exact handoff URL
        stremio._content_disposition = lambda u: ""
        ident = stremio.resolve_identity(
            "https://torrentio.strem.fun/resolve/torbox/1f7be332-5fea-4aa8-"
            "8814-46eabea63736/2e3be9519b9118c61c828b03571575902a444947/"
            "Spider-Man.2.2004.UHD.BluRay.2160p.TrueHD.Atmos.7.1.DV.HEVC."
            "HYBRID.REMUX-FraMeSToR.mkv/0/Spider-Man.2.2004.UHD.BluRay."
            "2160p.TrueHD.Atmos.7.1.DV.HEVC.HYBRID.REMUX-FraMeSToR.mkv",
            stremio.StreamingServer(""))
        check("incident handoff URL resolves to Spider-Man 2 (2004)",
              bool(ident) and ident.get("movie") is True
              and ident["stremio_imdb"] == "tt0316654"
              and ident["movie_name"] == "Spider-Man 2")
    finally:
        stremio.search_movies = saved_sm2
        stremio._content_disposition = saved_cd

    print("[5] wiring: play_media kicks the probe with the guard")
    src_pm = inspect.getsource(pv_mod.PlayerView.play_media)
    check("play_media arm block kicks the probe",
          "self._kick_stremio_probe(url)" in src_pm)
    src_probe = inspect.getsource(pv_mod.PlayerView._kick_stremio_probe)
    check("probe asks for a 3 s verdict",
          "probe_debrid(url, timeout_s=3.0)" in src_probe)
    check("probe_debrid lives in src/stremio.py with a range GET",
          callable(stremio.probe_debrid))

    # ------------------------------------------------------------------
    # the next-stream feature (2026-09-08 request): "if I load into a
    # stream and it's wrong — wrong language, no subtitles — press a
    # button and automatically try the next stream of the same episode/
    # movie, following the same provider-preference rules as next
    # episode. Prefer English in both language and subtitles."
    print("[6] English-preference ranking + next_stream_playable")
    H = {"a": "a" * 40, "b": "b" * 40, "c": "c" * 40, "d": "d" * 40,
         "e": "e" * 40, "f": "f" * 40}

    def s_url(tag, title, res_hint="1080p"):
        return {"name": "Addon \U0001F464 20 \U0001F4BE 2 GB %s" % res_hint,
                "title": title, "url": "http://debrid.invalid/%s" % tag,
                "infoHash": H[tag]}

    A = s_url("a", "Adventure.Time.S05E42.1080p.WEB-DL.DDP5.1.x264-NTb")
    B = s_url("b", "Adventure.Time.S05E42.1080p.BluRay.x264-ROVERS")
    C = {"name": "Addon \U0001F464 5 \U0001F4BE 1 GB 720p",
         "title": "Adventure.Time.S05E42.720p.WEB-DL.AAC2.0.H.264",
         "infoHash": H["c"], "fileIdx": 3}
    D = s_url("d", "Adventure.Time.S05E42.VOSTFRENCH.1080p.WEB-DL")

    check("plain release: no language penalty",
          stremio.lang_penalty(A) == 0)
    check("VOSTFRENCH release: foreign penalty",
          stremio.lang_penalty(D) == 1)
    check("dual audio release: no penalty (has English)",
          stremio.lang_penalty(
              {"title": "Anime.S01E01.1080p.DUAL.AUDIO.EN.JP"}) == 0)
    check("MULTI release: no penalty",
          stremio.lang_penalty(
              {"title": "Film.2023.2160p.MULTI.WEB-DL"}) == 0)
    # a foreign dub outranks NOTHING anymore: the plain 720p beats the
    # TRUEFRENCH 4K (the user prefers English in audio AND subtitles)
    fr_4k = {"title": "Film.2023.TRUEFRENCH.2160p.BluRay", "url": "http://x",
             "infoHash": H["d"]}
    plain_720 = {"title": "Film.2023.720p.WEB-DL.x264", "url": "http://y",
                 "infoHash": H["c"]}
    cfg2 = Config({}, None)
    check("plain 720p outranks a foreign-language 4K",
          stremio.best_stream(cfg2, [fr_4k, plain_720]) is plain_720)

    saved_qa, saved_ab = stremio._query_addon, stremio._addon_bases
    saved_srv = stremio.StreamingServer
    stremio._streams_cache.clear()
    bases = ("http://addon.invalid",)
    stremio._addon_bases = lambda cfg: bases
    asked = []
    ALL = [A, B, C, D]

    def fake_qa(base, url):
        asked.append(url)
        return [dict(s) for s in ALL]

    stremio._query_addon = fake_qa

    class FakeServer:
        def __init__(self, base=""):
            self.created = []

        def create(self, info_hash, trackers=()):
            self.created.append(info_hash)
            return True

        @staticmethod
        def play_url(info_hash, file_idx):
            return "http://server.invalid/%s/%d" % (info_hash, file_idx)

    stremio.StreamingServer = FakeServer

    def cur_of(stream, **extra):
        p = {"kind": "stremio",
             "title": "Adventure Time — S05E42 James",
             "url": stream.get("url")
             or "http://server.invalid/%s/%d" % (stream["infoHash"],
                                                 stream.get("fileIdx", 0)),
             "fav_key": "stremio:tt1305826:5:42",
             "stremio_imdb": "tt1305826", "season": 5, "episode": 42,
             "series_name": "Adventure Time"}
        if stream.get("infoHash"):
            p["info_hash"] = stream["infoHash"]
            p["file_idx"] = int(stream.get("fileIdx") or 0)
        p.update(extra)
        return p

    try:
        ranked = stremio.rank_streams(cfg2, ALL, cur_of(A))
        check("ranked order: 1080 pair, then 720p, French last",
              [s["url"] for s in ranked[:2]] == [A["url"], B["url"]]
              and ranked[2] is C and ranked[3] is D)
        check("best_stream is the head of rank_streams",
              stremio.best_stream(cfg2, ALL, cur_of(A)) is ranked[0])

        # walk the whole list, pressing the button each time
        nxt = stremio.next_stream_playable(cfg2, cur_of(A))
        check("from the best stream -> the second one (B)",
              nxt and nxt["url"] == B["url"]
              and nxt["info_hash"] == H["b"])
        check("identity carried: same fav_key/episode/title",
              nxt["fav_key"] == "stremio:tt1305826:5:42"
              and (nxt["season"], nxt["episode"]) == (5, 42)
              and nxt["series_name"] == "Adventure Time"
              and nxt["title"].startswith("Adventure Time"))
        nxt2 = stremio.next_stream_playable(cfg2, cur_of(B))
        check("from B -> the 720p TORRENT via the local server",
              nxt2 and nxt2["url"] == "http://server.invalid/%s/3" % H["c"]
              and nxt2["info_hash"] == H["c"] and nxt2["file_idx"] == 3)
        nxt3 = stremio.next_stream_playable(cfg2, cur_of(C))
        check("from C -> the demoted French one (last resort, not skipped)",
              nxt3 and nxt3["url"] == D["url"])
        check("from the last stream -> None (button says 'no other')",
              stremio.next_stream_playable(cfg2, cur_of(D)) is None)

        # the same torrent on two addons is the SAME release: the walk
        # must not re-serve it under the second addon's URL
        stremio._streams_cache.clear()
        A2 = dict(A)
        A2["_addon"] = "http://addon2.invalid"
        A2["url"] = "http://debrid2.invalid/a"
        stremio._query_addon = lambda base, url: [dict(s) for s in (A, A2, B)]
        nxt = stremio.next_stream_playable(cfg2, cur_of(A))
        check("same torrent on a second addon is skipped, not re-served",
              nxt and nxt["url"] == B["url"])

        # current not among the results (handoff from Stremio's own pick):
        # the walk starts from the top, logged
        stremio._streams_cache.clear()
        stremio._query_addon = fake_qa
        outsider = cur_of({"infoHash": "f" * 40, "fileIdx": 0})
        nxt = stremio.next_stream_playable(cfg2, outsider)
        check("unmatched current -> the best ranked stream",
              nxt and nxt["url"] == A["url"])

        # the 2026-09-08 11:34 incident (Alone Australia S04E05): the
        # local streaming server, busy serving the very stream being
        # watched, timed out (20 s ReadTimeout) creating the rank-2
        # TORRENT — the old walk died right there and the button
        # reported 'no other stream' while later candidates sat unused
        # behind it. A failed create must cost ONE timeout (latched) and
        # skip only torrent candidates, never the whole switch.
        class BusyServer(FakeServer):
            def create(self, info_hash, trackers=()):
                self.created.append(info_hash)
                return False            # the ReadTimeout shape

        E = {"name": "Addon \U0001F464 2 \U0001F4BE 0.5 GB 480p",
             "title": "Adventure.Time.S05E42.480p.WEB-DL.x264",
             "infoHash": "0" * 40, "fileIdx": 0}
        stremio._streams_cache.clear()
        stremio._query_addon = \
            lambda base, url: [dict(s) for s in (A, B, C, E, D)]
        busy = BusyServer("")
        stremio.StreamingServer = lambda base="": busy
        nxt = stremio.next_stream_playable(cfg2, cur_of(B))
        check("busy server: create failure walks past the torrent to the "
              "next candidate (incident 11:34)",
              nxt and nxt["url"] == D["url"])
        check("one dead engine cost exactly ONE create attempt",
              len(busy.created) == 1)

        # only torrents left + dead server -> None, again after one attempt
        stremio._streams_cache.clear()
        stremio._query_addon = lambda base, url: [dict(s) for s in (C, E)]
        busy2 = BusyServer("")
        stremio.StreamingServer = lambda base="": busy2
        nxt = stremio.next_stream_playable(cfg2, cur_of(C))
        check("dead server with only torrents left -> None after one "
              "attempt", nxt is None and len(busy2.created) == 1)
        stremio.StreamingServer = FakeServer

        # movies: the movie endpoint, and movie identity carried
        stremio._streams_cache.clear()
        asked.clear()
        M1 = s_url("e", "Everything.Everywhere.All.at.Once.2022.1080p.WEB")
        M2 = s_url("f", "Everything.Everywhere.All.at.Once.2022.2160p.WEB")
        stremio._query_addon = \
            lambda base, url: (asked.append(url)
                               or [dict(s) for s in (M1, M2)]
                               if "/stream/movie/" in url else [])
        mcur = {"kind": "stremio", "movie": True,
                "title": "Everything Everywhere All at Once (2022)",
                "url": M1["url"], "fav_key": "stremio:url:old",
                "info_hash": H["e"], "stremio_imdb": "tt6652842",
                "movie_name": "Everything Everywhere All at Once",
                "year": "2022"}
        mn = stremio.next_stream_playable(cfg2, mcur)
        check("movie asked the MOVIE endpoint",
              any("/stream/movie/tt6652842.json" in u for u in asked))
        check("movie walk: best -> second, movie identity carried",
              mn and mn["url"] == M2["url"] and mn.get("movie") is True
              and mn.get("movie_name") == mcur["movie_name"]
              and mn.get("year") == "2022")
    finally:
        stremio._streams_cache.clear()
        stremio._query_addon = saved_qa
        stremio._addon_bases = saved_ab
        stremio.StreamingServer = saved_srv

    print("[7] next-stream UI: button, resume position, staleness, "
          "coalescing, S shortcut")
    played = []

    def wait_for(cond, timeout=5.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            app.processEvents()
            if cond():
                return True
            time.sleep(0.02)
        app.processEvents()
        return cond()

    saved_nsp = stremio.next_stream_playable

    def mk_view(cur):
        v = PlayerView(Config({}, None))
        v._closing = False
        v._attach_done = True
        v.current = cur
        v.show()
        v._update_control_state()
        v._apply_button_visibility()
        rec = []

        def _fake_play(p, s=0.0):
            rec.append((p.get("fav_key"), s, p.get("url")))
            v.current = p
        v.play_media = _fake_play
        return v, rec

    e42_cur = cur_of(A)
    v, rec = mk_view(e42_cur)
    v.vlc.get_time = lambda: 313000          # 5:13 into the episode
    v.vlc.get_length = lambda: 705000
    stremio.next_stream_playable = \
        lambda cfg, cur: dict(e42_cur, url=B["url"], info_hash=H["b"])
    try:
        check("button visible + enabled on a Stremio episode",
              not v.btn_stream.isHidden() and v.btn_stream.isEnabled())
        v.btn_stream.click()
        check("click switched to the second stream",
              wait_for(lambda: rec and rec[0][2] == B["url"]))
        check("same episode resumed where it stood (313 s)",
              rec and abs(rec[0][1] - 313.0) < 0.01)
        check("current kept the episode's fav_key",
              (v.current or {}).get("fav_key") == "stremio:tt1305826:5:42")
        v.stop()

        # near the credits: start the replacement from zero (never land
        # it straight into end-of-media autoplay)
        v, rec = mk_view(cur_of(A))
        v.vlc.get_time = lambda: 690000       # 15 s before the end
        v.vlc.get_length = lambda: 705000
        v.btn_stream.click()
        check("resume near the end starts over at 0",
              wait_for(lambda: rec and rec[0][1] == 0.0))
        v.stop()

        # nothing else to try: the user is told, nothing switches
        v, rec = mk_view(cur_of(A))
        notes = []
        v.show_info = lambda text, **kw: notes.append(text)
        stremio.next_stream_playable = lambda cfg, cur: None
        v.btn_stream.click()
        check("'no other stream' banner shown",
              wait_for(lambda: any("No other stream" in t for t in notes)))
        check("no switch happened", not rec)
        v.stop()

        # spam coalesces into ONE lookup; the kind guard drops non-stremio
        v, rec = mk_view(cur_of(A))
        calls = []
        v._fetch_next_stream = lambda cur: calls.append(1) or None
        v._stream_busy_base = e42_cur["fav_key"]
        v.btn_stream.click()
        check("in-flight lookup: click coalesced",
              not calls and not rec)
        v._stream_busy_base = ""
        v.current = {"kind": "live", "title": "L", "url": "http://x",
                     "fav_key": "live:1"}
        v._update_control_state()
        v._apply_button_visibility()
        check("button hidden + disabled on live TV",
              v.btn_stream.isHidden() and not v.btn_stream.isEnabled())
        v.btn_stream.click()
        check("live click dropped by the kind guard", not calls)
        v.stop()

        # a Stremio MOVIE gets the button even without episode identity
        v, rec = mk_view(dict(mcur))
        check("button visible on a Stremio movie", not v.btn_stream.isHidden())
        v.stop()

        # the S shortcut is a window-level QShortcut wired to the handler
        from src.ui import main_window as mw_mod
        src_sc = inspect.getsource(mw_mod.MainWindow._setup_shortcuts)
        check("S shortcut is a window-level QShortcut wired to the handler",
              'QKeySequence("S")' in src_sc
              and "_next_stream_clicked" in src_sc)
    finally:
        stremio.next_stream_playable = saved_nsp

    print("[8] resume-shaped reopens keep the timeline (2026-09-08: "
          "'reload went to the right place but the timeline said 0:00')")
    # Every resume path (reload button, VOD stall rescue, next-stream
    # switch) funnels through play_media(start_at=...): VLC reopens AT
    # the resume point, but the tracked position was reset to 0 and the
    # tick's 3 s snap guard then rejected VLC's clock as 'too far' —
    # the scrubber crawled up from 0:00 at 0.4 s/tick and snapped the
    # bar back under drags. play_media now seeds the tracker with the
    # resume point (the same re-base _seek_ms/_jump_begin do).
    tv = PlayerView(Config({}, None))
    tv._closing = False
    tv._attach_done = True
    opened = []
    tv.vlc.play = lambda *a, **k: opened.append(k.get("start_seconds"))
    tv.vlc.stop_and_release = lambda: None
    tv.vlc.get_time = lambda: 313500        # 5:13.5 — where it reopened
    tv.vlc.get_length = lambda: 705000
    tv.vlc.state_name = lambda: "playing"
    tv.vlc.is_playing = lambda: True
    tv._begin_stremio_lookahead = lambda: None   # no network off a test open
    tv._begin_stremio_prevlook = lambda: None
    tv._on_media_for_profanity = lambda kind: None
    try:
        cur8 = {"kind": "stremio", "title": "Adventure Time — S05E42",
                "url": "http://127.0.0.1:11470/" + "a" * 40 + "/0",
                "fav_key": "stremio:tt1305826:5:42",
                "stremio_imdb": "tt1305826", "season": 5, "episode": 42,
                "series_name": "Adventure Time", "info_hash": "a" * 40,
                "file_idx": 0}
        tv.play_media(cur8, start_at=313.0)
        check("VLC reopened at the resume point",
              opened == [313.0])
        check("tracker seeded with the resume point (not 0:00)",
              tv._vid_s == 313.0)
        tv._tick()                          # one real tick, playing
        check("tick ACCEPTS the resumed clock (no crawl from zero)",
              313.0 <= tv._vid_s <= 314.5)
        check("scrubber shows the resumed position",
              tv.slider.value() >= 313000
              and tv.time_left.text() != "0:00")
        # and a fresh (non-resume) open still starts the tracker at 0
        tv.play_media(dict(cur8), start_at=0.0)
        check("a non-resume open still seeds 0",
              opened[-1] == 0.0 and tv._vid_s == 0.0)
    finally:
        tv.stop()

    view.stop()
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
