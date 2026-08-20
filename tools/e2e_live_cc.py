"""E2E: live stream -> recorder buffer -> CCSource -> cues -> mute windows.

One provider connection (the recorder's). CCSource tails the growing
buffer and pipes it into the real CCExtractor. A harmless word list
('the') guarantees windows so the mute engine can be verified on live
caption timing.
"""
import json, os, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtCore

import vlc
from src.live_cc import CCSource, find_ccextractor
from src.profanity import ProfanityEngine

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

live = api("get_live_streams")
# 24/7 channels that caption near-continuously; anything else risks an
# un-captioned stretch (or a channel with none at all) breaking the run.
# Keywords run in priority order — "4K: ESPN" is the verified carrier,
# main HD news feeds next; offshoot variants of a brand can lack
# caption carriage entirely.
CAPTIONED = ("4k: espn", "fox news hd", "cnn hd", "msnbc hd", "cnbc hd",
             "bbc news", "sky news", "fox news", "cnn", "msnbc", "espn")
ch = next((c for key in CAPTIONED
           for c in live if key in c["name"].lower()), live[0])
url = f"{base}/live/{user}/{pw}/{ch['stream_id']}.ts"
print(f"channel: {ch['name']!r}")
assert find_ccextractor(), "CCExtractor not found"

buf = os.path.abspath("build/e2e_cc_buffer.ts").replace("\\", "/")
try: os.remove(buf)
except OSError: pass

# the recorder (this is the ONLY provider connection)
inst = vlc.Instance(["--no-video-title-show", "--no-stats",
                     "--network-caching=1500", "--live-caching=1500"])
rec = inst.media_player_new()
m = inst.media_new(url)
m.add_option(f"http-user-agent={UA}")
m.add_option(f":sout=#std{{access=file,mux=ts,dst='{buf}'}}")
rec.set_media(m)
rec.play()
time.sleep(8)     # let the buffer gain some data to join onto
frontier = 8.0    # join at the content frontier (~8 s in)

app = QtCore.QCoreApplication([])
cues = []
src = CCSource()
src.cue.connect(lambda s, e, t: cues.append((s, e, t)))
ok = src.start(buf, frontier)
assert ok, "CCSource failed to start"

t0 = time.time()
while time.time() - t0 < 50:
    app.processEvents()
    time.sleep(0.25)
    if len(cues) > 60:
        break
rec.stop()
src.stop()
time.sleep(1)

print(f"cues: {len(cues)}")
assert len(cues) >= 5, "too few captions captured"
starts = [c[0] for c in cues]
assert min(starts) >= frontier - 2, f"cues before join point? {min(starts)}"
print(f"cue times: first={min(starts):.1f}s last={max(starts):.1f}s "
      f"(joined at frontier {frontier}s)")
print("sample:", " / ".join(c[2][:30] for c in cues[:3]))

# engine on live caption timing, harmless word for deterministic windows
class FP:
    def __init__(self): self.calls = []
    def set_filter_mute(self, on): self.calls.append(bool(on))
fp = FP()
eng = ProfanityEngine(fp)
eng.enabled = True
eng.words = [("the", "exact")]
eng.lead_s = 1.5
for s, e, t in cues:
    eng.add_cue(s, e, t)
assert eng.windows, "no mute windows from live captions"
print(f"mute windows: {len(eng.windows)}, first: "
      f"{eng.windows[0][0]:.1f}-{eng.windows[0][1]:.1f}s")
w = eng.windows[len(eng.windows) // 2]
eng.evaluate((w[0] + w[1]) / 2)
assert fp.calls and fp.calls[-1] is True, "no mute inside a live window"
eng.evaluate(w[1] + 30)
assert fp.calls[-1] is False
print("mute engine flips correctly on live caption timing")
print("E2E OK: live caption profanity pipeline works end to end")
