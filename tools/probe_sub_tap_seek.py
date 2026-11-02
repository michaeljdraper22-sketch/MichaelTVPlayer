# -*- coding: utf-8 -*-
"""Headless repro: does the subtitle tap survive a SEEK (window rebase)?

Simulates VLC's seek without any player: reader A pulls the relay
sequentially; at --seek-at seconds reader B opens with a Range at
--seek-frac of the file (VLC's seek connection) and takes over; reader
A closes shortly after (VLC drops the old connection). Cues should
RESUME from the new region within seconds — the 2026-11-02 00:08 Love
Is Blind E06 night says they don't.

Run: .venv\\Scripts\\python.exe tools\\probe_sub_tap_seek.py <url>
        [seek_at_s] [seek_frac] [run_s]
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
SEEK_AT = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
SEEK_FRAC = float(sys.argv[3]) if len(sys.argv) > 3 else 0.6
RUN_S = float(sys.argv[4]) if len(sys.argv) > 4 else 55.0
RATE = 4.0 * (1 << 20)          # bytes/s pull


def main():
    app = QtWidgets.QApplication([])
    cues = []
    relay = VodRelay()
    relay.cue.connect(lambda s, e, t: cues.append((time.time(), s, e, t)))
    relay.failed.connect(lambda m: print("FAILED signal:", m))
    t0 = time.time()
    local = relay.start(URL, USER_AGENT)
    assert local, "relay refused"
    print("relay up. total=%.2f GiB  seek target=%.0f%%"
          % (relay.total / 2**30, SEEK_FRAC * 100))

    stop = threading.Event()
    a_done = threading.Event()

    def reader(url, tag, done=None):
        try:
            r = urllib.request.urlopen(url, timeout=30)
            while not stop.is_set():
                b = r.read(1 << 18)
                if not b:
                    break
                time.sleep((1 << 18) / RATE)
            r.close()
        except Exception as exc:      # noqa: BLE001
            print("reader %s ended: %r" % (tag, exc))
        finally:
            if done:
                done.set()

    # reader A: sequential from 0 (VLC's main connection)
    ta = threading.Thread(target=reader, args=(local, "A"), daemon=True)
    ta.start()

    seek_opened = [False]

    def seeker():
        time.sleep(SEEK_AT)
        target = int(relay.total * SEEK_FRAC)
        url = local + ""  # plain GET; the handler asks US for the range
        # urllib can't send a Range on a plain urlopen — build a request
        req = urllib.request.Request(
            local, headers={"Range": "bytes=%d-" % target})
        try:
            r = urllib.request.urlopen(req, timeout=30)
        except Exception as exc:
            print("seek GET failed: %r" % exc)
            return
        seek_opened[0] = True
        print("t+%.0fs SEEK connection open at byte %d (%.0f%%)"
              % (time.time() - t0, target, SEEK_FRAC * 100))
        try:
            while not stop.is_set():
                b = r.read(1 << 18)
                if not b:
                    break
                time.sleep((1 << 18) / RATE)
            r.close()
        except Exception as exc:      # noqa: BLE001
            print("seek reader ended: %r" % exc)

    ts = threading.Thread(target=seeker, daemon=True)
    ts.start()

    # reader A dies 5 s after the seek (VLC drops the old connection)
    def a_reaper():
        time.sleep(SEEK_AT + 5.0)
        stop.clear()
        # nothing to do — A stops on its own via stop; emulate the close
        # by setting a flag A checks... simplest: just note it
    # (A keeps reading; closing it is the stop below)

    last = 0
    last_t = t0
    while time.time() - t0 < RUN_S:
        time.sleep(3)
        app.processEvents()
        now = time.time()
        made = len(cues) - last
        rate_s = now - last_t
        last, last_t = len(cues), now
        p = relay._parser
        print("t+%4.0fs base=%6dM size=%5dM sel=%r cues=%d (+%d/%.0fs) "
              "alive=%r parsed=%dM"
              % (now - t0, relay.cache_base >> 20, relay.cache_size >> 20,
                 getattr(p, "_selected", None), len(cues), made, rate_s,
                 relay._alive, relay._tap_parsed >> 20))
    stop.set()
    time.sleep(1.0)
    post = [c for c in cues if c[0] > t0 + SEEK_AT + 8.0]
    print("cues after the seek settled: %d" % len(post))
    if post:
        print("first post-seek cue @t+%.0fs: %r"
              % (post[0][0] - t0, post[0][3][:50]))
        print("last  cue time-axis: %.0f-%.0f s" % (post[-1][1], post[-1][2]))
    else:
        print("NO CUES AFTER SEEK — tap starved by the rebase")
    p = relay._parser
    print("parser autopsy: selected=%r saw_tracks=%r stack=%r skip=%d "
          "buf=%d cluster_tc=%d cues_internal=%d track_meta=%d"
          % (getattr(p, "_selected", None), getattr(p, "_saw_tracks", None),
             getattr(p, "_stack", None), getattr(p, "_skip", -1),
             len(getattr(p, "buf", b"")), getattr(p, "_cluster_tc", -1),
             len(getattr(p, "cues", [])),
             len(getattr(p, "_track_meta", {}) or {})))
    from src.mkv_subs import _vint_size
    b = getattr(p, "buf", b"")
    if b:
        print("buffer head:", bytes(b[:48]).hex(" "))
        eid, ilen = _vint_size(b, 0, keep_marker=True)
        sz = slen = None
        if eid is not None:
            sz, slen = _vint_size(b, ilen, keep_marker=False)
        print("first element decode: eid=%#x ilen=%s size=%s slen=%s "
              "avail=%d" % (eid if eid is not None else -1, ilen, sz, slen,
                            len(b) - (ilen or 0) - (slen or 0)))
    relay.stop()
    return 0 if post else 1


if __name__ == "__main__":
    if not URL:
        print(__doc__)
        sys.exit(2)
    sys.exit(main())
