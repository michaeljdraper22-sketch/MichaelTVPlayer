# -*- coding: utf-8 -*-
"""Offscreen regression test for the 2026-10-22 TMNT wrong-series
incident, both halves:

[1] Click-context re-anchor (the reported bug). The user clicked a
    stream on the TMNT **1987** series' S02E07 'Enter: The Fly' page in
    Stremio; torrentio had cross-listed a **2012**-series season pack
    there (matched by absolute episode number: 1987's 12th episode IS
    S02E07). The torbox resolve link died, the 10 s guard advanced, and
    the file-name identity — 'Teenage Mutant Ninja Turtles (2012)
    S01E12', honest for the FILE — walked the 2012 S01E12 candidate
    list, so the app played a different series' episode while the user
    asked for 'other streams of the same episode'. next_stream_playable
    with reanchor_click=True (the dead-debrid auto-advance path) must
    now notice the dead hash is NOT in its identity's own addon list,
    find which same-named series' page lists that hash at the SAME
    absolute episode position + fileIdx, re-anchor the identity there,
    and serve the next stream of THE CLICKED episode. The manual
    S-button (reanchor_click=False) must keep the old behavior: the
    content is playing there, so the file-name identity wins.

[2] Production-code movie veto (the same session's second handoff).
    '207 - Enter - The Fly.mkv' — the 1987 pack's own file naming —
    has no S/E marker, so resolve_identity fell through to the movie
    catalog and the word-overlap scorer matched 'The Fly (1986)': the
    banner retitled a TMNT episode as a Cronenberg film and subtitles
    searched the wrong title. A leading 2-4 digit production code with
    a spaced title after it must veto the movie hit (display-name
    partial only), while genuinely numeric movie titles survive
    ('2001: A Space Odyssey' starts with its own digits; dotted release
    heads like '1917.2019.1080p…' never trip the regex at all).

Run: python test_click_reanchor.py  (no network, no GUI, no playback)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import stremio  # noqa: E402

FAILS = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILS.append(label)


# the incident's real ids/hashes (truncated to valid 40-hex stand-ins
# where needed — the mechanics only compare strings)
RCVR = "56f24dbeda45bd8b90a3526225f35cfdc2b4dd0f"      # 2012 S01 pack, dead
RUTR = "3fc54a8c69328e4d87520c4986d3a19ab9a9ed06"      # 2012 S01 RU pack
FLY = "c39d48eb6f66440ed8fa5ae7f648ba79e962c6ef"        # 1987 pack (wanted)

RESOLVE = ("https://torrentio.strem.fun/resolve/torbox/"
           "1f7be332-5fea-4aa8-8814-46eabea63736/")

META_2012 = {"name": "Teenage Mutant Ninja Turtles",
             "videos": [{"season": 1, "episode": e,
                         "name": "2012 episode %d" % e}
                        for e in range(1, 27)]}
META_1987 = {"name": "Teenage Mutant Ninja Turtles",
             "videos": (
                 [{"season": 1, "episode": e, "name": "1987 s1 ep %d" % e}
                  for e in range(1, 6)] +
                 [{"season": 2, "episode": e,
                   "name": "Enter: The Fly" if e == 7 else "1987 s2 ep %d" % e}
                  for e in range(1, 14)])}


def _entry(h, idx, title):
    return {"name": "Torrentio\n1080p", "title": title,
            "infoHash": h, "fileIdx": idx,
            "behaviorHints": {"filename": title.splitlines()[-1]}}


LIST_2012_S01E12 = [
    _entry(RUTR, 11, "Черепашки Ниндзя / TMNT Сезон: 1 [2012 WEB-DL]\n"
                     "TMNT2012 S01E12 - It Came From the Depths.mkv"),
]
LIST_1987_S02E07 = [
    _entry(RCVR, 11, "Teenage Mutant Ninja Turtles S01\n"
                     "Teenage Mutant Ninja Turtles (2012) - S01E12 - It "
                     "Came From The Depths.mkv"),      # the cross-listed lie
    _entry(FLY, 11, "TMNT 1987\n207 - Enter - The Fly.mkv"),
]

CUR = {
    "kind": "stremio",
    "title": "Teenage Mutant Ninja Turtles \u2014 S01E12 "
             "It Came from the Depths",
    "url": RESOLVE + RCVR + "/Teenage%20Mutant%20Ninja%20Turtles%20(2012)"
           "%20-%20S01E12%20-%20It%20Came%20From%20The%20Depths.mkv/11/"
           "Teenage%20Mutant%20Ninja%20Turtles%20(2012).mkv",
    "fav_key": "stremio:tt1877889:1:12",
    "info_hash": RCVR,
    "file_idx": 11,
    "stremio_imdb": "tt1877889",
    "series_name": "Teenage Mutant Ninja Turtles",
    "season": 1,
    "episode": 12,
    "episode_name": "It Came from the Depths",
}


def _install():
    """Patch every network touchpoint with the incident's world."""
    saved = (stremio.series_meta, stremio.search_series,
             stremio.addon_streams)

    def fake_series_meta(imdb):
        return {"tt1877889": META_2012, "tt0131613": META_1987}.get(
            str(imdb), {})

    def fake_search_series(name):
        # ranked like the live search: 1987 first (the veto-walked
        # candidate from the log), 2012 second; plus a differently-named
        # show that must be ignored (exact-name collisions only)
        return [
            {"id": "tt0131613", "name": "Teenage Mutant Ninja Turtles"},
            {"id": "tt1877889", "name": "Teenage Mutant Ninja Turtles"},
            {"id": "tt49028756", "name": "Tales of the Teenage Mutant "
                                         "Ninja Turtles"},
            {"id": "tt0318913", "name": "Teenage Mutant Ninja Turtles"},
        ]

    def fake_addon_streams(config, imdb, season, episode):
        key = (str(imdb), int(season), int(episode))
        return {
            ("tt1877889", 1, 12): LIST_2012_S01E12,
            ("tt0131613", 2, 7): LIST_1987_S02E07,
        }.get(key, [])

    stremio.series_meta = fake_series_meta
    stremio.search_series = fake_search_series
    stremio.addon_streams = fake_addon_streams
    return saved


