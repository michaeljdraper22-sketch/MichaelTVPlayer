"""E2E: real VOD stream + styled instance + live delay (research tool)."""
import json, os, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.player import VLCPlayer, subtitle_instance_args

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

# styled player: bigger font, raised, yellow text, box, thick outline + delay
style = {"font": "Arial", "size": 28, "pos_pct": 20, "text_color": "#FFE070",
         "bg_enabled": True, "bg_color": "#101010", "bg_opacity": 60,
         "outline_enabled": True, "outline_color": "#000000",
         "outline_thickness": 6}
args = subtitle_instance_args(style)
print("instance args:", args)

cats = api("get_vod_categories")
vod = api("get_vod_streams", category_id=cats[0]["category_id"])
movie = vod[0]
ext = movie.get("container_extension") or "mp4"
url = f"{base}/movie/{user}/{pw}/{movie['stream_id']}.{ext}"
print(f"playing: {movie['name']!r} ({ext})")

p = VLCPlayer(timeshift=False, sub_args=args, spu_delay_ms=250)
p.play(url)
tracks = []
for i in range(40):
    time.sleep(0.5)
    if p.is_playing():
        tracks = p.spu_tracks()
        if tracks:
            break
assert tracks, "no subtitle tracks discovered with styled instance"
print(f"tracks found with styled instance: {len(tracks)}")

en = next((tid for tid, n in tracks if "english (united" in n.lower()
           or "english" in n.lower()), tracks[0][0])
p.set_spu(en)
time.sleep(1.0)
active = p.active_spu()
assert active == en, f"selection failed with delay set ({active} != {en})"
print(f"track {en} selected, active_spu={active}")

# live delay change mid-playback
p.set_spu_delay(-750)
time.sleep(0.6)
real_delay_us = p.player.video_get_spu_delay()
assert p.is_playing(), "playback died after delay change"
print(f"delay changed to -750ms -> VLC reports {real_delay_us} us")
assert real_delay_us == -750000, "delay not applied by VLC"

p.set_spu(-1)
time.sleep(0.5)
assert p.active_spu() == -1
print("subs disabled cleanly")
p.stop_and_release()
print("E2E OK: styled instance + live delay did not disturb playback")
