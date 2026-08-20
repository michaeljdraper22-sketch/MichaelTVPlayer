# -*- coding: utf-8 -*-
"""E2E: real movie -> CC menu selection -> playback rerouted through the
local relay -> app-rendered caption overlay. Verifies the VOD half of the
unified-subtitles work: a text track pick restarts playback through the
relay (ONE provider connection), cues flow from the MKV tap, the Qt
overlay paints styled lines, VLC's spu stays off, seeks stay smooth, and
Off stops the overlay without killing playback.

Run:  .venv\\Scripts\\python.exe -X utf8 tools/e2e_vod_captions.py
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

UA = "MichaelTVPlayer/1.0"
cfgj = json.load(open(os.path.join(os.environ["APPDATA"],
                                   "MichaelTVPlayer", "settings.json"),
                      encoding="utf-8"))
base, user, pw = cfgj["server_url"].rstrip("/"), cfgj["username"], \
    cfgj["password"]


def api(action=None, **extra):
    params = {"username": user, "password": pw}
    if action:
        params["action"] = action
    params.update(extra)
    url = base + "/player_api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


cats = api("get_vod_categories")
vod = api("get_vod_streams", category_id=cats[0]["category_id"])
movie = next((m for m in vod if "superbad" in m["name"].lower()), vod[0])
ext = movie.get("container_extension") or "mp4"
assert ext == "mkv", f"need an MKV with SRT subs, got .{ext}"
url = f"{base}/movie/{user}/{pw}/{movie['stream_id']}.{ext}"
print(f"movie: {movie['name']!r} ({ext})", flush=True)

app = QtWidgets.QApplication(sys.argv)
cfg = Config.load()
cfg.data["chase_delay"] = 5
cfg.data["profanity"] = {"enabled": False}   # isolate captions from the
#                                            # filter (its relay routing
#                                            # would mask the restart path)
view = PlayerView(cfg)
view._filter_engine.enabled = False
view._attach_done = True
view._attached = True

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name, flush=True)


def pump(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.03)


def wait_until(pred, timeout, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    print(f"  (timeout waiting for {what} after {timeout}s)", flush=True)
    return False


try:
    print("[1] play the movie directly (no captions wanted yet)", flush=True)
    view.play_media({"kind": "vod", "url": url, "title": movie["name"]})
    ok = wait_until(lambda: view.vlc.is_playing(), 40, "direct playback")
    check("direct playback starts", ok)
    check("no relay until captions are wanted", view._vod_relay is None)

    print("[2] pick the English track in the CC menu", flush=True)
    ok = wait_until(lambda: any("english" in n.lower()
                                for _, n in view.vlc.spu_tracks()),
                    30, "subtitle tracks")
    tracks = view.vlc.spu_tracks()
    check(f"tracks listed ({len(tracks)})", ok)
    eng = [(t, n) for t, n in tracks if "english" in n.lower()]
    tid, name = (eng[-1] if eng else tracks[0])   # prefer the full track
    # over a Forced variant
    view._select_spu(tid, name)
    check("selection latched the overlay claim", view._cap_want)
    # _restart_through_relay defers one event-loop turn, then play_media
    # does the blocking relay start
    ok = wait_until(lambda: view._vod_relay is not None, 30, "relay start")
    check("playback rerouted through the relay", ok)
    ok = wait_until(lambda: view.vlc.is_playing(), 40, "relay playback")
    check("playback running through the relay", ok)
    check("overlay owns captions", view._cap_on)
    check("VLC spu forced OFF", view.vlc.active_spu() == -1)

    print("[3] cues + overlay", flush=True)
    # the opening credits caption sparsely — jump into the dialogue-dense
    # section first (the old splitter e2e used the same trick)
    pump(3)
    if view.vlc.get_length() > 240000:
        view.vlc.set_time(105 * 1000)
        pump(5)
    ok = wait_until(lambda: len(view._cap_cues.cues) > 10, 90, "cue stream")
    check(f"cues flowing ({len(view._cap_cues.cues)})", ok)
    ok = wait_until(lambda: bool(view._cap_wid._lines), 30, "overlay lines")
    check(f"overlay painting ({view._cap_wid._lines[:1]})", ok)
    ok = wait_until(
        lambda: any(c.startswith("S_TEXT/UTF8") or c == "S_UTF8"
                    for c in (view._vod_relay.parser_tracks or {}).values()),
        20, "text track confirmed by the MKV parser")
    check("relay parser confirms a real text track", ok)
    check("no fallback latched", not view._cap_fail)

    print("[4] seek mid-movie stays smooth + captioned", flush=True)
    length = view.vlc.get_length()
    target = int(length * 0.45)
    view.vlc.set_time(target)
    pump(8)
    t = view.vlc.get_time()
    check(f"seek landed + still playing ({t/1000:.0f}s of "
          f"{length/1000:.0f}s)", view.vlc.is_playing()
          and abs(t - target) < 15000)
    check(f"provider stayed connected (opens="
          f"{view._vod_relay.provider_opens})",
          view._vod_relay.provider_opens <= 5)
    ok = wait_until(lambda: bool(view._cap_wid._lines), 25,
                    "overlay lines after seek")
    check("captions painting after the seek", ok)

    print("[5] Off keeps playback, drops the overlay", flush=True)
    view._select_spu(-1, "")
    check("overlay off", not view._cap_on)
    check("claim released", not view._cap_want)
    check("playback keeps running", view.vlc.is_playing())
finally:
    view.stop()
    pump(1)

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed", flush=True)
for f in FAIL:
    print("  FAILED:", f, flush=True)
os._exit(1 if FAIL else 0)
