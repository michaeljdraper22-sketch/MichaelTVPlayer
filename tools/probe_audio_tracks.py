# -*- coding: utf-8 -*-
"""Research: what does VLC NAME audio tracks on this provider's streams
(live .ts and VOD files), to ground the English-name matching in the
audio-track selector (src/player.py audio_tracks + player_view
_is_english_name). Headless by design: a libvlc instance with
--no-audio --no-video — no window, no sound, elementary streams still
parsed and track descriptions still reported.

Run:  .venv\\Scripts\\python.exe tools\\probe_audio_tracks.py
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vlc  # noqa: E402

from src.player import USER_AGENT  # noqa: E402
from src.mkv_subs import lang_matches  # noqa: E402

UA = USER_AGENT
cfg = json.load(open(os.path.join(os.environ["APPDATA"],
                                  "MichaelTVPlayer", "settings.json"),
                     encoding="utf-8"))
base, user, pw = (cfg["server_url"].rstrip("/"), cfg["username"],
                  cfg["password"])


def api(action=None, **extra):
    params = {"username": user, "password": pw}
    if action:
        params["action"] = action
    params.update(extra)
    url = base + "/player_api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def probe_audio(url, label, seconds=10):
    inst = vlc.Instance(["--no-audio", "--no-video", "--no-stats",
                         "--network-caching=1500", "--live-caching=1500"])
    p = inst.media_player_new()
    m = inst.media_new(url)
    m.add_option(f"http-user-agent={UA}")
    # the same hint the app passes in VLCPlayer.play_at
    m.add_option(":audio-language=en,eng")
    p.set_media(m)
    p.play()
    seen = {}
    active = None
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(0.5)
        try:
            desc = p.audio_get_track_description() or []
        except Exception:
            desc = []
        for t in desc:
            try:
                tid, name = int(t[0]), t[1]
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "replace")
                seen[tid] = name
            except Exception:
                pass
        try:
            active = int(p.audio_get_track())
        except Exception:
            pass
    print(f"== {label} ==")
    for tid, name in sorted(seen.items()):
        if tid < 1 or name.strip().lower() == "disable":
            continue
        eng = "EN" if lang_matches("english", "", name) else "  "
        mark = " <== active" if tid == active else ""
        print(f"   [{tid}] ({eng}) {name!r}{mark}")
    if not seen:
        print("   (no audio tracks)")
    try:
        p.stop()
    except Exception:
        pass
    time.sleep(0.3)
    try:
        p.release()
        inst.release()
    except Exception:
        pass
    return seen


def main():
    multi = 0
    probed = 0

    # --- live: a spread of channels across the list ---
    try:
        live = api("get_live_streams")
    except Exception as exc:
        print(f"live list failed: {exc!r}")
        live = []
    step = max(1, len(live) // 5)
    for ch in live[::step][:5]:
        url = f"{base}/live/{user}/{pw}/{ch['stream_id']}.ts"
        print(f"channel: {ch['name']!r}")
        seen = probe_audio(url, "live .ts")
        probed += 1
        if len([t for t in seen if t >= 1]) > 1:
            multi += 1

    # --- VOD: a couple of mkv movies ---
    try:
        cats = api("get_vod_categories")
        vod = api("get_vod_streams", category_id=cats[0]["category_id"])
    except Exception as exc:
        print(f"vod list failed: {exc!r}")
        vod = []
    mkvs = [m for m in vod
            if (m.get("container_extension") or "") == "mkv"][:2]
    for mv in mkvs:
        ext = mv.get("container_extension") or "mp4"
        url = f"{base}/movie/{user}/{pw}/{mv['stream_id']}.{ext}"
        print(f"movie: {mv['name']!r} ({ext})")
        seen = probe_audio(url, "VOD direct")
        probed += 1
        if len([t for t in seen if t >= 1]) > 1:
            multi += 1

    print(f"done: {probed} streams probed, {multi} with >1 audio track")


if __name__ == "__main__":
    main()
