# -*- coding: utf-8 -*-
"""Regression: the profanity filter must see each cue BEFORE its bytes
reach the player (parse-before-serve gate, 2026-10-22 incident).

The shape of the field report: a Stremio movie rode a near-realtime CDN,
so the cache frontier hugged VLC's read position. Provider chunks went
to VLC the instant they landed in the cache while the subtitle tap's
poll+parse lagged behind — two S-words played clean, their mute windows
armed only AFTER playback had passed them; the rewind replay muted
(because by then the windows existed) which is why "turning subtitles on
and rewinding" seemed to fix it.

These tests pin the fix's contract:

  [A] INVARIANT — with a dripping (near-realtime) provider, at the
      moment the client has received N bytes, the tap has parsed through
      N. VLC can only play what it has received, so every cue is emitted
      (mute window armed) before its words can possibly play.
  [B] NEGATIVE CONTROL — the same pull with the gate neutered on a
      fresh relay VIOLATES the invariant (the test can see the old bug).
  [C] DEAD-TAP SAFETY — a frozen tap must never stall playback: the
      gate's bounded timeout serves ungated, bytes stay identical.
  [D] REBASE — a forward seek past the frontier (cache window rebase)
      keeps the invariant on the new window's absolute offsets.

Run:  .venv\\Scripts\\python.exe test_vod_tap_gate.py
"""
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.profanity import find_ffmpeg  # noqa: E402
from src.vod_splitter import VodRelay  # noqa: E402

PASS, FAIL = [], []

FF = find_ffmpeg()
MEDIA = os.path.abspath("build/tap_gate_test.mkv").replace("\\", "/")
SRT_IN = os.path.abspath("build/tap_gate_test_in.srt").replace("\\", "/")

DRIP_BYTES = 32 << 10        # per provider write
DRIP_SLEEP = 0.012           # ~21 Mbps: comfortably realtime for the
#                             # fixture's bitrate, so the frontier rides
#                             # the consumer instead of racing ahead


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else " FAIL ") + name +
          ("   [" + detail + "]" if detail else ""), flush=True)


def build_media():
    """~4.7 MB CBR MKV (noise keeps byte offsets predictable) with cues
    spread across the file, including one deep inside what becomes the
    tail-prefetch region (its bytes reach the client only via _tail or a
    fully-caught-up cache — the gate's uncovered-by-design edge)."""
    os.makedirs("build", exist_ok=True)
    if os.path.exists(MEDIA) and os.path.getsize(MEDIA) > 3_000_000:
        return
    with open(SRT_IN, "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,000\n"
                "what the hell is this\n\n"
                "2\n00:00:20,000 --> 00:00:22,000\n"
                "clean as snow\n\n"
                "3\n00:00:40,000 --> 00:00:42,000\n"
                "damn dogs everywhere\n\n"
                "4\n00:01:20,000 --> 00:01:22,000\n"
                "late tail gamma\n")
    subprocess.run(
        [FF, "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=steelblue:s=256x144:d=90:r=10",
         "-i", SRT_IN,
         "-vf", "noise=alls=18:allf=t",
         "-map", "0:v", "-map", "1:s", "-c:v", "libx264",
         "-preset", "ultrafast",
         "-b:v", "420k", "-minrate", "420k", "-maxrate", "420k",
         "-bufsize", "84k", "-x264-params", "nal-hrd=cbr",
         "-c:s", "srt", MEDIA],
        check=True, timeout=300, creationflags=0x08000000)


