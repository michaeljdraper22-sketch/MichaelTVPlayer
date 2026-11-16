# -*- coding: utf-8 -*-
"""Probe: does a relay stall / provider body death leave the AUDIO output
behind the picture after pause+resume?  (field report 2026-11-13: ~1 s
audio lag on a Stremio VOD, a reload fixes it)

Measures, headless (dummy vout, muted, volume 0 — no window, no sound):
  * played_abuffers growth while PAUSED  (an audio tail: sound keeps
    being handed to the device after the picture freezes)
  * lost_abuffers / demux_discontinuity / demux_corrupted deltas
  * the get_time() recovery after resume

Runs the app's own VLCPlayer through the app's own VodRelay against a
local provider that cuts bodies exactly like torbox does (the same shape
as test_vod_av_desync.py).

Run:  .venv\\Scripts\\python.exe probe_av_offset.py [--aout NAME]
"""
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FF = "ffmpeg"
MEDIA = os.path.abspath("build/av_desync_5min.mkv").replace("\\", "/")
DUR_S = 300

AOUT = "directsound"
args = sys.argv[1:]
if "--aout" in args:
    AOUT = args[args.index("--aout") + 1]


def build_media():
    if os.path.exists(MEDIA) and os.path.getsize(MEDIA) > 1_000_000:
        return
    os.makedirs("build", exist_ok=True)
    subprocess.run(
        [FF, "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c=steelblue:s=256x144:d={DUR_S}:r=25",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={DUR_S}",
         "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "500k",
         "-c:a", "aac", "-b:a", "96k", MEDIA],
        check=True, timeout=600, creationflags=0x08000000)


class _CutHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(2.5)   # idle bodies get reaped

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
            elif not a_s:
                a = max(0, len(blob) - int(spec.lstrip("-") or 0))
        length = b - a + 1
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
    import vlc as _vlc
    m = vlc.player.get_media()
    st = _vlc.MediaStats()
    if m is None or not m.get_stats(st):
        return None
    return {
        "decoded_video": st.decoded_video,
        "displayed": st.displayed_pictures,
        "lost_pictures": st.lost_pictures,
        "played_a": st.played_abuffers,
        "lost_a": st.lost_abuffers,
        "disc": st.demux_discontinuity,
        "corrupt": st.demux_corrupted,
        "demux": st.demux_read_bytes,
    }


def run(label, kill_after, reopen_delay):
    from src.vod_splitter import VodRelay
    from src.player import VLCPlayer

    blob = open(MEDIA, "rb").read()
    prov = _CutProvider(blob)
    prov.kill_after = kill_after
    prov.reopen_delay = reopen_delay
    threading.Thread(target=prov.serve_forever, daemon=True).start()

    relay = VodRelay()
    local = relay.start(f"http://127.0.0.1:{prov.server_address[1]}/x.mkv",
                        "MichaelTVPlayer/1.0")
    assert local.startswith("http://"), f"{label}: relay failed"
    vlc = VLCPlayer(timeshift=False, volume=0,
                    extra_args=["--vout=dummy", f"--aout={AOUT}"])
    vlc.set_mute(True)
    vlc.play(local, timeshift=False, network_caching_ms=8000,
             start_wait_s=45)

    t0 = time.time()
    while time.time() - t0 < 40 and not (vlc.is_playing()
                                         and vlc.get_time() > 5000):
        time.sleep(0.2)

    def sample():
        return snapshot(vlc) or {}

    pre = sample()
    t_play = vlc.get_time()

    # --- pause and watch for an audio tail ------------------------------
    t_pause = time.monotonic()
    vlc.pause()
    while time.monotonic() - t_pause < 2.0:
        if vlc.state_name() == "paused":
            break
        time.sleep(0.05)
    pause_lag = time.monotonic() - t_pause
    time.sleep(0.35)
    a_035 = sample()
    time.sleep(1.2)
    a_155 = sample()
    time.sleep(1.5)
    a_300 = sample()
    time.sleep(4.0)
    a_end = sample()

    tail035 = a_035.get("played_a", 0) - pre.get("played_a", 0)
    tail155 = a_155.get("played_a", 0) - pre.get("played_a", 0)
    tail300 = a_300.get("played_a", 0) - pre.get("played_a", 0)
    tail700 = a_end.get("played_a", 0) - pre.get("played_a", 0)

    # --- resume and see the timeline ------------------------------------
    t_res = time.monotonic()
    vlc.resume()
    got = None
    while time.monotonic() - t_res < 20:
        t = vlc.get_time()
        if t > t_play + 1500:
            got = t
            break
        time.sleep(0.1)
    resume_s = time.monotonic() - t_res
    time.sleep(4.0)
    post = sample()
    t_post = vlc.get_time()

    print(f"\n[{label}] aout={AOUT} GETs={prov.gets} pause_lag={pause_lag:.2f}s")
    print(f"  audio buffers played after pause:   "
          f"+0.35s={tail035}  +1.55s={tail155}  +3.0s={tail300}  "
          f"+7.0s={tail700}")
    print(f"  resume: media time recovered in {resume_s:.2f}s "
          f"(t_now={t_post} ms, wall played)")
    print(f"  lost_a +{post.get('lost_a',0)-pre.get('lost_a',0)}  "
          f"lost_v +{post.get('lost_pictures',0)-pre.get('lost_pictures',0)}  "
          f"discont +{post.get('disc',0)-pre.get('disc',0)}  "
          f"corrupt +{post.get('corrupt',0)-pre.get('corrupt',0)}")
    print(f"  displayed +{post.get('displayed',0)-pre.get('displayed',0)}  "
          f"played_a +{post.get('played_a',0)-pre.get('played_a',0)}")

    vlc.stop_and_release()
    relay.stop()
    prov.shutdown()
    return {"tail300": tail300, "tail700": tail700, "resume_s": resume_s,
            "pause_lag": pause_lag}


def main():
    build_media()
    run("control (no cuts)", 1 << 62, 0.0)
    run("flaky (body cuts + slow heal)", 1_000_000, 1.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
