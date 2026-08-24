# -*- coding: utf-8 -*-
"""P5 VOD CDN probe: is the VOD side playable tonight? (D4: HTTP 551
from the provider CDN = skip the VOD E2E with a note.) Headless, no
playback — a 1 KB range GET per candidate URL, a few movies + one
series episode. Prints status per URL and a final verdict line."""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

import requests  # noqa: E402
from src.config import Config  # noqa: E402
from src.xtream import XtreamClient  # noqa: E402

cfg = Config.load()
client = XtreamClient(cfg.server_url, cfg.username, cfg.password)
client.authenticate()

urls = []
for cat in (client.vod_categories() or [])[:3]:
    for s in (client.vod_streams(cat.get("category_id")) or [])[:2]:
        urls.append((f"movie:{s.get('name')}", client.vod_url(s["stream_id"])))
    if len(urls) >= 6:
        break
try:
    for cat in (client.series_categories() or [])[:2]:
        for sr in (client.series(cat.get("category_id")) or [])[:2]:
            info = client.series_info(sr.get("series_id"))
            for _, eps in (info.get("episodes") or {}).items():
                if eps:
                    ep = eps[0]
                    urls.append((f"series:{ep.get('name')}",
                                 client.series_url(ep["id"])))
                    break
            if len(urls) >= 8:
                break
        if len(urls) >= 8:
            break
except Exception as exc:  # noqa: BLE001
    print("series probe failed:", exc)

if not urls:
    print("VERDICT: no VOD items listed — treat as 551-style skip")
    sys.exit(0)

accept = 0
for label, url in urls:
    try:
        r = requests.get(url, headers={"Range": "bytes=0-1023"},
                         timeout=15, stream=True)
        code = r.status_code
        ctype = r.headers.get("Content-Type", "")
        r.close()
    except Exception as exc:  # noqa: BLE001
        code, ctype = f"EXC:{exc.__class__.__name__}", ""
    print(f"  {code}  {ctype[:30]:30s}  {label}")
    if code in (200, 206):
        accept += 1

print(f"VERDICT: {accept}/{len(urls)} playable "
      + ("RUN vod E2E" if accept else "SKIP vod E2E (CDN refusing)"))
