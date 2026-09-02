# -*- coding: utf-8 -*-
"""Live-CDN connection-lifetime probe (diagnosis for the 2026-09-01
Incredibles mid-movie freezes).

Hypothesis: the debridio -> energycdn (premiumize) redirect hands out a
signed URL whose long-lived streaming GET gets killed at a fixed age
(~600 s). The user's two freezes both landed ~9.5-9.8 min into playback,
while the app's relay holds exactly ONE provider connection open for the
whole movie (vod_splitter._ProviderStream).

This streams the REAL provider URL at ~1x realtime (375 KB/s, the
movie's own bitrate) with the app's User-Agent and reads until the
connection dies or 12 minutes pass, logging age + byte position at death.
Traffic equals ~10 minutes of watching the same movie the user already
streamed twice tonight — no extra account load beyond that.

Usage: python cdn_probe.py <url>
"""
import sys
import time
import urllib.request

URL = sys.argv[1]
RATE = 375 * 1024            # ~1x realtime for the 2.58 GB / 115 min rip
CHUNK = 16384
MAX_S = 720                   # give up at 12 min

t0 = time.time()
total = 0
req = urllib.request.Request(
    URL, headers={"User-Agent": "MichaelTVPlayer/1.0",
                  "Range": "bytes=0-"})
last_note = 0.0
try:
    r = urllib.request.urlopen(req, timeout=30)
    print("%s opened status=%s len=%s cr=%s" % (
        time.strftime("%H:%M:%S"), getattr(r, "status", "?"),
        r.headers.get("Content-Length"), r.headers.get("Content-Range")),
        flush=True)
    while True:
        data = r.read(CHUNK)
        if not data:
            print("%s CLEAN EOF after %d bytes, %.1f s" % (
                time.strftime("%H:%M:%S"), total, time.time() - t0),
                flush=True)
            break
        total += len(data)
        # pace to ~1x realtime so the connection ages like real playback
        target = total / RATE
        while True:
            behind = target - (time.time() - t0)
            if behind <= 0:
                break
            time.sleep(min(0.05, behind))
        now = time.time() - t0
        if now - last_note >= 30.0:
            last_note = now
            print("%s alive: %.0f s, %d bytes" % (
                time.strftime("%H:%M:%S"), now, total), flush=True)
        if now >= MAX_S:
            print("%s SURVIVED past %.0f s (%d bytes) — no connection "
                  "cap observed" % (time.strftime("%H:%M:%S"), now, total),
                  flush=True)
            break
except Exception as exc:  # noqa: BLE001
    print("%s DIED after %d bytes, %.1f s: %r" % (
        time.strftime("%H:%M:%S"), total, time.time() - t0, exc), flush=True)
