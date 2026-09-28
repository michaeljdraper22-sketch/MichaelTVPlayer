# -*- coding: utf-8 -*-
"""Live headless repro of the 2026-09-03 21:26 Adventure Time glitching.

The user saw heavy visual glitches on panel series (MP4/H.264) AND Stremio
(MKV/H.265) through the vod splitter, with a silent player.log (no provider
body deaths, no watchdog rescues). This probe replays the exact URL through
a REAL VodRelay (offscreen, no UI) with:

  1. a VLC-shaped client: head probe, tail (seek-index) GET concurrent with
     the main sequential GET — hashing every byte served by the relay,
  2. the bundled VLC playing the relay URL headlessly with the app's exact
     instance options (--network-caching=15000, --avcodec-skiploopfilter=1,
     -V dummy --no-audio), RC `stats` snapshots + -vv stderr captured,
  3. post-hoc direct provider fetches of every hashed segment — any hash
     mismatch = the relay served wrong bytes (= the garble).

Pure HTTP + dummy-vout VLC: no window, no audio, nothing visible.
"""
import hashlib
import os
import subprocess
import sys
import threading
import time
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging  # noqa: E402
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

from src.vod_splitter import VodRelay  # noqa: E402

# The panel S03E17 'Thank You' episode the user first complained on
# (kind=series, 21:26:37, cf.534842.xyz, H.264 MP4, total=118455833).
URL = "http://cf.534842.xyz/series/726352471c/d809266e91/57520.mp4"
UA = "MichaelTVPlayer/1.0"
TOTAL = 118_455_833

SEG = 8 << 20            # hash-compare granularity
MAIN_BUDGET_S = 60.0     # how long the main GET reads
VLC_RUNTIME_S = 75.0     # --run-time for the bundled VLC

VLC_EXE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "dist", "vlc", "vlc.exe")


