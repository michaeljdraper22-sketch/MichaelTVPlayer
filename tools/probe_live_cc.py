# -*- coding: utf-8 -*-
"""Probe: live CC clock alignment — CCX-from-byte-0 vs VLC's get_time().

Records a captioned channel with the app's VlcRecorder (same options the
app uses), plays the growing buffer with a real VLC player (chase-style),
and pipes the SAME buffer into CCExtractor FROM BYTE 0 (the proposed
anchoring). Collects:

  - VLC get_time() samples (the displayed position, PTS-file domain)
  - CCX cue (start, end, text) with the wall moment they were parsed
  - buffer growth (write head)

Verdict metrics:
  A) axis alignment: (latest_cue_start - vlc_raw) sampled over time —
     must be STABLE (chase gap + caption latency) for byte-0 anchoring
     to work with the VLC clock.
  B) the OLD scheme's error: wall-frontier estimate at join vs the
     CCX-axis content time around the join.

Run: .venv\\Scripts\\python.exe -X utf8 tools\\probe_live_cc.py [minutes]
"""
import json
import os
import subprocess
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.dvr import VlcRecorder  # noqa: E402
from src.live_cc import ccx_args, find_ccextractor  # noqa: E402
from src.profanity import SrtParser  # noqa: E402
from src.player import USER_AGENT  # noqa: E402

MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0

cfgj = json.load(open(os.path.join(os.environ["APPDATA"],
                                   "MichaelTVPlayer", "settings.json"),
                      encoding="utf-8"))
base = cfgj["server_url"].rstrip("/")
user, pw = cfgj["username"], cfgj["password"]

import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402


def api(action, **extra):
    params = {"username": user, "password": pw, "action": action}
    params.update(extra)
    url = base + "/player_api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


CAPTIONED = ("4k: espn", "fox news hd", "cnn hd", "msnbc hd", "cnbc hd",
             "bbc news", "sky news", "fox news", "cnn", "msnbc", "espn")
live = api("get_live_streams")
ch = next((c for key in CAPTIONED
           for c in live if key in c["name"].lower()), None)
assert ch, "no captioned channel found"
url = f"{base}/live/{user}/{pw}/{ch['stream_id']}.ts"
print(f"channel: {ch['name']!r}", flush=True)

# ---- record (app-style) ----
rec = VlcRecorder(max_minutes=30, network_caching=1500)
rec.start(url)
buf = rec.file_path
first_data_t = None
t_start = time.time()

# ---- CCX from byte 0 ----
exe = find_ccextractor()
assert exe, "ccextractor missing"
proc = subprocess.Popen([exe] + ccx_args(exe), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        creationflags=0x08000000)
cues = []            # (wall_parsed, start, end, text)
raw_chunks = []
lock = threading.Lock()
alive = True


def read_out():
    while alive:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        with lock:
            raw_chunks.append(chunk)


threading.Thread(target=read_out, daemon=True).start()

parser = SrtParser(keep_lines=True)
fed = 0


def tailer():
    global fed
    while alive:
        try:
            size = os.path.getsize(buf)
        except OSError:
            time.sleep(0.3)
            continue
        if size > fed:
            with open(buf, "rb") as f:
                f.seek(fed)
                while alive:
                    chunk = f.read(1 << 18)
                    if not chunk:
                        break
                    try:
                        proc.stdin.write(chunk)
                    except Exception:
                        return
                    fed += len(chunk)
        time.sleep(0.3)


threading.Thread(target=tailer, daemon=True).start()


def harvest():
    with lock:
        raw = b"".join(raw_chunks)
        raw_chunks.clear()
    if not raw:
        return
    for s, e, txt in parser.feed(raw.decode("utf-8", "replace")):
        cues.append((time.time(), s, e, txt))


# ---- VLC display player (chase-style, opens the file early) ----
import vlc  # noqa: E402

inst = vlc.Instance(["--ignore-config", "--no-audio", "--vout=dummy",
                     "--network-caching=1500", "--live-caching=1500"])
