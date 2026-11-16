# -*- coding: utf-8 -*-
"""Offline regression test: the 2026-11-13 17:34 wrong-movie handoff.

The user picked The Santa Clause (1994, Tim Allen) from a working
Stremio stream list; MichaelTV opened a different Santa film.

Chain:
[1] The handoff URL named the file 'Santa Claus 1 (1994) UHDRDV cz
    en.mkv'. The word-floor matcher queried 'Santa Claus'. Cinemeta's
    1985 film 'Santa Claus' covers BOTH query words, while the film it
    actually is — 'The Santa Clause' — covers one (its title spells
    Clause), so the year tier never got a say and identity came out as
    the 1985 film (tt0089961).
[2] TorBox throttled the link (probe HTTP 429), the start guard read
    the unproven link as dead, and the next-stream walk rebuilt the
    list from the WRONG title's IMDb id and served a stream of the
    1985 film — the "weird other santa movie".

Fix covered here:
[a] A known release year SCOPES the candidate pool to that year before
    any word counting, and within it the closest TITLE TEXT
    (_title_sim) answers even below the word floor — a release that
    spells the title differently can never win on words, but it is
    plainly the same title.
[b] The LAST year in a release name is the release year ('1917.2019',
    '2020.2012' style titles carry a year of their own); the first
    match left the head empty and pointed the scoping at a year that
    cannot hold the film.

Cinemeta results are frozen from the live endpoint on 2026-11-13 (ids,
names and release years verbatim; only shape matters offline).
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


# Cinemeta /catalog/movie/top/search=Santa Claus.json, relevance order
SANTA_CLAUS = [
    {"id": "tt0111070", "name": "The Santa Clause", "year": "1994"},
    {"id": "tt0089961", "name": "Santa Claus", "year": "1985"},
    {"id": "tt0066327", "name": "Santa Claus Is Comin' to Town",
     "year": "1970"},
    {"id": "tt0072424", "name": "The Year Without a Santa Claus",
     "year": "1974"},
    {"id": "tt0304669", "name": "The Santa Clause 2", "year": "2002"},
    {"id": "tt0053241", "name": "Santa Claus", "year": "1959"},
    {"id": "tt0089488", "name": "The Life & Adventures of Santa Claus",
     "year": "1985"},
    {"id": "tt0058548", "name": "Santa Claus Conquers the Martians",
     "year": "1964"},
]
# search=Santa Claus 1.json — the '1' token drags in parade footage that
# shares both title words and is from the SAME year, which the word
# floor alone would hand the identity to
SANTA_CLAUS_1 = [
    {"id": "tt38691250", "name": "St. John's Jaycees/Avalon Mall Santa "
                                "Claus Parade", "year": "1994"},
    {"id": "tt0111070", "name": "The Santa Clause", "year": "1994"},
    {"id": "tt0304669", "name": "The Santa Clause 2", "year": "2002"},
    {"id": "tt0089961", "name": "Santa Claus", "year": "1985"},
    {"id": "tt0053241", "name": "Santa Claus", "year": "1959"},
]

# the incident's exact handoff URL (torrentio/torbox resolve shape; only
# the path layout matters — the tail carries the release name)
URL = ("https://torrentio.strem.fun/resolve/torbox/1f7be332-5fea-4aa8-"
       "8814-46eabea63736/b30a91a60a9d1eb6fe37042a00f187a1560a0a17/"
       "Santa%20Claus%201%20(1994)%20UHDRDV%20cz%20en.mkv/0/"
       "Santa%20Claus%201%20(1994)%20UHDRDV%20cz%20en.mkv")


def main():
    print("[1] _movie_queries: the LAST year is the release year")
    qs = stremio._movie_queries("Santa Claus 1 (1994) UHDRDV cz en.mkv")
    check("incident name heads to 'Santa Claus 1' + year 1994",
          bool(qs) and qs[0] == ("Santa Claus 1", "1994"))
    qs = stremio._movie_queries("1917.2019.2160p.UHD.BluRay.mkv")
    check("'1917.2019' keeps the title and takes 2019 as the year",
          bool(qs) and qs[0] == ("1917", "2019"))
    qs = stremio._movie_queries("2012.2009.DVDRip.mkv")
    check("'2012.2009' keeps the title and takes 2009 as the year",
          bool(qs) and qs[0] == ("2012", "2009"))
    qs = stremio._movie_queries(
        "Blade.Runner.2049.2017.1080p.BluRay.mkv")
    check("'Blade Runner 2049.2017' keeps the title and takes 2017",
          bool(qs) and qs[0] == ("Blade Runner 2049", "2017"))
    qs = stremio._movie_queries("Chinatown.1974.2160p.BluRay.mkv")
    check("single-year names are unchanged",
          bool(qs) and qs[0] == ("Chinatown", "1974"))

    print("[2] find_movie: the release year scopes the candidate pool")
    saved = stremio.search_movies
    try:
        stremio.search_movies = \
            lambda q: list(SANTA_CLAUS if q.startswith("Santa Claus")
                           and not q.endswith(" 1") else SANTA_CLAUS_1)
        hit = stremio.find_movie("Santa Claus 1", "1994")
        check("incident name resolves to The Santa Clause (1994), not "
              "the 1985 film",
              hit and hit["id"] == "tt0111070" and hit["year"] == "1994")
        # the 1985 film ranked FIRST — the live ordering that lost
        stremio.search_movies = lambda q: [
            SANTA_CLAUS[1]] + list(SANTA_CLAUS[::1])
        hit = stremio.find_movie("Santa Claus 1", "1994")
        check("1985-first ordering still resolves to the 1994 film",
              hit and hit["id"] == "tt0111070")
        check("without the year the old word-count answer stands "
              "(no new guessing on yearless names)",
              stremio.find_movie("Santa Claus 1")["id"] == "tt0089961")

        # same-year junk that shares both words must not beat the film
        # the text actually spells
        stremio.search_movies = lambda q: list(SANTA_CLAUS_1)
        hit = stremio.find_movie("Santa Claus 1", "1994")
        check("a same-year word-sharing parade does not become the movie",
              hit and hit["id"] == "tt0111070")

        print("[3] the scoped pool keeps the honest matches")
        # a foreign-language release head shares no letters but all the
        # words: the tight fallback (not similarity) still answers
        stremio.search_movies = lambda q: [
            {"id": "tt0105236", "name": "Reservoir Dogs", "year": "1992"}]
        hit = stremio.find_movie("Бешеные псы Reservoir Dogs", "1992")
        check("foreign release head still matches by words + year",
              hit and hit["id"] == "tt0105236")
        # the remake/remaster shape: year picks the right installment
        stremio.search_movies = lambda q: [
            {"id": "tt0100802", "name": "Total Recall", "year": "1990"},
            {"id": "tt1386703", "name": "Total Recall", "year": "2012"}]
        check("remake year picks the 1990 original",
              stremio.find_movie("Total Recall", "1990")["id"]
              == "tt0100802")
        check("remake year picks the 2012 remake",
              stremio.find_movie("Total Recall", "2012")["id"]
              == "tt1386703")
        # a release year the catalog does not carry (off year) must not
        # empty the answer out
        check("a year missing from the catalog falls back to the words",
              stremio.find_movie("Total Recall", "1991")["id"]
              == "tt0100802")

        print("[4] resolve_identity: the incident handoff URL end to end")
        stremio.search_movies = \
            lambda q: list(SANTA_CLAUS if q == "Santa Claus"
                           else SANTA_CLAUS_1)
        saved_cd = stremio._content_disposition
        stremio._content_disposition = lambda u: ""
        try:
            ident = stremio.resolve_identity(
                URL, stremio.StreamingServer(""))
        finally:
            stremio._content_disposition = saved_cd
        check("handoff resolves to The Santa Clause (1994)",
              bool(ident) and ident.get("movie") is True
              and ident.get("stremio_imdb") == "tt0111070"
              and ident.get("movie_name") == "The Santa Clause")
    finally:
        stremio.search_movies = saved

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
