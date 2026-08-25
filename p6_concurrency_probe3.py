"""Confirm the kill-after-download-close pattern (short download run).

Probe 1: A survived while B downloaded, EOF'd 0.6 s after B closed.
Control: A alone never EOFs (200 MB). This run repeats the experiment
with an 8 s download to confirm the kill is tied to B's CLOSE, and
watches A for 30 s afterwards.
"""

import sys
import threading
import time
import urllib.request

BASE = ("http://cf.534842.xyz/streaming/timeshift.php"
        "?username=726352471c&password=d809266e91"
        "&stream=497001&start=2026-08-25:02-15&duration=285&extension=ts")
WIN = ("http://cf.534842.xyz/streaming/timeshift.php"
       "?username=726352471c&password=d809266e91"
       "&stream=497001&start=2026-08-25:02-44&duration=35&extension=ts")
UA = "MichaelTVPlayer/1.0"


def dl(seconds):
    req = urllib.request.Request(WIN, headers={"User-Agent": UA})
    got = 0
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=30) as r:
        while time.time() - t0 < seconds:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            got += len(chunk)
    print(f"[B] download closed after {time.time() - t0:.1f}s, "
          f"{got / 1048576:.0f} MB", flush=True)


def main():
    events = []
    t0 = time.time()

    # A: playback connection
    req = urllib.request.Request(BASE, headers={"User-Agent": UA,
                                                "Range": "bytes=8000000-"})
    with urllib.request.urlopen(req, timeout=30) as a:
        threading.Thread(target=dl, args=(8,), daemon=True).start()
        got = 0
        while time.time() - t0 < 45:
            chunk = a.read(256 * 1024)
            if not chunk:
                print(f"[A] EOF at t+{time.time() - t0:.1f}s "
                      f"({got / 1048576:.1f} MB) — PLAYBACK CONNECTION KILLED",
                      flush=True)
                return
            got += len(chunk)
            time.sleep(0.05)
        print(f"[A] still alive at t+45s ({got / 1048576:.1f} MB) — "
              f"no kill this time", flush=True)


if __name__ == "__main__":
    sys.exit(main())
