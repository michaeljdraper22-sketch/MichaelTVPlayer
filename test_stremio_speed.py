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
        stremio.find_movie = lambda q: (
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

    print("[5] wiring: play_media kicks the probe with the guard")
    src_pm = inspect.getsource(pv_mod.PlayerView.play_media)
    check("play_media arm block kicks the probe",
          "self._kick_stremio_probe(url)" in src_pm)
    src_probe = inspect.getsource(pv_mod.PlayerView._kick_stremio_probe)
    check("probe asks for a 3 s verdict",
          "probe_debrid(url, timeout_s=3.0)" in src_probe)
    check("probe_debrid lives in src/stremio.py with a range GET",
          callable(stremio.probe_debrid))

    view.stop()
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
