# -*- coding: utf-8 -*-
"""Real-VLC repro for the 2026-10-21 Stremio A/V desync report.

Field shape (player.log 15:17-15:20, torbox via vod_splitter):
  - the provider cuts streaming bodies mid-file repeatedly (age/idle
    policy), the relay rides through with 0.5 s-backoff reopens,
  - the user PAUSES: VLC stops pulling, the relay's provider connection
    idles out and dies; on UNPAUSE VLC resumes reading through a healing
    relay,
  - reported: audio ran ~1 s into the pause and A/V stayed out of sync
    after unpausing.

This drives a REAL VLCPlayer through the relay against a cutting
provider, pauses 5 s mid-play, resumes, and compares libVLC's
lost_abuffers / lost_pictures / state against a clean (uncut) control.
Body cuts on VLC's connection are detected from the relay trace.

Run:  .venv\\Scripts\\python.exe test_vod_av_desync.py   (~2 min)
"""
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# HEADLESS: VLCPlayer is constructed below with extra_args=["--vout=dummy"]
# (its test-only hook) — the picture counters the test keys on are decoder-
# side and survive a dummy vout, while a real vout would pop a visible
# window and steal focus, which media tests here must never do. Audio
# stays a REAL (muted) pipeline: the pause-landing measurement needs the
# true aout. (Monkeypatching vlc.Instance does NOT work: vlc.py's errcheck
# and ctypes bindings resolve the module-global Instance class at call
# time — both live-hung / type-errored.)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["MTP_SPLIT_TRACE"] = "1"          # relay timeline -> stderr

FF = "ffmpeg"
MEDIA = os.path.abspath("build/av_desync_5min.mkv").replace("\\", "/")
SRT_IN = os.path.abspath("build/av_desync.srt")

DUR_S = 300
KILL_AFTER = 1_000_000      # 1 MB bodies with a 1.5 s heal ≈ 4 Mbps
#                           # effective — a flappy-but-adequate CDN (the
#                           # field's torbox), not a bandwidth-starved one

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          ("   [" + detail + "]" if detail else ""), flush=True)


def build_media():
    os.makedirs("build", exist_ok=True)
    if os.path.exists(MEDIA) and os.path.getsize(MEDIA) > 1_000_000:
        return
    with open(SRT_IN, "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,000\nhello line\n")
    subprocess.run(
        [FF, "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c=steelblue:s=256x144:d={DUR_S}:r=10",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={DUR_S}",
         "-i", SRT_IN,
         "-map", "0:v", "-map", "1:a", "-map", "2:s",
         "-c:v", "libx264", "-preset", "ultrafast",
         "-b:v", "300k", "-minrate", "300k", "-maxrate", "300k",
         "-bufsize", "60k", "-x264-params", "nal-hrd=cbr",
         "-c:a", "aac", "-b:a", "64k", "-c:s", "srt", MEDIA],
        check=True, timeout=600, creationflags=0x08000000)


class _CutHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        # the field CDN reaps IDLE bodies: while the relay stops reading
        # (VLC paused), our write to it blocks and the socket times out —
        # exactly the "provider body died during a long VLC pause" class
        self.connection.settimeout(2.5)

    def do_GET(self):
        srv = self.server
        srv.gets += 1
        blob = srv.blob
        rng = self.headers.get("Range") or ""
        a, b = 0, len(blob) - 1
        if rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            a_s, _, b_s = spec.partition("-")
            if a_s:
                a = int(a_s)
            if b_s:
                b = int(b_s)
        length = b - a + 1
        # let the relay's STARTUP gets through unharmed (head probe +
        # tail prefetch); reopens pay a real-network heal latency
        if srv.gets > 2:
            time.sleep(srv.reopen_delay)
        k = length if srv.gets <= 2 else min(srv.kill_after, length)
        self.send_response(206)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Content-Range", f"bytes {a}-{b}/{len(blob)}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        try:
            self.wfile.write(blob[a:a + k])
        except Exception:
            pass


class _CutProvider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, blob):
        super().__init__(("127.0.0.1", 0), _CutHandler)
        self.blob = blob
        self.gets = 0
        self.kill_after = 1 << 62
        self.reopen_delay = 0.0


def snapshot(vlc):
    """libVLC MediaStats tuple the desync hypothesis keys on."""
    import vlc as _vlc
    m = vlc.player.get_media()
    st = _vlc.MediaStats()
    if m is None or not m.get_stats(st):
        return None
    return (st.decoded_video, st.displayed_pictures, st.lost_pictures,
            st.played_abuffers, st.lost_abuffers)


