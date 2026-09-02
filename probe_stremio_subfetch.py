# -*- coding: utf-8 -*-
"""Offscreen probe: bitmap-only / textless streams auto-fetch a TEXT
subtitle online and render it in the app's styled caption overlay.

User report (2026-09-02): stremio streams "don't always let me style the
subtitles — VLC renders them". Log forensics 16:17-16:19: Adventure Time
S03E12, the only embedded sub track was S_HDMV/PGS (pictures — the
overlay can only restyle text), so _cap_vod_check funneled to
_cap_vod_handoff -> "No restyleable text track — VLC renders". The user
rejected both workarounds (pick subs in Stremio pre-handoff; pick WEB-DL
releases); the fetch must be automatic and silent.

This probe pins the new path end to end:
  * the fetch layer in src/stremio.py — opensubs_hash math, the ranked
    search (language filter, wrong-episode drops, release-overlap order,
    hash boost, videoHash tolerance), the capped download, and the lazy
    vod/series identity resolution;
  * the player integration — the dead-end funnel ARMS a search instead
    of latching failure (VLC's own bitmap rendering stays on meanwhile),
    the late-identity kick, success engage (overlay paints, VLC spu off,
    profanity engine fed), failure restoring today's exact classic body,
    stale-media drops, re-fetch clearing the store, the manual menu row,
    and the config gate.

No window, no focus, no audio: FakeVLC records calls. Offline legs use
scripted responses (save/restore monkeypatching); the LIVE legs at the
end hit the real keyless addon (bounded — skip with
MTP_SUBFETCH_SKIP_LIVE=1).
"""
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import DEFAULTS, Config          # noqa: E402
from src import stremio                          # noqa: E402
from src import profanity as prof_mod            # noqa: E402
from src.ui.player_view import PlayerView        # noqa: E402

from PyQt5 import QtWidgets                     # noqa: E402

app = QtWidgets.QApplication(sys.argv)
cfg = Config(dict(DEFAULTS), None)
pv = PlayerView(cfg)
pv.resize(1280, 720)
# the UI tick + cursor poll run against FakeVLC (None-return stubs): a
# pump window >= their interval once qFatal'd the harness (the old [9]
# note). The legs below sometimes need long pumps (drain/wait_delivered),
# so stop both — no leg depends on either timer.
pv.timer.stop()
pv.cursor_timer.stop()
fails = [0]


def check(name, cond, extra=""):
    print(("  ok   " if cond else "FAIL ") + name
          + ("" if cond or not extra else "  [%s]" % extra))
    if not cond:
        fails[0] += 1


tmpdir = tempfile.mkdtemp(prefix="mtp_probe_subfetch_")

SRT_BODY = ("1\n00:00:10,000 --> 00:00:12,000\nline one\nline two\n\n"
            "2\n00:00:20,000 --> 00:00:22,000\nwhat the fuck\n\n")


