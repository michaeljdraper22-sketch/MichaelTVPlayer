"""E2E: real movie -> ffmpeg SRT extraction -> profanity windows (research)."""
import json, os, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtCore

from src import profanity as P

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
vod = []
for c in cats[:6]:
    vod += api("get_vod_streams", category_id=c["category_id"])
movie = next((m for m in vod if "superbad" in m["name"].lower()), vod[0])
ext = movie.get("container_extension") or "mp4"
url = f"{base}/movie/{user}/{pw}/{movie['stream_id']}.{ext}"
print(f"movie: {movie['name']!r}")

ffmpeg = P.find_ffmpeg()
print("ffmpeg:", ffmpeg)
assert ffmpeg

app = QtCore.QCoreApplication([])
ex = P.SubtitleExtractor()
ex._prefer_language = "english"
ok = ex.probe_track(url, UA)
print("probe ok:", ok, "sub stream index:", ex._want_index)
assert ok

cues = []
def on_cue(s, e, t):
    cues.append((s, e, t))
ex.cue.connect(on_cue)
ex.start(url, UA, "english", 0.0, readrate=15)

deadline = time.time() + 45
while time.time() < deadline and len(cues) < 400:
    app.processEvents()
    time.sleep(0.05)
ex.stop()
print(f"cues extracted: {len(cues)}  frontier: {ex.frontier_s:.1f}s")
assert len(cues) > 20, "extraction produced too little"

mono = all(cues[i][0] <= cues[i + 1][0] + 0.01 for i in range(len(cues) - 1))
print("timestamps monotonic:", mono)

words = [tuple(w) for w in P.DEFAULT_WORDS]
wins = P.windows_from_cues(cues, words)
print(f"mute windows from default list: {len(wins)}")
shown = 0
for s, e, t in cues:
    if P.find_matches(t, words) and shown < 6:
        print(f"  [{s:7.1f}-{e:7.1f}] {P.mask_text(t, words)!r}")
        shown += 1
assert wins, "no profanity windows found in this movie (!?)"
print("E2E OK: extraction + windows work against the real stream")
