# -*- coding: utf-8 -*-
"""Diagnose CCSource live-tail latency inside the real PlayerView chase:
dump join offset, bytes piped, ccx liveness and parsed cues over time."""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

CH = {
    "kind": "live", "title": "US: FOX NEWS HD",
    "url": "http://cf.534842.xyz/live/726352471c/d809266e91/324923.ts",
    "stream_id": 324923,
}

app = QtWidgets.QApplication(sys.argv)
cfg = Config.load()
cfg.data["chase_delay"] = 5
view = PlayerView(cfg)
view._attach_done = True
view._attached = True
view.resize(960, 540)
view.show()

view.play_media(dict(CH))
t0 = time.time()
while time.time() - t0 < 30:
    app.processEvents()
    time.sleep(0.03)
    if view._mode == "chase" and view.vlc.is_playing():
        break
print(f"chase at {time.time()-t0:.1f}s mode={view._mode}", flush=True)
t0 = time.time()
while time.time() - t0 < 20:
    app.processEvents()
    time.sleep(0.03)

tracks = view.vlc.spu_tracks()
print("tracks:", [(t, n) for t, n in tracks][:4], flush=True)
tid, name = next((t, n) for t, n in tracks if "caption" in n.lower())
view._select_spu(tid, name)
print(f"selected {tid} {name!r} cap_on={view._cap_on} "
      f"cc={view._cc_source is not None}", flush=True)

src = view._cc_source
start = time.time()
last = 0
while time.time() - start < 75:
    app.processEvents()
    time.sleep(0.05)
    el = time.time() - start
    if el - last >= 5:
        last = el
        try:
            size = os.path.getsize(view.dvr.file_path)
            piped = src._ts_pos
            with src._out_lock:
                unparsed = sum(len(c) for c in src._out_chunks)
            alive = src._alive
            pc = src.proc.poll() if src.proc else "n/a"
            cues = len(view._cap_cues.cues)
            print(f"[{el:5.1f}s] file={size//1024}K piped={piped//1024}K "
                  f"unparsed={unparsed} alive={alive} ccx_rc={pc} "
                  f"cues={cues} lines={view._cap_wid._lines[:1]}", flush=True)
        except Exception as exc:
            print(f"[{el:5.1f}s] err {exc!r}", flush=True)
    if view._cap_wid._lines and el - last > 3:
        print(f"[{el:5.1f}s] FIRST LINES: {view._cap_wid._lines}", flush=True)
        break

# post-mortem: does the app's OWN buffer contain extractable captions?
src.stop()
import shutil  # noqa: E402
import subprocess  # noqa: E402
from src.live_cc import find_ccextractor  # noqa: E402
copy = os.path.abspath("build/diag_app_buffer.ts").replace("\\", "/")
try:
    shutil.copyfile(view.dvr.file_path, copy)
    join_bytes = src._ts_pos0 if hasattr(src, "_ts_pos0") else 0
    view.stop()
    time.sleep(1.0)
    exe = find_ccextractor()
    for label, blob in (("whole app buffer", open(copy, "rb").read()),):
        p = subprocess.run([exe, "-in=ts", "-srt", "-utf8", "--stdin",
                            "--stdout"], input=blob, capture_output=True,
                           timeout=180)
        txt = p.stdout.decode("utf-8", "replace")
        print(f"post-mortem {label}: {len(blob)//1024}K -> "
              f"{txt.count(' --> ')} cue timestamps", flush=True)
        print("  first lines:", txt[:200].replace("\r", " "), flush=True)
except Exception as exc:
    print("post-mortem failed:", repr(exc), flush=True)
    view.stop()
os._exit(0)
