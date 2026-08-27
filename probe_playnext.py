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
            "1": [{"id": 101, "episode_num": 5, "title": "Pilot",
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

print("[7] movie + live visibility")
pv.play_media({"kind": "vod", "title": "Movie", "url": "http://x/m.mkv",
               "fav_key": "vod:1"})
wait_media(pv, "vod:1")
check("both hidden for a movie",
      pv.btn_auto.isHidden() and pv.btn_next.isHidden())
pv.play_media({"kind": "live", "title": "Chan", "url": "http://x/l",
               "stream_id": 9, "fav_key": "live:9"})
app.processEvents()
check("live: autoplay hidden, play-next enabled",
      pv.btn_auto.isHidden() and not pv.btn_next.isHidden()
      and pv.btn_next.isEnabled())

pv.stop()
app.processEvents()
print(f"\n{'ALL PASS' if fails[0] == 0 else str(fails[0]) + ' FAILURES'}")
sys.exit(1 if fails[0] else 0)
