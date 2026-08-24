# -*- coding: utf-8 -*-
"""Diagnostic: why doesn't English auto-select stick on multi-language VOD?

Replays the exact PAW Patrol scenario from player.log against the real
file with the app's own VLCPlayer (which now always adds
:audio-language=en,eng at open, muted, --no-video — no window, no sound):

  Q1  what track does VLC ACTIVATE at open (does the alang hint win over
      the file's default-track flag)?
  Q2  does a set_audio(english) call ~8 s in actually STICK (the
      enforcement's mechanism), or does it no-op/revert?
  Q3  the full (id, name) list — what the matcher actually sees.

Run:  .venv\\Scripts\\python.exe tools\\diag_audio_switch.py [url]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.player import VLCPlayer  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

URL = (sys.argv[1] if len(sys.argv) > 1 else
       "http://cf.534842.xyz/series/726352471c/d809266e91/682805.mkv")

p = VLCPlayer(timeshift=False, sub_args=["--no-video"])
p.set_mute(True)
print(f"probing: {URL}")
p.play(URL, timeshift=False)

seen = {}
last_line = ""
switched_at = None
attempt_at = 8.0
eng_id = None
t0 = time.time()


def fmt(tracks, active):
    names = dict(tracks)
    cur = names.get(active, "?")
    eng = "(EN)" if PlayerView._is_english_name(str(cur)) else "    "
    return f"{eng} active={cur!r}"


while time.time() - t0 < 30:
    time.sleep(0.5)
    t = time.time() - t0
    tracks = p.audio_tracks()
    for tid, name in tracks:
        seen.setdefault(tid, name)
    active = p.active_audio()
    line = fmt(tracks, active)
    if line != last_line:
        print(f"t={t:5.1f}s  {line}")
        last_line = line
    if eng_id is None:
        for tid, name in tracks:
            if PlayerView._is_english_name(name):
                eng_id = tid
                print(f"t={t:5.1f}s  english track found: "
                      f"[{tid}] {name!r}")
                break
    if eng_id is not None and switched_at is None and t >= attempt_at:
        names = dict(tracks)
        print(f"t={t:5.1f}s  SET_AUDIO({eng_id}) "
              f"(active now {names.get(active, '?')!r})")
        p.set_audio(eng_id)
        switched_at = t

print()
print("== final track list ==")
for tid, name in sorted(seen.items()):
    mark = " (EN-match)" if PlayerView._is_english_name(name) else ""
    print(f"   [{tid}] {name!r}{mark}")
print(f"final active: {dict(tracks).get(p.active_audio(), '?')!r}")
print(f"set_audio attempted at t={switched_at}s -> "
      f"{'stuck' if dict(tracks).get(p.active_audio(), '') and PlayerView._is_english_name(str(dict(tracks).get(p.active_audio(), ''))) else 'DID NOT STICK'}")
p.stop_and_release()
print("done")
