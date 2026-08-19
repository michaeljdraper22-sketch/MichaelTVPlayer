"""Spike: single-connection subtitle tap via VLC sout (go/no-go test).

The playing media fans out INTERNALLY: display + a local MKV file that
should receive ONLY the subtitle elementary stream (select="spu"). If the
tap file ends up small and text-readable, the profanity filter can run on
it with ZERO extra provider connections.
"""
import json, os, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vlc

UA = "MichaelTVPlayer/1.0"
cfg = json.load(open(os.path.join(os.environ["APPDATA"], "MichaelTVPlayer", "settings.json"), encoding="utf-8"))
base, user, pw = cfg["server_url"].rstrip("/"), cfg["username"], cfg["password"]

def api(action=None, **extra):
    params = {"username": user, "password": pw}
    if action: params["action"] = action
    params.update(extra)
    url = base + "/player_api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

cats = api("get_vod_categories")
vod = api("get_vod_streams", category_id=cats[0]["category_id"])
movie = next((m for m in vod if "superbad" in m["name"].lower()), vod[0])
ext = movie.get("container_extension") or "mp4"
url = f"{base}/movie/{user}/{pw}/{movie['stream_id']}.{ext}"
print(f"movie: {movie['name']!r}")

tap = os.path.abspath("build/subtap.mkv").replace("\\", "/")
try:
    os.remove(tap)
except OSError:
    pass

inst = vlc.Instance(["--no-video-title-show", "--no-stats",
                     "--network-caching=1500", "--live-caching=1500"])
mp = inst.media_player_new()
media = inst.media_new(url)
media.add_option(f"http-user-agent={UA}")
sout = (f"#duplicate{{dst=display,"
        f"dst=std{{access=file,mux=mkv,dst='{tap}'}},select=\"spu\"}}")
print("sout:", sout)
media.add_option(f":sout={sout}")
mp.set_media(media)
mp.play()

t0 = time.time()
last_size = 0
while time.time() - t0 < 55:
    time.sleep(2.5)
    try:
        size = os.path.getsize(tap) if os.path.exists(tap) else 0
    except OSError:
        size = 0
    playing = bool(mp.is_playing())
    t = mp.get_time() / 1000
    print(f"  t+{time.time()-t0:4.0f}s playing={playing} "
          f"pos={t:6.1f}s tap={size/1024:8.1f} KiB")
    if size > 0:
        last_size = size
    if t > 90:
        break

mp.stop()
time.sleep(1)

print()
print(f"tap file: {tap}")
if os.path.exists(tap):
    print(f"  final size: {os.path.getsize(tap)/1024:.1f} KiB")
    import subprocess
    ff = r"C:\Users\micha\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin\ffprobe.exe"
    out = subprocess.run([ff, "-v", "error", "-show_entries",
                          "stream=index,codec_type,codec_name:stream_tags=language,title",
                          "-of", "json", tap], capture_output=True, text=True)
    print("  streams:", out.stdout.strip()[:500])
    # extract text cues from the tap
    ffx = ff.replace("ffprobe.exe", "ffmpeg.exe")
    out2 = subprocess.run([ffx, "-y", "-v", "error", "-i", tap,
                           "-map", "0:s:0", "-f", "srt", "pipe:1"],
                          capture_output=True, text=True, timeout=30)
    lines = [l for l in out2.stdout.splitlines() if l.strip()]
    print(f"  extracted SRT lines: {len(lines)}")
    print("  first cues:", " | ".join(lines[2:10])[:220])
else:
    print("  NO TAP FILE — select=spu isolation failed")
