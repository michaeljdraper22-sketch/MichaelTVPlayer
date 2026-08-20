# -*- coding: utf-8 -*-
"""Probe several live channels for ACTIVE closed captions right now:
record ~25 s each through the same sout pipeline the DVR uses, feed the
buffer to CCSource, count cues. Prints the best channel for caption e2e.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtCore

import vlc
from src.live_cc import CCSource, find_ccextractor
from src.player import USER_AGENT

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


assert find_ccextractor(), "CCExtractor not found"
live = api("get_live_streams")
# news/talk channels caption near-continuously
pats = ("fox news", "cnn", "msnbc", "abc news", "nbc", "sky news", "bbc news",
        "nfl network", "espn", "discovery", "history")
picked = []
for p in pats:
    for c in live:
        if p in c["name"].lower() and c["name"] not in [x[0] for x in picked]:
            picked.append((c["name"], c["stream_id"]))
    if len(picked) >= 6:
        break

app = QtCore.QCoreApplication([])
buf = os.path.abspath("build/diag_cap_buffer.ts").replace("\\", "/")
inst = vlc.Instance(["--no-video-title-show", "--no-stats",
                     "--network-caching=1500", "--live-caching=1500"])

best = []
try:
    for name, sid in picked[:6]:
        url = f"{base}/live/{user}/{pw}/{sid}.ts"
        try:
            os.remove(buf)
        except OSError:
            pass
        rec = inst.media_player_new()
        m = inst.media_new(url)
        m.add_option(f"http-user-agent={UA}")
        m.add_option(f":sout=#std{{access=file,mux=ts,dst='{buf}'}}")
        rec.set_media(m)
        rec.play()
        time.sleep(10)     # recorder still RUNNING: CCSource tails the
        #                   growing buffer exactly like the app does
        cues = []
        src = CCSource()
        src.cue.connect(lambda s, e, t: cues.append((s, e, t)))
        if src.start(buf, 0.0):
            t0 = time.time()
            while time.time() - t0 < 18:
                app.processEvents()
                time.sleep(0.1)
            src.stop()
        rec.stop()
        rec.set_media(None)
        time.sleep(0.6)
        print(f"{name!r} (id {sid}): {len(cues)} cues")
        if cues:
            best.append((len(cues), name, sid))
            print(f"   sample: {cues[0][2][:60]!r}")
        try:
            os.remove(buf)
        except OSError:
            pass
finally:
    try:
        os.remove(buf)
    except OSError:
        pass

if best:
    best.sort(reverse=True)
    print("\nBEST:", best[0])
else:
    print("\nno captioning channel found right now")
