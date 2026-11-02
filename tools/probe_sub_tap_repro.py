# -*- coding: utf-8 -*-
"""Headless repro: does the VodRelay subtitle tap emit cues for a stream?

Plays NO video: a plain HTTP reader pulls the relay's local URL at a
playback-like rate while the harness watches the MKV head parse verdict,
the tap's selected track, and cue emission. Use it against the exact
provider URL from a failing night (player.log 'vod splitter:' lines).

Run: .venv\\Scripts\\python.exe tools\\probe_sub_tap_repro.py <url> [secs]
"""
import os
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets  # noqa: E402

from src.vod_splitter import VodRelay  # noqa: E402
from src.player import USER_AGENT  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else ""
RUN_S = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
READ_RATE = 2.0 * (1 << 20)     # bytes/s — playback-like pull


def main():
    app = QtWidgets.QApplication([])
    cues = []
    fails = []
    relay = VodRelay()
    relay.cue.connect(lambda s, e, t: cues.append((s, e, t)))
    relay.failed.connect(lambda m: fails.append(m))
    t0 = time.time()
    local = relay.start(URL, USER_AGENT)
    if not local:
        print("relay refused the URL (non-mkv/mp4 or open failure)")
        return 2
    print("relay up:", local, "total=%.1f MiB" % (relay.total / 1048576))

    stop = threading.Event()

    def reader():
        # VLC-like sequential pull of the local URL
        try:
            r = urllib.request.urlopen(local, timeout=30)
            while not stop.is_set():
                b = r.read(1 << 18)
                if not b:
                    break
                time.sleep((1 << 18) / READ_RATE)
            r.close()
        except Exception as exc:      # noqa: BLE001
            print("reader ended: %r" % exc)

    threading.Thread(target=reader, daemon=True).start()

    last = 0
    while time.time() - t0 < RUN_S:
        time.sleep(3)
        app.processEvents()
        tracks = getattr(relay, "parser_tracks", None) or {}
        meta = getattr(relay, "parser_tracks_meta", None) or {}
        grew = len(cues) - last
        last = len(cues)
        print("t+%4.0fs verdict=%s tracks=%s sel=%r cues=%d (+%d) "
              "cache=%.1fM base=%dM fails=%d"
              % (time.time() - t0,
                 getattr(relay, "tracks_verdict_ready", None),
                 tracks,
                 getattr(relay, "parser_selected", None),
                 len(cues), grew,
                 relay.cache_size / 1048576, relay.cache_base >> 20,
                 len(fails)))
        if fails:
            print("  failures:", fails[:3])
        if len(cues) > 400:
            break
    stop.set()
    time.sleep(0.5)
    meta = getattr(relay, "parser_tracks_meta", None) or {}
    for n, m in sorted(meta.items()):
        print("  track %s: codec=%r lang=%r name=%r" %
              (n, m.get("codec"), m.get("lang"), m.get("name")))
    if cues:
        print("first cues:", [c[2][:40] for c in cues[:3]])
        print("last  cues:", [c[2][:40] for c in cues[-3:]])
    else:
        print("NO CUES EMITTED")
    relay.stop()
    return 0 if cues else 1


if __name__ == "__main__":
    if not URL:
        print(__doc__)
        sys.exit(2)
    sys.exit(main())
