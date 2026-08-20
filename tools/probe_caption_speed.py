# -*- coding: utf-8 -*-
"""Probe: caption load speed + live sync, headless (no video, no audio).

Measures exactly the user-facing complaints:
  [L] LIVE — recorder + CCSource + the PlayerView arrival anchor:
        - time from engage to the first FRESH anchor (captions usable),
        - where the newest cue's ANCHORED window sits vs the frontier:
          must hover ~-_CC_LAG_S s (the pipeline lag), NOT the 12-32 s
          and drifting PTS-axis error measured before the fix,
        - mid-session RE-ENGAGE on an old buffer with join_bytes: time
          to fresh captions again (was: ~1 s CPU per buffered minute).
  [V] VOD — the real relay, driven by a real VLC (--no-audio,
        --vout=dummy):
        - wall time relay.start() blocks the caller (was: 4 MB tail
          prefetch synchronously on the UI thread),
        - time to VLC Playing, time to the first cue ON THE PLAYED
          POSITION ("subtitles as soon as the stream plays"),
        - start_offset engage mid-file: time to a cue at the resume
          position (the "switch subtitles on a playing movie" path),
        - set_prefer_language switch: time to the next cue.

Run: .venv\\Scripts\\python.exe -X utf8 tools\\probe_caption_speed.py
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtCore  # noqa: E402

from src.dvr import VlcRecorder  # noqa: E402
from src.live_cc import CCSource, find_ccextractor  # noqa: E402
from src.mkv_subs import MkvSubParser, is_text_codec  # noqa: E402
from src.player import USER_AGENT  # noqa: E402
from src.vod_splitter import VodRelay  # noqa: E402

CC_LAG_S = 1.0          # PlayerView._CC_LAG_S (kept in sync by hand here;
#                        # the app also EWMA-smooths the anchor — the probe
#                        # replicates the first-anchor behavior, close
#                        # enough for the stability check)

cfgj = json.load(open(os.path.join(os.environ["APPDATA"],
                                   "MichaelTVPlayer", "settings.json"),
                      encoding="utf-8"))
base, user, pw = (cfgj["server_url"].rstrip("/"), cfgj["username"],
                  cfgj["password"])


def api(action, **extra):
    params = {"username": user, "password": pw, "action": "action=" + action}
    params = {"username": user, "password": pw, "action": action}
    params.update(extra)
    url = base + "/player_api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


app = QtCore.QCoreApplication(sys.argv)


def pump(sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents()
        time.sleep(0.02)


# ============================ LIVE LEG ============================
if os.environ.get("SKIP_LIVE"):
    rec = None
else:
  print("=" * 60)
  print("[L] live: engage latency + arrival-anchor stability")
  assert find_ccextractor(), "CCExtractor not found"
  live = api("get_live_streams")
  CAPTIONED = ("4k: espn", "fox news hd", "cnn hd", "msnbc hd", "cnbc hd",
             "bbc news", "sky news", "fox news", "cnn", "msnbc", "espn")
  ch = next((c for key in CAPTIONED
           for c in live if key in c["name"].lower()), None)
  assert ch, "no captioned channel found"
  url = f"{base}/live/{user}/{pw}/{ch['stream_id']}.ts"
  print(f"  channel: {ch['name']!r}", flush=True)

  # the app's own recorder (headless: sout only — no vout, no aout)
  rec = VlcRecorder(max_minutes=30, network_caching=1500)
  rec.start(url)
  buf = rec.file_path
  first_data = None


  def frontier_s():
    """The app's wall-growth frontier estimate for the young buffer."""
    if first_data is None:
        return 0.0
    return time.time() - first_data


  # PlayerView._on_cc_cue's anchor, replicated verbatim in spirit
  anchor = {"off": None, "last_c": None, "last_t": 0.0}
  engage = {"t": time.time()}
  stats = {"cues": 0, "first_anchor_after": None, "leads": []}


  def on_cue(start, end, text):
    now = time.time()
    last_c = anchor["last_c"]
    elapsed = 1.0 if anchor["last_t"] <= 0 else now - anchor["last_t"]
    advance = None if last_c is None else end - last_c
    fresh = ((last_c is None and frontier_s() < 20.0)
             or (advance is not None
                 and 0.0 < advance <= elapsed * 3.0 + 5.0))
    if last_c is None or end > last_c:
        anchor["last_c"] = end
        anchor["last_t"] = now
    if fresh:
        anchor["off"] = frontier_s() - CC_LAG_S - end
        if stats["first_anchor_after"] is None:
            stats["first_anchor_after"] = now - engage["t"]
    stats["cues"] += 1
    if anchor["off"] is not None:
        # where this cue's END sits on the app clock, vs the frontier NOW
        stats["leads"].append((frontier_s(), end + anchor["off"]))


  t_engage = time.time()
  src = CCSource()
  src.cue.connect(lambda s, e, t: on_cue(s, e, t))
  started = False
  while time.time() - t_engage < 30:
    app.processEvents()
    try:
        if os.path.getsize(buf) > 50000:
            if first_data is None:
                first_data = time.time() - 1.0   # ~1 s of burst credit
            if not started:
                started = src.start(buf)
                break
    except OSError:
        pass
    time.sleep(0.1)
  print(f"  cc source started={started} after {time.time()-t_engage:.1f}s",
      flush=True)

  # watch 100 s of natural flow
  t0 = time.time()
  last = 0
  while time.time() - t0 < 100:
    app.processEvents()
    now = time.time() - t0
    if now - last >= 20:
        last = now
        f = frontier_s()
        lead = stats["leads"][-1] if stats["leads"] else None
        print(f"  t={now:5.0f}s frontier={f:6.1f} cues={stats['cues']:4d} "
              f"anchored_end={lead[1] if lead else float('nan'):6.1f} "
              f"(lead {f - lead[1] if lead else float('nan'):5.1f}s)",
              flush=True)
    time.sleep(0.02)

  if stats["leads"]:
    tail_leads = [f - e for f, e in stats["leads"][len(stats["leads"]) // 2:]]
    tail_leads = tail_leads[-40:]
    import statistics
    print(f"  [L1] first fresh anchor after engage: "
          f"{stats['first_anchor_after']:.1f}s", flush=True)
    print(f"  [L2] frontier - anchored_end over the last cues: mean "
          f"{statistics.mean(tail_leads):.2f}s  min {min(tail_leads):.2f}s  "
          f"max {max(tail_leads):.2f}s  (want ~{CC_LAG_S}s, was 12-32 s "
          f"drifting on the PTS axis)", flush=True)

  # mid-session re-engage with join_bytes (the Off->On + old-buffer path)
  src.stop()
  pump(0.5)
  f = frontier_s()
  size = os.path.getsize(buf)
  join = int(size * max(0.0, f - 8.0) / max(1.0, f))
  join -= join % 188
  print(f"  re-engage on {f:.0f}s-old buffer: join_byte={join} "
      f"({100*join/size:.0f}% of {size/1e6:.0f} MB)", flush=True)
  anchor.update(off=None, last_c=None, last_t=0.0)
  stats["first_anchor_after"] = None
  t_re = engage["t"] = time.time()
  src2 = CCSource()
  src2.cue.connect(lambda s, e, t: on_cue(s, e, t))
  src2.start(buf, join_bytes=join)
  while time.time() - t_re < 60 and stats["first_anchor_after"] is None:
    app.processEvents()
    time.sleep(0.02)
  print(f"  [L3] re-engage fresh anchor after "
          f"{stats['first_anchor_after'] if stats['first_anchor_after'] else -1:.1f}s"
          f" (whole-file was ~{f:.0f}s of CPU)", flush=True)
  src2.stop()
  rec.safe_stop()

# ============================ VOD LEG ============================
print("=" * 60)
print("[V] vod: relay startup latency + cue availability")
movie = None
murl = None
cats = api("get_vod_categories")
# multi-subs categories first (text tracks guaranteed-ish); skip the 4K
# ones by NAME too — their movie names all carry "4k" and are multi-GB
cat_order = [c for c in cats
             if "multi-subs" in c["category_name"].lower()]
cat_order += [c for c in cats if c not in cat_order][:2]
for cat in cat_order[:4]:
    vod = api("get_vod_streams", category_id=cat["category_id"])
    for m in vod[:40]:
        if (m.get("container_extension") or "mp4") != "mkv":
            continue
        if "4k" in m["name"].lower():
            continue   # 4K rips are multi-GB; provider throughput dominates
        cand = f"{base}/movie/{user}/{pw}/{m['stream_id']}.mkv"
        time.sleep(0.3)             # the provider throttles rapid GETs
        try:
            req = urllib.request.Request(
                cand, headers={"User-Agent": USER_AGENT,
                               "Range": "bytes=0-2097151"})
            with urllib.request.urlopen(req, timeout=20) as r:
                blob = r.read(1 << 21)
        except Exception:  # noqa: BLE001
            continue
        p = MkvSubParser()
        p.feed(blob)
        if any(is_text_codec(t["codec"]) for t in p._track_meta.values()):
            movie = (m, cand)
            break
    if movie:
        break
assert movie, "no SRT MKV movie found in the first three categories"
m, murl = movie
print(f"  movie: {m['name'][:50]!r}", flush=True)

import vlc  # noqa: E402


def vlc_pull(local, start_time=None, seconds=25):
    """Play the relay URL in a real VLC (--no-audio, --vout=dummy) and
    collect (played_position, cues_on_position) samples."""
    inst = vlc.Instance(["--ignore-config", "--no-audio", "--vout=dummy",
                         "--network-caching=1500"])
    player = inst.media_player_new()
    media = inst.media_new(local)
    media.add_option(f"http-user-agent={USER_AGENT}")
    if start_time:
        media.add_option(f":start-time={start_time}")
    player.set_media(media)
    t_play = time.time()
    player.play()
    playing_at = None
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        if player.get_time() >= 0 and playing_at is None:
            playing_at = time.time() - t_play
        time.sleep(0.05)
    pos = player.get_time() / 1000.0
    player.stop()
    del media, player, inst
    return playing_at, pos


# --- fresh open (subtitles wanted from the start) ---
relay = VodRelay()
cues = []
relay.cue.connect(lambda s, e, t: cues.append((s, e, t)))
fail = []
relay.failed.connect(lambda why: fail.append(why))
t0 = time.time()
local = relay.start(murl, USER_AGENT, prefer_language="eng")
t_url = time.time() - t0
print(f"  [V1] relay.start() returned in {t_url:.2f}s "
      f"(UI-thread block; tail prefetch is async now)", flush=True)
playing_at, pos = vlc_pull(local, seconds=20)
first_cue = cues[0] if cues else None
print(f"  [V2] VLC playing after {playing_at if playing_at else -1:.1f}s, "
      f"position {pos:.1f}s, cues={len(cues)} "
      f"first={first_cue[0] if first_cue else '-'}", flush=True)
on_pos = [c for c in cues if c[0] <= pos + 5]
print(f"  [V3] cues at/behind the played position: {len(on_pos)} "
      f"(overlay paints immediately when they cover the position)",
      flush=True)

# --- language switch on a running relay ---
if len({t for t in ()}):
    pass
relay.set_prefer_language("eng")     # no-op switch
n_before = len(cues)
t_sw = time.time()
# force a real switch cycle even for the same hint: restart the tap
relay._tap_restart = True
while time.time() - t_sw < 15 and len(cues) == n_before:
    app.processEvents()
    time.sleep(0.05)
print(f"  [V4] tap re-anchor emitted cues again after "
      f"{time.time()-t_sw:.1f}s (language switch responsiveness)",
      flush=True)
relay.stop()
pump(0.5)

# --- mid-movie engage: start_offset (the 'pick a track while playing') ---
total = relay.total or 0
off = total // 4 if total else 0
relay2 = VodRelay()
cues2 = []
relay2.cue.connect(lambda s, e, t: cues2.append((s, e, t)))
relay2.failed.connect(lambda why: fail.append(why))
t0 = time.time()
local2 = relay2.start(murl, USER_AGENT, prefer_language="eng",
                      start_offset=off)
print(f"  [V5] offset engage start() in {time.time()-t0:.2f}s "
      f"(offset={off/1e6:.0f} MB of {total/1e6:.0f} MB)", flush=True)
playing_at2, pos2 = vlc_pull(local2, start_time=30, seconds=20)
near = [c for c in cues2 if abs(c[0] - pos2) < 10]
print(f"  [V6] offset engage: playing after "
      f"{playing_at2 if playing_at2 else -1:.1f}s at {pos2:.1f}s, "
      f"cues={len(cues2)}, near-position cues={len(near)} "
      f"(resume/switch keeps captions right at the position)", flush=True)
relay2.stop()

if fail:
    print(f"  relay failures observed: {fail}", flush=True)
print("done.", flush=True)
os._exit(0)
