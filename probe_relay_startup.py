# -*- coding: utf-8 -*-
"""Headless startup timeline for the VOD relay (adoption gate for the
Stremio parallel-tail mode).

Measures, in one process:
  probe/start, relay ready, tail ready, first served byte, seek-index
  GET latency, and (optionally) VLC's time to ``playing`` / first media
  time with --vout=dummy --no-audio (no window, no audio, no focus).

Usage:
  .venv\\Scripts\\python.exe probe_relay_startup.py                 # last URL in player.log, parallel
  .venv\\Scripts\\python.exe probe_relay_startup.py --serial        # baseline (IPTV path)
  .venv\\Scripts\\python.exe probe_relay_startup.py --url <resolve-url>
  .venv\\Scripts\\python.exe probe_relay_startup.py --vlc           # also time VLC to first frame
  .venv\\Scripts\\python.exe probe_relay_startup.py --runs 3        # repeat, print each run + median

Exit status: 0 = every phase succeeded; 2 = relay could not start;
1 = a phase timed out (tail / index / VLC).
"""
import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore  # noqa: E402

from src import vod_splitter  # noqa: E402
from src.player import VLCPlayer  # noqa: E402
from src.vod_splitter import VodRelay  # noqa: E402

UA = "MichaelTVPlayer/1.0"
_LOG_DIR = os.path.join(os.environ.get("APPDATA")
                        or os.path.expanduser("~"), "MichaelTVPlayer")
_LOG = os.path.join(_LOG_DIR, "player.log")
_URL_RE = re.compile(
    r"vod splitter: (https?://\S+) -> 127\.0\.0\.1:\d+ \(total=")


def last_logged_url():
    try:
        with open(_LOG, "r", encoding="utf-8", errors="replace") as f:
            for line in reversed(f.readlines()[-4000:]):
                m = _URL_RE.search(line)
                if m:
                    return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return ""


def one_run(url, parallel, with_vlc):
    marks = {}
    t0 = time.monotonic()

    def el():
        return time.monotonic() - t0

    relay = VodRelay(parallel_tail=parallel)
    local = relay.start(url, UA, "eng", 0)
    marks["relay_start"] = el()
    if not local:
        return marks, None, None

    done = threading.Event()

    def poll():
        while not done.is_set() and el() < 60:
            if "ready" not in marks and relay._ready.is_set():
                marks["ready"] = el()
            if "tail_ready" not in marks and relay._tail_ready.is_set():
                marks["tail_ready"] = el()
            if "ready" in marks and "tail_ready" in marks:
                return
            time.sleep(0.01)
    threading.Thread(target=poll, daemon=True).start()

    def get(rng):
        req = urllib.request.Request(local, headers={"Range": rng,
                                                     "User-Agent": "VLC/3.0.24"})
        t = time.monotonic()
        with urllib.request.urlopen(req, timeout=40) as r:
            data = r.read()
            st = r.status
        return st, data, time.monotonic() - t

    status, head, dt = get("bytes=0-65535")
    marks["first_byte"] = el()
    marks["head_ok"] = (status == 206 and len(head) >= 65536)
    marks["first_byte_wait"] = dt

    if relay.total:
        a = max(0, relay.total - 1000)
        try:
            st, idx, dt_idx = get("bytes=%d-" % a)
            marks["index_elapsed"] = dt_idx
            marks["index_ok"] = st == 206 and len(idx) == 1000
        except Exception as exc:  # noqa: BLE001
            marks["index_error"] = repr(exc)

    vlc = None
    if with_vlc:
        try:
            vlc = VLCPlayer(timeshift=False, volume=100,
                            extra_args=["--vout=dummy", "--no-audio"])
            vlc.play(local)
            t_v = time.monotonic()
            while time.monotonic() - t_v < 30:
                time.sleep(0.05)
                if "vlc_playing" not in marks and vlc.state_name() == "playing":
                    marks["vlc_playing"] = el()
                if vlc.player.get_time() > 0:
                    marks["vlc_first_pos"] = el()
                    break
        finally:
            if vlc is not None:
                vlc.stop()

    done.set()
    marks["provider_opens"] = relay.provider_opens
    marks["tail_bytes"] = len(relay._tail)
    marks["total"] = relay.total
    marks["container"] = relay._container
    relay.stop()
    return marks, relay, local


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--serial", action="store_true",
                    help="parallel_tail=False (IPTV baseline)")
    ap.add_argument("--vlc", action="store_true",
                    help="also time VLC to playing/first frame (silent)")
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()

    url = args.url or last_logged_url()
    if not url:
        print("no URL: pass --url or play a Stremio stream once")
        return 2
    mode = "serial" if args.serial else "parallel"
    print("mode=%s runs=%d url=%s...(%d chars)"
          % (mode, max(1, args.runs), url[:60], len(url)))

    QtCore.QCoreApplication([])
    rows = []
    for i in range(max(1, args.runs)):
        marks, _relay, _local = one_run(url, not args.serial, args.vlc)
        if marks.get("relay_start") is None or not _local:
            print("relay.start failed (probe/container refused) — dead or "
                  "non-MKV/MP4 URL?")
            return 2
        rows.append(marks)
        print("run %d: start=%.2f ready=%s tail=%s first_byte=%s "
              "index=%s vlc_playing=%s vlc_pos=%s opens=%s tail_bytes=%s"
              % (i + 1, marks["relay_start"],
                 _f(marks.get("ready")), _f(marks.get("tail_ready")),
                 _f(marks.get("first_byte")),
                 _f(marks.get("index_elapsed")),
                 _f(marks.get("vlc_playing")),
                 _f(marks.get("vlc_first_pos")),
                 marks.get("provider_opens"), marks.get("tail_bytes")))
        ok = (marks.get("ready") is not None
              and marks.get("head_ok")
              and marks.get("index_ok", True))
        if not ok:
            print("phase failure:", json.dumps(
                {k: v for k, v in marks.items() if "error" in k
                 or k in ("ready", "head_ok", "index_ok")}, default=str))
            return 1
        time.sleep(0.5)

    if len(rows) > 1:
        for key in ("relay_start", "ready", "tail_ready", "first_byte",
                    "index_elapsed", "vlc_playing", "vlc_first_pos"):
            vals = [r[key] for r in rows if key in r]
            if vals:
                print("%-14s median=%.2fs  min=%.2f max=%.2f"
                      % (key, statistics.median(vals), min(vals), max(vals)))
    return 0


def _f(v):
    return "-" if v is None else "%.2f" % v


if __name__ == "__main__":
    sys.exit(main())
