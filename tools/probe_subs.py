"""Probe provider streams for subtitle tracks (research, not shipped)."""
import json, os, subprocess, sys, urllib.request

FFPROBE = r"C:\Users\micha\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin\ffprobe.exe"
UA = "MichaelTVPlayer/1.0"

cfg = json.load(open(os.path.join(os.environ["APPDATA"], "MichaelTVPlayer", "settings.json"), encoding="utf-8"))
base, user, pw = cfg["server_url"].rstrip("/"), cfg["username"], cfg["password"]

def api(action=None, **extra):
    import urllib.parse
    params = {"username": user, "password": pw}
    if action: params["action"] = action
    params.update({k: v for k, v in extra.items() if v is not None})
    url = base + "/player_api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def probe(url, live=False):
    cmd = [FFPROBE, "-v", "error", "-show_entries",
           "stream=index,codec_type,codec_name:stream_disposition=subtitle",
           "-of", "json", "-user_agent", UA]
    if live:
        cmd += ["-analyzeduration", "8M", "-probesize", "8M"]
    cmd += [url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        data = json.loads(out.stdout or "{}")
        subs = [s for s in data.get("streams", []) if s.get("codec_type") == "subtitle"]
        others = [s.get("codec_type") for s in data.get("streams", [])]
        return subs, others, out.stderr.strip()[-200:] if not subs and not others else ""
    except Exception as exc:
        return None, None, repr(exc)[:200]

subs_total = probed = 0
print("=== LIVE (MPEG-TS) ===")
live = api("get_live_streams")[:25]
for s in live:
    url = f"{base}/live/{user}/{pw}/{s['stream_id']}.ts"
    subs, others, err = probe(url, live=True)
    probed += 1
    if subs:
        subs_total += 1
        print(f"  {s['name'][:40]!r}: SUBS {[ (x['codec_name']) for x in subs ]}")
    else:
        print(f"  {s['name'][:40]!r}: no subs (streams={others[:6]})" + (f" ERR {err}" if err else ""))

print("=== VOD (movies) ===")
cats = api("get_vod_categories")
vod = []
for c in cats[:4]:
    vod += api("get_vod_streams", category_id=c["category_id"])[:6]
for s in vod[:20]:
    ext = s.get("container_extension") or "mp4"
    url = f"{base}/movie/{user}/{pw}/{s['stream_id']}.{ext}"
    subs, others, err = probe(url)
    probed += 1
    if subs:
        subs_total += 1
        print(f"  {s['name'][:40]!r}: SUBS {[(x['codec_name']) for x in subs]}")
    else:
        print(f"  {s['name'][:40]!r}: no subs (streams={others[:6]})" + (f" ERR {err[:120]}" if err else ""))

print("=== SERIES episodes ===")
scats = api("get_series_categories")
ser = []
for c in scats[:3]:
    ser += api("get_series", category_id=c["category_id"])[:3]
ep_count = 0
for sx in ser[:6]:
    info = api("get_series_info", series_id=sx["series_id"])
    for season, eps in (info.get("episodes") or {}).items():
        for ep in eps[:2]:
            ext = ep.get("container_extension") or "mp4"
            url = f"{base}/series/{user}/{pw}/{ep['id']}.{ext}"
            subs, others, err = probe(url)
            probed += 1
            ep_count += 1
            if subs:
                subs_total += 1
                print(f"  {sx['name'][:25]!r} S{season}E{ep.get('episode_num')}: SUBS {[(x['codec_name']) for x in subs]}")
            else:
                print(f"  {sx['name'][:25]!r} S{season}E{ep.get('episode_num')}: no subs (streams={others[:6]})" + (f" ERR {err[:120]}" if err else ""))
            if ep_count >= 10: break
        if ep_count >= 10: break
    if ep_count >= 10: break

print(f"\nTOTAL probed={probed} with_subs={subs_total}")