def write(name: str, body: str):
    p = os.path.join(tmpdir, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p


# ---- scripted-response plumbing (probe_stremio's _Sess pattern) ----

class _R:
    def __init__(self, status=200, json_data=None, chunks=None):
        self.status_code = status
        self._json = json_data
        self._chunks = chunks

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    def iter_content(self, chunk_size=65536):
        for c in (self._chunks or []):
            yield c


class _Sess:
    """Scripted GETs: search (json) and download (stream) share one tape;
    entries may be _R objects or callables(url, kwargs) -> _R."""

    def __init__(self, script):
        self.script = script
        self.calls = []
        self.i = 0

    def get(self, url, **kw):
        self.calls.append((url, kw))
        item = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if callable(item) and not isinstance(item, _R):
            item = item(url, kw)
        return item


def sub(id_, lang="eng", fname="", release="", season=None, episode=None,
        url=None, **extra):
    d = {"id": id_, "lang": lang,
         "url": url or ("http://x/%s.srt" % id_),
         "subtitleFileName": fname, "movieReleaseName": release,
         "season": season, "episode": episode}
    d.update(extra)
    return d


print("[1] opensubs_hash — 64KB-chunk moviehash, pure math")


def ref_oshash(head, tail, total):
    h = int(total) & 0xFFFFFFFFFFFFFFFF
    for chunk in (bytes(tail)[-65536:], bytes(head)[:65536]):
        for i in range(0, len(chunk) - len(chunk) % 8, 8):
            h = (h + int.from_bytes(chunk[i:i + 8], "little")) \
                & 0xFFFFFFFFFFFFFFFF
    return h


eight = b"\x01" * 8
check("8-byte file counted twice (head==tail): hand vector",
      stremio.opensubs_hash(eight, eight, 8) == 0x020202020202020A,
      hex(stremio.opensubs_hash(eight, eight, 8)))
big_head = b"\x01" * 131072
big_tail = b"\x02" * 131072
check(">64KB chunks trimmed to 64KB each (matches reference)",
      stremio.opensubs_hash(big_head, big_tail, 262144)
      == ref_oshash(big_head, big_tail, 262144))
check("distinguishing bytes past the head chunk ignored",
      stremio.opensubs_hash(b"\x01" * 65536 + b"\xff" * 65536,
                            b"\x03" * 65536, 262144)
      == ref_oshash(b"\x01" * 65536 + b"\xff" * 65536,
                    b"\x03" * 65536, 262144))
check("empty buffers -> just the size",
      stremio.opensubs_hash(b"", b"", 12345) == 12345)
check("mod 2^64 wraparound held",
      stremio.opensubs_hash(b"\xff" * 65536, b"\xff" * 65536,
                            (1 << 64) + 7)
      == ref_oshash(b"\xff" * 65536, b"\xff" * 65536, (1 << 64) + 7))

print("[2] search_online_subtitles — ranking (scripted addon)")
saved_sess = stremio._session
try:
    tape = [{"subtitles": [
        sub("a", fname="Show.S01E01.DTS.ENG.srt", release="Show DTS",
            season=1, episode=1),
        sub("b", lang="rus", fname="Show.S01E01.RUS.srt", season=1,
            episode=1),
        sub("c", fname="Show.S08E02.WEB.ENG.srt", season=1, episode=1),
        sub("d", fname="Show.S01E02.ENG.srt", season=8, episode=2),
        sub("e", fname="Show.S01E01.ENG.OTHER-GROUP.srt", release="other",
            season=1, episode=1),
        sub("f", fname="Show.S01E01.ENG.mached.srt", season=1, episode=1,
            matchedByHash="1"),
        sub("g", fname="no-marker-fields-lie", release="whatever",
            season=1, episode=1),
    ]}]
    stremio._session = _Sess([_R(200, {"subtitles": tape[0]["subtitles"]})])
    got = stremio.search_online_subtitles(
        "series", "tt123", 1, 1, want_lang="eng",
        file_hint="Show.S01E01.DTS-GOODGROUP")
    ids = [c["id"] for c in got]
    check("language filter (rus dropped)", "b" not in ids)
    check("filename marker beats lying fields (S08E02 file dropped)",
          "c" not in ids)
    check("fields-only mismatch dropped (no marker, S/E 8:2)",
          "d" not in ids)
    check("fields-lie + matching marker KEPT (ground truth)",
          "a" in ids and "e" in ids and "g" in ids)
    check("token overlap ranks the matching release first",
          ids[0] == "a", repr(ids))
    check("no videoHash -> no hash boost anywhere",
          not any(c["hash_match"] for c in got))
    check("<=6 candidates", len(got) <= 6)
    url_called = stremio._session.calls[0][0]
    check("series URL shape", "/subtitles/series/tt123:1:1.json" in
          url_called, url_called)

    stremio._session = _Sess([_R(200, {"subtitles": tape[0]["subtitles"]})])
    got = stremio.search_online_subtitles(
        "series", "tt123", 1, 1, want_lang="eng",
        file_hint="unrelated hint entirely", video_hash="deadbeef00000000")
    check("videoHash appended to the URL when provided",
          "videoHash=deadbeef00000000" in stremio._session.calls[0][0])
    check("hash boost outranks token overlap",
          got[0]["id"] == "f" and got[0]["hash_match"], repr(got[0]))
    check("stable API order as the final tiebreak",
          [c["id"] for c in got[1:3]] == ["a", "e"], repr(got))

    stremio._session = _Sess([_R(200, {"subtitles": tape[0]["subtitles"]})])
    got = stremio.search_online_subtitles(
        "movie", "tt777", want_lang="eng", file_hint="Chinatown")
    check("movie URL shape + no S/E filter on the movie path",
          "/subtitles/movie/tt777.json" in stremio._session.calls[0][0]
          and len(got) >= 5, stremio._session.calls[0][0])

    stremio._session = _Sess([_R(504), _R(504)])
    check("all-fail search -> [] quietly (both retries burned)",
          stremio.search_online_subtitles("series", "tt9", 1, 1) == []
          and len(stremio._session.calls) == 2)

    stremio._session = _Sess([])
    check("empty imdb -> [] without touching the network",
          stremio.search_online_subtitles("series", "", 1, 1) == []
          and not stremio._session.calls)
finally:
    stremio._session = saved_sess

print("[3] download_online_subtitle — capped, retried, no .part litter")
p_dest = os.path.join(tmpdir, "dl.srt")
try:
    stremio._session = _Sess([_R(200, chunks=[b"1\n00:00:01,000 --> ",
                                             b"00:00:02,000\nhi\n\n"])])
    check("streamed write lands and parses",
          stremio.download_online_subtitle("http://x/1", p_dest)
          and prof_mod.parse_subtitle_cues(
              prof_mod.read_subtitle_text(p_dest)))
    check(".part cleaned up on success",
          not os.path.exists(p_dest + ".part"))

    stremio._session = _Sess([_R(500), _R(500)])
    check("non-200 twice -> False", not stremio.download_online_subtitle(
        "http://x/2", p_dest) and not os.path.exists(p_dest + ".part"))

    stremio._session = _Sess([_R(200, chunks=[b""])])
    check("empty body -> False",
          not stremio.download_online_subtitle("http://x/3", p_dest))

    huge = [b"\x00" * 65536] * 33          # 2.1 MB > the 2 MB cap
    stremio._session = _Sess([_R(200, chunks=huge), _R(200, chunks=huge)])
    check("over-cap payload -> False; the earlier GOOD file survives, "
          "no .part litter",
          not stremio.download_online_subtitle("http://x/4", p_dest)
          and os.path.exists(p_dest)
          and "hi" in (prof_mod.read_subtitle_text(p_dest) or "")
          and not os.path.exists(p_dest + ".part"))
finally:
    stremio._session = saved_sess
check("subtitle_cache_path sanitized + under TEMP/MichaelTVPlayer",
      stremio.subtitle_cache_path("tt123", 'we"ird/id').endswith(
          os.path.join("MichaelTVPlayer", "subs", "tt123-we_ird_id.srt"))
      and "MichaelTVPlayer" in stremio.subtitle_cache_path("a", "b"))

print("[4] resolve_vod_identity — lazy IPTV identity (patched catalog)")
saved_find = (stremio.find_series, stremio.find_movie)
try:
    stremio.find_series = lambda n: ("tt0944947", "Game of Thrones") \
        if "game" in n.lower() else None
    stremio.find_movie = lambda n: {"id": "tt0071315", "name": "Chinatown",
                                    "year": "1974", "poster": ""} \
        if "chinatown" in n.lower() else None
    ident = stremio.resolve_vod_identity({
        "kind": "series", "series_name": "Game of Thrones",
        "season": 3, "episode": 12})
    check("series -> imdb + S/E (same shape resolve_identity uses)",
          ident == {"stremio_imdb": "tt0944947",
                    "series_name": "Game of Thrones",
                    "season": 3, "episode": 12}, repr(ident))
    ident = stremio.resolve_vod_identity(
        {"kind": "vod", "title": "Chinatown (1974) 4K"})
    check("vod title -> movie identity",
          ident == {"movie": True, "stremio_imdb": "tt0071315"},
          repr(ident))
    check("no catalog hit -> None",
          stremio.resolve_vod_identity({"kind": "vod", "title": "???za"})
          is None)
    check("series without a name -> None",
          stremio.resolve_vod_identity({"kind": "series",
                                        "series_name": "", "season": 1,
                                        "episode": 1}) is None)
    check("unknown kind -> None",
          stremio.resolve_vod_identity({"kind": "live"}) is None)
finally:
    stremio.find_series, stremio.find_movie = saved_find


# ---- GUI plumbing ----

class FakeVLC:
    def __init__(self):
        self.now_ms = 0
        self.spu = -1
        self.set_spu_calls = []
        self.tracks = []

    def get_time(self):
        return self.now_ms

    def is_playing(self):
        return True

    def spu_tracks(self):
        return list(self.tracks)

    def active_spu(self):
        return self.spu

    def set_spu(self, tid):
        self.spu = int(tid)
        self.set_spu_calls.append(int(tid))

    def __getattr__(self, name):
        def _noop(*_a, **_k):
            return None
        return _noop


class FakeRelay:
    def __init__(self, tracks=None, selected=None, metas=None,
                 head=b"", tail=b"", total=0):
        self.parser_tracks = tracks or {}
        self.parser_selected = selected
        self.parser_tracks_meta = metas or {}
        self._head = head
        self._tail = tail
        self.total = total


stub = FakeVLC()
pv.vlc = stub
notes = []
_orig_note = pv._cap_note
pv._cap_note = lambda *a, **k: (notes.append(a[0] if a else ""),
                                _orig_note(*a, **k))[1]
saved_fetch = (stremio.search_online_subtitles,
               stremio.download_online_subtitle,
               stremio.resolve_vod_identity)
fetch_calls = []


def patch_fetch(search_ret, body=SRT_BODY, resolve=None, delay=0.0):
    def _search(kind, imdb, season=0, episode=0, want_lang="eng",
                file_hint="", video_hash=""):
        if delay:
            time.sleep(delay)
        fetch_calls.append(dict(kind=kind, imdb=imdb, season=season,
                                episode=episode, lang=want_lang,
                                hint=file_hint, vhash=video_hash))
        return search_ret

    def _dl(url, dest):
        d = os.path.dirname(dest)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(body)
        return True

    stremio.search_online_subtitles = _search
    stremio.download_online_subtitle = _dl
    stremio.resolve_vod_identity = resolve or (lambda p: None)


def restore_fetch():
    (stremio.search_online_subtitles, stremio.download_online_subtitle,
     stremio.resolve_vod_identity) = saved_fetch


def reset_state(current, relay):
    stub.set_spu_calls = []
    stub.spu = -1
    notes.clear()
    fetch_calls.clear()
    pv.current = current
    pv._vod_relay = relay
    pv._cap_want = True
    pv._cap_on = False
    pv._cap_fail = False
    pv._cap_store_ext = False
    pv._cap_vod_tries = 0
    pv._cap_cues.clear()
    pv._stremio_sub_path = ""
    pv._stremio_sub_cues = []
    pv._spu_want = 0
    pv._spu_name = "English (United States) - [English]"
    pv._sub_fetch_pending = False
    pv._sub_fetching = False
    pv._sub_fetch_fail = None
    pv._filter_engine.clear()
    pv._set_cap_on(True)


def wait_job(timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if not pv._sub_fetching:
            return True
        time.sleep(0.02)
    return False


# delivery spy: a SECOND connection on the same runner signal (the
# view's own slot runs first — connection order). The FIXED callback
# deliberately leaves flags untouched on a stale drop, so wait_job's
# flag polling cannot see those deliveries; matching the delivered
# result by content can.
recv = []
pv._subfetch_runner.finished.connect(recv.append)


def drain(seconds=0.4):
    """Deliver any still-queued results from earlier legs, then forget
    them. A worker's emit can queue just after the previous leg's last
    pump; a stray delivery into the next leg would otherwise confuse
    the waits below (observed live: [9]'s result once landed in [9b])."""
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.02)
    del recv[:]


def _dest_of(r):
    """The downloaded-file path inside a delivered result tuple (None
    for failure-shaped results)."""
    try:
        val = r[1]
        return val[2][0] if isinstance(val[2], tuple) else None
    except (IndexError, TypeError):
        return None


def wait_delivered(pred, timeout=10.0):
    """Pump until a fetch result matching pred has been DELIVERED to the
    GUI thread (worker done + queued signal dispatched)."""
    t0 = time.time()
    while not any(pred(r) for r in recv) and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.02)
    return any(pred(r) for r in recv)

