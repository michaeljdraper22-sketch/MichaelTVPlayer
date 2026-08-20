# -*- coding: utf-8 -*-
"""Probe: the app's REAL VOD caption flow under the user's real config.

Mimics exactly what a user session does when the profanity filter is on:
  play movie (relay auto-engaged) -> wait for VLC tracks -> pick the
  English track via the CC menu path -> watch NATURAL playback (no seek)
  and report whether the overlay ever paints, plus clock diagnostics.

Run: .venv\\Scripts\\python.exe -X utf8 tools\\probe_app_vod.py [url-path]
"""
import copy
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

PATH = (sys.argv[1] if len(sys.argv) > 1
        else "movie/726352471c/d809266e91/1986901.mkv")

app = QtWidgets.QApplication(sys.argv)
cfg = Config.load()
print(f"profanity enabled in real config: "
      f"{cfg.profanity.get('enabled')}", flush=True)

view = PlayerView(cfg)
view._filter_engine.enabled = bool(cfg.profanity.get("enabled"))
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


import json  # noqa: E402
cfgj = json.load(open(os.path.join(os.environ["APPDATA"],
                                   "MichaelTVPlayer", "settings.json"),
                      encoding="utf-8"))
url = f"{cfgj['server_url'].rstrip('/')}/{PATH}"

print("[1] play movie (initial, like a user double-click)", flush=True)
view.play_media({"kind": "vod", "url": url, "title": "probe movie"})
ok = wait_until(lambda: view.vlc.is_playing(), 40, "playback")
print(f"  playing={ok} relay={view._vod_relay is not None}", flush=True)

ok = wait_until(lambda: view.vlc.spu_tracks(), 30, "VLC track list")
tracks = view.vlc.spu_tracks()
print(f"[2] VLC track list: {len(tracks)} tracks", flush=True)
for t, n in tracks[:6]:
    print(f"    #{t} {n!r} eligible={view._cap_eligible(n)}", flush=True)

pick = next(((t, n) for t, n in tracks if "english" in n.lower()),
            tracks[0] if tracks else None)
if not pick:
    raise SystemExit("no tracks to pick")
print(f"[3] picking {pick[1]!r} (user CC-menu action)", flush=True)
view._select_spu(*pick)
print(f"    after pick: _cap_on={view._cap_on} _cap_want={view._cap_want} "
      f"_cap_fail={view._cap_fail} relay_sel="
      f"{getattr(view._vod_relay, 'parser_selected', None)}", flush=True)

print("[4] natural playback watch (no seeking) — 75 s", flush=True)
t0 = time.time()
last_report = 0
while time.time() - t0 < 75:
    app.processEvents()
    now = time.time() - t0
    if now - last_report >= 5:
        last_report = now
        vlc_t = view.vlc.get_time() / 1000.0
        cues = view._cap_cues.cues
        print(f"  t={now:5.1f}s vlc_clock={vlc_t:7.1f}s cues={len(cues):4d} "
              f"lines={view._cap_wid._lines[:1]} cap_on={view._cap_on} "
              f"fail={view._cap_fail} sel="
              f"{getattr(view._vod_relay, 'parser_selected', None)} "
              f"ovl_vis={view._cap_wid.isVisible()}", flush=True)
    time.sleep(0.03)

print("[5] final state", flush=True)
cues = view._cap_cues.cues
print(f"  cues={len(cues)} cap_on={view._cap_on} fail={view._cap_fail} "
      f"active_spu={view.vlc.active_spu()} spu_want={view._spu_want}",
      flush=True)
if cues:
    in_win = [c for c in cues
              if c[0] <= view.vlc.get_time() / 1000.0 + 5]
    print(f"  last 3 cues: {[(round(c[0],1), round(c[1],1), c[2][:30]) for c in cues[-3:]]}",
          flush=True)
    print(f"  vlc clock now: {view.vlc.get_time()/1000.0:.1f}s", flush=True)

view.stop()
pump(1)
os._exit(0)