def get_range(url, a, b=None, timeout=30):
    rng = f"bytes={a}-" if b is None else f"bytes={a}-{b}"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Range": rng})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def main():
    t_start = time.monotonic()
    relay = VodRelay()
    local = relay.start(URL, UA)
    if not local:
        print("FAIL: relay refused to start (not mkv/mp4?)")
        return 2
    print(f"relay up: {local} total={relay.total}")
    if not relay._ready.wait(40):
        print("FAIL: relay startup never became ready")
        relay.stop()
        return 2
    print(f"relay ready: base={relay.cache_base} cached={relay.cache_size} "
          f"total={relay.total}")

    # ---- 2. bundled VLC on the relay URL, headless, app's options ----
    vlc_log = open("probe_advtime_vlc.log", "wb")
    vlc_err = open("probe_advtime_vlc.err", "wb")
    vlc = subprocess.Popen(
        [VLC_EXE, "-I", "rc", "-vv",
         "--ignore-config", "--no-video-title-show", "--stats",
         "--network-caching=15000", "--live-caching=15000",
         "--file-caching=1000", "--disc-caching=1000",
         "--avcodec-skiploopfilter=1",
         "--no-audio", "-V", "dummy",
         "--run-time", str(VLC_RUNTIME_S), "vlc://quit",
         local],
        stdin=subprocess.PIPE, stdout=vlc_log, stderr=vlc_err,
        creationflags=subprocess.CREATE_NO_WINDOW)

    def stats_pump():
        try:
            while vlc.poll() is None:
                vlc.stdin.write(b"stats\n")
                vlc.stdin.flush()
                time.sleep(5.0)
        except Exception:
            pass
    threading.Thread(target=stats_pump, daemon=True).start()

    # ---- 1. VLC-shaped client through the same relay ----
    results = {"head": None, "tail": None, "segments": []}  # (a,b,relay_h)

    head = get_range(local, 0, 1023)
    results["head"] = (0, len(head), hashlib.sha1(head).hexdigest())
    print(f"head probe: {len(head)} bytes")

    def tail_fetch():
        time.sleep(2.5)   # VLC asks for the index after starting the body
        try:
            tl = get_range(local, TOTAL - 2_411_724)
            results["tail"] = (TOTAL - len(tl), len(tl),
                               hashlib.sha1(tl).hexdigest())
            print(f"tail fetch: {len(tl)} bytes")
        except Exception as exc:
            print(f"tail fetch failed: {exc!r}")
    threading.Thread(target=tail_fetch, daemon=True).start()

    seg_a, seg_h = 0, hashlib.sha1()
    got = 0
    t0 = time.monotonic()
    marks = []
    try:
        req = urllib.request.Request(local, headers={"User-Agent": UA,
                                                     "Range": "bytes=0-"})
        with urllib.request.urlopen(req, timeout=60) as r:
            while time.monotonic() - t0 < MAIN_BUDGET_S:
                chunk = r.read(1 << 18)
                if not chunk:
                    print(f"main GET: clean EOF at {got}")
                    break
                seg_h.update(chunk)
                got += len(chunk)
                while got - seg_a >= SEG:
                    # close out the finished segment (may straddle: rehash
                    # from the boundary is impossible on a stream — so
                    # segments are cut exactly on read boundaries)
                    results["segments"].append(
                        (seg_a, got, seg_h.copy().hexdigest()))
                    print(f"segment [{seg_a}, {got}) hashed "
                          f"({got - seg_a} bytes, "
                          f"{(got)/1048576/(time.monotonic()-t0):.1f} MB/s)")
                    seg_a = got
                    seg_h = hashlib.sha1()
                marks.append((round(time.monotonic() - t0, 1), got))
    except Exception as exc:
        print(f"main GET died at {got}: {exc!r}")
    if got > seg_a:
        results["segments"].append((seg_a, got, seg_h.copy().hexdigest()))
        print(f"segment [{seg_a}, {got}) hashed (final)")
    print(f"main GET total: {got} bytes in {time.monotonic()-t0:.1f}s")

    # let VLC finish its budget, then quit
    try:
        vlc.wait(timeout=max(5.0, VLC_RUNTIME_S + 20))
    except Exception:
        vlc.kill()
    vlc_log.close(); vlc_err.close()
    relay.stop()

    # ---- 3. direct provider fetches vs the relay-served hashes ----
    bad = 0
    print("\n== direct-vs-relay verification ==")
    try:
        a, n, h = results["head"]
        d = get_range(URL, a, a + n - 1)
        ok = hashlib.sha1(d).hexdigest() == h
        print(f"head   [{a}+{n}]  {'OK' if ok else 'MISMATCH'}")
        bad += 0 if ok else 1
    except Exception as exc:
        print(f"head direct fetch failed: {exc!r}")
        bad += 1
    if results["tail"]:
        try:
            a, n, h = results["tail"]
            d = get_range(URL, a, a + n - 1)
            ok = hashlib.sha1(d).hexdigest() == h
            print(f"tail   [{a}+{n}]  {'OK' if ok else 'MISMATCH'}")
            bad += 0 if ok else 1
        except Exception as exc:
            print(f"tail direct fetch failed: {exc!r}")
            bad += 1
    for a, b, h in results["segments"]:
        try:
            d = get_range(URL, a, b - 1)
            ok = hashlib.sha1(d).hexdigest() == h
            print(f"body   [{a}, {b})  {'OK' if ok else 'MISMATCH'}")
            bad += 0 if ok else 1
        except Exception as exc:
            print(f"body [{a},{b}) direct fetch failed: {exc!r}")
            bad += 1

    print(f"\nVERDICT: {'CLEAN — relay bytes verified identical' if bad == 0 else f'{bad} MISMATCHES — relay served wrong bytes'}")
    print(f"(vlc rc log: probe_advtime_vlc.log, stderr: probe_advtime_vlc.err)")
    print(f"elapsed {time.monotonic()-t_start:.0f}s")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
