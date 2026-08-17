# -*- coding: utf-8 -*-
"""End-to-end driver: real provider, real VodBrowser + SeriesEpisodesDialog,
simulated clicks. Run:  .venv\\Scripts\\python.exe -X utf8 test_vod_series.py"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.browsers import SeriesEpisodesDialog, VodBrowser  # noqa: E402
from src.xtream import XtreamClient  # noqa: E402

app = QtWidgets.QApplication(sys.argv)
cfg = Config.load()
client = XtreamClient(cfg.normalized_server(), cfg.username, cfg.password)


def wait_for(pred, timeout_s, what):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    print(f"TIMEOUT waiting for {what!r}", flush=True)
    return False


def spin(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.03)


print("== VOD tab ==", flush=True)
vod = VodBrowser(cfg, client, "vod")
# With the fix the tab opens on the FIRST CATEGORY (fast), not "All".
t0 = time.time()
ok = wait_for(lambda: vod.list.count() > 0
              or "Error" in vod.status.text(), 90, "vod items")
spin(1.0)
print("load seconds:", round(time.time() - t0, 1), flush=True)
print("default combo index:", vod.cat_combo.currentIndex(),
      "(0=recent 1=All 2+=category)", flush=True)
print("status:", vod.status.text(), flush=True)
print("items in list:", vod.list.count(), flush=True)

got = []


def spy(p):
    got.append(p)


vod.media_activated.connect(spy)
if vod.list.count() > 0:
    vod._activate(vod.list.item(0))
    spin(0.5)
    if got:
        p = got[-1]
        print("activated playable kind:", p.get("kind"), flush=True)
        print("url:", p.get("url"), flush=True)
        print("title:", p.get("title"), flush=True)
    else:
        print("NO playable emitted on activation!", flush=True)

print()
print("== Series episodes dialog (series_id=45515) ==", flush=True)
dlg = SeriesEpisodesDialog(client, cfg,
                           {"name": "probe", "series_id": 45515}, None)
ok = wait_for(lambda: "Loading" not in dlg.status.text(), 60, "series info")
spin(0.5)
seasons = dlg.tree.topLevelItemCount()
eps = sum(dlg.tree.topLevelItem(i).childCount() for i in range(seasons))
print("status:", dlg.status.text(), flush=True)
print("season nodes:", seasons, "episode nodes:", eps, flush=True)
print("EPISODES FIXED!" if eps > 0 else ">>> BUG STILL PRESENT", flush=True)

# now drive an episode activation through MainWindow's logic equivalent
got2 = []
dlg.media_activated.connect(lambda p: got2.append(p))
if eps:
    for i in range(seasons):
        root = dlg.tree.topLevelItem(i)
        for j in range(root.childCount()):
            dlg._on_double(root.child(j), 0)
            break
        if got2:
            break
    spin(0.3)
    if got2:
        p = got2[-1]
        print("episode playable kind:", p.get("kind"), flush=True)
        print("url:", p.get("url"), flush=True)
    else:
        print("NO playable emitted from episode!", flush=True)
else:
    print(">>> BUG CONFIRMED: no episode nodes to play", flush=True)

# quick VOD URL sanity via requests HEAD
if got:
    import requests
    try:
        r = requests.get(got[-1]["url"], headers={"Range": "bytes=0-99999"},
                         timeout=25, stream=True)
        chunk = next(r.iter_content(100000), b"")
        r.close()
        print("movie URL GET:", r.status_code,
              r.headers.get("Content-Type"), len(chunk), "bytes", flush=True)
    except Exception as exc:
        print("movie URL GET failed:", repr(exc), flush=True)

print()
print("== REAL VLC playback of a movie ==", flush=True)
if got:
    from src.player import VLCPlayer
    vp = VLCPlayer(volume=30, network_caching=1500)
    vp.play(got[-1]["url"])
    t0 = time.time()
    ok = wait_for(lambda: vp.is_playing() and vp.get_time() > 0,
                  40, "movie playback")
    spin(3.0)
    print("state:", vp.state_name(), " time(s):",
          round(vp.get_time() / 1000, 1),
          " length(s):", round(vp.get_length() / 1000, 1),
          f" after {time.time() - t0:.1f}s", flush=True)
    print("VLC MOVIE PLAYBACK WORKS" if vp.is_playing() else
          ">>> VLC MOVIE PLAYBACK FAILED", flush=True)
    vp.stop_and_release()

print()
print("== REAL VLC playback of a series episode ==", flush=True)
if got2:
    from src.player import VLCPlayer
    vp = VLCPlayer(volume=30, network_caching=1500)
    vp.play(got2[-1]["url"])
    ok = wait_for(lambda: vp.is_playing() and vp.get_time() > 0,
                  40, "episode playback")
    spin(3.0)
    print("state:", vp.state_name(), " time(s):",
          round(vp.get_time() / 1000, 1),
          " length(s):", round(vp.get_length() / 1000, 1), flush=True)
    print("VLC EPISODE PLAYBACK WORKS" if vp.is_playing() else
          ">>> VLC EPISODE PLAYBACK FAILED", flush=True)
    vp.stop_and_release()