def run_session(label, kill_after, reopen_delay):
    """Play ~20 s, pause 6 s, resume ~25 s; return damage stats."""
    from src.vod_splitter import VodRelay
    from src.player import VLCPlayer
    blob = open(MEDIA, "rb").read()
    prov = _CutProvider(blob)
    prov.kill_after = kill_after
    prov.reopen_delay = reopen_delay
    threading.Thread(target=prov.serve_forever, daemon=True).start()

    relay = VodRelay()
    local = relay.start(
        f"http://127.0.0.1:{prov.server_address[1]}/x.mkv",
        "MichaelTVPlayer/1.0")
    assert local.startswith("http://"), f"{label}: relay failed to start"
    deadline = time.time() + 20
    while time.time() < deadline and relay.cache_size == 0:
        time.sleep(0.1)

    vlc = VLCPlayer(timeshift=False, volume=0,
                    extra_args=["--vout=dummy"])
    vlc.set_mute(True)
    vlc.play(local, timeshift=False, network_caching_ms=15000,
             start_wait_s=45)

    def wait(cond, timeout, what):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cond():
                return True
            time.sleep(0.1)
        print(f"    ({label}: timeout waiting for {what})", flush=True)
        return False

    ok = wait(lambda: vlc.is_playing() and vlc.get_time() > 6000,
              45, "first 6 s of playback")

    def watch_freeze(stop_evt, out):
        """Poll get_time at 200 ms; a >1.2 s stall while not paused (or
        >1.5 s to actually PAUSE) is a client-visible relay stall — the
        input thread was blocked reading, the pause landed late, the
        aout kept its buffer (the reported 1 s audio tail)."""
        last_t, last_w = vlc.get_time(), time.monotonic()
        paused_at = None
        while not stop_evt.is_set():
            time.sleep(0.2)
            t = vlc.get_time()
            w = time.monotonic()
            playing = vlc.is_playing() and t >= 0
            if t != last_t:
                last_t, last_w = t, w
            elif playing and w - last_w > 1.2:
                out.append(("stall", round(w - last_w, 2), t / 1000))
                last_w = w
        return paused_at

    stalls = []
    stop_watch = threading.Event()
    watcher = threading.Thread(target=watch_freeze,
                               args=(stop_watch, stalls), daemon=True)
    watcher.start()

    pre = snapshot(vlc) or (0, 0, 0, 0, 0)
    # the pause moment mirrors the field report: pause, hold, resume
    t_pause = time.monotonic()
    vlc.pause()
    paused_ok = wait(lambda: vlc.state_name() == "paused", 2.0,
                     "pause landing")
    pause_lag = time.monotonic() - t_pause
    time.sleep(6.0)
    mid = snapshot(vlc) or pre
    vlc.resume()
    ok2 = wait(lambda: vlc.get_time() > 20000, 45, "post-resume playback")
    time.sleep(6.0)                      # settle, then read the damage
    post = snapshot(vlc) or mid
    stop_watch.set()
    watcher.join(timeout=2)
    vlc.stop_and_release()
    relay.stop()

    d_lost_a = post[4] - pre[4]
    d_lost_v = post[2] - pre[2]
    played_a = post[3] - pre[3]
    real_stalls = [s for s in stalls if s[0] == "stall"]
    print(f"    {label}: GETs={prov.gets} pause landed in "
          f"{pause_lag:.2f}s lost_a +{d_lost_a} (of {played_a}) "
          f"lost_v +{d_lost_v} stalls={real_stalls}", flush=True)
    return {
        "started": ok, "resumed": ok2, "paused_ok": paused_ok,
        "pause_lag": pause_lag,
        "lost_a": d_lost_a, "lost_v": d_lost_v, "gets": prov.gets,
        "stalls": real_stalls,
        "time_after": vlc.get_time(),
    }


def main():
    build_media()
    ctrl = run_session("control (no cuts)", 1 << 62, 0.0)
    check("control: played through the pause",
          ctrl["started"] and ctrl["resumed"],
          f"t={ctrl['time_after']}ms")
    check("control: pause lands instantly (<0.5 s)",
          ctrl["paused_ok"] and ctrl["pause_lag"] < 0.5,
          f"lag={ctrl['pause_lag']:.2f}s")
    check("control: zero stalls / zero lost audio",
          not ctrl["stalls"] and ctrl["lost_a"] == 0,
          f"stalls={ctrl['stalls']} lost_a={ctrl['lost_a']}")

    flaky = run_session("flaky (field cuts + idle reap + slow heal)",
                        KILL_AFTER, 1.5)
    check("flaky: provider bodies actually cut",
          flaky["gets"] > ctrl["gets"], f"{flaky['gets']} vs {ctrl['gets']}")
    check("flaky: playback still reached 20 s+",
          flaky["started"] and flaky["resumed"],
          f"t={flaky['time_after']}ms")
    check("flaky: pause lands instantly like the control",
          flaky["paused_ok"] and flaky["pause_lag"] < 0.5,
          f"lag={flaky['pause_lag']:.2f}s")
    check("flaky: NO playback stalls around the pause",
          not flaky["stalls"], f"stalls={flaky['stalls']}")
    check("flaky: audio buffers lost == control",
          flaky["lost_a"] <= ctrl["lost_a"],
          f"flaky lost_a={flaky['lost_a']} vs ctrl {ctrl['lost_a']} "
          f"(lost_v={flaky['lost_v']})")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
