"""Headless probe: explicit set_time on a DVR buffer that ignores :start-time.

Companion to probe_chase_wedge.py. Plays a buffer from 0, then calls
set_time() and reports whether the position actually moved.
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
seek_to = float(sys.argv[2]) if len(sys.argv) > 2 else 70.0

inst = vlc.Instance("--no-video-title-show", "--vout=dummy", "--aout=dummy",
                    "--quiet")
player = inst.media_player_new()
player.set_media(inst.media_new(path))
player.play()

t0 = time.time()
while time.time() - t0 < 12.0 and player.get_time() < 2000:
    time.sleep(0.2)
print(f"before seek: {player.get_time() / 1000.0:.2f}s "
      f"len={player.get_length() / 1000.0:.2f}s")

print(f"set_time({seek_to:.1f}s) -> {player.set_time(int(seek_to * 1000))}")
for i in range(12):
    time.sleep(0.5)
    print(f"  +{(i + 1) * 0.5:4.1f}s  time={player.get_time() / 1000.0:7.2f}s "
          f"state={player.get_state()}")
