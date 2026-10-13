# -*- coding: utf-8 -*-
"""Offline regression test: the 2026-10-06 12:07/12:08 Adventure Time
S04E11 wrong-episode incident.

The user picked S04E11 in Stremio twice; both times the app played the
WRONG episode under an S04E11 banner. Two defects, each covered here:

[1] probe_debrid treated torrentio's HTTP 429 (rate limit) as "this
    debrid link is dead" and auto-advanced off the user's own pick —
    twice in a row, on healthy handoffs, within ~2 s each. A 429 is
    throttling, not death: the probe now reports alive and leaves the
    decision to VLC / the 10 s start guard.
[2] The advance then walked torrentio's list for the episode, where a
    season pack carries 'Adventure.Time.S04E11.Daddys.Little.Monster
    .1080p.AMZN.WEB-DL.mkv' (fileIdx 10) — torrentio matched the
    SxxExy STRING, but the pack was numbered by production order, so
    the FILE plays Daddy's Little Monster, aired as S04E06. The app
    trusted the label. Stream picks (advance + autoplay) now veto any
    candidate whose file name provably titles a DIFFERENT episode of
    the series.
"""

import sys
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import stremio  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else " FAIL ") + name)


# the live incident list, distilled — the names torrentio actually
# served for tt1305826:4:11 on 2026-10-06
META_AT = {"name": "Adventure Time", "videos": [
    {"season": 4, "episode": 6, "name": "Daddy's Little Monster"},
    {"season": 4, "episode": 10, "name": "Goliad"},
    {"season": 4, "episode": 11, "name": "Beyond This Earthly Realm"},
    {"season": 4, "episode": 12, "name": "Gotcha!"},
]}

LIE_FILE = ("Adventure.Time.S04E11.Daddys.Little.Monster.1080p."
            "AMZN.WEB-DL.mkv")