player = inst.media_player_new()
opened = False
samples = []         # (wall, vlc_ms, size, fed)
last_size = 0
frontier_first_data = None

print(f"recording {MINUTES:.0f} min ...", flush=True)
t_end = time.time() + MINUTES * 60
while time.time() < t_end:
    try:
        size = os.path.getsize(buf)
    except OSError:
        size = 0
    now = time.time()
    if size > 0 and first_data_t is None:
        first_data_t = now
    harvest()
    # chase-style: open the buffer once it exists, a few seconds behind
    if not opened and first_data_t and now - first_data_t > 4.0:
        media = inst.media_new(buf)
        player.set_media(media)
        player.play()
        opened = True
        print(f"  playback opened {now - first_data_t:.1f}s after first "
              f"data (size={size/1e6:.0f}MB)", flush=True)
    t = player.get_time()
    samples.append((now, t, size, fed))
    if len(samples) % 120 == 0:      # ~every 30 s
        lc = cues[-1][1] if cues else float("nan")
        print(f"  wall={now - t_start:6.1f}s vlc={t/1000:7.1f}s "
              f"size={size/1e6:5.0f}MB cues={len(cues):3d} "
              f"latest_cue_start={lc:7.1f}s", flush=True)
    time.sleep(0.25)

alive = False
player.stop()
rec.safe_stop()
try:
    proc.stdin.close()
    proc.kill()
except Exception:
    pass

# ---- analysis ----
print("\n=== analysis ===", flush=True)
print(f"samples: {len(samples)}  cues: {len(cues)}", flush=True)
if not cues:
    print("NO CUES — channel may be un-captioned right now", flush=True)
    sys.exit(0)

vlc0 = next((s for s in samples if s[1] >= 0), None)
if vlc0:
    print(f"first sane vlc get_time: {vlc0[1]/1000:.1f}s "
          f"(file had {vlc0[2]/1e6:.0f}MB at that point)", flush=True)

# A) axis stability: latest_cue_start - vlc_raw over the session
diffs = []
for i, (w, ms, size, fd) in enumerate(samples):
    if ms < 0:
        continue
    prior = [c for c in cues if c[0] <= w]
    if not prior:
        continue
    diffs.append((w - t_start, prior[-1][1] - ms / 1000.0))
if diffs:
    vals = [d for _, d in diffs]
    import statistics
    tailv = [d for _, d in diffs[len(diffs) // 2:]] or vals
    print(f"[A] latest_cue_start - vlc_raw: first-quarter mean "
          f"{statistics.mean(vals[:max(1,len(vals)//4)]):7.2f}s   "
          f"last-half mean {statistics.mean(tailv):7.2f}s   "
          f"stdev {statistics.pstdev(vals):5.2f}s", flush=True)
    print("    (stable ~constant => CCX byte-0 axis == VLC clock axis)",
          flush=True)

# B) old-scheme error: wall-frontier at join vs CCX content time there.
# join moment = when CCX first got bytes (t=0 here); the app joins at
# first-data + ~1 poll with frontier ~= 0..2s. CCX-axis content time at
# the join byte = start time of the first cue arriving after join.
first_cues = cues[:3]
if first_cues and first_data_t:
    join_wall = first_data_t + 1.2          # typical app join delay
    soon = [c for c in cues if c[0] - t_start < 30]
    if soon:
        # cues parsed in the first 30s reflect content written within the
        # first burst: their starts are the CCX-axis content times of the
        # join-era bytes
        starts = [c[1] for c in soon]
        print(f"[B] join-era content (CCX axis, first 30s of parses): "
              f"cue starts span {min(starts):.1f}..{max(starts):.1f}s; "
              f"app-style frontier offset would be ~0-2s => baked-in "
              f"error up to ~{max(starts):.0f}s under the OLD scheme",
              flush=True)

print("\nlast 5 cues:", flush=True)
for w, s, e, txt in cues[-5:]:
    print(f"  parsed_at={w - t_start:6.1f}s  {s:7.1f}-{e:7.1f}  "
          f"{txt.splitlines()[0][:50]!r}", flush=True)
