"""E2E: real provider movie -> VodRelay -> VLC playback + cues + seek."""
import json, os, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets

from src.player import VLCPlayer
from src.profanity import ProfanityEngine
from src.vod_splitter import VodRelay

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
print(f"movie: {movie['name']!r} ({ext})")

app = QtWidgets.QApplication([])
cues = []
relay = VodRelay()
relay.cue.connect(lambda s, e, t: cues.append((s, e, t)))
local = relay.start(url, UA)
assert local, "relay refused (non-mkv or open failure)"
print("relay:", local, f"total={relay.total/1048576:.0f} MiB")

p = VLCPlayer(timeshift=False)
p.play(local)
playing = False
length = 0
t0 = time.time()
while time.time() - t0 < 40:
    time.sleep(1)
    app.processEvents()
    if p.is_playing():
        playing = True
        length = p.get_length()
        if length > 60000:
            break
assert playing, "playback through the relay failed"
print(f"playing, length {length/1000:.0f}s")

p.set_time(105 * 1000)      # jump into the dialogue-dense section
t0 = time.time()
n = 0
while time.time() - t0 < 45:
    app.processEvents()
    time.sleep(0.5)
    n += 1
    if n % 8 == 0:
        pr = relay._parser
        print(f"  t+{time.time()-t0:.0f}s play={p.is_playing()} t={p.get_time()/1000:.0f}s "
              f"base={relay.cache_base/1048576:.1f}M size={relay.cache_size/1048576:.1f}M cues={len(cues)}")
    if len(cues) > 60:
        break
print(f"cues: {len(cues)}; sample: {cues[0][2][:60]!r}" if cues else "no cues!")
assert len(cues) > 10, "too few subtitle cues from the tap"

# engine on real cue timing (harmless word for deterministic windows)
class FP:
    def __init__(self): self.calls = []
    def set_filter_mute(self, on): self.calls.append(bool(on))
fp = FP()
eng = ProfanityEngine(fp)
eng.enabled = True
eng.words = [("the", "exact")]
for s, e, t in cues:
    eng.add_cue(s, e, t, lead_s=0.0)
assert eng.windows, "no windows from real cues"
w = eng.windows[len(eng.windows) // 2]
eng.evaluate((w[0] + w[1]) / 2)
assert fp.calls and fp.calls[-1] is True
eng.evaluate(w[1] + 60)
assert fp.calls[-1] is False
print(f"engine verified on {len(eng.windows)} real windows")

# SEEK far beyond the cached prefix: forces the single provider
# connection to restart at a Range offset
target = int(length * 0.45)
p.set_time(target)
time.sleep(4)
app.processEvents()
t = p.get_time()
print(f"seek to {target}ms -> playing={p.is_playing()} t={t}ms "
      f"cache={relay.cache_size/1048576:.0f} MiB")
assert p.is_playing(), "playback died after the seek"
assert abs(t - target) < 15000, "seek did not land near target"

p.stop_and_release()
relay.stop()
time.sleep(0.5)
assert not os.path.exists(relay.cache_path or "x"), "cache not cleaned"
print("E2E OK: real movie filtered through the splitter, seek survived")
