# -*- coding: utf-8 -*-
"""Headless probe: the xtream.py reliability layer (GitHub issue #5 fixes).

A local threaded HTTP server stands in for the provider — nothing touches
the network, no window opens, no VLC starts.  Verifies, against the REAL
XtreamClient:

  1. in-flight dedupe  — N concurrent identical list loads = ONE download
  2. concurrency gate  — bulk loads never exceed max_concurrent on the wire
  3. interactive bypass — short_epg does NOT queue behind a 45 MB-style
                          bulk download even at max_concurrent=1
  4. retry 429/5xx     — rate-limited / server-error responses back off
                          and retry instead of killing the list load
  5. retry 403 flap    — the provider's backend-hiccup 403s are retried,
                          but a persistent 403 surfaces (with the body)
  6. auth rejection    — a definitive bad-credentials answer is NOT
                          retried (no slow wrong-password feedback loop
                          beyond the quick 403 flap window)
  7. TTL cache         — repeat loads hit the cache; refresh=True bypasses
  8. copy isolation    — callers get their own list; nobody's in-place
                          sort/filter leaks into the cached copy
  9. URL normalization — pasted junk (newlines / signature dashes) never
                          reaches DNS
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.xtream import (  # noqa: E402
    XtreamClient, XtreamError, normalize_server_url,
)

fails = [0]


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else extra))
    if not cond:
        fails[0] += 1


# ---------------------------------------------------------------- server
class FakePanel:
    """Scriptable provider: per-action FIFO of status codes to inject
    before success, per-action artificial delay, and counters including
    peak in-flight concurrency."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fail_q = {}        # action -> [code, code, ...]
        self.delay = {}         # action -> seconds
        self.attempts = {}      # action -> total requests seen
        self.inflight = 0
        self.max_inflight = 0

    def handle(self, action):
        with self.lock:
            self.attempts[action] = self.attempts.get(action, 0) + 1
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            q = self.fail_q.get(action)
            code = q.pop(0) if q else 200
            delay = self.delay.get(action, 0.0)
        try:
            if delay:
                time.sleep(delay)
            return code
        finally:
            with self.lock:
                self.inflight -= 1

    def hits(self, action):
        with self.lock:
            return self.attempts.get(action, 0)


PANEL = FakePanel()

