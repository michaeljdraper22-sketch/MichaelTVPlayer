"""Headless probe: does libvlc honour :start-time on a DVR buffer file?

Reproduces the 2026-11-14 12:10-12:12 chase loop: every rescue reopen of the
rotated buffer logged a different ``start=`` value, yet VLC reported the same
raw position (~21.7 s) eight seconds later. Either the start-time is ignored
on this file, or the display wedges at a fixed point in it.

Run:  .venv\\Scripts\\python.exe probe_chase_wedge.py <buffer.ts> [start]
No window, no audio (vout/aout = dummy).
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_VLC_DIR = os.path.join(_HERE, "vlc")
os.environ["VLC_PLUGIN_PATH"] = os.path.join(_VLC_DIR, "plugins")
os.add_dll_directory(_VLC_DIR)
os.environ["PYTHON_VLC_LIB_PATH"] = os.path.join(_VLC_DIR, "libvlc.dll")

import vlc  # noqa: E402

path = sys.argv[1]
start = float(sys.argv[2]) if len(sys.argv) > 2 else 70.6

inst = vlc.Instance("--no-video-title-show", "--vout=dummy", "--aout=dummy",
                    "--quiet")
player = inst.media_player_new()
media = inst.media_new(path)
if start > 0:
    media.add_option(f":start-time={start:.3f}")
player.set_media(media)
player.play()

t0 = time.time()
last = None
print(f"requested start={start:.1f}s  file={os.path.basename(path)}")
while time.time() - t0 < 40.0:
    time.sleep(0.5)
    t = player.get_time() / 1000.0
    state = player.get_state()
    if t != last:
        print(f"  {time.time() - t0:5.1f}s  state={state}  time={t:7.2f}s")
        last = t
    if state in (vlc.State.Ended, vlc.State.Error):
        print(f"  -> ended/errored at time={t:.2f}s")
        break
print(f"final: time={player.get_time() / 1000.0:.2f}s state={player.get_state()}")
