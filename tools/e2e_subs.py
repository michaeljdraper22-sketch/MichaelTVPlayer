"""E2E: real provider VOD stream -> VLCPlayer subtitle API (research tool)."""
import json, os, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.player import VLCPlayer

UA = "MichaelTVPlayer/1.0"
cfg = json.load(open(os.path.join(os.environ["APPDATA"], "MichaelTVPlayer", "settings.json"), encoding="utf-8"))
base, user, pw = cfg["server_url"].rstrip("/"), cfg["username"], cfg["password"]

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
movie = vod[0]
ext = movie.get("container_extension") or "mp4"
url = f"{base}/movie/{user}/{pw}/{movie['stream_id']}.{ext}"
print(f"playing: {movie['name']!r} ({ext})")

p = VLCPlayer(timeshift=False)
p.play(url)
tracks = []
for i in range(40):           # tracks appear a few seconds after Playing
    time.sleep(0.5)
    if p.is_playing():
        tracks = p.spu_tracks()
        if tracks:
            break
print(f"spu tracks found ({len(tracks)}): {[n for _, n in tracks][:8]}...")
assert tracks, "no subtitle tracks discovered during playback"

en = next((tid for tid, n in tracks if "english" in n.lower()), tracks[0][0])
name = next(n for tid, n in tracks if tid == en)
p.set_spu(en)
time.sleep(1.0)
active = p.active_spu()
print(f"set_spu({en}={name!r}) -> active_spu()={active}")
assert active == en, "selection did not stick"

p.set_spu(-1)
time.sleep(0.8)
print(f"set_spu(-1) -> active_spu()={p.active_spu()}")
assert p.active_spu() == -1

p.stop_and_release()
print("E2E OK: tracks discovered, track selected, subtitles disabled")
