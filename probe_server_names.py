"""What does the local streaming server say about hash 03727f0b file 73?

Read-only: stats GETs only. Reproduces the resolve_identity name loop.
"""
import json
import sys

sys.path.insert(0, r"D:\Coding\MichaelTVPlayer")

from src import stremio  # noqa: E402

HASH = "03727f0bdebfd938dc6d56986ae7dc45429416dd"
IDX = 73

server = stremio.StreamingServer("")

print("=== per-torrent stats ===")
per = server.stats(HASH, IDX)
print(json.dumps(per, indent=2, ensure_ascii=False)[:3000] if per else per)

print("\n=== names as torrent_names() collects them (order matters) ===")
played, names = server.torrent_names(HASH, IDX)
print("played (files[idx]):", played)
for n in names:
    print("  %-70r parse_se=%s" % (n[:70], stremio.parse_se(n)))

print("\n=== resolve_identity's pick over that list ===")
file_name, torrent_name = played, ""
if not stremio.parse_se(played):
    torrent_name = next((n for n in names if n != played), "")
se = stremio.parse_se(file_name) or stremio.parse_se(torrent_name)
print("file_name=%r torrent_name=%r -> se=%s" % (file_name, torrent_name, se))
if se:
    hit = stremio.find_series(stremio.clean_show_name(file_name or torrent_name))
    print("find_series ->", hit)
