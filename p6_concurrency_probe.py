"""Probe: does a concurrent window download stall the playback connection?

Reproduces the 8/25 freeze scenario headlessly:
  A) "playback": a ranged connection to the SAME timeshift URL the app's
     CatchupRelay holds open for VLC, read at ~playback pace, rate logged
     every 2 s.
  B) "download": a second connection to a window timeshift URL (different
     start), read at FULL speed like FileDownloader for ~20 s, then closed.

Verdict comes from A's byte rate timeline: healthy before B, starved or
dead during/after B => the second connection is what froze the stream.
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

rate_log = []          # (t, phase, bytes_in_bucket)
lock = threading.Lock()
phase = ["A-only"]


def reader(tag, url, rng, pace, stop, cap_mb):
    """Read the URL, logging bytes per 2 s bucket. pace=None => full speed."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if rng:
        req.add_header("Range", f"bytes={rng[0]}-")
    got = 0
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            bucket = 0
            t_bucket = time.time()
            while not stop.is_set() and got < cap_mb * 1048576:
                chunk = r.read(256 * 1024)
                if not chunk:
                    with lock:
                        rate_log.append(
                            (time.time(), phase[0], tag, bucket, "EOF"))
                    return
                bucket += len(chunk)
                got += len(chunk)
                now = time.time()
                if now - t_bucket >= 2.0:
                    with lock:
                        rate_log.append(
                            (time.time(), phase[0], tag, bucket, ""))
                    bucket = 0
                    t_bucket = now
                if pace is not None:
                    # playback pace: ~pace MB/s average
                    time.sleep(max(0.0, len(chunk) / 1048576.0 / pace - 0.02))
    except Exception as exc:  # noqa: BLE001
        with lock:
            rate_log.append((time.time(), phase[0], tag, 0, repr(exc)[:60]))
    finally:
        with lock:
            rate_log.append((time.time(), phase[0], tag, None,
                             f"closed got={got // 1048576}MB"))


def main():
    t0 = time.time()
    stop_a = threading.Event()
    stop_b = threading.Event()

    # A: playback connection, mid-file range, ~0.8 MB/s (4K stream pace)
    ta = threading.Thread(target=reader,
                          args=("A-play", BASE, (5_000_000, None), 0.8,
                                stop_a, 60), daemon=True)
    ta.start()
    time.sleep(8.0)                       # baseline: A alone

    phase[0] = "A+B"                      # now start the window download
    tb = threading.Thread(target=reader,
                          args=("B-dl", WIN, None, None, stop_b, 400),
                          daemon=True)
    tb.start()
    time.sleep(20.0)                      # download runs ~20 s at full speed

    phase[0] = "A-after-B"
    stop_b.set()
    tb.join(timeout=5)
    time.sleep(12.0)                      # does A recover after B closes?

    stop_a.set()
    ta.join(timeout=5)

    print(f"{'t':>6}  {'phase':<9} {'who':<6} {'MB/2s':>7}  note")
    for t, ph, tag, bucket, note in rate_log:
        mb = f"{bucket / 1048576:.2f}" if bucket is not None else "-"
        print(f"{t - t0:6.1f}  {ph:<9} {tag:<6} {mb:>7}  {note}")


if __name__ == "__main__":
    sys.exit(main())
