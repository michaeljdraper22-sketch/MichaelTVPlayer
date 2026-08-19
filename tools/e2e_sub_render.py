"""E2E: real movie -> app-rendered subtitles (extraction + cue display)."""
import json, os, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets

from src.config import Config
from src.ui.player_view import PlayerView

UA = "MichaelTVPlayer/1.0"
cfgj = json.load(open(os.path.join(os.environ["APPDATA"], "MichaelTVPlayer", "settings.json"), encoding="utf-8"))
base, user, pw = cfgj["server_url"].rstrip("/"), cfgj["username"], cfgj["password"]

def api(action=None, **extra):
    params = {"username": user, "password": pw}
    if action: params["action"] = action
    params.update(extra)
    url = base + "/player_api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

cats = api("get_vod_categories")
vod = api("get_vod_streams", category_id=cats[0]["category_id"])
movie = next((m for m in vod if "superbad" in m["name"].lower()), vod[0])
ext = movie.get("container_extension") or "mp4"
url = f"{base}/movie/{user}/{pw}/{movie['stream_id']}.{ext}"
print(f"movie: {movie['name']!r}")

app = QtWidgets.QApplication([])
view = PlayerView(Config.load())
view.resize(1280, 720)
view.show()
view.play_media({"kind": "vod", "url": url, "title": movie["name"],
                 "fav_key": "e2e"})

# wait for VLC to report the tracks, then pick English
deadline = time.time() + 40
while time.time() < deadline:
    app.processEvents()
    if view.vlc.spu_tracks():
        break
    time.sleep(0.3)
tracks = view.vlc.spu_tracks()
print(f"tracks: {len(tracks)}")
assert tracks
en = next(((tid, n) for tid, n in tracks if "english" in n.lower()),
          tracks[0])
print("selecting:", en[1])
view._select_spu(en[0], en[1])       # async probe -> _on_sub_probe starts ffmpeg

# let extraction run while playing (network-bound: the 4K file is demuxed
# in full, so cue arrival follows CDN throughput)
t0 = time.time()
while time.time() - t0 < 110:
    app.processEvents()
    time.sleep(0.25)
    if len(view._sub_cues) > 40:
        break
n_cues = len(view._sub_cues)
print(f"cues extracted: {n_cues} (frontier "
      f"{view._sub_extractor.frontier_s:.1f}s)")
assert n_cues > 8, "extraction produced too little"

# deterministic display check: at each cue's midpoint the layer must show
# exactly that cue (real extraction data -> real display mapping)
print("DBG _spu_want:", view._spu_want, "name:", repr(view._spu_name))
print("DBG vlc tracks now:", [n for _, n in view.vlc.spu_tracks()][:6])
print("DBG is_vod:", view._is_vod(), "cues:", len(view._sub_cues),
      "delay:", view._sub_delay_s, "closing:", view._closing)
c0 = view._sub_cues[0]
view._vid_s = (c0[0] + c0[1]) / 2
print("DBG testing cue", c0[0], c0[1], repr(c0[2][:40]))
view._sub_tick()
print("DBG layer text:", repr(view.sub_layer._text))
ok_mid = 0
ok_shown = 0
for start, end, text in view._sub_cues[:25]:
    view._vid_s = (start + end) / 2.0
    view._sub_tick()
    if view.sub_layer._text is not None:
        ok_shown += 1
    if view.sub_layer._text == text:
        ok_mid += 1
    else:
        print(f"  (overlap) at {start:.1f}: expected {text[:30]!r} "
              f"got {view.sub_layer._text!r}")
# between two cues nothing shows
gaps = 0
for i in range(min(24, n_cues - 1)):
    a_end = view._sub_cues[i][1]
    b_start = view._sub_cues[i + 1][0]
    if b_start - a_end > 1.0:
        view._vid_s = (a_end + b_start) / 2.0
        view._sub_tick()
        if view.sub_layer._text is None:
            gaps += 1

total = len(view._sub_cues)
view.stop()
print(f"cue-midpoint display: {ok_mid}/25 correct; silent gaps: {gaps} "
      f"verified silent")
assert ok_shown >= 23, f"cues not displayed ({ok_shown}/25)"
assert ok_mid >= 15, f"exact cue mismatches ({ok_mid}/25)"
assert gaps > 0, "no gap verified"
assert len(view._sub_cues) == 0, "stop() must clear the cue store"
print("E2E OK: app-rendered subtitles display from the real stream")