# -*- coding: utf-8 -*-
"""Research: what does VLC name spu tracks on (a) a VOD MKV played through
the local relay, (b) a live DVR chase buffer? Drives the text/bitmap/ass
classification for the caption overlay. Run: python tools/diag_track_names.py
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.player import VLCPlayer, USER_AGENT

UA = USER_AGENT
cfg = json.load(open(os.path.join(os.environ["APPDATA"], "MichaelTVPlayer",
                                  "settings.json"), encoding="utf-8"))
base, user, pw = cfg["server_url"].rstrip("/"), cfg["username"], cfg["password"]


def api(action=None, **extra):
    params = {"username": user, "password": pw}
    if action:
        params["action"] = action
    params.update(extra)
    url = base + "/player_api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def probe_spu(url, label, seconds=14):
    p = VLCPlayer(timeshift=False)
    p.play(url, timeshift=False)
    seen = {}
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(0.5)
        for tid, name in p.spu_tracks():
            seen[tid] = name
    print(f"== {label} ==")
    for tid, name in sorted(seen.items()):
        print(f"   [{tid}] {name!r}")
    if not seen:
        print("   (no spu tracks)")
    p.stop_and_release()
    return seen


# --- VOD MKV (movies are .mkv on this provider) ---
cats = api("get_vod_categories")
vod = api("get_vod_streams", category_id=cats[0]["category_id"])
mkv = next((m for m in vod
            if (m.get("container_extension") or "") == "mkv"), vod[0])
ext = mkv.get("container_extension") or "mp4"
url = f"{base}/movie/{user}/{pw}/{mkv['stream_id']}.{ext}"
print(f"movie: {mkv['name']!r} ({ext})")
probe_spu(url, "VOD direct")

# --- live chase buffer (sout re-muxed TS, as the recorder writes it) ---
live = api("get_live_streams")
ch = next((c for c in live if "nfl" in c["name"].lower()), live[0])
lurl = f"{base}/live/{user}/{pw}/{ch['stream_id']}.ts"
print(f"channel: {ch['name']!r}")
buf = os.path.abspath("build/diag_chase_buffer.ts").replace("\\", "/")
try:
    os.remove(buf)
except OSError:
    pass
import vlc as vlc_mod
inst = vlc_mod.Instance(["--no-video-title-show", "--no-stats",
                         "--network-caching=1500", "--live-caching=1500"])
rec = inst.media_player_new()
m = inst.media_new(lurl)
m.add_option(f"http-user-agent={UA}")
m.add_option(f":sout=#std{{access=file,mux=ts,dst='{buf}'}}")
rec.set_media(m)
rec.play()
time.sleep(10)
probe_spu(buf, "live chase buffer (re-muxed TS)")
rec.stop()
rec.set_media(None)
time.sleep(0.5)
try:
    os.remove(buf)
except OSError:
    pass
print("done")