GOT_CUR = {"kind": "stremio", "title": "GoT — S01E01",
           "url": "http://x/s", "fav_key": "stremio:t:1",
           "stremio_imdb": "tt0944947", "season": 1, "episode": 1,
           "file_name": "Game.of.Thrones.S01E01.720p.HDTV.x264-EbP.mkv"}
PGS_RELAY = FakeRelay(tracks={"1": "S_HDMV/PGS"},
                      metas={"1": {"lang": "eng", "name": "English",
                                   "codec": "S_HDMV/PGS"}},
                      head=b"\x07" * 65536, tail=b"\x09" * 65536,
                      total=10 ** 9)

print("[5] PGS-only stremio (identity known) — arm, fetch, ENGAGE")
try:
    patch_fetch([{"id": "111", "url": "http://x/1.srt", "lang": "eng",
                  "name": "GoT.S01E01.ENG.srt", "release": "GoT",
                  "hash_match": False}])
    reset_state(dict(GOT_CUR), PGS_RELAY)
    pv._cap_vod_check()
    check("dead-end armed, nothing latched",
          not pv._cap_fail and pv._sub_fetching)
    check("VLC's bitmap rendering stays on (set_spu re-picked)",
          stub.set_spu_calls and stub.set_spu_calls[-1] == 0)
    check("overlay down while searching (VLC owns rendering)",
          not pv._cap_on)
    check("searching pill shown",
          any("searching online" in n for n in notes), repr(notes))
    check("waited for the job", wait_job())
    check("job fired immediately (imdb already on cur, no pending)",
          len(fetch_calls) == 1)
    check("moviehash computed from the relay caches (16 hex)",
          len(fetch_calls[0]["vhash"]) == 16
          and int(fetch_calls[0]["vhash"], 16)
          == stremio.opensubs_hash(b"\x07" * 65536, b"\x09" * 65536,
                                   10 ** 9))
    check("file hint carries the playing file name",
          "EbP" in fetch_calls[0]["hint"])
    check("fetched file stored on cur (stremio -> sub_file)",
          pv.current.get("sub_file", "").endswith(
              stremio.subtitle_cache_path("tt0944947", "111")
              .split(os.sep)[-1]))
    check("overlay engaged (direct engage, no _cap_fail veto)",
          pv._cap_on and not pv._cap_fail)
    check("VLC's own spu forced off", -1 in stub.set_spu_calls)
    check("cues in the store (ownership flipped to the file)",
          pv._cap_store_ext and len(pv._cap_cues.cues) == 2)
    stub.now_ms = 11000
    pv._caption_tick()
    check("styled overlay PAINTS the fetched cues",
          pv._cap_wid._lines == ["line one", "line two"],
          repr(pv._cap_wid._lines))
    check("profanity engine fed from the same parse",
          len(pv._filter_engine.windows) >= 1)
    check("sticky pick renamed to the online track",
          pv._spu_want == -1 and pv._spu_name == "English (online)")
    check("found-online pill shown",
          any("found online" in n for n in notes), repr(notes))