def main():
    print("[1] probe_debrid: 429 is throttling, not death")
    served = {"code": 429}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(served["code"])
            self.end_headers()

        def log_message(self, *args):  # noqa: N802
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d/resolve/x" % port
        served["code"] = 429
        check("HTTP 429 -> alive (the guard is the backstop)",
              stremio.probe_debrid(url, timeout_s=2.0) is True)
        served["code"] = 404
        check("HTTP 404 -> dead (advancing stays correct)",
              stremio.probe_debrid(url, timeout_s=2.0) is False)
        served["code"] = 200
        check("HTTP 200 -> alive",
              stremio.probe_debrid(url, timeout_s=2.0) is True)
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("[2] _file_is_other_episode: the Adventure Time pack lies")
    want = (4, 11)
    veto = stremio._file_is_other_episode
    check("the production-order pack file IS vetoed",
          veto(LIE_FILE, META_AT, *want, "Adventure Time"))
    check("the correct single-episode file stays",
          not veto("Adventure.Time.S04E11.Beyond.This.Earthly.Realm."
                   "1080p.HMAX.WEBRip.DD.2.0.H.265.-EDGE2020.mkv",
                   META_AT, *want, "Adventure Time"))
    check("'Show - SxxExx - Title' pack layout stays",
          not veto("Season 04/Adventure Time (2008) - S04E11 - "
                   "Beyond This Earthly Realm (1080p x264).mkv",
                   META_AT, *want, "Adventure Time"))
    check("the two-in-one file stays (it lists the wanted title)",
          not veto("Season 4/Adventure Time S04E11-S04E12 Beyond This "
                   "Earthly Realm + Gotcha.mkv",
                   META_AT, *want, "Adventure Time"))
    check("a bare-number file stays (no title to disagree)",
          not veto("Season 04/89 - Beyond This Earthly Realm.mkv",
                   META_AT, *want, "Adventure Time"))
    check("a no-title release stays",
          not veto("Adventure.Time.S04E11.1080p.BluRay.EAC3.AV1-TiZU.mkv",
                   META_AT, *want, "Adventure Time"))
    check("a foreign-title pack file stays (never matches, never vetoes)",
          not veto("S04/Hora De Aventuras_S04E11_El monstruito de "
                   "papa.mp4", META_AT, *want, "Adventure Time"))
    check("two other titles = ambiguous, stays (fileIdx decides)",
          not veto("Daddys.Little.Monster.+.Gotcha.pack.mkv",
                   META_AT, *want, "Adventure Time"))
    check("empty meta never vetoes",
          not veto(LIE_FILE, {}, *want, "Adventure Time"))
    check("specials (season 0) are never vetoed",
          not veto(LIE_FILE, META_AT, 0, 1, "Adventure Time"))
    check("movies (no season) are never vetoed",
          not veto(LIE_FILE, META_AT, 0, 0, ""))

    print("[3] _episode_titles guards")
    meta_self = {"name": "Adventure Time", "videos": META_AT["videos"] + [
        {"season": 9, "episode": 13, "name": "Adventure Time"}]}
    titles = stremio._episode_titles(meta_self, "Adventure Time")
    check("a title equal to the show name is excluded (the (9,13) trap)",
          (9, 13) not in titles)
    check("real episode titles are kept",
          (4, 6) in titles and (4, 11) in titles)
    meta_short = {"name": "X", "videos": [
        {"season": 1, "episode": 2, "name": "Up"}]}
    check("titles too short to match on are excluded",
          not stremio._episode_titles(meta_short, "X"))

    print("[4] next_stream_playable: the walk skips the lie, serves the "
          "next honest entry")
    from src.config import Config
    cur_hash = "b632d3bcaaff" + "0" * 28
    lie_hash = "e27069e28889" + "0" * 28
    good_hash = "982d331a0240" + "0" * 28
    rbase = "https://torrentio.strem.fun/resolve/torbox/key/"
    saved_sm, saved_as = stremio.series_meta, stremio.addon_streams
    try:
        stremio.series_meta = lambda imdb: dict(META_AT) \
            if imdb == "tt1305826" else {}
        stremio.addon_streams = \
            lambda cfg, imdb, s, e: [
                {"name": "current 1080p", "infoHash": cur_hash,
                 "fileIdx": 10,
                 "title": "current\nAdventure.Time.S04E11.Beyond.This."
                          "Earthly.Realm.1080p.HMAX.WEBRip.mkv"},
                {"name": "pack lie 1080p", "infoHash": lie_hash,
                 "fileIdx": 10,
                 "title": "pack\n" + LIE_FILE},
                {"name": "honest release 1080p", "infoHash": good_hash,
                 "fileIdx": 10,
                 "title": "honest\nAdventure.Time.S04E11.1080p.HMAX."
                          "WEB-DL.x264.mkv"}]
        cur = {"kind": "stremio", "title": "Adventure Time — S04E11",
               "url": rbase + cur_hash + "/EDGE2020.mkv/10/EDGE2020.mkv",
               "fav_key": "stremio:tt1305826:4:11",
               "stremio_imdb": "tt1305826", "series_name":
               "Adventure Time", "movie": False,
               "season": 4, "episode": 11,
               "info_hash": cur_hash, "file_idx": 10}
        nxt = stremio.next_stream_playable(Config({}, None), dict(cur))
        check("the walk lands past the pack lie, on the honest release",
              bool(nxt) and nxt.get("info_hash") == good_hash)
        check("the served link re-resolves the honest torrent",
              bool(nxt) and str(nxt.get("url", "")).startswith(
                  rbase + good_hash))

        stremio.addon_streams = \
            lambda cfg, imdb, s, e: [
                {"name": "current 1080p", "infoHash": cur_hash,
                 "fileIdx": 10,
                 "title": "current\nAdventure.Time.S04E11.mkv"},
                {"name": "pack lie 1080p", "infoHash": lie_hash,
                 "fileIdx": 10, "title": "pack\n" + LIE_FILE}]
        check("all-lie remainder: the walk refuses rather than serve a "
              "different episode",
              stremio.next_stream_playable(
                  Config({}, None), dict(cur)) is None)
    finally:
        stremio.series_meta, stremio.addon_streams = saved_sm, saved_as

    print("[5] _adjacent_playable: autoplay ranks only honest files")
    saved_sm, saved_as = stremio.series_meta, stremio.addon_streams
    try:
        stremio.series_meta = lambda imdb: dict(META_AT) \
            if imdb == "tt1305826" else {}
        stremio.addon_streams = \
            lambda cfg, imdb, s, e: [
                {"name": "pack lie 1080p \U0001F464 900 \U0001F4BE "
                         "2.0GB",
                 "infoHash": lie_hash, "fileIdx": 10,
                 "title": "pack\n" + LIE_FILE},
                {"name": "honest 720p \U0001F464 100 \U0001F4BE 1.0GB",
                 "infoHash": good_hash, "fileIdx": 10,
                 "title": "honest\nAdventure.Time.S04E11.720p.mkv"}]
        cur = {"kind": "stremio", "title": "Adventure Time — S04E10",
               "url": rbase + cur_hash + "/EDGE2020.mkv/10/EDGE2020.mkv",
               "fav_key": "stremio:tt1305826:4:10",
               "stremio_imdb": "tt1305826", "series_name":
               "Adventure Time", "movie": False,
               "season": 4, "episode": 10}
        nxt = stremio._adjacent_playable(Config({}, None), dict(cur), +1)
        check("autoplay's S04E11 pick is the honest release, not the "
              "higher-ranked lie",
              bool(nxt) and nxt.get("info_hash") == good_hash
              and (nxt.get("season"), nxt.get("episode")) == (4, 11))
    finally:
        stremio.series_meta, stremio.addon_streams = saved_sm, saved_as

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
