"""Diagnose: do freetype instance args actually style rendered subtitles?

Plays a real movie with subs on, twice: default look vs an unmistakable
style (40px bright green + thick outline), snapshots the vout mid-dialogue,
scans the subtitle band for the expected colors.
"""
import json, os, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets

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

cats = api("get_vod_categories")
vod = api("get_vod_streams", category_id=cats[0]["category_id"])
movie = next((m for m in vod if "superbad" in m["name"].lower()), vod[0])
ext = movie.get("container_extension") or "mp4"
url = f"{base}/movie/{user}/{pw}/{movie['stream_id']}.{ext}"
print(f"movie: {movie['name']!r}")

app = QtWidgets.QApplication([])
win = QtWidgets.QWidget()
win.resize(854, 480)
win.show()

SEEK = 112.0   # inside the 'Hey, man, I was doing some research...' cues

def run(tag, style):
    p = VLCPlayer(timeshift=False,
                  sub_args=subtitle_instance_args(style or {}))
    p.set_window(int(win.winId()))
    p.play_at(url, SEEK)
    en = None
    for _ in range(50):
        app.processEvents()
        time.sleep(0.4)
        if p.spu_tracks():
            en = next((tid for tid, n in p.spu_tracks()
                       if "english" in n.lower()), None)
            if en is not None:
                break
    assert en is not None, "no English track"
    p.set_spu(en)
    # sit inside the cue window and grab a frame
    for _ in range(14):
        app.processEvents()
        time.sleep(0.2)
    snap = os.path.abspath(f"build/snap_{tag}.png").replace("\\", "/")
    p.player.video_take_snapshot(0, snap, 0, 0)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.1)
    t = p.get_time() / 1000.0
    p.stop_and_release()
    time.sleep(0.8)
    return snap, t

def scan(path):
    from PyQt5 import QtGui
    img = QtGui.QImage(path)
    w, h = img.width(), img.height()
    band_top = int(h * 0.62)
    green = white = 0
    for y in range(band_top, h, 1):
        for x in range(0, w, 1):
            c = img.pixelColor(x, y)
            r, g, b = c.red(), c.green(), c.blue()
            if g > 180 and r < 110 and b < 110:
                green += 1
            elif r > 215 and g > 215 and b > 215:
                white += 1
    return green, white

s1, t1 = run("default", None)
g1, w1 = scan(s1)
print(f"default  @{t1:.0f}s: green={g1} white={w1}")
styled = {"text_color": "#00FF00", "size": 40,
          "outline_enabled": True, "outline_color": "#000000",
          "outline_thickness": 8}
s2, t2 = run("styled", styled)
g2, w2 = scan(s2)
print(f"styled  @{t2:.0f}s: green={g2} white={w2}")

if g2 > 200 and g2 > g1 * 5:
    print("VERDICT: freetype args DO work (styled text is green)")
elif w2 < 50 and g2 < 50:
    print("VERDICT: no subtitle rendered at all in either run (test flaw)")
else:
    print("VERDICT: freetype args DO NOT reach the renderer (styled text "
          "NOT green)")
