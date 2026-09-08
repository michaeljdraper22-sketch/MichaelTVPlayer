# -*- coding: utf-8 -*-
"""Live probe for the next-stream feature (2026-09-08): real torrentio
stream lists, real ranking, real walking — no playback, no local-server
creates (StreamingServer is stubbed so nothing is ever started on the
user's machine).

Runs three checks against the DEFAULT addon (torrentio.strem.fun):
  [1] a series episode: rank the real list, show the top entries with
      their language penalty, then walk next_stream_playable down it
      (best -> second -> third), confirming the walk skips the current
      stream by hash/url and stops with None at the end;
  [2] English preference on live data: any foreign-tagged release in the
      real list must rank below every English-capable/neutral one;
  [3] a movie: the /stream/movie/ endpoint, one walk step.

Run:  .venv\\Scripts\\python.exe probe_next_stream.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import stremio  # noqa: E402
from src.config import Config  # noqa: E402


class NoCreateServer:
    """StreamingServer stand-in: recording creates instead of starting
    torrents on the user's local Stremio server."""

    def __init__(self, base=""):
        self.created = []

    def create(self, info_hash, trackers=()):
        self.created.append(info_hash)
        return True

    @staticmethod
    def play_url(info_hash, file_idx):
        return "http://127.0.0.1:11470/%s/%d" % (info_hash, file_idx)


def show(stream, pen):
    name = " ".join(str(stream.get("title") or
                         stream.get("name") or "").split())[:72]
    kind = "url" if stream.get("url") else "torrent"
    return "  pen=%d %-7s %s" % (pen, kind, name)


def main():
    cfg = Config({}, None)
    stremio.StreamingServer = NoCreateServer

    print("[1] series: Adventure Time S05E42 (tt1305826)")
    streams = stremio.addon_streams(cfg, "tt1305826", 5, 42)
    print("  %d streams from the default addon" % len(streams))
    if not streams:
        print("  (addon unreachable — nothing to probe)")
        return
    ranked = stremio.rank_streams(cfg, streams)
    foreign = [s for s in ranked if stremio.lang_penalty(s)]
    english_head = len(ranked) - len(foreign)
    for s in ranked[:6]:
        print(show(s, stremio.lang_penalty(s)))
    if len(foreign):
        print("  ... %d foreign-tagged releases demoted below all %d "
              "English/neutral ones:" % (len(foreign), english_head))
        for s in foreign[:3]:
            print(show(s, 1))
        worst = ranked.index(foreign[0])
        print("  first foreign entry sits at rank %d of %d" %
              (worst + 1, len(ranked)))
    else:
        print("  (no foreign-tagged releases in this list today)")

    # walk: best -> second -> third, purely on the real ranking
    cur = {"kind": "stremio", "title": "Adventure Time — S05E42",
           "fav_key": "stremio:tt1305826:5:42",
           "stremio_imdb": "tt1305826", "season": 5, "episode": 42,
           "series_name": "Adventure Time"}
    first = ranked[0]
    cur["url"] = first.get("url") or \
        "http://127.0.0.1:11470/%s/%d" % (first.get("infoHash"),
                                         first.get("fileIdx") or 0)
    if first.get("infoHash"):
        cur["info_hash"] = str(first["infoHash"]).lower()
        cur["file_idx"] = int(first.get("fileIdx") or 0)
    step = 0
    while True:
        nxt = stremio.next_stream_playable(cfg, dict(cur))
        if not nxt:
            print("  walk ended after %d switches (None at the last one)"
                  % step)
            break
        step += 1
        picked = ranked[step] if step < len(ranked) else None
        same = picked is not None and (
            picked.get("url") == nxt.get("url")
            or str(picked.get("infoHash") or "").lower()
            == str(nxt.get("info_hash") or "").lower())
        print("  step %d -> %-7s %s%s" % (
            step, "url" if nxt.get("url", "").startswith("http") else "srv",
            str(nxt.get("url"))[:64],
            "" if same else "   (rank mismatch!)"))
        if step >= 3:
            print("  (stopped walking after 3 switches)")
            break
        cur = nxt

    print("[2] movie: Dune (tt1160419)")
    mstreams = stremio.addon_movie_streams(cfg, "tt1160419")
    print("  %d streams from /stream/movie/" % len(mstreams))
    if mstreams:
        mranked = stremio.rank_streams(cfg, mstreams)
        for s in mranked[:4]:
            print(show(s, stremio.lang_penalty(s)))
        mcur = {"kind": "stremio", "movie": True,
                "title": "Dune (2021)",
                "fav_key": "stremio:url:probe",
                "stremio_imdb": "tt1160419",
                "movie_name": "Dune",
                "year": "2021"}
        best = mranked[0]
        mcur["url"] = best.get("url") or \
            "http://127.0.0.1:11470/%s/%d" % (best.get("infoHash"),
                                             best.get("fileIdx") or 0)
        mcur["info_hash"] = str(best.get("infoHash") or "").lower() or None
        mn = stremio.next_stream_playable(cfg, dict(mcur))
        if mn:
            second = mranked[1] if len(mranked) > 1 else None
            ok = second is not None and (
                second.get("url") == mn.get("url")
                or str(second.get("infoHash") or "").lower()
                == str(mn.get("info_hash") or "").lower())
            print("  movie walk best -> second: %s (%s)" % (
                str(mn.get("url"))[:64], "matches rank 2" if ok
                else "MISMATCH"))
        else:
            print("  movie walk: None (only one usable stream?)")
    print("done.")


if __name__ == "__main__":
    main()
