# -*- coding: utf-8 -*-
"""Real-VLC probe: the relay must ride through provider connection
death (the 2026-09-01 Incredibles + 2026-09-02 Paw Patrol freezes).

Live incidents (player.log):
  * 2026-09-01 22:13 + 22:22 — a kind=stremio movie (2.58 GB
    debridio/premiumize MKV) through the local VOD relay froze at
    ~9.5-9.8 MINUTES, twice, deterministically: the CDN caps connection
    AGE (~600 s), cutting the relay's ONE long-lived body mid-file.
  * 2026-09-02 13:46 — Paw Patrol S04E06 (torbox MP4) froze after a
    ~13-min PAUSE: VLC stops reading while paused, the provider reaps
    the idle connection, and the resume ran into a truncated body.

Pre-fix, vod_splitter._stream_out just broke ("provider-eof"), letting
VLC see a body that ends N % in — frozen picture, 'playing' state,
clock advancing, nothing recovers it (the stall watchdog excluded
kind=stremio and only tested a frozen clock).

The fix under test (two layers):
  * vod_splitter: the serve loop REOPENS the provider at the exact byte
    position on every mid-body death (unbounded for bodies that
    delivered; open errors / zero-byte bodies burn a small failure
    budget, then the body is CUT with a WARN — loud, not silent).
  * player_view: the VOD stall watchdog now covers kind=stremio and
    fires on frozen cumulative DEMUX bytes too (the starved shape keeps
    the clock advancing).

Harness notes (learned the hard way):
  * Each leg runs in its OWN SUBPROCESS and os._exit()s at >=85% of the
    media clock — libvlc's in-process END-OF-MEDIA teardown reliably
    access-violates in this offscreen harness (any libvlc build tried,
    with or without the relay; independent of this fix), which is the
    same reason main.py itself os._exit()s instead of tearing the
    libvlc instance down at app close. Legs therefore prove whole-file
    ride-through via the >=85% clock + relay/provider counters; the
    'ended'-state semantics themselves live in probe_jump_live.
    A child that dies mid-leg is reported as a leg failure.
  * This vlc.py binding's Media.get_stats takes a CALLER-ALLOCATED
    struct — the bare m.get_stats() call raises TypeError (silently
    swallowed by earlier runs, which is why the stats counters "never
    populated").
  * VLC never runs visibly or audibly: --vout=dummy, volume 0.

Legs:
  A — ONE age-kill mid-body: playback must ride the transparent reopen
      and play deep into the file (pre-fix: frozen at the kill).
  B — age-kills on EVERY long body: playback must ride each reopen,
      frames decoding throughout.
  C — bounded failure: the provider refuses for good after 2 kills —
      the relay must retry a BOUNDED number of times, WARN in the log,
      then cut (visible failure, not a silent freeze).
  D — the Paw Patrol scenario: pause mid-playback long enough for the
      provider to reap the IDLE body server-side, resume — playback
      must ride the reopen and keep playing deep into the file.
"""
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Bind the BUNDLED VLC runtime exactly like the shipped app does
# (main.py._setup_bundled_vlc): the probe must exercise the same build
# the user runs, not whatever system VLC happens to be on PATH (a
# system-VLC update is what made the old in-process runs start
# access-violating at end-of-media).
_BUNDLED_VLC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "dist", "vlc")
if os.path.isfile(os.path.join(_BUNDLED_VLC, "libvlc.dll")):
    os.environ["PYTHON_VLC_LIB_PATH"] = \
        os.path.join(_BUNDLED_VLC, "libvlc.dll")
    os.environ["VLC_PLUGIN_PATH"] = os.path.join(_BUNDLED_VLC, "plugins")
    try:
        os.add_dll_directory(_BUNDLED_VLC)
    except (OSError, AttributeError):
        pass

from PyQt5 import QtCore                        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "probe_stall_media.mkv")
DURATION = 150            # big enough that >=3 kill cycles land above
#                         # the tail-prefetch exemption region

# fake-provider policy (class attrs — set per leg)
KILL_S = 8.0              # a paced (streaming) body dies at this age
IDLE_KILL_S = 10.0        # a BLOCKED body (VLC paused) dies on the
#                         # socket timeout instead — the idle reap
RATE = 300_000            # bytes/s cap on the streaming body (~4x
#                         # content rate; VLC's own backpressure may
#                         # slow it further — either way kills land)
STATE = {"kills": 0, "opens": 0, "refused": 0, "idle_kills": 0,
         "kill_max": 1, "refuse_after": None}
