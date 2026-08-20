# -*- coding: utf-8 -*-
"""Probe a real provider movie through the app's own caption pipeline.

For each given stream path (movie/series URL path):
  1. probe the MKV head with MkvSubParser -> subtitle track codecs,
  2. run the real VodRelay, play the relay URL in a real VLC player,
  3. collect relay cue times + VLC's get_time() samples, and report
     whether the cue windows actually intersect the playback clock
     (i.e. would the overlay paint anything at the natural position).

Run: .venv\\Scripts\\python.exe -X utf8 tools\\probe_vod_captions.py movie/726352471c/d809266e91/2007185.mkv
"""
import json
import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtCore  # noqa: E402

from src.mkv_subs import MkvSubParser, is_text_codec  # noqa: E402
from src.player import USER_AGENT  # noqa: E402
from src.vod_splitter import VodRelay  # noqa: E402

cfgj = json.load(open(os.path.join(os.environ["APPDATA"],
                                   "MichaelTVPlayer", "settings.json"),
                      encoding="utf-8"))
base = cfgj["server_url"].rstrip("/")

paths = sys.argv[1:] or ["movie/726352471c/d809266e91/2007185.mkv"]

app = QtCore.QCoreApplication(sys.argv)

for path in paths:
    url = f"{base}/{path}"
    print(f"\n=== {path} ===")

    # ---- 1. head probe: what tracks does this file carry? ----
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-2097151"})
    with urllib.request.urlopen(req, timeout=30) as r:
        blob = r.read(1 << 21)
    p = MkvSubParser()
    p.feed(blob)
    meta = p._track_meta
    print(f"  subtitle tracks in head: {len(meta)}")
    for n, m in sorted(meta.items()):
        text = is_text_codec(m["codec"])
        print(f"    #{n}: codec={m['codec']!r} lang={m.get('lang')!r} "
              f"name={m.get('name')!r} text={text}")
    if not any(is_text_codec(m["codec"]) for m in meta.values()):
        print("  -> NO text track: overlay can never render; "
              "VLC renders bitmaps (if any)")
        continue

    # ---- 2. full relay + VLC through it ----
    relay = VodRelay()
    cues = []
    relay.cue.connect(lambda s, e, t: cues.append((s, e, t)))
    fail = []
    relay.failed.connect(lambda why: fail.append(why))
    local = relay.start(url, USER_AGENT, prefer_language="eng")
    if not local:
        print(f"  relay start FAILED: {fail}")
        continue
    print(f"  relay up: {local}")

    import vlc  # noqa: E402
    inst = vlc.Instance(["--ignore-config", "--no-audio", "--vout=dummy",
                         "--network-caching=1500"])
    player = inst.media_player_new()
    media = inst.media_new(local)
    media.add_option(f"http-user-agent={USER_AGENT}")
    player.set_media(media)
    player.play()

    t0 = time.time()
    samples = []
    while time.time() - t0 < 30:
        app.processEvents()
        t = player.get_time()
        if t >= 0:
            samples.append(t / 1000.0)
        time.sleep(0.2)
    player.stop()
    relay.stop()

    print(f"  vlc clock: {samples[0]:.1f}s -> {samples[-1]:.1f}s "
          f"({len(samples)} samples)")
    print(f"  relay cues: {len(cues)}  failures: {fail}")
    if cues:
        starts = [c[0] for c in cues]
        print(f"    first cue @{starts[0]:.1f}s  last cue @{starts[-1]:.1f}s")
        print(f"    sample: {cues[0][2]!r}")
        vfrom, vto = samples[0], samples[-1]
        inside = [c for c in cues if c[0] <= vto and c[1] >= vfrom]
        print(f"    cues intersecting the played window "
              f"[{vfrom:.1f}, {vto:.1f}]: {len(inside)}")
        for c in inside[:5]:
            print(f"      {c[0]:8.1f} - {c[1]:8.1f}  {c[2][:40]!r}")
    else:
        print("    -> NO CUES EXTRACTED while VLC played 30 s")
    if fail:
        print(f"    relay failures: {fail}")
    print(f"  parser_tracks: {relay.parser_tracks} "
          f"selected={relay.parser_selected}")

app.processEvents()
