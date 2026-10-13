# -*- coding: utf-8 -*-
"""Offline regression test: the 2026-10-13 02:12 wrong-show identification.

Adventure Time handoffs stopped autoplaying and both episode buttons
reported nothing, while Stremio's own next-episode button kept working.
Root cause chain, each link covered here:

[1] The release head 'Adventure Time With Finn And Jake' carries the
    show's long/original title; the catalog lists the CANONICAL short
    title 'Adventure Time' (2 of 6 words), which can never reach
    _find_catalog's 60% word floor. The leading-trim ladder then peeled
    the distinctive head word and lowered the floor until subtitle-word
    garbage tied it — Cinemeta's search order flipped the coin between
    'Equal Time with Ted Red and Lou Blue' (tt8619554, 2026-10-13) and
    'Living by Design with Jake and Jazz' (tt14023406, 2026-10-07).
    Fix: when a trim level's floor fails, accept the best TIGHT
    candidate (a title made entirely of query words, >= 2 of them).
[2] Tight-acceptance guard rails: it fires only under a failed floor
    (floor-passing picks are unchanged) and never for 1-word titles.
[3] resolve_identity wrong-show veto: the file name carries an explicit
    S05E25, so a series whose episode map lacks that slot is NOT the
    file's show — the search's other candidates are walked for one that
    has it (the net for matcher shapes [1] still misses).
[4] No candidate carrying the slot: refuse rather than guess (None).
[5] An empty episode map (meta fetch failed) never vetoes.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import stremio  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else " FAIL ") + name)


# Cinemeta search results, frozen from the live endpoint on 2026-10-13
# (ids and names verbatim; only shape matters offline)
TRIM0 = [  # query 'Adventure Time With Finn And Jake'
    {"id": "tt1305826", "name": "Adventure Time"},
    {"id": "tt21308840", "name": "Adventure Time Ep 3, Finn Has a "
                                 "Girlfriend"},
    {"id": "tt15248880", "name": "Adventure Time: Fionna & Cake"},
    {"id": "tt32603995", "name": "Adventure Time: Side Quests"},
    {"id": "tt11165358", "name": "Adventure Time: Distant Lands"},
    {"id": "tt0350448", "name": "Real Time with Bill Maher"},
    {"id": "tt34688214", "name": "Pompeii: Out of Time with Tom "
                                 "Hiddleston"},
    {"id": "tt0078627", "name": "Huckleberry Finn and His Friends"},
]
TRIM1 = [  # query 'Time With Finn And Jake'
    {"id": "tt1305826", "name": "Adventure Time"},
    {"id": "tt8619554", "name": "Equal Time with Ted Red and Lou Blue"},
    {"id": "tt14023406", "name": "Living by Design with Jake and Jazz"},
    {"id": "tt6290792", "name": "Raging Bull: Movie Time with Ed & "
                                "Leroy"},
    {"id": "tt11800410", "name": "It's Miller Time with Billy & Bobby"},
    {"id": "tt2129722", "name": "The Time with Matthew & Randy Show"},
    {"id": "tt0350448", "name": "Real Time with Bill Maher"},
    {"id": "tt0078627", "name": "Huckleberry Finn and His Friends"},
]
# the 2026-10-07 coin flip had the other garbage ranked first at trim 1
TRIM1_OCT7 = [dict(TRIM1[0]), dict(TRIM1[2]), dict(TRIM1[1])] + TRIM1[3:]

BY_QUERY = {
    "Adventure Time With Finn And Jake": TRIM0,
    "Time With Finn And Jake": TRIM1,
}

# the live incident URL (debridio resolve shape; only the path layout
# matters — the tail carries the full release name)
URL = ("https://addon.debridio.com/play/series/torbox/0889be7cbed8417f"
       "47e58540afea55c0/1f7be332-5fea-4aa8-8814-46eabea63736/"
       "d11f2ab15c9fb76f7f0b932c01d59ffaa432df58/Adventure.Time.With."
       "Finn.And.Jake.S05E25.1080p.BluRay.x264-DEiMOS.mkv")

META_AT = {"name": "Adventure Time", "videos":
           [{"season": s, "episode": e, "name": "ep title"}
            for s in range(1, 11) for e in range(1, 30)]}
META_EQUAL = {"name": "Equal Time with Ted Red and Lou Blue",
              "videos": [{"season": 1, "episode": e, "name": "x"}
                         for e in range(1, 7)]}
META_LIVING = {"name": "Living by Design with Jake and Jazz",
               "videos": [{"season": s, "episode": e, "name": "x"}
                          for s in (1, 2, 3) for e in range(1, 13)]}


def metas_by_imdb(imdb):
    return {"tt1305826": META_AT, "tt8619554": META_EQUAL,
            "tt14023406": META_LIVING}.get(imdb, {"name": "?",
                                                  "videos": []})


def main():
    print("[1] _find_catalog: tight candidates answer a failed floor")
    check("full-title query hits Adventure Time (not the trim-1 "
          "garbage)",
          stremio._find_catalog(
              lambda q: BY_QUERY.get(q, []), "Adventure Time With Finn "
              "And Jake")["id"] == "tt1305826")
    check("the 2026-10-07 ordering (Living by Design first) also hits "
          "Adventure Time",
          stremio._find_catalog(
              lambda q: (TRIM1_OCT7 if q == "Time With Finn And Jake"
                         else TRIM0),
              "Adventure Time With Finn And Jake")["id"] == "tt1305826")
    check("floor-reaching picks unchanged: Maher must still lose to "
          "Adventure Time",
          stremio._find_catalog(
              lambda q: [{"id": "tt1", "name": "Real Time with Bill "
                                              "Maher"},
                         {"id": "tt2", "name": "Adventure Time"}],
              "Adventure Time")["id"] == "tt2")

    print("[2] tight-acceptance guard rails")
    check("a 1-word tight title is never the tight answer",
          stremio._find_catalog(
              lambda q: [{"id": "x", "name": "Silo"}],
              "Silo Stories") is None)
    check("no tight candidate, floor fails -> still None (no new "
          "false hits)",
          stremio._find_catalog(
              lambda q: [{"id": "x", "name": "Completely Unrelated "
                                             "Show"}],
              "Adventure Time With Finn And Jake") is None)

    print("[3] resolve_identity: the wrong-show veto + candidate walk")
    saved_fs, saved_ss, saved_sm = (stremio.find_series,
                                    stremio.search_series,
                                    stremio.series_meta)
    try:
        queries = []
        stremio.find_series = \
            lambda q: queries.append(q) or ("tt8619554",
                                            META_EQUAL["name"])
        stremio.search_series = lambda q: list(TRIM1)
        stremio.series_meta = metas_by_imdb
        ident = stremio.resolve_identity(URL, stremio.StreamingServer(""))
        check("veto walks past Equal Time to Adventure Time",
              bool(ident) and ident["stremio_imdb"] == "tt1305826"
              and (ident["season"], ident["episode"]) == (5, 25))
        check("the cleaned full title was the query",
              queries[-1] == "Adventure Time With Finn And Jake")

        # the 2026-10-07 shape: Living by Design wins the matcher first
        stremio.find_series = lambda q: ("tt14023406",
                                         META_LIVING["name"])
        ident = stremio.resolve_identity(URL, stremio.StreamingServer(""))
        check("S04E23-era shape (Living by Design first) also recovers",
              bool(ident) and ident["stremio_imdb"] == "tt1305826")

        print("[4] nothing carries the slot: refuse rather than guess")
        stremio.find_series = lambda q: ("tt8619554",
                                         META_EQUAL["name"])
        stremio.search_series = lambda q: [
            {"id": "tt8619554", "name": META_EQUAL["name"]},
            {"id": "tt14023406", "name": META_LIVING["name"]}]
        check("no candidate has S05E25 -> identity refuses (None)",
              stremio.resolve_identity(URL,
                                       stremio.StreamingServer("")) is None)

        print("[5] an empty episode map never vetoes")
        stremio.find_series = lambda q: ("tt8619554",
                                         META_EQUAL["name"])
        stremio.series_meta = lambda imdb: {}
        ident = stremio.resolve_identity(URL, stremio.StreamingServer(""))
        check("meta fetch failed -> the hit stands (no network-fail "
              "veto)", bool(ident)
              and ident["stremio_imdb"] == "tt8619554")
    finally:
        (stremio.find_series, stremio.search_series,
         stremio.series_meta) = saved_fs, saved_ss, saved_sm

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