PAUSE_S = 35.0            # LEG D pause — VLC keeps reading for seconds
#                         # after pause() while its read-ahead buffers
#                         # fill; only then does the provider body go
#                         # idle, and the socket-timeout reap lands
#                         # ~25 s in (measured). Hold 35 s.

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (" — " + str(detail) if detail else ""), flush=True)


def make_media():
    if os.path.exists(MEDIA) and os.path.getsize(MEDIA) > 100_000:
        return
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=duration={DURATION}:"
                              "size=480x270:rate=15",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}",
         "-c:v", "libx264", "-preset", "ultrafast",
         "-b:v", "500k", "-maxrate", "500k", "-bufsize", "1000k",
         "-c:a", "aac", "-b:a", "64k", "-shortest", MEDIA],
        check=True)


class ProviderHandler(BaseHTTPRequestHandler):
    """The 'CDN': range-capable, but a long streaming body (not the
    tiny probe, not the relay's tail/index prefetch — those start
    inside the last 3 MB) dies at KILL_S connection age, and a body
    BLOCKED by backpressure (VLC paused) dies on the socket timeout."""

    protocol_version = "HTTP/1.1"
    timeout = IDLE_KILL_S
    total = 0

    def log_message(self, *a):
        pass

    def _range(self):
        r = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", r)
        if not m or not (m.group(1) or m.group(2)):
            return None, None
        if m.group(1):
            return int(m.group(1)), \
                (int(m.group(2)) if m.group(2) else self.total - 1)
        return max(0, self.total - int(m.group(2))), self.total - 1

    def do_GET(self):
        STATE["opens"] += 1
        if STATE["refuse_after"] is not None \
                and STATE["kills"] >= STATE["refuse_after"]:
            STATE["refused"] += 1
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            print("    provider: REFUSED (down for good)", flush=True)
            return
        a, b = self._range()
        if a is None:
            a, b = 0, self.total - 1
        if a >= self.total:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{self.total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        b = min(b, self.total - 1)
        length = b - a + 1
        self.send_response(206)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Content-Range",
                         f"bytes {a}-{b}/{self.total}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        stream_body = (length >= 1_000_000
                       and a < self.total - 3_000_000)
        t0 = time.monotonic()
        sent = 0
        with open(MEDIA, "rb") as f:
            f.seek(a)
            while sent < length:
                chunk = f.read(min(64_000, length - sent))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except socket.timeout:
                    STATE["idle_kills"] += 1
                    print("    provider: IDLE body reaped after %.1fs "
                          "(%d of %d bytes)"
                          % (time.monotonic() - t0, sent, length),
                          flush=True)
                    self.close_connection = True   # never loop back onto
                    return                         # a timed-out socket
                except Exception:
                    return
                sent += len(chunk)
                if stream_body and RATE:
                    # pace + age-check: die mid-body at KILL_S
                    target = sent / RATE
                    while True:
                        if time.monotonic() - t0 >= KILL_S \
                                and (STATE["kill_max"] is None
                             or STATE["kills"] < STATE["kill_max"]):
                            STATE["kills"] += 1
                            print("    provider: KILL connection after "
                                  "%.1fs (%d of %d bytes)"
                                  % (time.monotonic() - t0, sent, length),
                                  flush=True)
                            return          # mid-body close (FIN)
                        behind = target - (time.monotonic() - t0)
                        if behind <= 0:
                            break
                        time.sleep(min(0.05, behind))


def snap(w):
    """state, is_playing, time + media stats (decoded/displayed/played).
    NB: this vlc.py binding's get_stats takes a caller-allocated
    struct — the bare m.get_stats() call raises TypeError (swallowed),
    which is why the pre-fix runs saw the counters 'never populate'."""
    import vlc as _vlc
    st = {"state": w.state_name(), "playing": w.is_playing(),
          "t": w.get_time()}
    try:
        m = w.player.get_media()
        if m is not None:
            ms = _vlc.MediaStats()
            if m.get_stats(ms):
                st["demux"] = ms.demux_read_bytes
                st["dec_v"] = ms.decoded_video
                st["disp"] = ms.displayed_pictures
                st["abuf"] = ms.played_abuffers
    except Exception:
        pass
    return st


