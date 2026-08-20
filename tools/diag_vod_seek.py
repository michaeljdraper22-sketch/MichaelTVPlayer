"""Diagnose VLC's post-seek re-buffering through the VodRelay.

Plays a real provider movie through the relay with MTP_SPLIT_TRACE=1,
performs forward/backward seeks past the cached window, and reports how
long VLC takes to resume and where the time went (relay requests,
provider reopens, rebases) on the shared trace timeline.

Run:  .venv\\Scripts\\python.exe tools\\diag_vod_seek.py
"""
import os
import sys
import time

os.environ["MTP_SPLIT_TRACE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402

from PyQt5 import QtWidgets  # noqa: E402

import src.vod_splitter as vs  # noqa: E402
from src.player import VLCPlayer  # noqa: E402
from src.vod_splitter import VodRelay  # noqa: E402

UA = "MichaelTVPlayer/1.0"
cfg = json.load(open(os.path.join(os.environ["APPDATA"],
                                  "MichaelTVPlayer", "settings.json"),
                     encoding="utf-8"))
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


def mark(msg):
    # same time base as the relay trace -> interleavable
    print("[split %+9.3f] === %s" % (time.monotonic() - vs.TRACE_T0, msg),
          flush=True)


def wait_playing(p, app, budget, want_length=True):
    t0 = time.time()
    length = 0
    while time.time() - t0 < budget:
        time.sleep(0.5)
        app.processEvents()
        if p.is_playing():
            length = p.get_length()
            if not want_length or length > 60000:
                return length
    return length


def seek_and_measure(p, app, target_ms, budget=60):
    mark("SEEK -> %.1fs" % (target_ms / 1000))
    t0 = time.time()
    p.set_time(target_ms)
    last_t = -1
    resume = None
    samples = []
    while time.time() - t0 < budget:
        app.processEvents()
        time.sleep(0.2)
        t = p.get_time()
        playing = p.is_playing()
        samples.append((round(time.time() - t0, 2), t, playing))
        if resume is None and playing and abs(t - target_ms) < 8000 \
                and t > last_t and last_t >= 0:
            resume = time.time() - t0
        last_t = max(last_t, t)
        if resume is not None and t - target_ms > 2500:
            break     # clearly advancing past the target
    stall_spans = []
    run = None
    for dt, _t, playing in samples:
        if not playing:
            run = (run or 0) + 0.2
        else:
            if run:
                stall_spans.append(round(run, 1))
            run = None
    if run:
        stall_spans.append(round(run, 1))
    mark("RESUME after %.1fs (t=%.1fs, stalls=%s)"
         % (resume if resume is not None else -1, last_t / 1000,
            stall_spans[:10]))
    return resume


def main():
    cats = api("get_vod_categories")
    vod = api("get_vod_streams", category_id=cats[0]["category_id"])
    movie = next((m for m in vod if "superbad" in m["name"].lower()), vod[0])
    ext = movie.get("container_extension") or "mp4"
    url = f"{base}/movie/{user}/{pw}/{movie['stream_id']}.{ext}"
    print(f"movie: {movie['name']!r} ({ext})")

    app = QtWidgets.QApplication([])
    relay = VodRelay()
    local = relay.start(url, UA)
    assert local, "relay refused"
    print("relay:", local, f"total={relay.total/1048576:.0f} MiB")

    p = VLCPlayer(timeshift=False)
    p.play(local)
    length = wait_playing(p, app, 60)
    assert length > 60000, f"playback failed (length={length})"
    print(f"playing, length {length/1000:.0f}s")
    time.sleep(5)      # let the read-ahead settle
    app.processEvents()

    results = []
    for frac in (0.35, 0.60, 0.08, 0.80):
        results.append(seek_and_measure(p, app, int(length * frac)))
        time.sleep(3)
        app.processEvents()

    p.stop_and_release()
    relay.stop()
    print("\nresume times:", ["%.1fs" % (r if r is not None else -1)
                              for r in results])
    print("DIAG DONE")


if __name__ == "__main__":
    main()