class _DripHandler(BaseHTTPRequestHandler):
    """A provider that behaves like the field's near-realtime CDN: Range
    support plus a metered body. The frontier can only advance at DRIP
    rate, which is what made the pre-gate tap lag visible."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        blob = self.server.blob
        rng = self.headers.get("Range") or ""
        a, b = 0, len(blob) - 1
        if rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            a_s, _, b_s = spec.partition("-")
            if a_s:
                a = int(a_s)
            if b_s:
                b = int(b_s)
        self.send_response(206)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Content-Range", f"bytes {a}-{b}/{len(blob)}")
        self.send_header("Content-Length", str(b - a + 1))
        self.end_headers()
        pos = a
        try:
            while pos <= b:
                piece = blob[pos:min(pos + DRIP_BYTES, b + 1)]
                self.wfile.write(piece)
                pos += len(piece)
                time.sleep(DRIP_SLEEP)
        except Exception:
            pass


class _DripProvider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, blob):
        super().__init__(("127.0.0.1", 0), _DripHandler)
        self.blob = blob


def start_relay(app, blob, note_cues=True):
    """Relay on the drip provider; waits out the async startup."""
    prov = _DripProvider(blob)
    threading.Thread(target=prov.serve_forever, daemon=True).start()
    relay = VodRelay()
    cues = []
    if note_cues:
        relay.cue.connect(lambda s, e, t: cues.append((s, e, t)))
    local = relay.start(
        f"http://127.0.0.1:{prov.server_address[1]}/gate.mkv",
        "MichaelTVPlayer/1.0")
    assert local, "relay refused the fixture"
    deadline = time.time() + 60
    while time.time() < deadline and not relay._ready.is_set():
        app.processEvents()
        time.sleep(0.05)
    assert relay._ready.is_set() and not relay._startup_failed
    return relay, prov, local, cues


def pull_checking_gate(relay, local, start=0, chunk=48 << 10,
                       max_bytes=None, label=""):
    """Stream ``bytes=start-`` from the relay; after EVERY received chunk
    assert the tap has parsed through the received absolute end. Returns
    (body, worst_lag_bytes, violations). A violation means the handler
    handed the player bytes whose cues the filter had not seen yet.

    Offsets at/above the prefetched tail (_tail_base) are EXEMPT by
    design: those bytes are served straight from the _tail buffer and
    their cues come from the one-shot tail harvest at startup, not from
    the cache-window parse the gate tracks."""
    tail_base = relay._tail_base if relay._tail_base >= 0 else 1 << 62
    req = urllib.request.Request(local)
    if start > 0:
        req.add_header("Range", f"bytes={start}-")
    body = b""
    worst = 0
    violations = []
    exempt = 0
    with urllib.request.urlopen(req, timeout=120) as r:
        while True:
            piece = r.read(chunk)
            if not piece:
                break
            body += piece
            end_abs = start + len(body)
            parsed = relay._tap_parsed
            if parsed < end_abs:
                if end_abs > tail_base:
                    exempt += 1
                else:
                    violations.append((end_abs, parsed))
                    worst = max(worst, end_abs - parsed)
            if max_bytes is not None and len(body) >= max_bytes:
                break
    if exempt:
        print(f"    ({label or 'pull'}: {exempt} tail-buffer reads "
              "exempt — served from the startup prefetch, "
              "harvest-parsed)")
    return body, worst, violations


def main():
    assert FF, "ffmpeg not found"
    build_media()
    blob = open(MEDIA, "rb").read()
    app = QtWidgets.QApplication(sys.argv)

    print("[A] INVARIANT: tap parsed every byte before the client got it "
          "(near-realtime provider)")
    relay, prov, local, cues = start_relay(app, blob)
    t0 = time.time()
    body, worst, viol = pull_checking_gate(relay, local)
    dt = time.time() - t0
    check("[A] no ungated bytes served (0 violations)",
          not viol, f"{len(viol)} violations, worst lag {worst} B")
    check("[A] byte fidelity through the gate", body == blob,
          f"{len(body)} of {len(blob)} bytes")
    check("[A] gate did not throttle below the drip rate",
          dt < len(blob) / DRIP_BYTES * DRIP_SLEEP * 2.5 + 10,
          f"{dt:.1f}s for {len(blob)} bytes")
    deadline = time.time() + 20
    while time.time() < deadline and len(cues) < 4:
        app.processEvents()
        time.sleep(0.1)
    check("[A] all four cues still emitted (incl. the tail-region one)",
          len({c[2] for c in cues}) == 4,
          "; ".join(sorted({c[2] for c in cues}))[:80])
    relay.stop()
    prov.shutdown()
    time.sleep(0.3)

    print("[B] NEGATIVE CONTROL: the same pull with the gate neutered "
          "must violate (proves the test sees the old bug)")
    relay_b, prov_b, local_b, _cues_b = start_relay(app, blob)
    relay_b.wait_tap_parsed = lambda end_abs: None    # pre-gate behavior
    _body_b, _worst_b, viol_b = pull_checking_gate(relay_b, local_b,
                                                   chunk=8 << 10)
    check("[B] ungated relay DOES serve ahead of the tap",
          len(viol_b) > 0,
          f"{len(viol_b)} violations, worst lag {_worst_b} B")
    relay_b.stop()
    prov_b.shutdown()
    time.sleep(0.3)

    print("[C] DEAD-TAP SAFETY: a frozen tap never stalls playback")
    relay_c, prov_c, local_c, _cues_c = start_relay(app, blob)
    # freeze the tap inside its publish (it can't take _tap_gate to
    # update the high-water mark). Pull PAST the frontier so chunks
    # arrive that the frozen tap cannot publish: the gate must time out
    # and serve them ungated — bounded, bytes identical.
    holder = threading.Thread(
        target=lambda: [relay_c._tap_gate.acquire(),
                        time.sleep(3.0),
                        relay_c._tap_gate.release()],
        daemon=True)
    holder.start()
    time.sleep(0.2)               # let the freeze land
    t0 = time.time()
    body_c, _w, _v = pull_checking_gate(relay_c, local_c,
                                        max_bytes=1536 << 10)
    dt_c = time.time() - t0
    check("[C] frozen tap: bytes still flow (bounded gate timeout)",
          len(body_c) >= 1536 << 10 and body_c == blob[:len(body_c)],
          f"{len(body_c)} bytes in {dt_c:.1f}s")
    check("[C] frozen-tap pull resolves within the freeze + timeouts",
          dt_c < 15.0, f"{dt_c:.1f}s")
    holder.join(timeout=10)
    check("[C] tap unfrozen: parsing resumes and republishes",
          relay_c._tap_parsed >= 0)
    relay_c.stop()
    prov_c.shutdown()
    time.sleep(0.3)

    print("[D] REBASE: forward seek past the frontier keeps the invariant")
    relay_d, prov_d, local_d, cues_d = start_relay(app, blob)
    # pull the head, then seek past the frontier into uncached country —
    # the window rebases there; the served bytes' cues (cue 3 at 40 s,
    # ~2.1 MB in) must still be parsed before the client receives them
    head_part, _w, _v = pull_checking_gate(
        relay_d, local_d, max_bytes=1 << 20, label="D head")
    check("[D] head pull: no ungated bytes",
          not _v, f"{len(_v)} violations, worst {_w} B")
    check("[D] head pull: length + fidelity",
          len(head_part) >= 1 << 20 and head_part == blob[:len(head_part)],
          f"{len(head_part)} bytes")
    seek_at = 1_500_000      # past the ~1.1 MB frontier: a real rebase
    body_d, worst_d, viol_d = pull_checking_gate(relay_d, local_d,
                                                 start=seek_at,
                                                 max_bytes=768 << 10,
                                                 label="D seek")
    check("[D] rebased window serves gated too (0 violations)",
          not viol_d, f"{len(viol_d)} violations, worst {worst_d} B")
    check("[D] rebase byte fidelity",
          body_d == blob[seek_at:seek_at + len(body_d)])
    deadline = time.time() + 20
    while time.time() < deadline and not any(
            "damn dogs" in c[2] for c in cues_d):
        app.processEvents()
        time.sleep(0.1)
    check("[D] cues flow on the rebased window",
          any("damn dogs" in c[2] for c in cues_d))
    relay_d.stop()
    prov_d.shutdown()
    time.sleep(0.3)

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