def fmt(s):
    return ("state=%-7s play=%d t=%5.1fs demux=%7d dec_v=%4d disp=%4d "
            "abuf=%4d" % (s["state"], s["playing"], s["t"] / 1000.0,
                          s.get("demux", -1), s.get("dec_v", -1),
                          s.get("disp", -1), s.get("abuf", -1)))


def play_through_relay():
    """Fresh fake provider + REAL VodRelay + REAL VLC; returns
    (vlcwrapper, relay, provider_stats_snapshot)."""
    ProviderHandler.total = os.path.getsize(MEDIA)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    srv.handle_error = lambda *a, **k: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from src.vod_splitter import VodRelay
    relay = VodRelay()
    local = relay.start(
        f"http://127.0.0.1:{srv.server_address[1]}/v.mkv",
        "MichaelTVPlayer/1.0")
    assert local, "relay refused the stream (not mkv?)"
    from src.player import VLCPlayer
    w = VLCPlayer(volume=0, sub_args=["--vout=dummy"])
    w.play(local)
    return w, relay, srv


def wait_playing(w, timeout=25.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if w.state_name() == "playing":
            return True
        time.sleep(0.2)
    return False


def wait_deep(w, frac, budget_s):
    """Tight poll until the clock reaches ``frac`` of the media (or
    'ended' — a healthy end also satisfies the caller). NEVER let the
    process sit past end-of-media: VLC's in-process EOF teardown
    access-violates in this harness, so legs os._exit while still
    mid-playback. Also notes whether the stats counters ever populated
    (LEG B's frame-flow check only asserts when they do)."""
    global STATS_ALIVE
    target = DURATION * 1000 * frac
    hit = None
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if w.state_name() == "ended":
            hit = snap(w)
            break
        s = snap(w)
        if s.get("demux", 0) > 0:
            STATS_ALIVE = True
        if s["t"] >= target:
            hit = s
            break
        time.sleep(0.1)
    return hit


def leg_a_single_kill():
    print("[LEG A] one provider kill mid-body — the reopen must ride "
          "through it transparently (pre-fix: frozen stream)", flush=True)
    STATE.update(kills=0, opens=0, refused=0, idle_kills=0, kill_max=1,
                 refuse_after=None)
    w, relay, srv = play_through_relay()
    try:
        check("A: playback started", wait_playing(w))
        deep = wait_deep(w, 0.85, DURATION + 90)
        check("A: provider actually killed the body",
              STATE["kills"] == 1, STATE)
        check("A: playback rode the kill deep into the file",
              deep is not None, fmt(deep or snap(w)))
    finally:
        print("LEG-DONE", flush=True)


def leg_b_every_kill():
    print("[LEG B] provider kills EVERY long body — relay must ride "
          "through each transparently", flush=True)
    STATE.update(kills=0, opens=0, refused=0, idle_kills=0, kill_max=None,
                 refuse_after=None)
    w, relay, srv = play_through_relay()
    try:
        check("B: playback started", wait_playing(w))
        deep = wait_deep(w, 0.85, DURATION + 90)
        check("B: playback rode every kill deep into the file",
              deep is not None, fmt(deep or snap(w)))
        if STATS_ALIVE and deep is not None:
            check("B: frames still decoding at the deep mark (post-ride)",
                  deep.get("dec_v", 0) > 100 and deep.get("disp", 0) > 100,
                  fmt(deep))
        check("B: provider reopened for each kill",
              STATE["kills"] >= 3 and STATE["opens"] >= STATE["kills"] + 2,
              STATE)
    finally:
        print("LEG-DONE", flush=True)


def leg_c_bounded(refuse_after=2):
    print("[LEG C] provider dies FOR GOOD after %d kills — bounded "
          "retries, loud failure" % refuse_after, flush=True)
    STATE.update(kills=0, opens=0, refused=0, idle_kills=0, kill_max=None,
                 refuse_after=refuse_after)
    w, relay, srv = play_through_relay()
    try:
        check("C: playback started", wait_playing(w))
        # The relay-side refusal round is the assertion target; finish
        # BEFORE VLC can drain its buffer onto the cut body's fake EOF
        # (the in-process teardown hazard this harness os._exits away
        # from in every other leg).
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and STATE["refused"] < 3:
            time.sleep(0.2)
        print("    vlc at cut time: " + fmt(snap(w)), flush=True)
        # bound covers: probe + tail + startup opens, the kill rides,
        # the relay's 3 refused attempts, and a couple of VLC-side
        # re-GET rounds — each round is itself bounded.
        check("C: relay gave up after bounded retries",
              STATE["refused"] >= 3
              and STATE["opens"] <= refuse_after + 12, STATE)
    finally:
        print("LEG-DONE", flush=True)


def leg_d_pause():
    print("[LEG D] extended pause — provider reaps the IDLE body, "
          "resume must ride the transparent reopen (the 2026-09-02 "
          "Paw Patrol freeze)", flush=True)
    STATE.update(kills=0, opens=0, refused=0, idle_kills=0, kill_max=None,
                 refuse_after=None)
    w, relay, srv = play_through_relay()
    try:
        check("D: playback started", wait_playing(w))
        end = time.monotonic() + 40
        while time.monotonic() < end and w.get_time() < 20_000:
            time.sleep(0.2)
        check("D: reached mid-file before pausing",
              w.get_time() >= 20_000, "t=%s" % w.get_time())
        deaths_before = STATE["kills"] + STATE["idle_kills"]
        opens_before = STATE["opens"]
        w.pause()
        time.sleep(PAUSE_S)   # VLC's read-ahead keeps draining for
        #                     # seconds, then stops reading: the body
        #                     # it entered the pause with dies either
        #                     # to the age cap mid-drain or the idle
        #                     # reap — never survives to the resume
        check("D: provider body died during the pause "
              "(age kill or idle reap)",
              STATE["kills"] + STATE["idle_kills"] > deaths_before,
              STATE)
        w.resume()
        deep = wait_deep(w, 0.85, DURATION + 90)
        check("D: resume rode the reopen deep into the file",
              deep is not None, fmt(deep or snap(w)))
        check("D: relay reopened the provider after the pause",
              STATE["opens"] > opens_before, STATE)
    finally:
        print("LEG-DONE", flush=True)


# ---- child: one leg, one process, hard exit before EOF ----
LEGS = {"a": leg_a_single_kill, "b": leg_b_every_kill,
        "c": leg_c_bounded, "d": leg_d_pause}

LOG_LINES = []
STATS_ALIVE = False


def run_single_leg(leg):
    make_media()
    app = QtCore.QCoreApplication(sys.argv)   # VodRelay is a QObject
    rec = logging.Handler()
    rec.emit = lambda r: LOG_LINES.append(r.getMessage())
    logging.getLogger("mtp").addHandler(rec)
    logging.getLogger("mtp").setLevel(logging.INFO)
    LEGS[leg]()
    if leg in ("a", "b"):
        check("%s: reopening marked in the log" % leg.upper(),
              any("reopen" in l.lower() for l in LOG_LINES),
              "\n".join(LOG_LINES)[-300:])
    if leg == "c":
        check("C: give-up WARNed in the log",
              any("cutting" in l.lower() for l in LOG_LINES),
              "\n".join(LOG_LINES)[-300:])
    print("LEG-RESULT: %d pass, %d fail" % (len(PASS), len(FAIL)),
          flush=True)
    sys.stdout.flush()
    os._exit(0 if not FAIL else 1)


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    if only in LEGS:
        run_single_leg(only)
        return 0        # (never reached — run_single_leg os._exits)
    if only and only != "all":
        print("usage: probe_stall.py [a|b|c|d|all]")
        return 2

    make_media()
    legs = list(LEGS) if only in ("", "all") else []
    total_pass = total_fail = 0
    for leg in legs:
        print("[LEG %s] running in subprocess" % leg.upper(), flush=True)
        try:
            p = subprocess.run(
                [sys.executable, os.path.abspath(__file__), leg],
                capture_output=True, text=True,
                timeout=DURATION + 240,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
            out = p.stdout
            rc = p.returncode
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            rc = -1
        for line in out.splitlines():
            if line.startswith(("  ok", "  FAIL", "    provider:",
                                "LEG-RESULT")):
                print(line, flush=True)
        n_pass = n_fail = 0
        for line in out.splitlines():
            if line.startswith("  ok"):
                n_pass += 1
            elif line.startswith("  FAIL"):
                n_fail += 1
                total_fail += 1
        if rc != 0:
            print("  FAIL leg %s: subprocess exit %s (crash/timeout "
                  "before finishing)" % (leg.upper(), rc), flush=True)
            total_fail += 1
        else:
            total_pass += n_pass

    print(f"RESULT: {total_pass} pass, {total_fail} fail", flush=True)
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
