# -*- coding: utf-8 -*-
"""Offline regression test: the 2026-09-22 20:19/20:21 next-stream
deaths on Yu-Gi-Oh! handoffs.

The S-button (next stream) reported "no other stream found" while
torrentio listed dozens — next_stream_playable bails at resolve_identity
BEFORE ever asking the addons, and identity died on three independent
name-parsing holes, each covered here:

[1] parse_abs_episode: 'Episode (11)' end-anchored absolute numbering,
    no season marker (anime/episode packs); mid-title 'Episode 7' movie
    names must NOT match.
[2] clean_show_name: leading '[Group]' release tags strip off the head
    (the DragsterPS handoff searched for 'DragsterPS] Yu Gi Oh!').
[3] _find_catalog: an all-short-word title ('Yu Gi Oh!' — every word
    ≤2 chars) left the >2-char word filter with NOTHING; the trim loop
    searched '' and gave up. Fallback: search the cleaned name itself,
    take the first candidate whose title contains it.
[4] resolve_identity else-branch: a Content-Disposition name only
    overrides the URL tail when the tail is opaque junk or the header
    name carries the marker the tail lacks — the tail IS the played
    file's name on torrentio links ('Yu-Gi-Oh! Duel Monsters Episode
    (11)' must survive a bare 'Yu-Gi-Oh!' pack name from the header).
[5] resolve_identity abs path: the absolute number places on the
    series meta's own episode order ('Episode (11)' -> S01E11 on a
    224-episode map); unplaceable numbers refuse rather than guess.
[6] next_stream_playable end-to-end: with identity resolving, the
    debrid re-resolve walk actually serves the next-ranked torrent –
    the "dozens available, none served" symptom.
[7] next_stream_playable walks the ADDON's own list order (the order
    Stremio displays), NOT the autoplay ranking — the 2026-09-27 22:31
    Alone S13E11 shape: first torbox resolve dead, the rank order's
    next picks (auto mode = 4K-first) burned two more dead re-resolves,
    while the list's own #2 played fine in Stremio. Also: a current
    stream missing from the list is never re-served from the top.
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


# the two live incident URLs (api key truncated to a placeholder shape —
# only the path layout matters for parsing)
URL_A = ("https://torrentio.strem.fun/resolve/torbox/1f7be332-5fea-4aa8"
         "-8814-46eabea63736/cab78825102358c75d61a21dc8e329cfba714f2d/"
         "%5BDragsterPS%5D%20Yu-Gi-Oh!%20S01E11%20%5B480p%5D%20%5BMulti-"
         "Audio%5D%20%5BMulti-Subs%5D%20%5B1C20D328%5D.mkv/10/%5BDragster"
         "PS%5D%20Yu-Gi-Oh!%20S01E11%20%5B480p%5D%20%5BMulti-Audio%5D%20"
         "%5BMulti-Subs%5D%20%5B1C20D328%5D.mkv")
URL_B = ("https://torrentio.strem.fun/resolve/torbox/1f7be332-5fea-4aa8"
         "-8814-46eabea63736/c66c9e1db02208684c5c860f363e38e218fd491c/"
         "Yu-Gi-Oh!%20Duel%20Monsters%20Episode%20(11).mp4/21/Yu-Gi-Oh!"
         "%20Duel%20Monsters%20Episode%20(11).mp4")

META_DM = {"name": "Yu-Gi-Oh! Duel Monsters", "videos": [
    {"season": s, "episode": e, "name": "Duel %d" % n}
    for n, (s, e) in enumerate(
        [(s, e) for s in (1, 2) for e in range(1, 13)], 1)]}


def fake_series_meta(imdb):
    return dict(META_DM) if imdb == "tt0247902" else {"name": "?",
                                                      "videos": []}


def main():
    print("[1] parse_abs_episode: end-anchored absolute numbering")
    check("'Episode (11)' -> 11",
          stremio.parse_abs_episode(
              "Yu-Gi-Oh! Duel Monsters Episode (11)") == 11)
    check("'Episode 11' -> 11",
          stremio.parse_abs_episode("Show Episode 11") == 11)
    check("'Ep.11' -> 11",
          stremio.parse_abs_episode("Show Ep.11") == 11)
    check("mid-title 'Episode 7' stays a movie (Star Wars shape)",
          stremio.parse_abs_episode(
              "Star Wars Episode 7 The Force Awakens") is None)
    check("no keyword, no match",
          stremio.parse_abs_episode("Some Movie 2023") is None)
    check("episode 0 is not an episode",
          stremio.parse_abs_episode("Show Episode (0)") is None)
    check("empty text, no match", stremio.parse_abs_episode("") is None)

    print("[2] clean_show_name: leading [Group] tags strip off the head")
    check("the DragsterPS head cleans to 'Yu Gi Oh!'",
          stremio.clean_show_name(
              "[DragsterPS] Yu-Gi-Oh! S01E11 [480p] [Multi-Audio] "
              "[Multi-Subs] [1C20D328]", strip_year=True) == "Yu Gi Oh!")
    check("canonical title years still kept by default",
          stremio.clean_show_name("Space: 1999") == "Space: 1999")
    check("dotted release head still cleans (Bluey 2018 fix intact)",
          stremio.clean_show_name(
              "Bluey.2018.S01E50.1080p.DSNP.WEB-DL.x265",
              strip_year=True) == "Bluey")
    check("tag-less heads unchanged",
          stremio.clean_show_name(
              "Love.is.Blind.UK.S03E11.1080p.WEB.h264-EDITH",
              strip_year=True) == "Love is Blind UK")

    print("[3] _find_catalog: all-short-word titles")
    yu_metas = [{"id": "tt0249327", "name": "Yu-Gi-Oh!"},
                {"id": "tt0247902", "name": "Yu-Gi-Oh! Duel Monsters"},
                {"id": "tt0493334", "name": "Yu-Gi-Oh! GX"}]
    hit = stremio._find_catalog(lambda q: list(yu_metas), "Yu Gi Oh!")
    check("'Yu Gi Oh!' hits the most popular containing candidate",
          bool(hit) and hit["id"] == "tt0249327")
    check("non-containing candidates still miss",
          stremio._find_catalog(
              lambda q: [{"id": "tt1", "name": "Duel Masters"}],
              "Yu Gi Oh!") is None)
    check("word scoring unchanged: Maher must not beat Adventure Time",
          stremio._find_catalog(
              lambda q: [{"id": "tt1", "name": "Real Time with Bill "
                                              "Maher"},
                         {"id": "tt2", "name": "Adventure Time"}],
              "Adventure Time")["id"] == "tt2")
    check("case B shape: 'Duel Monsters' beats 'Duel Masters' on words",
          stremio._find_catalog(
              lambda q: [{"id": "tt0247902", "name": "Yu-Gi-Oh! Duel "
                                                    "Monsters"},
                         {"id": "tt0404597", "name": "Duel Masters"}],
              "Duel Monsters")["id"] == "tt0247902")

    print("[4] _opaque_tail: what a Content-Disposition name may replace")
    check("bare index tail is opaque", stremio._opaque_tail("21"))
    check("1-char tail is opaque", stremio._opaque_tail("v"))
    check("uuid tail is opaque",
          stremio._opaque_tail("1f7be332-5fea-4aa8-8814-46eabea63736"))
    check("the episode tail survives",
          not stremio._opaque_tail(
              "Yu-Gi-Oh! Duel Monsters Episode (11)"))
    check("a single-word title tail survives",
          not stremio._opaque_tail("Silo"))

    saved_cd, saved_fs, saved_sm = (stremio._content_disposition,
                                    stremio.find_series,
                                    stremio.series_meta)
    try:
        print("[5] resolve_identity: the two live incident URLs")
        # ---- 20:19: [DragsterPS] Yu-Gi-Oh! S01E11 (torrentio tail) ----
        cd_calls, queries = [], []
        stremio._content_disposition = \
            lambda u: cd_calls.append(u) or "pack-name.mkv"
        stremio.find_series = \
            lambda q: queries.append(q) or ("tt0249327", "Yu-Gi-Oh!")
        stremio.series_meta = fake_series_meta
        ident = stremio.resolve_identity(URL_A,
                                         stremio.StreamingServer(""))
        check("DragsterPS head identifies as Yu-Gi-Oh! S01E11",
              bool(ident) and ident["stremio_imdb"] == "tt0249327"
              and (ident["season"], ident["episode"]) == (1, 11))
        check("the bracket-tag query searched as 'Yu Gi Oh!'",
              queries[-1] == "Yu Gi Oh!")
        check("marker in the tail: Content-Disposition never probed",
              not cd_calls)

        # ---- 20:21: 'Episode (11)' pack, CD says the bare pack name ----
        cd_calls.clear(), queries.clear()
        stremio._content_disposition = \
            lambda u: cd_calls.append(u) or "Yu-Gi-Oh!.mp4"
        stremio.find_series = \
            lambda q: queries.append(q) or ("tt0247902",
                                            "Yu-Gi-Oh! Duel Monsters")
        ident = stremio.resolve_identity(URL_B,
                                         stremio.StreamingServer(""))
        check("'Episode (11)' pack identifies as Duel Monsters S01E11",
              bool(ident) and ident["stremio_imdb"] == "tt0247902"
              and (ident["season"], ident["episode"]) == (1, 11))
        check("the informative tail survived the Content-Disposition "
              "probe", bool(ident)
              and ident["file_name"] == "Yu-Gi-Oh! Duel Monsters "
              "Episode (11)")
        check("episode token dropped from the series query",
              queries[-1] == "Yu Gi Oh! Duel Monsters")
        check("abs numbering beyond the map refuses to guess",
              stremio.resolve_identity(
                  URL_B.replace("(11)", "(999)"),
                  stremio.StreamingServer("")) is None)
        stremio.series_meta = lambda imdb: {}
        check("empty meta map refuses to guess",
              stremio.resolve_identity(
                  URL_B, stremio.StreamingServer("")) is None)
        stremio.series_meta = fake_series_meta

        # ---- CD still wins when the tail is opaque junk ----
        stremio._content_disposition = \
            lambda u: cd_calls.append(u) or "Pack.Show.S02E05.mkv"
        url_junk = ("https://torrentio.strem.fun/resolve/torbox/key/"
                    "c66c9e1db02208684c5c860f363e38e218fd491c/"
                    "whatever/21/21")
        ident = stremio.resolve_identity(url_junk,
                                         stremio.StreamingServer(""))
        check("opaque '21' tail replaced by the CD name's marker",
              bool(ident) and ident["file_name"] == "Pack.Show.S02E05.mkv"
              and (ident["season"], ident["episode"]) == (2, 5))

        # ---- a plain movie tail still takes the movie path ----
        stremio._content_disposition = lambda u: ""
        saved_mi, stremio._movie_identity = (
            stremio._movie_identity,
            lambda f, t: {"movie": True, "display_name": f,
                          "file_name": f, "torrent_name": t})
        try:
            ident = stremio.resolve_identity(
                "https://addon.example/play/xyz/Some.Movie.Name.2019/"
                "0/Some.Movie.Name.2019",
                stremio.StreamingServer(""))
            check("markerless movie name still reaches the movie path",
                  bool(ident) and ident.get("movie")
                  and ident["file_name"] == "Some.Movie.Name.2019")
        finally:
            stremio._movie_identity = saved_mi
    finally:
        (stremio._content_disposition, stremio.find_series,
         stremio.series_meta) = saved_cd, saved_fs, saved_sm

    print("[6] next_stream_playable: identity fixed -> the walk serves")
    from src.config import Config
    saved_ri, saved_as = (stremio.resolve_identity,
                          stremio.addon_streams)
    try:
        stremio.resolve_identity = lambda url, server: {
            "stremio_imdb": "tt0247902",
            "series_name": "Yu-Gi-Oh! Duel Monsters",
            "season": 1, "episode": 11,
            "torrent_name": "", "file_name": "Episode (11)"}
        stremio.addon_streams = \
            lambda cfg, imdb, s, e: [
                {"name": "current release 1080p", "infoHash":
                 "c66c9e1db02208684c5c860f363e38e218fd491c",
                 "fileIdx": 21},
                {"name": "another release 720p", "infoHash":
                 "0123456789abcdef0123456789abcdef01234567",
                 "fileIdx": 0}]
        cur = {"kind": "stremio", "title": "Stremio stream",
               "url": URL_B, "fav_key": "stremio:c66c9e1db0220868:21",
               "info_hash": "c66c9e1db02208684c5c860f363e38e218fd491c",
               "file_idx": 21}
        nxt = stremio.next_stream_playable(Config({}, None), dict(cur))
        check("the next-ranked torrent is served through debrid "
              "re-resolve", bool(nxt)
              and nxt["info_hash"] == "0123456789abcdef0123456789abcd"
              "ef01234567"
              and str(nxt["url"]).startswith(
                  "https://torrentio.strem.fun/resolve/torbox/"))
        check("the playable keeps the same episode identity",
              bool(nxt) and nxt.get("stremio_imdb") == "tt0247902"
              and (nxt.get("season"), nxt.get("episode")) == (1, 11))
    finally:
        stremio.resolve_identity, stremio.addon_streams = \
            saved_ri, saved_as

    print("[7] next_stream_playable: the walk follows the ADDON's list "
          "order, not the rank order")
    saved_ri, saved_as = (stremio.resolve_identity,
                          stremio.addon_streams)
    try:
        ident = {"stremio_imdb": "tt4803766",
                 "series_name": "Alone", "season": 13, "episode": 11,
                 "torrent_name": "", "file_name": "Alone S13E11"}
        stremio.resolve_identity = lambda url, server: dict(ident)
        # the incident shape, distilled: the list the addon returns (and
        # Stremio displays) is 1080p-current -> 720p -> 4K. With
        # resolution_pref "auto" the RANKING puts the 4K first among the
        # remaining — exactly the two dead picks of 2026-09-27 — but the
        # walk must step to the LIST's #2 (the 720p).
        cur_hash = "2e08b62d3a42" + "0" * 28
        second_hash = "aaaa" + "0" * 36
        big4k_hash = "bbbb" + "0" * 36
        stremio.addon_streams = \
            lambda cfg, imdb, s, e: [
                {"name": "Alone S13E11 1080p \U0001F464 80 \U0001F4BE 2.1GB",
                 "infoHash": cur_hash, "fileIdx": 0},
                {"name": "Alone S13E11 720p \U0001F464 12 \U0001F4BE 1.4GB",
                 "infoHash": second_hash, "fileIdx": 0},
                {"name": "Alone S13E11 2160p 4K \U0001F464 300 \U0001F4BE "
                 "8.2GB",
                 "infoHash": big4k_hash, "fileIdx": 0}]
        cur = {"kind": "stremio", "title": "Alone — S13E11",
               "url": "https://torrentio.strem.fun/resolve/torbox/key/"
                      + cur_hash + "/x/0",
               "info_hash": cur_hash, "file_idx": 0,
               "fav_key": "stremio:tt4803766:13:11",
               "stremio_imdb": "tt4803766", "movie": False,
               "season": 13, "episode": 11}
        cfg = Config({"stremio_resolution_pref": "auto"}, None)
        # sanity: the RANKING really does prefer the 4K here (that's the
        # trap this regression pins)
        ranked = stremio.rank_streams(
            cfg, stremio.addon_streams(cfg, "tt4803766", 13, 11), cur)
        check("rank order (auto) prefers the 4K — the old trap",
              ranked[0]["infoHash"] == big4k_hash)
        nxt = stremio.next_stream_playable(cfg, dict(cur))
        check("the walk serves the LIST's next entry (720p), not the 4K "
              "the ranking prefers",
              bool(nxt) and nxt["info_hash"] == second_hash)
        check("second press walks to the list's third entry (the 4K)",
              bool(stremio.next_stream_playable(cfg, dict(
                  cur, info_hash=second_hash,
                  url="https://torrentio.strem.fun/resolve/torbox/key/"
                      + second_hash + "/x/0")))
              and stremio.next_stream_playable(
                  cfg, dict(cur, info_hash=big4k_hash,
                            url="https://torrentio.strem.fun/resolve/"
                                "torbox/key/" + big4k_hash + "/x/0"))
              is None)

        # current stream NOT in the addon list: start from the top but
        # never re-serve the current one (matched by URL here)
        outside = "cccc" + "0" * 36
        cur2 = dict(cur, info_hash=outside,
                    url="https://torrentio.strem.fun/resolve/torbox/key/"
                        + outside + "/x/0")
        stremio.addon_streams = \
            lambda cfg, imdb, s, e: [
                {"name": "list top is the CURRENT url", "url":
                 "https://torrentio.strem.fun/resolve/torbox/key/"
                 + outside + "/x/0"},
                {"name": "list second", "infoHash": second_hash,
                 "fileIdx": 0},
                {"name": "list third", "infoHash": big4k_hash,
                 "fileIdx": 0}]
        nxt2 = stremio.next_stream_playable(cfg, dict(cur2))
        check("not-in-list walk skips the top entry that IS the current "
              "stream", bool(nxt2) and nxt2["info_hash"] == second_hash)
    finally:
        stremio.resolve_identity, stremio.addon_streams = \
            saved_ri, saved_as

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