finally:
    restore_fetch()

print("[6] late stremio identity — pending arms, kick on arrival")
try:
    patch_fetch([{"id": "222", "url": "http://x/2.srt", "lang": "eng",
                  "name": "m.eng.srt", "release": "", "hash_match": False}])
    cur = {"kind": "stremio", "title": "Stremio stream",
           "url": "http://x/s2", "fav_key": "stremio:t:2",
           "file_name": "Chinatown.1974.2160p.BluRay.mkv"}
    reset_state(cur, FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv._cap_vod_check()
    check("dead-end pended WITHOUT a job (no imdb yet)",
          pv._sub_fetch_pending and not pv._sub_fetching
          and not fetch_calls)
    check("still nothing latched while waiting",
          not pv._cap_fail and not pv._cap_on)
    ident = {"movie": True, "stremio_imdb": "tt0071315",
             "movie_name": "Chinatown", "year": "1974"}
    pv._on_stremio_identity(("ok", ("stremio:t:2", ident)))
    check("identity arrival kicked the job",
          not pv._sub_fetch_pending and pv._sub_fetching
          and len(fetch_calls) == 1)
    check("movie identified -> movie endpoint",
          fetch_calls[0]["kind"] == "movie")
    check("waited for the job", wait_job())
    check("engaged after the late kick",
          pv._cap_on and pv.current.get("sub_file"))
finally:
    restore_fetch()

print("[6b] identity FAILURE while pending — quiet classic handoff")
try:
    patch_fetch([])
    cur = {"kind": "stremio", "title": "Stremio stream",
           "url": "http://x/s3", "fav_key": "stremio:t:3"}
    reset_state(cur, FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv._cap_vod_check()
    check("pended on the dead-end", pv._sub_fetch_pending)
    pv._on_stremio_identity(("err", "boom"))
    check("pending resolved, NO job fired",
          not pv._sub_fetch_pending and not fetch_calls)
    check("classic body restored verbatim (latch + VLC renders + pill)",
          pv._cap_fail and not pv._cap_on and stub.set_spu_calls
          and any("VLC renders" in n for n in notes), repr(notes))
finally:
    restore_fetch()

print("[6c] STALE identity failure — the current media's pending survives")
try:
    patch_fetch([{"id": "223", "url": "http://x/23.srt", "lang": "eng",
                  "name": "b.srt", "release": "", "hash_match": False}])
    cur = {"kind": "stremio", "title": "Stremio B", "url": "http://x/s4",
           "fav_key": "stremio:t:4"}
    reset_state(cur, FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv._cap_vod_check()
    check("B pended on its dead-end", pv._sub_fetch_pending)
    # OLD media A's identity lookup fails late. resolve_identity coming
    # back empty-handed arrives as ("ok", (base, None)) — A's fav_key is
    # stamped in the tuple, so the staleness gate CAN and must see it
    pv._on_stremio_identity(("ok", ("stremio:t:99", None)))
    check("stale failure did NOT resolve B's pending",
          pv._sub_fetch_pending and not pv._cap_fail
          and not pv._sub_fetching and not fetch_calls)
    # B's OWN identity then arrives — the armed search must still fire
    pv._on_stremio_identity(
        ("ok", ("stremio:t:4", {"movie": True,
                                "stremio_imdb": "tt0071315"})))
    check("B's own identity kicked the search",
          not pv._sub_fetch_pending and pv._sub_fetching
          and len(fetch_calls) == 1)
    check("waited for B's job", wait_job())
    check("B engages after its own identity",
          pv._cap_on and pv.current.get("sub_file"))
finally:
    restore_fetch()

print("[7] vod + series — identity resolves INSIDE the job")
try:
    got_series = {"stremio_imdb": "tt0944947",
                  "series_name": "Game of Thrones", "season": 4,
                  "episode": 6}
    patch_fetch([{"id": "333", "url": "http://x/3.srt", "lang": "eng",
                  "name": "s.eng.srt", "release": "", "hash_match": False}],
                resolve=lambda p: got_series if p.get("kind") == "series"
                else {"movie": True, "stremio_imdb": "tt0071315"})
    reset_state({"kind": "series", "title": "GoT S04E06",
                 "url": "http://x/ep", "fav_key": "episode:1",
                 "series_name": "Game of Thrones", "season": 4,
                 "episode": 6}, FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv._cap_vod_check()
    check("series dead-end kicked the job directly",
          pv._sub_fetching and not pv._sub_fetch_pending)
    check("waited", wait_job())
    check("series search carries imdb + S/E from the resolved identity",
          fetch_calls[0]["kind"] == "series"
          and fetch_calls[0]["imdb"] == "tt0944947"
          and (fetch_calls[0]["season"], fetch_calls[0]["episode"])
          == (4, 6), repr(fetch_calls[:1]))
    check("fetched file stored kind-agnostically (_fetched_sub)",
          pv.current.get("_fetched_sub") and not pv.current.get("sub_file"))
    check("engaged on series too", pv._cap_on and pv._cap_store_ext)

    fetch_calls.clear()
    notes.clear()
    reset_state({"kind": "vod", "title": "Chinatown (1974) 4K",
                 "url": "http://x/m", "fav_key": "vod:9"},
                FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv._cap_vod_check()
    check("waited (vod)", wait_job())
    check("vod identity resolved in-job -> movie endpoint",
          fetch_calls and fetch_calls[0]["kind"] == "movie"
          and fetch_calls[0]["imdb"] == "tt0071315")
    check("vod engages the fetched file",
          pv._cap_on and pv.current.get("_fetched_sub"))
finally:
    restore_fetch()

print("[8] fetch failure — today's exact classic bodies restored")
try:
    patch_fetch([])          # search finds nothing
    reset_state(dict(GOT_CUR), FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv._cap_vod_check()
    check("waited for the failed job", wait_job())
    check("classic handoff body ran (latch + VLC renders + pill)",
          pv._cap_fail and not pv._cap_on
          and any("VLC renders" in n for n in notes), repr(notes))

    # sel-None flavor: text tracks exist, none match the language
    notes.clear()
    reset_state(dict(GOT_CUR),
                FakeRelay(tracks={"1": "S_TEXT/UTF8"}, selected=None,
                          metas={"1": {"lang": "rus", "name": "Russian",
                                       "codec": "S_TEXT/UTF8"}}))
    pv._cap_vod_check()
    check("sel-none dead-end armed (sticky pick cleared meanwhile)",
          pv._sub_fetching and pv._spu_want == -1
          and pv._spu_name == "")
    check("waited (sel-none)", wait_job())
    check("sel-none classic body restored verbatim",
          pv._cap_fail and not pv._cap_on
          and any("no English text track" in n for n in notes),
          repr(notes))

    # manual row failure: nothing latched, just a note
    notes.clear()
    stub.set_spu_calls = []
    reset_state(dict(GOT_CUR), FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv._cap_fail = False
    pv._manual_fetch_online_sub()
    check("manual search kicked", pv._sub_fetching and wait_job())
    check("manual failure latches NOTHING, notes only",
          not pv._cap_fail
          and any("none found online" in n for n in notes),
          repr(notes))
finally:
    restore_fetch()

print("[9] stale media drop — the generation guard")
try:
    patch_fetch([{"id": "444", "url": "http://x/4.srt", "lang": "eng",
                  "name": "x.srt", "release": "", "hash_match": False}])
    reset_state(dict(GOT_CUR), FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    drain()
    pv._cap_vod_check()
    check("slow job in flight", pv._sub_fetching)
    # the user moved on mid-search: play_media's exact reset (the :1735
    # session bump + the :1792-94 field clears). The probe used to bump
    # alone and rely on the OLD callback clearing _sub_fetching on a
    # stale drop — the fixed callback must leave flags untouched
    pv._session += 1                      # the user moved on mid-search
    pv._sub_fetch_pending = False
    pv._sub_fetching = False
    pv._sub_fetch_fail = None
    check("stale result DELIVERED to the GUI thread",
          wait_delivered(
              lambda r: (_dest_of(r) or "").endswith("444.srt")))
    check("stale result dropped (no store, no engage, no cur writes)",
          not pv._cap_on and not pv._cap_fail
          and not pv.current.get("sub_file")
          and len(pv._cap_cues.cues) == 0)
    pv._session -= 1                      # leave the counter sane
finally:
    restore_fetch()

print("[9b] two-kick race — media A's late result cannot pose as B's")
try:
    gate_a = threading.Event()
    gate_b = threading.Event()

    def _race_search(kind, imdb, season=0, episode=0, want_lang="eng",
                     file_hint="", video_hash=""):
        fetch_calls.append(dict(kind=kind, imdb=imdb, season=season,
                                episode=episode, lang=want_lang,
                                hint=file_hint, vhash=video_hash))
        (gate_a if imdb == "ttA" else gate_b).wait(10.0)
        return [{"id": "a1" if imdb == "ttA" else "b1",
                 "url": "http://x/%s.srt" % imdb, "lang": "eng",
                 "name": "%s.srt" % imdb, "release": "",
                 "hash_match": False}]

    def _race_dl(url, dest):
        d = os.path.dirname(dest)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write("1\n00:00:10,000 --> 00:00:12,000\n%s line\n\n"
                    % ("media A" if "ttA" in url else "media B"))
        return True

    saved_race = (stremio.search_online_subtitles,
                  stremio.download_online_subtitle,
                  stremio.resolve_vod_identity)
    stremio.search_online_subtitles = _race_search
    stremio.download_online_subtitle = _race_dl
    stremio.resolve_vod_identity = lambda p: None

    # media A kicks a GATED job (its worker parks inside the search)
    reset_state({"kind": "stremio", "title": "A", "url": "http://x/a",
                 "fav_key": "stremio:t:10", "stremio_imdb": "ttA",
                 "season": 1, "episode": 1},
                PGS_RELAY)
    drain()
    pv._cap_vod_check()
    check("A's job kicked (in flight)", pv._sub_fetching)
    t0 = time.time()                 # no processEvents: nothing to deliver
    while len(fetch_calls) < 1 and time.time() - t0 < 5.0:
        time.sleep(0.02)
    check("A's worker parked in the gated search",
          fetch_calls and fetch_calls[0]["imdb"] == "ttA")
    check("A's dead-end armed its fail hook",
          pv._sub_fetch_fail is not None)
    # the user switches to media B: play_media's bump, then B's own kick
    # (the old shared _sub_fetch_gen stamp was overwritten HERE — A's
    # late result then sailed through the guard and engaged on B)
    pv._session += 1
    reset_state({"kind": "stremio", "title": "B", "url": "http://x/b",
                 "fav_key": "stremio:t:11", "stremio_imdb": "ttB",
                 "season": 2, "episode": 2},
                PGS_RELAY)
    pv._cap_vod_check()
    t0 = time.time()
    while not fetch_calls and time.time() - t0 < 5.0:
        time.sleep(0.02)
    check("B's own job in flight too (both workers parked)",
          pv._sub_fetching and len(fetch_calls) == 1
          and fetch_calls[0]["imdb"] == "ttB",
          repr(fetch_calls))
    # release A first: its late result arrives while B's job still runs
    gate_a.set()
    check("A's late result delivered",
          wait_delivered(
              lambda r: (_dest_of(r) or "").endswith("ttA-a1.srt")))
    check("A did NOT engage on B (no sub_file/_fetched_sub, no cues, "
          "overlay down)",
          not pv.current.get("sub_file")
          and not pv.current.get("_fetched_sub")
          and len(pv._cap_cues.cues) == 0
          and not pv._cap_on and not pv._cap_store_ext)
    check("A did NOT clear B's armed fail hook",
          pv._sub_fetch_fail is not None)
    check("A did NOT latch B's classic dead-end", not pv._cap_fail)
    check("A did NOT clobber B's _sub_fetching flag mid-flight",
          pv._sub_fetching)
    # release B: the CURRENT job engages fine
    gate_b.set()
    check("B's result delivered",
          wait_delivered(
              lambda r: (_dest_of(r) or "").endswith("ttB-b1.srt")))
    check("B engages its own file",
          pv._cap_on and pv._cap_store_ext
          and pv.current.get("sub_file", "").endswith(
              stremio.subtitle_cache_path("ttB", "b1").split(os.sep)[-1])
          and pv._sub_fetch_fail is None)
    stub.now_ms = 11000
    pv._caption_tick()
    check("overlay paints B's cues (A's file never parsed)",
          pv._cap_wid._lines == ["media B line"],
          repr(pv._cap_wid._lines))
finally:
    (stremio.search_online_subtitles, stremio.download_online_subtitle,
     stremio.resolve_vod_identity) = saved_race

print("[9c] captions turned OFF mid-search — no resurrect on delivery")
try:
    patch_fetch([{"id": "445", "url": "http://x/45.srt", "lang": "eng",
                  "name": "y.srt", "release": "", "hash_match": False}])
    reset_state(dict(GOT_CUR), FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    drain()
    pv._cap_vod_check()
    check("search in flight", pv._sub_fetching)
    pv._cap_want = False                # the user hit CC Off mid-search
    check("waited for delivery", wait_job())
    check("captions stay OFF (no forced re-enable)",
          not pv._cap_on and not pv._cap_store_ext)
    check("no engage side effects (no cur writes, no cues)",
          not pv.current.get("sub_file")
          and not pv.current.get("_fetched_sub")
          and len(pv._cap_cues.cues) == 0)
    check("no fail-restore either (the user wants captions off)",
          not pv._cap_fail)
    check("pending/fetching state dropped",
          not pv._sub_fetch_pending and not pv._sub_fetching)
finally:
    restore_fetch()

print("[10] re-fetch clears the store — no double-painted files")
try:
    patch_fetch([{"id": "555", "url": "http://x/5.srt", "lang": "eng",
                  "name": "y.srt", "release": "", "hash_match": False}],
                body="1\n00:00:10,000 --> 00:00:12,000\nonly line\n\n")
    reset_state(dict(GOT_CUR), FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv.current["sub_file"] = write("prev.srt", SRT_BODY)
    pv._engage_stremio_external()         # previous file owns the store
    check("setup: previous file engaged (2 cues)",
          pv._cap_store_ext and len(pv._cap_cues.cues) == 2)
    pv._cap_on = True
    pv._manual_fetch_online_sub()
    check("waited for the re-fetch", wait_job())
    check("store holds ONLY the new file's cues (cleared, not appended)",
          len(pv._cap_cues.cues) == 1, len(pv._cap_cues.cues))
    stub.now_ms = 11000
    pv._caption_tick()
    check("overlay paints the new cue alone",
          pv._cap_wid._lines == ["only line"], repr(pv._cap_wid._lines))
finally:
    restore_fetch()

print("[11] every dead-end funnel arm — metadata + language mismatch")
try:
    patch_fetch([{"id": "666", "url": "http://x/6.srt", "lang": "eng",
                  "name": "z.srt", "release": "", "hash_match": False}])
    # (c) no track metadata at all (tries exhausted)
    reset_state(dict(GOT_CUR), FakeRelay())     # parser_tracks == {}
    pv._cap_vod_check()
    check("no-track-metadata dead-end armed", pv._sub_fetching
          and not pv._cap_fail)
    check("waited", wait_job())

    # (b) language mismatch: picked English, only text track is Russian
    reset_state(dict(GOT_CUR),
                FakeRelay(tracks={1: "S_TEXT/UTF8"}, selected=1,
                          metas={1: {"lang": "rus", "name": "",
                                     "codec": "S_TEXT/UTF8"}}))
    pv._cap_vod_check()
    check("language-mismatch dead-end armed", pv._sub_fetching
          and not pv._cap_fail)
    check("waited (mismatch)", wait_job())
    check("both funnels engaged the fetched file",
          pv._cap_on and pv._cap_store_ext)
finally:
    restore_fetch()

print("[12] manual row — presence rules + dispatch + config gate")
try:
    captured = []
    orig_open = pv._open_ctl_panel
    pv._open_ctl_panel = lambda *a, **k: captured.append(a[2])
    try:
        for cur in (dict(GOT_CUR),
                    {"kind": "vod", "title": "M", "fav_key": "v"},
                    {"kind": "series", "title": "S", "fav_key": "s"},
                    {"kind": "live", "title": "L", "fav_key": "l"}):
            reset_state(cur, None)
            pv._subs_panel()
        row_kinds = [[r.get("id") for r in rows] for rows in captured]
        check("row offered on stremio/vod/series",
              all("fetch" in ids for ids in row_kinds[:3]))
        check("row NEVER offered on live TV",
              "fetch" not in row_kinds[3], repr(row_kinds[3]))
    finally:
        pv._open_ctl_panel = orig_open

    patch_fetch([{"id": "777", "url": "http://x/7.srt", "lang": "eng",
                  "name": "m.srt", "release": "", "hash_match": False}])
    reset_state(dict(GOT_CUR), FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv._on_spu_row_picked({"id": "fetch"})
    check("picker dispatch runs the manual fetch",
          pv._sub_fetching and any("searching online" in n
                                   for n in notes))
    check("waited (manual dispatch)", wait_job())

    # the config gate: auto-arm refuses when fetch_online_subs is off
    notes.clear()
    pv.config.data["fetch_online_subs"] = False
    reset_state(dict(GOT_CUR), FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv._cap_vod_check()
    check("config off -> classic handoff verbatim (no arm)",
          pv._cap_fail and not pv._sub_fetching and not pv._sub_fetch_pending
          and any("VLC renders" in n for n in notes))
    pv.config.data["fetch_online_subs"] = True
    # ...but the manual row still acts (explicit user intent)
    notes.clear()
    pv._cap_fail = False
    pv._manual_fetch_online_sub()
    check("manual fetch ignores the config gate",
          pv._sub_fetching and wait_job())
finally:
    pv.config.data["fetch_online_subs"] = True
    restore_fetch()

print("[13] generalized cues reader — handoff byte-identical, vod rides")
pv._stremio_sub_path = ""
pv._stremio_sub_cues = []
p_hand = write("handoff.srt", SRT_BODY)
pv.current = {"kind": "stremio", "title": "S", "url": "u",
              "fav_key": "stremio:t:5", "sub_file": p_hand}
check("stremio handoff file still parses",
      len(pv._stremio_handoff_cues()) == 2)
pv._stremio_sub_path = ""
pv.current = {"kind": "vod", "title": "M", "url": "u", "fav_key": "v",
              "_fetched_sub": p_hand}
check("vod _fetched_sub renders through the same reader",
      len(pv._stremio_handoff_cues()) == 2)
pv.current = {"kind": "live", "title": "L", "url": "u", "fav_key": "l",
              "sub_file": p_hand}
check("live TV never reads external files", pv._stremio_handoff_cues()
      == [])
pv.current = {"kind": "vod", "title": "M2", "url": "u", "fav_key": "v2"}
check("no file anywhere -> []", pv._stremio_handoff_cues() == [])
check("config default on + property round-trip",
      DEFAULTS.get("fetch_online_subs") is True and cfg.fetch_online_subs)

print("[14] engage-fetched shortcut — no network on a re-run dead-end")
try:
    patch_fetch([{"id": "888", "url": "http://x/8.srt", "lang": "eng",
                  "name": "fresh.srt", "release": "", "hash_match": False}],
                resolve=lambda p: {"movie": True, "stremio_imdb": "tt1"})
    p_keep = write("keep.srt", SRT_BODY)
    reset_state({"kind": "vod", "title": "M", "url": "u", "fav_key": "v3",
                 "_fetched_sub": p_keep, "_fetched_lang": "English"},
                FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv._cap_vod_check()
    check("re-run dead-end re-engaged the EXISTING file (0 searches)",
          pv._cap_on and not fetch_calls
          and pv._spu_name == "English (online)")
    stub.now_ms = 11000
    pv._caption_tick()
    check("and it paints", pv._cap_wid._lines == ["line one", "line two"])
    reset_state({"kind": "vod", "title": "M", "url": "u", "fav_key": "v4",
                 "_fetched_sub": os.path.join(tmpdir, "gone.srt")},
                FakeRelay(tracks={"1": "S_HDMV/PGS"}))
    pv._cap_vod_check()
    check("missing temp file falls through to a fresh search",
          len(fetch_calls) == 1 and wait_job())
finally:
    restore_fetch()

print("[15] LIVE — keyless addon, bounded (skip: MTP_SUBFETCH_SKIP_LIVE=1)")
if os.environ.get("MTP_SUBFETCH_SKIP_LIVE"):
    print("  skipped (MTP_SUBFETCH_SKIP_LIVE)")
else:
    try:
        cands = stremio.search_online_subtitles(
            "series", "tt0944947", 1, 1, want_lang="eng",
            file_hint="Game.of.Thrones.S01E01.720p.HDTV.DD5.1.x264-EbP")
        check("live: GoT 1:1 >=1 eng candidate with a url",
              bool(cands) and all(c["url"] for c in cands))
        dest = os.path.join(tmpdir, "live.srt")
        ok = stremio.download_online_subtitle(cands[0]["url"], dest)
        cues = prof_mod.parse_subtitle_cues(
            prof_mod.read_subtitle_text(dest)) if ok else []
        check("live: one download parses to real cues",
              ok and len(cues) > 50, len(cues))
        cands2 = stremio.search_online_subtitles(
            "series", "tt0944947", 1, 1, want_lang="eng",
            video_hash="0000000000000000")
        check("live: bogus videoHash tolerated (ranking survives "
              "the ignored param)", bool(cands2))
        mcands = stremio.search_online_subtitles(
            "movie", "tt0071315", want_lang="eng",
            file_hint="Chinatown.1974")
        check("live: movie manifest (Chinatown) >=1 eng",
              bool(mcands))
    except Exception as exc:                  # noqa: BLE001
        check("live legs", False, repr(exc))

print()
if fails[0]:
    print("FAILURES: %d" % fails[0])
    sys.exit(1)
print("ALL PASS")
