"""Headless probe for the Play-next / Autoplay-next feature."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import DEFAULTS, Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

from PyQt5 import QtWidgets  # noqa: E402

app = QtWidgets.QApplication(sys.argv)
cfg = Config(dict(DEFAULTS), None)
pv = PlayerView(cfg)
pv.resize(1280, 720)
fails = [0]


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails[0] += 1


def wait_media(pv, fav, timeout=15.0):
    import time as _time
    t0 = _time.time()
    while _time.time() - t0 < timeout:
        app.processEvents()
        if (pv.current or {}).get("fav_key") == fav:
            app.processEvents()
            return True
        _time.sleep(0.05)
    return False


print("[1] pre-stream state")
check("autoplay btn hidden pre-stream", not pv.btn_auto.isVisible()
      or pv.btn_auto.isHidden())
check("play-next btn hidden pre-stream", pv.btn_next.isHidden())
check("play-prev btn hidden pre-stream", pv.btn_prev.isHidden())
check("autoplay default ON + persisted",
      pv.btn_auto.isChecked() and cfg.autoplay_next)

print("[2] series playback")
series = {"kind": "series", "title": "Show - S1E5 Pilot",
          "url": "http://x/1.mp4", "fav_key": "episode:101",
          "series_id": 7, "series_name": "Show", "season": 1, "episode": 5}
pv.play_media(series)
check("series media actually started", wait_media(pv, "episode:101"))
check("autoplay visible for series", not pv.btn_auto.isHidden())
check("play-next visible + enabled for series",
      not pv.btn_next.isHidden() and pv.btn_next.isEnabled())

print("[3] toggle")
pv.btn_auto.toggle()
check("toggling persists OFF", not cfg.autoplay_next)
pv.btn_auto.toggle()
check("toggling persists ON", cfg.autoplay_next)

print("[4] next-episode logic (fake client)")


class FakeClient:
    def series_info(self, sid):
        assert sid == 7
        return {"episodes": {
            "1": [{"id": 100, "episode_num": 4, "title": "Before",
                   "container_extension": "mp4", "info": {}},
                  {"id": 101, "episode_num": 5, "title": "Pilot",
                   "container_extension": "mp4", "info": {}},
                  {"id": 102, "episode_num": 6, "title": "Finale",
                   "container_extension": "mp4", "info": {}}],
            "2": [{"id": 201, "episode_num": 1, "title": "New Season",
                   "container_extension": "mp4", "info": {}}],
        }}

    def series_url(self, eid, ext="mp4"):
        return f"http://x/{eid}.{ext}"

    def short_epg(self, sid, limit=4):
        return []


pv.client = FakeClient()
nxt = pv._fetch_series_next(series)
check("S1E5 -> S1E6", nxt and nxt["season"] == 1 and nxt["episode"] == 6)
check("playable carries url/title", nxt["url"] == "http://x/102.mp4"
      and "S1E6" in nxt["title"])
nxt2 = pv._fetch_series_next(nxt)
check("season finale -> next season E1",
      nxt2 and nxt2["season"] == 2 and nxt2["episode"] == 1)
nxt3 = pv._fetch_series_next(nxt2)
check("series end -> None", nxt3 is None)

prv = pv._fetch_series_prev(series)
check("S1E5 -> prev S1E4", prv and prv["season"] == 1
      and prv["episode"] == 4)
check("prev playable url/title", prv["url"] == "http://x/100.mp4"
      and "S1E4" in prv["title"])
first = pv._fetch_series_prev({"kind": "series", "series_id": 7,
                               "season": 1, "episode": 4,
                               "series_name": "Show"})
check("S1E4 -> no earlier episode", first is None)
roll = pv._fetch_series_prev({"kind": "series", "series_id": 7,
                              "season": 2, "episode": 1,
                              "series_name": "Show"})
check("S2E1 rolls back to the S1 finale",
      roll and roll["season"] == 1 and roll["episode"] == 6)
legacy = pv._ordered_series_episodes({
    "seasons": [{"season_number": 2, "episode": [
        {"id": 201, "episode_num": 1}]},
        {"season_number": 1, "episode": [
            {"id": 101, "episode_num": 5},
            {"id": 100, "episode_num": 4}]}]})
check("legacy seasons shape ordered", [(s, n) for s, n, _ in legacy]
      == [(1, 4), (1, 5), (2, 1)])

print("[5] catch-up next program (fake client)")


class Epg:
    def __init__(self, t, s, title):
        self.title, self.start_timestamp, self.stop_timestamp = title, t, s


import time as _t  # noqa: E402

now = int(_t.time())


class FakeCuClient(FakeClient):
    def epg_table(self, sid):
        return [Epg(now - 7200, now - 6600, "Old"),
                Epg(now - 3600, now - 3000, "Current"),
                Epg(now - 3000, now - 2400, "Next"),
                Epg(now + 600, now + 3600, "Future")]

    def timeshift_url(self, sid, st, dur):
        return f"http://x/ts/{sid}/{st}/{dur}"


pv.client = FakeCuClient()
cu = {"kind": "catchup", "title": "Ch — Current", "fav_key": "cu:5:1",
      "stream_id": 5, "utc_start": now - 3600, "utc_end": now - 3000,
      "channel": "Ch"}
cn = pv._fetch_catchup_next(cu)
check("catch-up picks the program after the current one",
      cn and cn["utc_start"] == now - 3000 and cn["kind"] == "catchup")
cp = pv._fetch_catchup_prev(cu)
check("catch-up picks the program before the current one",
      cp and cp["utc_start"] == now - 7200 and cp["kind"] == "catchup")
cp0 = pv._fetch_catchup_prev(dict(cu, utc_start=now - 7200))
check("catch-up earliest -> no earlier program", cp0 is None)

print("[6] EOF autoplay")
pv.play_media(series)
wait_media(pv, "episode:101")
pv._eof_next_done = False
pv._maybe_autoplay_next(playing=False, length_ms=100000, raw_ms=99800)
check("EOF fires the next lookup (runner thread)",
      True)  # result lands async; runner is daemon — give it a moment
for _ in range(100):
    app.processEvents()
    _t.sleep(0.05)
    if (pv.current or {}).get("fav_key") == "episode:102":
        break
check("autoplay switched to S1E6",
      pv.current and pv.current.get("episode") == 6)
pv._maybe_autoplay_next(playing=False, length_ms=100000, raw_ms=100000)
check("second EOF for same media does not refire", pv._eof_next_done)

print("[7] movie / live / stremio visibility")
pv.play_media({"kind": "vod", "title": "Movie", "url": "http://x/m.mkv",
               "fav_key": "vod:1"})
wait_media(pv, "vod:1")
check("all three hidden for a movie",
      pv.btn_auto.isHidden() and pv.btn_next.isHidden()
      and pv.btn_prev.isHidden())
pv.play_media({"kind": "live", "title": "Chan", "url": "http://x/l",
               "stream_id": 9, "fav_key": "live:9"})
app.processEvents()
check("live: autoplay hidden, prev+next visible and enabled",
      pv.btn_auto.isHidden() and not pv.btn_next.isHidden()
      and pv.btn_next.isEnabled() and not pv.btn_prev.isHidden()
      and pv.btn_prev.isEnabled())

# stremio handoff with NO identity yet (a movie can never grow one — the
# episode buttons must stay hidden until a season/episode is known)
import src.stremio as stremio_mod  # noqa: E402
stremio_mod.next_playable = lambda cfg, cur: None   # keep lookahead offline
pv.play_media({"kind": "stremio", "title": "Stremio stream",
               "url": "http://x/s", "fav_key": "stremio:zz:0"})
wait_media(pv, "stremio:zz:0")
check("stremio without identity: prev/next/auto hidden",
      pv.btn_prev.isHidden() and pv.btn_next.isHidden()
      and pv.btn_auto.isHidden())
stremio_ep = {"kind": "stremio", "title": "Show — S01E02 Ep Name",
              "url": "http://x/s2", "fav_key": "stremio:tt1:1:2",
              "stremio_imdb": "tt1", "season": 1, "episode": 2}
pv.play_media(dict(stremio_ep))
wait_media(pv, "stremio:tt1:1:2")
check("stremio episode: prev visible + enabled, next/auto visible",
      not pv.btn_prev.isHidden() and pv.btn_prev.isEnabled()
      and not pv.btn_next.isHidden() and not pv.btn_auto.isHidden())

stremio_mod.prev_playable = lambda cfg, cur: {
    "kind": "stremio", "title": "Show — S01E01", "url": "http://x/e1",
    "fav_key": "stremio:tt1:1:1"}
sp = pv._fetch_stremio_prev(dict(stremio_ep))
check("stremio prev fetch returns the playable",
      sp and sp["url"] == "http://x/e1")

print("[8] jump-to-begin / jump-to-live indicator rebase (stub VLC)")


class FakeVLC:
    """Just the surface _jump_begin/_jump_live/_seek_ms/play_media touch."""

    def __init__(self):
        self.state = "playing"
        self.now_ms = 300000
        self.set_times = []
        self.plays = []
        self.jump_live_calls = 0

    def get_length(self):
        return 600000

    def get_time(self):
        return self.now_ms

    def state_name(self):
        return self.state

    def is_playing(self):
        return self.state == "playing"

    def set_time(self, ms):
        self.set_times.append(ms)
        self.now_ms = ms

    def seek_ms(self, delta_ms):
        target = max(0, min(600000, self.now_ms + delta_ms))
        self.set_time(target)
        return target

    def jump_to_live(self):
        self.jump_live_calls += 1
        self.set_time(600000)

    def play(self, url, timeshift=False, start_seconds=0.0,
             start_wait_s=20.0, sub_file=None):
        self.plays.append((url, start_seconds))
        self.now_ms = int(start_seconds * 1000)
        self.state = "playing"

    def is_mute(self):
        return False

    def active_spu(self):
        return -1

    def spu_tracks(self):
        return []

    def audio_tracks(self):
        return []

    def __getattr__(self, name):
        def _noop(*_a, **_k):
            return None
        return _noop


real_vlc = pv.vlc
stub = FakeVLC()
pv.vlc = stub
pv._mode = "live"
pv.dvr = None
pv.current = {"kind": "series", "title": "Show - S1E6", "url": "http://x/1.mp4",
              "fav_key": "episode:102", "series_id": 7, "season": 1,
              "episode": 6}
pv._vid_s = 300.0
pv._last_raw = 300.0
pv._last_vod_len_ms = 600000
pv._jump_begin()
check("begin: set_time(0) issued",
      bool(stub.set_times) and stub.set_times[-1] == 0)
check("begin: tracked position rebased to 0 (indicator follows)",
      pv._vid_s == 0.0 and pv._last_raw is None)

pv._vid_s = 300.0
pv._last_raw = 300.0
stub.now_ms = 300000
pv._seek_ms(-60000)
check("seek -60s rebases the tracker to 240s", pv._vid_s == 240.0)

pv._jump_live()
check("live: jumped to the end of the file", stub.jump_live_calls == 1
      and stub.set_times[-1] == 600000)
check("live: tracked position at the end", pv._vid_s == 600.0)

# the v1.5.2 regression: LIVE on an ENDED stremio stream reconnected and
# RESTARTED the episode — it must hold at the end instead
pv.current = {"kind": "stremio", "title": "Show — S01E02", "url": "http://x/s",
              "fav_key": "stremio:tt1:1:2", "stremio_imdb": "tt1",
              "season": 1, "episode": 2}
stub.state = "ended"
stub.plays.clear()
pv._jump_live()
check("stremio ended: LIVE holds at the end (no restart)",
      not stub.plays and stub.jump_live_calls == 2)

# BEGIN at EOF replays the item from the top (set_time is a no-op once
# VLC ended)
pv._jump_begin()
check("stremio ended: BEGIN replays from the top",
      bool(stub.plays) and stub.plays[-1][1] == 0.0)
pv.vlc = real_vlc

print("[9] stremio identity-failure regression (the Silo crash)")
pv.current = {"kind": "stremio", "title": "Stremio stream",
              "url": "http://x/s", "fav_key": "stremio:zz:0"}
try:
    pv._on_stremio_identity(("ok", ("stremio:zz:0", None)))
    check("identity None: no crash (message path)", True)
except Exception as exc:
    check("identity None: no crash (message path)", False, repr(exc))
try:
    pv._on_stremio_identity(("ok", None))
    check("result None: no crash", True)
except Exception as exc:
    check("result None: no crash", False, repr(exc))
check("failed identity left the playable untouched",
      pv.current.get("title") == "Stremio stream")
ident = {"stremio_imdb": "tt1", "series_name": "Silo", "season": 3,
         "episode": 7, "episode_name": "The Book of Quinn"}
pv._on_stremio_identity(("ok", ("stremio:zz:0", dict(ident))))
check("identity success still lands (title + episode buttons)",
      pv.current.get("season") == 3
      and "S03E07" in pv.current.get("title", "")
      and not pv.btn_prev.isHidden() and not pv.btn_next.isHidden())

pv.stop()
app.processEvents()
print(f"\n{'ALL PASS' if fails[0] == 0 else str(fails[0]) + ' FAILURES'}")
sys.exit(1 if fails[0] else 0)
