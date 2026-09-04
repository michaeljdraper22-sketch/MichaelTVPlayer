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
    query no title could match, so every Adventure Time lookup honestly
    missed — prev/next/autoplay dead all day).
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
