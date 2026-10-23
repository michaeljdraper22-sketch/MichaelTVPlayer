# -*- coding: utf-8 -*-
"""Offscreen regression test for the fast-debrid-handoff package:

[1] stremio.probe_debrid against a local HTTP server: 206/200/redirect
    read as alive; 502 / connection-refused / hang read as dead — and a
    hang returns within the requested timeout, not the default 3 s.
    The probe must ask for ONE byte (Range: bytes=0-0), never the body.
    A 2xx that lands on the resolve host's /videos/failed* placeholder
    (torrentio masks a dead debrid resolve as a 302 to a ~137 KB error
    mp4 — live-seen 2026-09-29) reads as DEAD despite the 200.
[2] _stremio_guard_tick alive rule: only playing/paused or >5 s of
    tracked playback counts as alive. The opening-hang shape (VLC stuck
    in 'opening', is_playing() True — live-seen 2026-09-02 16:01, 55 s
    dead screen) must now advance after the 10 s budget; a mid-play
    stall (>5 s played) must NOT restart the stream from zero.
[3] _on_stremio_probe: a dead probe advances to the next-ranked stream
    immediately; an alive probe, a stale fav_key, an already-done
    advance, or a VLC that already started all leave it alone. A VOD
    relay that is ALIVE on the same url (settled total or cached bytes
    — it read the link seconds ago) OVERRULES a dead probe verdict
    (live-seen 2026-09-29 16:12: a 0.0 s probe killed a healthy handoff
    whose splitter was mid-prefetch, and the advance landed on a dead
    replacement).
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
            elif self.path.startswith("/failvid"):
                # torrentio's dead-resolve shape: 302 to its own
                # placeholder error mp4 (which answers a real 200)
                self.send_response(302)
                self.send_header(
                    "Location", "/videos/failed_unexpected_v3.mp4")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path.startswith("/videos/failed"):
                body = b"f" * 100        # the 136,909-byte error clip
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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
    check("failure-video redirect (200 on a dead resolve) -> dead",
          stremio.probe_debrid(base + "/failvid", timeout_s=3.0) is False)
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
        dead-stream advance began."""
        fired = []
        view.current = {"kind": "stremio", "fav_key": "stremio:abc:0",
                        "title": "T", "url": base + "/ok206"}
        view._closing = False
        view._vid_s = vid_s
        view._stremio_nextstream_done = done
        view._stremio_guard_t0 = pv_mod.now_s() - age_s
        view.vlc.state_name = lambda: state_name
        view.vlc.is_playing = lambda: playing
        view._begin_stremio_nextstream = lambda: fired.append(1)
        view._stremio_guard_tick()
        app.processEvents()
        return fired

    check("opening-hang (is_playing() True, 0 s played) -> advance fires",
          len(arm("opening", True, 0.0)) == 1)
    check("same shape inside the 10 s budget -> still waiting",
          len(arm("opening", True, 0.0, age_s=4.0)) == 0)
    check("playing -> alive, no advance",
          len(arm("playing", True, 600.0)) == 0)
    check("paused -> alive, no advance",
          len(arm("paused", False, 600.0)) == 0)
    check("502 shape (ended, 0 s played) -> advance fires",
          len(arm("ended", False, 0.0)) == 1)
    check("played >5 s then ended -> alive (EOF paths own it)",
          len(arm("ended", False, 120.0)) == 0)
    check("mid-play buffering stall -> alive (no restart from zero)",
          len(arm("buffering", True, 120.0)) == 0)
    check("advance already spent -> nothing left to try",
          len(arm("ended", False, 0.0, done=True)) == 0)

    print("[3] probe result handler")

    class _FakeRelay:
        def __init__(self, url, alive=True, total=0, cached=0):
            self.url = url
            self._alive = alive
            self.total = total
            self.cache_size = cached

    def probe_case(alive, state_name="opening", fav="stremio:abc:0",
                   done=False, vid_s=0.0, relay=None):
        fired = []
        view.current = {"kind": "stremio", "fav_key": fav,
                        "title": "T", "url": base + "/ok206"}
        view._closing = False
        view._vid_s = vid_s
        view._stremio_nextstream_done = done
        view._vod_relay = relay
        view.vlc.state_name = lambda: state_name
        view.vlc.is_playing = lambda: False
        view._begin_stremio_nextstream = lambda: fired.append(1)
        view._stremio_guard.start()
        view._on_stremio_probe(("ok", ("stremio:abc:0", alive, 1.5)))
        app.processEvents()
        return fired, view._stremio_guard.isActive()

    fired, guard_on = probe_case(False)
    check("dead probe -> early advance + guard disarmed",
          len(fired) == 1 and guard_on is False)
    fired, guard_on = probe_case(True)
    check("alive probe -> no advance, guard stays armed as backstop",
          len(fired) == 0 and guard_on is True)
    fired, _ = probe_case(False, fav="stremio:other:0")
    check("stale fav_key (user moved on) -> no advance", len(fired) == 0)
    fired, _ = probe_case(False, state_name="playing")
    check("VLC already started (beat the probe home) -> no advance",
          len(fired) == 0)
    fired, _ = probe_case(False, done=True)
    check("advance already spent -> no second try", len(fired) == 0)
    # the relay override (2026-09-29 16:12 shape): VLC still 'opening'
    # through the async relay startup, probe says dead — but the relay
    # on THIS url already read the link (settled total / cached bytes)
    fired, guard_on = probe_case(
        False, relay=_FakeRelay(base + "/ok206", total=1287333531))
    check("relay serving this link (total settled) -> dead probe "
          "overruled, no advance, guard armed",
          len(fired) == 0 and guard_on is True)
    fired, guard_on = probe_case(
        False, relay=_FakeRelay(base + "/ok206", cached=2621440))
    check("relay serving this link (bytes cached) -> no advance",
          len(fired) == 0 and guard_on is True)
    fired, _ = probe_case(
        False, relay=_FakeRelay("http://elsewhere/x", total=999))
    check("relay on a DIFFERENT url (stale) -> advance fires",
          len(fired) == 1)
    fired, _ = probe_case(
        False, relay=_FakeRelay(base + "/ok206", alive=False, total=999))
    check("dead relay proves nothing -> advance fires", len(fired) == 1)

    print("[3b] dead-debrid advance chain (guard -> lookup -> switch)")
    # The full wiring the probe/guard fire: _begin_stremio_nextstream
    # -> worker (next_stream_playable, debrid-only, dead-hash excluded)
    # -> _on_stremio_nextstream -> _start_next -> play_media. Canned
    # lookup; the engine's create() must never be called.
    nsp_calls = []
    real_nsp = stremio.next_stream_playable
    stremio.next_stream_playable = (
        lambda cfg, cur, exclude=None, debrid_only=None,
        reanchor_click=False:
        nsp_calls.append({"debrid_only": debrid_only,
                          "exclude": set(exclude or ()),
                          "reanchor_click": reanchor_click}) or {
            "kind": "stremio", "title": "T",
            "fav_key": "stremio:abc:0",
            "url": "https://debrid.example/next.mkv"})
    played = []
    real_play = view.play_media
    view.play_media = lambda media, start_at=0.0: \
        played.append((media.get("url"), start_at))
    try:
        # [2]/[3] stubbed the advance on this instance — put the real
        # method back before driving the chain
        view._begin_stremio_nextstream = \
            PlayerView._begin_stremio_nextstream.__get__(view)
        view.current = {"kind": "stremio", "fav_key": "stremio:abc:0",
                        "title": "T", "url": base + "/ok206",
                        "info_hash": "a" * 40, "file_idx": 3}
        view._closing = False
        view._stremio_nextstream_done = False
        view._stremio_dead = set()
        view._stremio_dead_key = ""
        view._stremio_chain = 0
        view._begin_stremio_nextstream()
        for _ in range(300):
            app.processEvents()
            if played:
                break
            time.sleep(0.01)
        check("advance chain switches playback to the next debrid stream",
              played and played[0] == ("https://debrid.example/next.mkv",
                                       0.0))
        check("advance chain asks debrid-only with the dead hash excluded",
              nsp_calls and nsp_calls[0]["debrid_only"] is True
              and "a" * 40 in nsp_calls[0]["exclude"])
        check("advance chain asks for the click-context re-anchor "
              "(never-played stream: recover what page was clicked)",
              bool(nsp_calls) and nsp_calls[0]["reanchor_click"] is True)
    finally:
        stremio.next_stream_playable = real_nsp
        view.play_media = real_play

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

    # ---- the 2026-10-16 apostrophe incident: release heads spell the
    # title without the apostrophe ('Hells Kitchen (2005) S13E01') while
    # the catalog keeps it ("Hell's Kitchen") — \W+ split the catalog
    # title into 'hell'+'s', the release token 'hells' never matched
    # 'hell', the 60% floor failed on a 2-word name (no trim rung below
    # 2 words), and identity/autoplay/subtitles all died with 'catalog
    # search found no show' (player.log 12:08 and 12:21, twice the same
    # night). Both spellings must now resolve to the same show.
    hk_metas = [
        {"id": "tt0437005", "name": "Hell's Kitchen",
         "releaseInfo": "2005-"},
        {"id": "tt0412137", "name": "Hell's Kitchen",
         "releaseInfo": "2004-2009"},
        {"id": "tt29432151", "name": "Hell's Kitchen Famosos",
         "releaseInfo": "2023-"},
        {"id": "tt1626459", "name": "My Kitchen Rules",
         "releaseInfo": "2010-"},
        {"id": "tt3475084", "name": "The Kitchen",
         "releaseInfo": "2014-"},
    ]
    hk_release = ("Hells Kitchen (2005) S13E01 - Hollywood Premiere "
                  "Shocks New Chefs - WEBDL-1080p.mp4")
    hit = stremio._find_catalog(search(hk_metas), "Hells Kitchen")
    check("apostrophe-less release matches the apostrophe title",
          hit and hit["id"] == "tt0437005")
    hit = stremio._find_catalog(search(hk_metas), "Hell's Kitchen US")
    check("apostrophe release still matches (no regression)",
          hit and hit["id"] == "tt0437005")
    hit = stremio._find_catalog(
        search(hk_metas), stremio.clean_show_name(hk_release,
                                                  strip_year=True))
    check("the incident's exact release name resolves (clean+match)",
          hit and hit["id"] == "tt0437005")
    hit = stremio._find_catalog(
        search([{"id": "tt9", "name": "Hell or High Water"}]),
        "Hells Kitchen")
    check("folding does not blur DIFFERENT words (hells != hell)",
          hit is None)

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
        check("debrid context (B): the torrent-only C is SKIPPED, "
              "the next DIRECT stream (D) is served",
              nxt2 and nxt2["url"] == D["url"]
              and nxt2.get("info_hash") == H["d"])
        nxt3 = stremio.next_stream_playable(cfg2, cur_of(C))
        check("engine context (C): the demoted French one is still "
              "reachable last-resort",
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
        # engine-context cur (the incident's shape: the engine was
        # serving the very stream being watched); a debrid-context cur
        # would never ask the engine at all (see the gate block below)
        nxt = stremio.next_stream_playable(cfg2, cur_of(C))
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

        # ---- 2026-09-19 debrid gate: a direct (debrid) link in hand
        # means NO automatic switch may land on the local-server torrent
        # engine — the machine never joins a swarm from a debrid
        # context (user policy: debrid services back the torrents).
        check("_wants_debrid_only: debrid/direct url -> True",
              stremio._wants_debrid_only(
                  {"url": "https://torbox.app/friede/file.mkv"}) is True)
        check("_wants_debrid_only: local-server url -> False",
              stremio._wants_debrid_only(
                  {"url": "http://127.0.0.1:11470/" + "a" * 40 + "/2"})
              is False)
        check("_wants_debrid_only: no url -> False",
              stremio._wants_debrid_only({}) is False)
        stremio._streams_cache.clear()
        stremio._query_addon = \
            lambda base, url: [dict(s) for s in (A, B, C, E, D)]
        gate_srv = FakeServer("")
        stremio.StreamingServer = lambda base="": gate_srv
        nxt = stremio.next_stream_playable(cfg2, cur_of(B))
        check("gate: debrid context walks straight past every "
              "torrent-only candidate",
              nxt and nxt["url"] == D["url"]
              and gate_srv.created == [])
        nxt = stremio.next_stream_playable(cfg2, cur_of(C), debrid_only=True)
        check("gate: explicit debrid_only forces direct links even from "
              "an engine url",
              nxt and nxt["url"] == D["url"]
              and gate_srv.created == [])
        # autoplay / next-episode (via _adjacent_playable) honors the
        # same gate: the next EPISODE must also land on a direct link.
        # Candidates: the 720p torrent-only C outranks the demoted
        # French debrid D — exactly the shape that used to autoplay
        # straight onto the engine.
        saved_meta = stremio.series_meta
        stremio.series_meta = lambda imdb: {
            "name": "Adventure Time",
            "videos": [{"season": 5, "episode": 42, "name": "James"},
                       {"season": 5, "episode": 43, "name": "Oracle"}]}
        try:
            stremio._streams_cache.clear()
            stremio._query_addon = \
                lambda base, url: [dict(s) for s in (C, D)]
            ap = stremio.next_playable(cfg2, cur_of(B))
            check("autoplay from a debrid link -> next episode on a "
                  "DIRECT stream, engine never asked",
                  ap and ap["url"] == D["url"]
                  and (ap["season"], ap["episode"]) == (5, 43)
                  and gate_srv.created == [])
            stremio._streams_cache.clear()
            ap = stremio.next_playable(cfg2, cur_of(C))
            check("autoplay from an engine link keeps engine rights "
                  "(non-debrid setups)",
                  ap and ap["url"].startswith("http://server.invalid/")
                  and gate_srv.created == [H["c"]])
        finally:
            stremio.series_meta = saved_meta
        stremio.StreamingServer = FakeServer

        # ---- 2026-09-19 10:06 incident: the addons list is PLAIN
        # torrentio (no debrid key), so every candidate of every episode
        # is torrent-only — the debrid-only walk and the autoplay gate
        # served NOTHING ("no other stream") while 19-22 streams sat
        # listed, minutes after the same torrents played in Stremio web.
        # The handoff URL is a torrentio resolve link: its provider+key
        # prefix is a reusable resolve pipeline, so ranked torrents
        # re-resolve through the user's OWN debrid (still direct links
        # — the machine never joins the swarm).
        H["g"] = "g" * 40
        RU = ("https://torrentio.strem.fun/resolve/torbox/key-1234/"
              "%s/Adventure.Time.S05E42.1080p.WEB-DL.NTb.mkv/2/"
              "Adventure.Time.S05E42.1080p.WEB-DL.NTb.mkv" % H["g"])
        check("resolve_base: torrentio resolve prefix extracted",
              stremio.resolve_base(RU) ==
              "https://torrentio.strem.fun/resolve/torbox/key-1234/")
        # live debridio shape (2026-10-13): /play/{type}/{provider}/
        # {addonId}/{userKey}/{40-hex}/{file} — a reusable pipeline now
        DB = ("https://addon.debridio.com/play/series/torbox/"
              "0889be7cbed8417f47e58540afea55c0/"
              "1f7be332-5fea-4aa8-8814-46eabea63736/")
        check("resolve_base: debridio play prefix extracted",
              stremio.resolve_base(DB + H["c"] + "/Some.Show.S01E01.mkv")
              == DB)
        check("resolve_base: server/direct urls and a hash-less debridio "
              "stub -> \"\"",
              stremio.resolve_base("http://127.0.0.1:11470/%s/2" % H["g"])
              == "" and stremio.resolve_base(
                  "https://addon.debridio.com/play/series/premiumize/"
                  "k/x/NAME") == "" and stremio.resolve_base(
                  "https://torbox.app/friede/file.mkv") == "")
        built = stremio.build_resolve_url(
            stremio.resolve_base(RU), H["c"],
            "Adventure.Time.S05E42.720p.WEB-DL.AAC2.0.H.264.mkv", 3)
        check("build_resolve_url: parse_resolve_url recovers the "
              "identity (dead-chain exclusion keeps working)",
              stremio.parse_resolve_url(built) == (H["c"], 3))
        dbuilt = stremio.build_resolve_url(
            stremio.resolve_base(DB + H["c"] + "/x.mkv"), H["b"],
            "Adventure.Time.S05E42.1080p.BluRay.x264-DEiMOS.mkv", 0)
        check("build_resolve_url: debridio shape /{hash}/{name}, "
              "identity recoverable",
              dbuilt == DB + H["b"] + "/Adventure.Time.S05E42.1080p."
              "BluRay.x264-DEiMOS.mkv"
              and stremio.parse_resolve_url(dbuilt) == (H["b"], 0))

        # ALL FOUR of the user's live debrid pipelines (2026-10-13 sweep
        # of player.log: debridio pm/tb movie+series, torrentio pm/tb —
        # real key shapes, fake hashes) must carry a resolve pipeline or
        # autoplay dead-ends at "no resolve pipeline, engine never
        # engages" on that whole addon family.
        import struct as _struct
        PM = "tdgs5gcukfk2fcc3"           # premiumize key (live shape)
        TBK = "1f7be332-5fea-4aa8-8814-46eabea63736"   # torbox uuid
        AID = "0889be7cbed8417f47e58540afea55c0"       # debridio addonId
        shapes = {
            "torrentio pm": "https://torrentio.strem.fun/resolve/"
                            "premiumize/%s/" % PM,
            "torrentio tb": "https://torrentio.strem.fun/resolve/"
                            "torbox/%s/" % TBK,
            "debridio pm movie": "https://addon.debridio.com/play/movie/"
                                 "premiumize/%s/%s/" % (AID, PM),
            "debridio pm series": "https://addon.debridio.com/play/series/"
                                  "premiumize/%s/%s/" % (AID, PM),
            "debridio tb movie": "https://addon.debridio.com/play/movie/"
                                 "torbox/%s/%s/" % (AID, TBK),
            "debridio tb series": "https://addon.debridio.com/play/series/"
                                  "torbox/%s/%s/" % (AID, TBK),
        }
        for name, base in shapes.items():
            url = base + H["a"] + "/Some.Show.S02E03.1080p.mkv"
            b = stremio.resolve_base(url)
            r = stremio.build_resolve_url(
                b, H["b"], "Some.Show.S02E03.1080p.mkv", 5)
            want_idx = 0 if "/play/" in b else 5
            check("resolve pipeline: %s armed (base+build+identity)" % name,
                  b == base
                  and stremio.parse_resolve_url(r) == (H["b"], want_idx))

        # ---- mkv_head_duration_s: the online-sub SYNC gate's clock ----
        def _vid(v):
            L = (v.bit_length() + 5) // 7
            return (v | (1 << (7 * L))).to_bytes(L, "big")

        def _vsz(v):
            L = max(1, (v.bit_length() + 6) // 7)
            return (v | (1 << (7 * L))).to_bytes(L, "big")

        def _el(eid, payload):
            return _vid(eid) + _vsz(len(payload)) + payload

        _EBML, _SEG, _INF, _DUR, _TCS = (0x1A45DFA3, 0x18538067,
                                         0x1549A966, 0x4489, 0x2AD7B1)
        _ebmlh = _el(_EBML, b"\x00" * 8)
        _mkv = lambda info: _ebmlh + _el(_SEG, _el(_INF, info))
        check("mkv_head_duration_s: default scale float8",
              stremio.mkv_head_duration_s(
                  _mkv(_el(_DUR, _struct.pack(">d", 725000.0)))) == 725.0)
        check("mkv_head_duration_s: non-default scale float4",
              stremio.mkv_head_duration_s(
                  _mkv(_el(_TCS, (100000).to_bytes(3, "big"))
                       + _el(_DUR, _struct.pack(">f", 7250000.0)))) == 725.0)
        check("mkv_head_duration_s: garbage/no-Info/absurd -> 0.0",
              stremio.mkv_head_duration_s(b"\xab" * 64) == 0.0
              and stremio.mkv_head_duration_s(_ebmlh) == 0.0
              and stremio.mkv_head_duration_s(
                  _mkv(_el(_DUR, _struct.pack(">d", 1e12)))) == 0.0)

        # ---- mp4_head_duration_s: the SYNC gate's clock for the MP4
        # WEBDL rips Stremio actually hands off (the 2026-10-16 Hell's
        # Kitchen files were both .mp4 — the MKV-only gate never fired).
        # mvhd v0: timescale u32 at +16, duration u32 at +20.
        def _mvhd_v0(ts, dur):
            body = (b"\x00" * 4 + b"\x00" * 8
                    + ts.to_bytes(4, "big") + dur.to_bytes(4, "big")
                    + b"\x00" * 80)
            return ((8 + len(body)).to_bytes(4, "big") + b"mvhd"
                    + body)

        # mvhd v1: timescale u32 at +20, duration u64 at +24.
        def _mvhd_v1(ts, dur):
            body = (b"\x01" + b"\x00" * 3 + b"\x00" * 16
                    + ts.to_bytes(4, "big") + dur.to_bytes(8, "big"))
            return ((8 + len(body)).to_bytes(4, "big") + b"mvhd"
                    + body)

        check("mp4_head_duration_s: v0 in head (faststart)",
              stremio.mp4_head_duration_s(
                  b"\x00" * 64 + _mvhd_v0(1000, 2515 * 1000)) == 2515.0)
        check("mp4_head_duration_s: v1 in tail (moov-at-end)",
              stremio.mp4_head_duration_s(
                  b"", b"\x00" * 64 + _mvhd_v1(90000, 2515 * 90000))
              == 2515.0)
        check("mp4_head_duration_s: head wins over tail",
              stremio.mp4_head_duration_s(
                  _mvhd_v0(1000, 600_000), _mvhd_v0(1000, 100))
              == 600.0)
        check("mp4_head_duration_s: garbage/absurd/absent -> 0.0",
              stremio.mp4_head_duration_s(b"mvhd" * 40) == 0.0
              and stremio.mp4_head_duration_s(
                  b"", _mvhd_v0(1, 0xFFFFFFFF)) == 0.0
              and stremio.mp4_head_duration_s(b"", b"") == 0.0)
        check("_resolve_file_name: title line 2 is the file name",
              stremio._resolve_file_name(
                  {"title": "Torrent Name\nFile.Name.S01E01.mkv\n"
                            "\U0001F464 5"}) == "File.Name.S01E01.mkv")

        # ---- search_online_subtitles: multi-source (the 2026-10-16
        # Hell's Kitchen night: the keyless OpenSubtitles addon returned
        # ZERO subs for the show's middle seasons — three 'none found'
        # fetches while the user's own SubSense addon carried five
        # English SRTs for the exact episode). The user's installed
        # subtitle addons are queried FIRST, in parallel; the keyless
        # fallback stays when the profile has none.
        class _FakeResp:
            def __init__(self, status, payload):
                self.status_code = status
                self._payload = payload

            def json(self):
                return self._payload

        class _FakeSession:
            def __init__(self, table):
                self.table = table      # url prefix -> resp | exc | list
                self.hits = []

            def get(self, url, timeout=10):
                self.hits.append(url)
                for prefix, actions in self.table.items():
                    if not url.startswith(prefix):
                        continue
                    if isinstance(actions, list):
                        # sequential answers: earlier ones pop, the
                        # last repeats forever
                        action = actions.pop(0) if len(actions) > 1 \
                            else actions[0]
                    else:
                        action = actions
                    if isinstance(action, Exception):
                        raise action
                    return action
                return _FakeResp(200, {"subtitles": []})

        SUBSENSE = "https://subsense.example"
        KEYLESS = "https://opensubtitles-v3.strem.io"
        saved_session, saved_bases = (stremio._session,
                                      stremio.subtitle_addon_bases)
        try:
            fake = _FakeSession({
                KEYLESS: _FakeResp(200, {"subtitles": []}),
                SUBSENSE: _FakeResp(200, {"subtitles": [
                    # SubSense shapes: label/releaseName, NO season/
                    # episode fields — must be trusted (episode-scoped
                    # query), not dropped as (0,0) mismatch
                    {"id": "subsense-srt-gestdown-eng-1",
                     "url": "https://subsense.example/dl/1",
                     "lang": "eng", "label": "Gestdown · [SRT] · LOL",
                     "releaseName": "LOL"},
                    {"id": "subsense-srt-subsource-eng-0",
                     "url": "https://subsense.example/dl/0",
                     "lang": "eng",
                     "label": "Subsource · [SRT]",
                     "releaseName":
                         "Hells Kitchen US S13E01 PDTV x264"},
                    {"id": "subsense-srt-x-ger",
                     "url": "https://subsense.example/dl/9",
                     "lang": "ger", "label": "German"},
                ]}),
            })
            stremio._session = fake
            stremio.subtitle_addon_bases = lambda: [SUBSENSE, KEYLESS]
            cands = stremio.search_online_subtitles(
                "series", "tt0437005", season=13, episode=1,
                file_hint="Hells Kitchen (2005) S13E01 - WEBDL-1080p")
            check("multi-source: user's addon answers where the keyless "
                  "one is empty",
                  len(cands) == 2
                  and {c["id"] for c in cands} ==
                  {"subsense-srt-subsource-eng-0",
                   "subsense-srt-gestdown-eng-1"})
            check("multi-source: overlap ranks the named release first",
                  cands[0]["id"] == "subsense-srt-subsource-eng-0")
            check("multi-source: non-wanted language filtered",
                  all(c["lang"] == "eng" for c in cands))
            check("multi-source: both bases actually queried",
                  any(u.startswith(SUBSENSE) for u in fake.hits)
                  and any(u.startswith(KEYLESS) for u in fake.hits))

            # episode evidence: the opensubtitles shape (fields always
            # present) still drops a wrong-episode listing, and a
            # contradicting SxxEyy marker in the entry's own name
            # overrules agreeing fields
            fake = _FakeSession({
                KEYLESS: _FakeResp(200, {"subtitles": [
                    {"id": "ok-fields", "url": "https://x/dl1",
                     "lang": "eng", "subtitleFileName": "e1.srt",
                     "season": 13, "episode": 1},
                    {"id": "wrong-fields", "url": "https://x/dl2",
                     "lang": "eng", "subtitleFileName": "e2.srt",
                     "season": 1, "episode": 1},
                    {"id": "wrong-marker", "url": "https://x/dl3",
                     "lang": "eng",
                     "subtitleFileName": "Hells.Kitchen.S08E02.srt",
                     "season": 13, "episode": 1},
                ]}),
            })
            stremio._session = fake
            stremio.subtitle_addon_bases = lambda: [KEYLESS]
            cands = stremio.search_online_subtitles(
                "series", "tt0437005", season=13, episode=1,
                file_hint="Hells Kitchen S13E01")
            check("episode evidence: agreeing fields kept, disagreeing "
                  "fields and lying markers dropped",
                  [c["id"] for c in cands] == ["ok-fields"])

            # a failed base gets exactly one retry round, and only when
            # nothing usable came back
            fake = _FakeSession({
                SUBSENSE: ConnectionError("down"),
                KEYLESS: _FakeResp(200, {"subtitles": [
                    {"id": "keyless-hit", "url": "https://x/dl1",
                     "lang": "eng", "season": 13, "episode": 1},
                ]}),
            })
            stremio._session = fake
            stremio.subtitle_addon_bases = lambda: [SUBSENSE, KEYLESS]
            cands = stremio.search_online_subtitles(
                "series", "tt0437005", season=13, episode=1)
            check("failed base: no retry while another source answered "
                  "(one hit per base)",
                  sum(u.startswith(SUBSENSE) for u in fake.hits) == 1
                  and [c["id"] for c in cands] == ["keyless-hit"])

            fake = _FakeSession({
                SUBSENSE: [ConnectionError("down"),
                           _FakeResp(200, {"subtitles": [
                               {"id": "retry-hit", "url": "https://x/dl1",
                                "lang": "eng"}]})],
            })
            stremio._session = fake
            stremio.subtitle_addon_bases = lambda: [SUBSENSE]
            cands = stremio.search_online_subtitles(
                "series", "tt0437005", season=13, episode=1)
            check("failed base: retried once when nothing else answered",
                  sum(u.startswith(SUBSENSE) for u in fake.hits) == 2
                  and [c["id"] for c in cands] == ["retry-hit"])
        finally:
            stremio._session = saved_session
            stremio.subtitle_addon_bases = saved_bases

        # ---- the fetch WORKER's sync decision (2026-10-16 Hell's
        # Kitchen evidence: the WEBDL runs 2407 s, every English SRT on
        # every source is the 2515 s broadcast timing — the old 1.05
        # ceiling waved that through as ~108 s of end drift, the
        # user's "out of sync on Stremio" exactly). The worker must:
        # pass a matching span untouched, SKIP a wrong-cut span, and
        # when nothing passes but one span is a plausible same-episode
        # cut (0.9-1.15), RETIME it to the media and hand back a real
        # retimed file. Driven synchronously by capturing the runner's
        # callable — asserts on the worker's own decision tuple.
        import tempfile as _tf
        from types import SimpleNamespace as _NS
        import src.profanity as _prof
        wv = PlayerView(Config({}, None))
        wv.current = {"kind": "stremio", "fav_key": "stremio:tt0437005",
                      "stremio_imdb": "tt0437005", "season": 13,
                      "episode": 1, "file_name":
                      "Hells Kitchen (2005) S13E01 WEBDL-1080p.mp4"}
        wv.vlc = _NS(get_length=lambda: 0)
        media_head = (b"\x00" * 64 + _mvhd_v0(1000, 2407306)
                      + b"\x00" * 66000)
        wv._vod_relay = _NS(total=750647726, _head=media_head,
                            _tail=b"", parser_tracks={})
        captured = []
        wv._subfetch_runner.run = \
            lambda fn, *a, **k: captured.append(fn())
        _tmpdir = _tf.mkdtemp(prefix="mtp_subsync_")

        def _ts_srt(t):
            ms = int(round(float(t) * 1000.0))
            h, ms = divmod(ms, 3600000)
            m, ms = divmod(ms, 60000)
            s, ms2 = divmod(ms, 1000)
            return "%02d:%02d:%02d,%03d" % (h, m, s, ms2)

        def _srt_cues_text(last_s):
            return ("1\r\n%s --> %s\r\nhi\r\n\r\n"
                    "2\r\n%s --> %s\r\nbye\r\n"
                    % (_ts_srt(1.0), _ts_srt(4.0),
                       _ts_srt(max(5.0, last_s - 3.0)),
                       _ts_srt(last_s)))

        def _run_with(sub_last_s, cand_name="HK"):
            src_cues = _prof.parse_subtitle_cues(
                _srt_cues_text(sub_last_s))
            dest = os.path.join(_tmpdir, "cand-%d.srt" % sub_last_s)
            saved_dl = stremio.download_online_subtitle
            saved_cp = stremio.subtitle_cache_path
            saved_search = stremio.search_online_subtitles
            try:
                stremio.subtitle_cache_path = \
                    lambda imdb, sid: dest
                stremio.download_online_subtitle = \
                    lambda url, path: stremio.write_srt_cues(
                        src_cues, path)
                stremio.search_online_subtitles = \
                    lambda *a, **k: [{"id": "x", "url": "u",
                                      "name": cand_name}]
                wv._sub_fetching = False
                wv._sub_fetch_pending = False
                captured.clear()
                wv._arm_online_sub_fetch(None, manual=True)
            finally:
                stremio.download_online_subtitle = saved_dl
                stremio.subtitle_cache_path = saved_cp
                stremio.search_online_subtitles = saved_search
            return captured[0] if captured else None

        res = _run_with(2407)      # exact-cut span: passes the gate
        check("worker sync: matching span engaged untouched",
              res and res[2] == "ok" and res[3]
              and res[3][0].endswith(".srt")
              and not res[3][0].endswith("-sync.srt"))
        res = _run_with(2515)      # broadcast cut on the WEBDL media
        check("worker sync: wrong cut skipped, best candidate retimed",
              res and res[2] == "ok" and res[3]
              and res[3][0].endswith("-sync.srt")
              and "timing adjusted" in res[3][1])
        _sync_text = _prof.read_subtitle_text(res[3][0])
        _sync_cues = _prof.parse_subtitle_cues(_sync_text) \
            if _sync_text else []
        _sync_last = max((e for _s, e, _t in _sync_cues), default=0)
        check("worker sync: the retimed file really ends at the media "
              "duration (%.0f s of 2407 s)" % _sync_last,
              2390.0 < _sync_last <= 2407.5)

        # ---- the 15:41 PDTV incident: 2484 s media, 2510.5 s sub —
        # the old ±1.5% band engaged this RAW (unsynced from the first
        # second). The tight ±max(8, 0.6%) gate must SKIP it, and the
        # |diff| <= 45 s fallback fits a CONSTANT shift (head trim).
        media_head2 = (b"\x00" * 64 + _mvhd_v0(1000, 2484306)
                       + b"\x00" * 66000)
        wv._vod_relay = _NS(total=349591771, _head=media_head2,
                            _tail=b"", parser_tracks={})
        res = _run_with(2510.5)
        check("worker sync: 26.5 s head-trim skipped and SHIFT-fitted",
              res and res[2] == "ok" and res[3]
              and res[3][0].endswith("-sync.srt"))
        _sh_text = _prof.read_subtitle_text(res[3][0])
        _sh_cues = _prof.parse_subtitle_cues(_sh_text) if _sh_text else []
        _sh_first = min((s for s, _e, _t in _sh_cues), default=-1)
        _sh_last = max((e for _s, e, _t in _sh_cues), default=0)
        check("worker sync: shift model moves EVERY cue by the same "
              "-26.2 s (constant, not scaled)",
              abs(_sh_first - 0.0) < 0.01
              and abs(_sh_last - 2484.3) < 0.05)
        res = _run_with(2486)      # a true same-airing match (1.7 s off)
        check("worker sync: same-cut span (±1.7 s) engaged untouched",
              res and res[2] == "ok" and res[3]
              and not res[3][0].endswith("-sync.srt"))
        res = _run_with(4000)      # a recap movie: nothing salvageable
        check("worker sync: gross span (4000 s on 2484 s) refused "
              "with no retime",
              res and res[2] == "ok" and res[3] is None)
        import shutil as _shutil
        _shutil.rmtree(_tmpdir, ignore_errors=True)

        # ---- [9] retimed-sub drift re-anchor: G/Shift+G (VLC's
        # subtitle-delay keys) turn a press into a PERMANENT piecewise
        # correction (the 2026-10-16 report: "subtitles went ahead of
        # the audio and I could not get them to sync back up
        # automatically" — a wrong-cut retimed track drifts in steps no
        # constant delay can chase). Anchors ride the cur dict, so a
        # stream reload keeps them; the overlay store AND the filter
        # windows re-map together.
        print("[9] external-sub sync anchors: G/Shift+G fixed steps, "
              "step-function map, Ctrl+0 reset (the 2026-10-16 16:46-"
              "16:50 press storm: adaptive steps snowballed to 4 s "
              "jumps and linear interpolation re-skewed already-synced "
              "regions on every press)")
        sync_dir = _tf.mkdtemp(prefix="mtp_anchor_")
        sync_path = os.path.join(sync_dir, "tt-test-subsource-eng-0"
                                          "-sync.srt")
        base_cues = [(10.0, 13.0, "hello"), (100.0, 104.0,
                                             "Holy [Bleep]."),
                     (200.0, 205.0, "mid"), (400.0, 406.0, "late")]
        stremio.write_srt_cues(base_cues, sync_path)
        stream_url = ("https://torrentio.strem.fun/resolve/torbox/"
                      "key/aaaa/tt-test/Hells.Kitchen.S13E01.mp4")
        nv = PlayerView(Config({}, None))
        nv.current = {"kind": "stremio", "fav_key": "stremio:x",
                      "url": stream_url,
                      "stremio_imdb": "tt0437005", "season": 13,
                      "episode": 1, "sub_file": sync_path}
        nv._filter_engine.words = ["bleep"]
        check("the '-sync.srt' path auto-flags the playable as retimed",
              nv._stremio_handoff_cues() == base_cues
              and nv.current["_sub_retimed"] is True)
        nv._load_stremio_sub_cues()
        base_windows = [tuple(w) for w in nv._filter_engine.windows]
        check("filter armed from the retimed file before any nudge",
              len(base_windows) == 1)
        clock = {"t": 300.0}
        nv._caption_clock_s = lambda: clock["t"]
        # isolate the anchor store BEFORE any press (nudges write
        # through) — never the real user's sync-anchors.json
        _store_file = os.path.join(sync_dir, "sync-anchors.json")
        nv._sub_anchor_store_path = lambda: _store_file

        def _store_times():
            return {str(t): (float(s), float(e))
                    for s, e, t in nv._cap_cues.cues}

        from src.profanity import windows_from_cues as _wfc

        def windows_from_cues_exp(_view, cues, words):
            wins = _wfc(cues, words)
            return [(max(0.0, ws - 0.4), max(0.0, we - 0.4))
                    for ws, we in wins]

        # press 1: FIXED 0.5 s at t=300 — cues BEFORE the anchor never
        # move (step function, not interpolation)
        nv.sub_delay_hot(0.5)
        check("nudge 1 anchors (300, +0.5) — fixed step",
              nv.current.get("_sub_anchors") == [(300.0, 0.5)])
        times = _store_times()
        check("cue at 400 s (after the anchor) moves the full +0.5",
              abs(times["late"][0] - 400.5) < 0.01)
        check("cue at 10 s (before the anchor) is UNTOUCHED",
              times["hello"][0] == 10.0 and times["mid"][0] == 200.0)
        _exp = windows_from_cues_exp(nv, base_cues[:3]
                                     + [(400.5, 406.5, "late")],
                                     ["bleep"])
        check("filter window (cue 100 < anchor) re-mapped unchanged",
              _exp and len(nv._filter_engine.windows) == 1
              and abs(nv._filter_engine.windows[0][0] - _exp[0][0])
              < 0.01)
        nv.sub_delay_hot(0.5)      # press 2 at the same spot: 1.0 now
        check("nudge 2 (same spot) refines the SAME anchor to +1.0",
              nv.current.get("_sub_anchors") == [(300.0, 1.0)]
              and abs(_store_times()["late"][0] - 401.0) < 0.01)
        clock["t"] = 450.0
        nv.sub_delay_hot(-0.25)    # press 3 later: a second anchor
        check("nudge 3 adds (450, +0.75) — map ((300,+1.0),(450,+0.75))",
              nv.current.get("_sub_anchors") == [(300.0, 1.0),
                                                 (450.0, 0.75)])
        times = _store_times()
        check("cue at 400 s keeps the PREVIOUS anchor's +1.0 (steps, "
              "no interpolation)",
              abs(times["late"][0] - 401.0) < 0.01)
        check("cue at 200 s still untouched",
              times["mid"][0] == 200.0)

        # ---- anchors persist per (stream, sub) pair: a fresh playable
        # (the Stremio re-handoff / app restart case) restores them and
        # the engage applies the map with them
        import json as _json
        check("anchors written to the persistent store",
              os.path.isfile(_store_file)
              and nv._sub_anchor_key(nv.current) in
              _json.load(open(_store_file, encoding="utf-8")))
        nv2 = PlayerView(Config({}, None))
        nv2._sub_anchor_store_path = lambda: _store_file
        nv2.current = {"kind": "stremio", "fav_key": "stremio:x",
                       "url": stream_url, "stremio_imdb": "tt0437005",
                       "season": 13, "episode": 1,
                       "sub_file": sync_path}
        check("fresh playable restores saved anchors on cue parse",
              nv2._stremio_handoff_cues() == base_cues
              and nv2.current.get("_sub_anchors") == [(300.0, 1.0),
                                                      (450.0, 0.75)])
        nv2._engage_stremio_external()
        t2 = {str(t): (float(s), float(e)) for s, e, t in
              nv2._cap_cues.cues}
        check("engage applies the restored map (late cue at 401.0)",
              abs(t2["late"][0] - 401.0) < 0.01)

        # the settings dialog's delay routes through the SAME map for
        # file-owned subs: +1.5 s from the dialog = an anchor press
        nv2._caption_clock_s = lambda: 120.0
        nv2._apply_sub_delay(1500)
        check("dialog delay delta lands as an anchor (120, +1.5)",
              nv2.current.get("_sub_anchors") == [(120.0, 1.5),
                                                  (300.0, 1.0),
                                                  (450.0, 0.75)])
        nv2._apply_sub_delay(0)     # the dialog's reset clears all
        check("dialog reset (0.00 s) clears every anchor",
              not nv2.current.get("_sub_anchors")
              and {str(t): (float(s), float(e)) for s, e, t in
                   nv2._cap_cues.cues}["late"][0] == 400.0)

        # Ctrl+0 reset: same wipe, store included
        nv.sub_sync_reset()
        check("sub_sync_reset clears anchors AND the persisted pair",
              not nv.current.get("_sub_anchors")
              and _store_times()["late"][0] == 400.0
              and nv._sub_anchor_key(nv.current) not in
              _json.load(open(_store_file, encoding="utf-8")))

        # an unparseable sub file: the press must NOT leak into the
        # global config delay (the refusal branch)
        nv.current["sub_file"] = os.path.join(sync_dir, "missing.srt")
        nv._stremio_sub_path = ""
        nv._stremio_sub_cues = []
        nv.config.subtitle_appearance["delay_ms"] = 0
        nv.sub_delay_hot(0.5)
        check("external sub that cannot parse -> press refused, global "
              "delay untouched",
              int(nv.config.subtitle_appearance.get("delay_ms", 0))
              == 0)

        # classic fallthrough: no external sub on this media -> the
        # constant config delay path
        nv.current.pop("sub_file", None)
        nv.current["_sub_retimed"] = False
        nv._stremio_sub_cues = []
        nv._stremio_sub_path = ""
        nv.config.subtitle_appearance["delay_ms"] = 0
        nv.sub_delay_hot(0.25)
        check("media without an external sub falls back to the classic "
              "constant delay (config +0.25 s)",
              int(nv.config.subtitle_appearance.get("delay_ms", 0))
              == 250)

        # ---- content_cue_end: the retime scale measures dialog, not
        # the fansub signature
        check("content_cue_end strips the trailing credit line",
              stremio.content_cue_end(
                  [(0.0, 4.0, "hello"), (2510.0, 2515.0,
                   "Synced & Corrected by Seppuku17\nwww.addic7ed.com")]
              ) == 4.0)
        check("content_cue_end ignores symbol-only tails",
              stremio.content_cue_end(
                  [(0.0, 4.0, "hello"), (2512.0, 2514.0, "\u2663 \u25c8")])
              == 4.0)
        check("content_cue_end keeps real dialog ends (mid credits "
              "harmless)",
              stremio.content_cue_end(
                  [(0.0, 4.0, "www.example.com is down"),
                   (2506.0, 2510.5, "Unfortunately, JP stayed there.")])
              == 2510.5)
        check("content_cue_end: all-credit cues -> 0.0",
              stremio.content_cue_end(
                  [(1.0, 2.0, "addic7ed")]) == 0.0)
        _shutil.rmtree(sync_dir, ignore_errors=True)

        # the morning's exact shape: torrent-only candidates only, cur
        # handed off as a (dead) resolve link -> the walk re-resolves
        # instead of dead-ending
        T1 = {"name": "Torrentio\n1080p",
              "title": "Adventure.Time.S05E42.1080p.WEB-DL.x264-GROUP\n"
                       "Adventure.Time.S05E42.1080p.WEB-DL.x264-GROUP.mkv\n"
                       "\U0001F464 20 \U0001F4BE 2 GB \u2699\ufe0f 1337x",
              "infoHash": H["a"], "fileIdx": 0}
        T2 = {"name": "Torrentio\n720p",
              "title": "Adventure.Time.S05E42.720p.WEB-DL.x264-OTHER\n"
                       "Adventure.Time.S05E42.720p.WEB-DL.x264-OTHER.mkv\n"
                       "\U0001F464 5 \U0001F4BE 1 GB \u2699\ufe0f 1337x",
              "infoHash": H["b"], "fileIdx": 4}
        stremio._streams_cache.clear()
        stremio._query_addon = lambda base, url: [dict(T1), dict(T2)]
        res_srv = FakeServer("")
        stremio.StreamingServer = lambda base="": res_srv
        rcur = cur_of({"infoHash": H["g"], "fileIdx": 2}, url=RU)
        nxt = stremio.next_stream_playable(cfg2, rcur)
        check("incident: torrent-only list re-resolves through the "
              "handoff's debrid pipeline (T1 first)",
              nxt and nxt["url"] == stremio.build_resolve_url(
                  stremio.resolve_base(RU), H["a"],
                  "Adventure.Time.S05E42.1080p.WEB-DL.x264-GROUP.mkv", 0)
              and nxt.get("info_hash") == H["a"]
              and nxt.get("file_idx") == 0
              and res_srv.created == [])
        nxt = stremio.next_stream_playable(
            cfg2, rcur, exclude={H["a"]})
        check("incident: the dead-chain exclusion walks the re-resolve "
              "candidates in rank order",
              nxt and stremio.parse_resolve_url(nxt["url"]) == (H["b"], 4))
        # a direct (non-resolve) debrid link still has no pipeline:
        # torrent-only candidates stay skipped there
        stremio._streams_cache.clear()
        nxt = stremio.next_stream_playable(
            cfg2, cur_of({"infoHash": H["g"], "fileIdx": 0},
                         url="https://torbox.app/friede/file.mkv"))
        check("non-resolve debrid link: no pipeline, torrent-only still "
              "skipped (no other stream)",
              nxt is None and res_srv.created == [])
        # autoplay / next-episode from a resolve handoff: the next
        # episode's torrent-only list re-resolves too (the 22/22
        # S07E04 lookahead dead-end)
        saved_meta2 = stremio.series_meta
        stremio.series_meta = lambda imdb: {
            "name": "Adventure Time",
            "videos": [{"season": 5, "episode": 42, "name": "James"},
                       {"season": 5, "episode": 43, "name": "Oracle"}]}
        try:
            stremio._streams_cache.clear()
            stremio._query_addon = \
                lambda base, url: [dict(T1), dict(T2)]
            ap = stremio.next_playable(cfg2, rcur)
            check("autoplay from a resolve handoff -> next episode "
                  "RE-RESOLVED through the debrid pipeline",
                  ap and stremio.parse_resolve_url(ap["url"])[0] == H["a"]
                  and (ap["season"], ap["episode"]) == (5, 43)
                  and ap.get("info_hash") == H["a"]
                  and res_srv.created == [])
        finally:
            stremio.series_meta = saved_meta2
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
