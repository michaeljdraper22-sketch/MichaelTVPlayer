# -*- coding: utf-8 -*-
"""Probe: the app's REAL live-CC flow end to end.

play live channel (always-chase) -> wait for the CC track -> pick it ->
watch NATURAL chase playback (no seeking) and report: cue flow, overlay
painting, and the clock relationship (vlc raw vs cue starts vs the old
wall-integrated _vid_s that used to desync captions).

Run: .venv\\Scripts\\python.exe -X utf8 tools\\probe_app_live.py [minutes]
"""
import copy
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.player import USER_AGENT  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0

cfgj = json.load(open(os.path.join(os.environ["APPDATA"],
                                   "MichaelTVPlayer", "settings.json"),
                      encoding="utf-8"))
base, user, pw = (cfgj["server_url"].rstrip("/"), cfgj["username"],
                  cfgj["password"])


def api(action, **extra):
    params = {"username": user, "password": pw, "action": action}
    params.update(extra)
    url = base + "/player_api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


CAPTIONED = ("4k: espn", "fox news hd", "cnn hd", "msnbc hd", "cnbc hd",
             "bbc news", "sky news", "fox news", "cnn", "msnbc", "espn")
live = api("get_live_streams")
ch = next((c for key in CAPTIONED
           for c in live if key in c["name"].lower()), None)
assert ch, "no captioned channel found"
print(f"channel: {ch['name']!r}", flush=True)

app = QtWidgets.QApplication(sys.argv)
cfg = Config.load()
orig = copy.deepcopy(cfg.data)
cfg.data["profanity"] = {"enabled": False}   # isolate the caption path
view = PlayerView(cfg)
view._filter_engine.enabled = False
view._attach_done = True
view._attached = True
view.resize(960, 540)
view.show()


def pump(sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents()
        time.sleep(0.03)


def wait_until(pred, timeout, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    print(f"  (timeout: {what} after {timeout}s)", flush=True)
    return False


view.play_media({"kind": "live",
                 "url": f"{base}/live/{user}/{pw}/{ch['stream_id']}.ts",
                 "title": ch["name"], "stream_id": ch["stream_id"]})
ok = wait_until(lambda: view._mode == "chase", 40, "chase mode")
print(f"[1] chase engaged={ok}", flush=True)
ok = wait_until(lambda: view.vlc.is_playing(), 40, "chase playing")
print(f"[2] chase playing={ok}", flush=True)

ok = wait_until(
    lambda: any("caption" in n.lower() or n.lower().startswith("cc")
                for _, n in view.vlc.spu_tracks()), 30, "CC tracks")
tracks = view.vlc.spu_tracks()
print(f"[3] CC tracks listed={ok}: {[n for _, n in tracks][:4]}", flush=True)
if ok:
    tid, name = next((t, n) for t, n in tracks
                     if "caption" in n.lower() or n.lower().startswith("cc"))
    view._select_spu(tid, name)
    print(f"[4] picked {name!r}: cap_on={view._cap_on} "
          f"cc_source={view._cc_source is not None}", flush=True)

painted = 0
t0 = time.time()
last = 0
while time.time() - t0 < MINUTES * 60:
    app.processEvents()
    now = time.time() - t0
    if now - last >= 10:
        last = now
        raw = view.vlc.get_time() / 1000.0
        cues = view._cap_cues.cues
        latest = cues[-1][0] if cues else float("nan")
        lines = view._cap_wid._lines
        if lines:
            painted += 1
        print(f"  t={now:5.0f}s raw={raw:7.1f} vid_s={view._vid_s:7.1f} "
              f"cues={len(cues):4d} latest_start={latest:7.1f} "
              f"(lead {latest - raw:6.1f}s) lines={lines[:1]}", flush=True)
    time.sleep(0.03)

cues = view._cap_cues.cues
print(f"[5] done: cues={len(cues)} painted-samples={painted} "
      f"cap_on={view._cap_on} fail={view._cap_fail} "
      f"cc_alive={view._cc_source._alive if view._cc_source else None}",
      flush=True)
if cues:
    print("  last 3 cues:", flush=True)
    for s, e, txt in cues[-3:]:
        print(f"    {s:7.1f}-{e:7.1f}  {txt.splitlines()[-1][:50]!r}",
              flush=True)

view.stop()
pump(1)
cfg.data = orig
cfg.save()
os._exit(0)