print("[1] click-context re-anchor (2026-10-22 18:41 incident)")
saved = _install()
try:
    # auto-advance shape: dead link, never played, reanchor on
    nxt = stremio.next_stream_playable(
        None, dict(CUR), exclude=set(), debrid_only=True,
        reanchor_click=True)
    check("re-anchored to the clicked series (tt0131613)",
          bool(nxt) and nxt.get("stremio_imdb") == "tt0131613")
    check("re-anchored to the clicked episode S02E07",
          bool(nxt) and int(nxt.get("season") or 0) == 2
          and int(nxt.get("episode") or 0) == 7)
    check("serves the 1987 pack's file (the stream the user ended up "
          "clicking by hand 50 s later)",
          bool(nxt) and str(nxt.get("info_hash") or "") == FLY)
    check("built link rides the user's resolve pipeline + fileIdx 11",
          bool(nxt) and str(nxt.get("url") or "").startswith(RESOLVE + FLY)
          and "/11/" in str(nxt.get("url") or ""))
    check("title carries the recovered episode",
          bool(nxt) and "Enter: The Fly" in str(nxt.get("title") or ""))
    check("fav_key follows the clicked episode (fresh dead-chain bookkeeping)",
          bool(nxt) and nxt.get("fav_key") == "stremio:tt0131613:2:7")

    # manual S-button shape: same cur, no re-anchor — the old walk
    nxt = stremio.next_stream_playable(
        None, dict(CUR), exclude=set(), debrid_only=True)
    check("manual button (no reanchor) keeps the file-name identity",
          bool(nxt) and nxt.get("stremio_imdb") == "tt1877889"
          and str(nxt.get("info_hash") or "") == RUTR)

    # the fileIdx guard: a page listing the hash at ANOTHER file of the
    # pack is not the click page — no re-anchor, identity stands
    wrong_idx = [dict(_entry(RCVR, 4, "pack\nSome Other File.mkv"))]

    def streams_wrong_idx(config, imdb, season, episode):
        if (str(imdb), int(season), int(episode)) == ("tt0131613", 2, 7):
            return wrong_idx
        return []
    saved_as = stremio.addon_streams
    stremio.addon_streams = streams_wrong_idx
    anchor = stremio._click_context_reanchor(None, dict(CUR))
    stremio.addon_streams = saved_as
    check("a listing at a different fileIdx does not hijack the identity",
          anchor is None)
finally:
    (stremio.series_meta, stremio.search_series,
     stremio.addon_streams) = saved

print("[2] production-code movie veto ('The Fly (1986)' retitling)")
saved_fm = stremio.find_movie
saved_cd = stremio._content_disposition
try:
    stremio._content_disposition = lambda u: ""
    stremio.find_movie = lambda q, year="": (
        {"id": "tt0091064", "name": "The Fly", "releaseInfo": "1986"}
        if "fly" in q.lower() else
        {"id": "tt0062622", "name": "2001: A Space Odyssey",
         "releaseInfo": "1968"})

    ident = stremio.resolve_identity(
        RESOLVE + FLY + "/207%20-%20Enter%20-%20The%20Fly.mkv/11/"
        "207%20-%20Enter%20-%20The%20Fly.mkv",
        stremio.StreamingServer(""))
    check("incident file no longer becomes the movie 'The Fly'",
          bool(ident) and not ident.get("stremio_imdb")
          and ident.get("display_name") == "Enter The Fly")

    ident = stremio.resolve_identity(
        RESOLVE + FLY + "/2001%20A%20Space%20Odyssey%201968%201080p/"
        "0/2001%20A%20Space%20Odyssey%201968%201080p",
        stremio.StreamingServer(""))
    check("a movie title starting with its own digits survives",
          bool(ident) and ident.get("stremio_imdb") == "tt0062622")
finally:
    stremio.find_movie = saved_fm
    stremio._content_disposition = saved_cd

# dotted release heads never trip the veto regex at all
check("dotted year head '1917.2019' shape stays a plain movie path",
      stremio._PROD_CODE_RE.match("1917.2019.1080p.BluRay.x264") is None)

print()
if FAILS:
    print("FAILED %d check(s):" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