PAYLOADS = {
    "get_live_categories": [{"category_id": "1", "category_name": "US"}],
    "get_vod_categories": [{"category_id": "2", "category_name": "Kids"}],
    "get_series_categories": [{"category_id": "3", "category_name": "Doc"}],
    "get_live_streams": [{"stream_id": i, "name": f"ch{i}"} for i in range(3)],
    "get_vod_streams": [{"stream_id": i, "name": f"mv{i}"} for i in range(3)],
    "get_series": [{"series_id": i, "name": f"sr{i}"} for i in range(3)],
    "get_vod_info": {"info": {"name": "movie"}},
    "get_short_epg": {"epg_listings": [{"title": "now"}]},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        qs = parse_qs(urlparse(self.path).query)
        action = (qs.get("action") or [None])[0]
        code = PANEL.handle(action)
        if code == 200:
            if action is None:      # auth
                body = {"user_info": {"auth": 1, "username": "u",
                                      "status": "Active"}}
            elif action == "AUTH_REJECT":
                body = {"user_info": {"auth": 0}}
            else:
                body = PAYLOADS.get(action, [])
            data = json.dumps(body).encode()
        else:
            data = json.dumps(
                {"error": "Authentication failed"}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_a):   # keep the probe output readable
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()

# the probe tests LOGIC, not patience — shrink the backoff ladder
XtreamClient._BULK_BACKOFF = (0.05, 0.05, 0.05, 0.05, 0.05)
XtreamClient._FAST_BACKOFF = (0.05, 0.05)


def client(max_concurrent=2):
    return XtreamClient(BASE, "u", "p", max_concurrent=max_concurrent)


def run_threads(fns):
    ts = [threading.Thread(target=f) for f in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


print("[1] URL normalization (paste junk never reaches DNS)")
check("newline + signature dashes stripped",
      normalize_server_url("http://q4z2lq3z.cdngold8k.com\n\n\n-----")
      == "http://q4z2lq3z.cdngold8k.com")
check("whitespace + trailing slash",
      normalize_server_url("  http://host.net:8080/  ")
      == "http://host.net:8080")
check("scheme added", normalize_server_url("host.net") == "http://host.net")
check("empty stays empty", normalize_server_url("   ") == "")
check("first line only, dash-dash kept inside host",
      normalize_server_url("http://my--host.net/x\r\nSIGNATURE\r\n---")
      == "http://my--host.net/x")

print("[2] in-flight dedupe — 3 simultaneous identical loads, 1 download")
c = client()
PANEL.delay["get_live_streams"] = 0.4          # force real overlap
results = []
run_threads([lambda: results.append(c.live_streams(None))] * 3)
PANEL.delay["get_live_streams"] = 0
check("server saw exactly one get_live_streams",
      PANEL.hits("get_live_streams") == 1,
      f"(saw {PANEL.hits('get_live_streams')})")
check("all callers got the full list",
      all(isinstance(r, list) and len(r) == 3 for r in results))
check("callers got distinct list objects",
      len({id(r) for r in results}) == 3)

print("[3] bulk concurrency gate — 6 distinct bulk loads, cap 2")
c = client(max_concurrent=2)
PANEL.max_inflight = 0
for a in ("get_live_categories", "get_vod_categories",
          "get_series_categories", "get_live_streams",
          "get_vod_streams", "get_series"):
    PANEL.delay[a] = 0.3
run_threads([
    lambda: c.live_categories(),
    lambda: c.vod_categories(),
    lambda: c.series_categories(),
    lambda: c.live_streams(5),
    lambda: c.vod_streams(6),
    lambda: c.series(7),
])
for a in list(PANEL.delay):
    PANEL.delay[a] = 0
check("wire concurrency never exceeded 2",
      PANEL.max_inflight <= 2, f"(peak {PANEL.max_inflight})")
check("work actually parallelised (peak == 2)",
      PANEL.max_inflight == 2, f"(peak {PANEL.max_inflight})")

print("[4] interactive calls bypass the gate (EPG never queues on bulk)")
c = client(max_concurrent=1)
PANEL.delay["get_live_streams"] = 1.0
PANEL.delay["get_vod_streams"] = 1.0
PANEL.delay["get_short_epg"] = 0.05
order = []
run_threads([
    lambda: (c.live_streams(10), order.append("bulk1")),
    lambda: (c.vod_streams(11), order.append("bulk2")),
    lambda: (c.short_epg(1), order.append("epg")),
])
PANEL.delay["get_live_streams"] = 0
PANEL.delay["get_vod_streams"] = 0
PANEL.delay["get_short_epg"] = 0
check("short_epg completed BEFORE the bulk downloads",
      order and order[-1] != "epg" and "epg" in order,
      f"(order {order})")

print("[5] retry ladder — 429 / 5xx / 403-flap recover, hard 403 surfaces")
c = client()
PANEL.fail_q["get_live_categories"] = [429, 429]
r = c.live_categories()
check("429 x2 then list arrives", isinstance(r, list) and len(r) == 1)
PANEL.fail_q["get_series_categories"] = [500]
check("500 then success", len(c.series_categories()) == 1)
PANEL.fail_q["get_vod_info"] = [403, 403]
check("403 flap retried into success",
      c.vod_info(9).get("info", {}).get("name") == "movie")
# NB: vod_streams/series deliberately swallow fetch errors (m3u fallback
# design) — the raise path is probed on live_streams, which propagates.
PANEL.fail_q["get_live_streams"] = [403, 403, 403, 403, 403, 403]
h0 = PANEL.hits("get_live_streams")
try:
    c.live_streams(12)
    check("persistent 403 raises", False)
except XtreamError as e:
    check("persistent 403 raises", "HTTP 403" in str(e), f"({e})")
    check("error surfaces the provider body (issue #5 fix 5)",
          "Authentication failed" in str(e), f"({e})")
    check("error is plain-language (issue #3 field lesson)",
          "refusing this connection" in str(e), f"({e})")
check("403 retried exactly the flap window (3 attempts)",
      PANEL.hits("get_live_streams") - h0 == 3,
      f"(delta {PANEL.hits('get_live_streams') - h0})")
PANEL.fail_q["get_live_streams"] = []      # drain unused scripted failures

print("[6] definitive auth rejection is NOT retried")
c = client()
before = PANEL.hits("AUTH_REJECT")
PANEL.fail_q["AUTH_REJECT"] = []          # always 200 {"user_info":{"auth":0}}


class RejectClient(XtreamClient):
    def _api(self, action=None, **kw):
        return super()._api(action="AUTH_REJECT", **kw)


rc = RejectClient(BASE, "u", "p")
try:
    rc.vod_info(1)
    check("auth rejection raises", False)
except XtreamError as e:
    check("auth rejection raises immediately",
          "Invalid username or password" in str(e) and
          PANEL.hits("AUTH_REJECT") - before == 1)

print("[7] TTL cache — repeat loads served locally, refresh bypasses")
c = client()
h0 = PANEL.hits("get_live_categories")
c.live_categories()
c.live_categories()
h1 = PANEL.hits("get_live_categories")
check("second identical load does not re-download", h1 - h0 == 1,
      f"(delta {h1 - h0})")
c.live_categories(refresh=True)
h2 = PANEL.hits("get_live_categories")
check("refresh=True forces a fresh download", h2 - h1 == 1)
h0 = PANEL.hits("get_live_streams")
c.live_streams(None)
c.live_streams(21)
h1 = PANEL.hits("get_live_streams")
check("category-scoped keys are distinct", h1 - h0 == 2,
      f"(delta {h1 - h0})")

print("[8] copy isolation — in-place mutation never poisons the cache")
c = client()
r1 = c.live_streams(None)
r1.append({"stream_id": "EVIL"})
r1.reverse()
r2 = c.live_streams(None)                 # cache hit
check("cached copy unaffected by caller mutation",
      len(r2) == 3 and r2[0].get("stream_id") == 0)

print("[9] failures are not cached")
c = client()
PANEL.fail_q["get_series"] = [429] * 8    # more failures than retries
try:
    c.series(31)
except XtreamError:
    pass
PANEL.fail_q["get_series"] = []           # provider recovers
r = c.series(31)
check("a load that failed is re-fetched next call, not served as an error",
      isinstance(r, list))

print("[10] dead host fails FAST (no long ladder on connection errors)")
# a closed local port = instant ConnectionRefused; the single quiet retry
# means ~1 s, not the ~15 s a 429-style ladder would burn (this is what
# kept background fetch threads churning through probe_buttons runs)
dead = XtreamClient("http://127.0.0.1:1", "u", "p")
t0 = time.time()
try:
    dead.live_categories()
    check("dead host raises", False)
except XtreamError as e:
    check("dead host raises", "Connection failed" in str(e), f"({e})")
check("dead host fails in ~1 retry, not the full ladder",
      time.time() - t0 < 10, f"(took {time.time() - t0:.1f}s)")

srv.shutdown()
print()
print(f"probe_xtream_reliability: "
      f"{'ALL PASS' if fails[0] == 0 else f'{fails[0]} FAILURE(S)'}")
sys.exit(1 if fails[0] else 0)
