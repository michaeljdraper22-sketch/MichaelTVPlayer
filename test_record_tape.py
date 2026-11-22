# -*- coding: utf-8 -*-
"""Kept recordings are an append-only TAPE over the DVR buffer, and the
DVR axis follows the buffer file the display actually plays.

Why (2026-11-17, Dancing With The Stars): the kept recording was a second
``dst=std{access=file}`` branch of the recorder's sout. A provider cut
2 h 40 m in made the stall watchdog restart the recorder twice, and each
restart re-opened that destination — libVLC truncates a reused sout
destination, so the 2 h 48 m recording on disk was its last 137 seconds.
The same restart rotated the buffer to a new sibling whose generation base
(10 000+ s) the PCR edge calibration ignored: the live edge collapsed to
the viewer's own position, every scrub clamped there, and every rescue
mapped the position into the new file as 0.0.

Part [1] exercises BufferTapeCopier against real files.
Part [2] drives the REAL PlayerView axis/generation logic with FakeVLC.
Run:  .venv\\Scripts\\python.exe test_record_tape.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
import src.dvr as dvr_mod  # noqa: E402
import src.ui.player_view as pv  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("   [%s]" % extra) if extra and not cond else ""))


def wait_for(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


P = 188


def ts(ch, n):
    return ch * (P * n)


# ----------------------------------------------------------------------------
# [1] BufferTapeCopier: exact bytes, across segments, never truncated
# ----------------------------------------------------------------------------
print("[1] the kept recording is an append-only tape of the buffer bytes")
td = tempfile.mkdtemp(prefix="mtp_tape_")
buf = os.path.join(td, "buffer.ts")
out = os.path.join(td, "Show_20261117_200533.ts")
A = ts(b"A", 10)
with open(buf, "wb") as f:
    f.write(A)
cp = dvr_mod.BufferTapeCopier(out)
ok = cp.start(source=buf, at_end=True)      # mature buffer: from the live edge
check("copier starts on a mature buffer", ok and cp.running)
B = ts(b"B", 3)
with open(buf, "ab") as f:
    f.write(B)
check("...and appends exactly the bytes written after REC",
      wait_for(lambda: cp.written >= len(B))
      and open(out, "rb").read() == B)

# recorder restart -> fresh sibling: the tape must grow, not restart
buf2 = os.path.join(td, "buffer_2.ts")
open(buf2, "wb").close()
cp.retarget(buf2, at_end=False)
C = ts(b"C", 5)
with open(buf2, "ab") as f:
    f.write(C)
check("a recorder restart appends across the seam",
      wait_for(lambda: cp.written >= len(B) + len(C))
      and open(out, "rb").read() == B + C)

# the writer truncating its destination must not lose the tape's past
with open(buf2, "r+b") as f:
    f.truncate(0)
    f.write(ts(b"D", 2))
check("a source truncation continues from the new head",
      wait_for(lambda: cp.written >= len(B) + len(C) + 2 * P)
      and open(out, "rb").read() == B + C + ts(b"D", 2))

# detach drains what is on disk and releases the handle (Windows delete)
E = ts(b"E", 4)
with open(buf2, "ab") as f:
    f.write(E)
cp.detach()
check("detach drains the tail before the temp dir dies",
      open(out, "rb").read() == B + C + ts(b"D", 2) + E)
try:
    os.remove(buf2)
    removed = True
except OSError:
    removed = False
check("detach closes the source handle (file deletable on Windows)",
      removed)
cp.stop()
check("stop is idempotent and finalizes the file",
      open(out, "rb").read() == B + C + ts(b"D", 2) + E)
cp.stop()

# a fresh copier on a young buffer records from its head
young = os.path.join(td, "young.ts")
open(young, "wb").close()
out2 = os.path.join(td, "young_rec.ts")
cp2 = dvr_mod.BufferTapeCopier(out2)
cp2.start(source=young, at_end=False)
F = ts(b"F", 6)
with open(young, "ab") as f:
    f.write(F)
check("REC right after tuning records from the buffer head",
      wait_for(lambda: cp2.written >= len(F))
      and open(out2, "rb").read() == F)
cp2.stop()

# an unopenable destination is refused (the caller unsets REC)
cp3 = dvr_mod.BufferTapeCopier(os.path.join(td, "nope", "x.ts"))
check("an unwritable recording path is reported, not faked",
      cp3.start() is False and not cp3.running and cp3.error)

# a disk that fills up mid-recording must mark the tape failed (the tick
# then unsets REC and tells the viewer) instead of silently stopping
class _BadOut:
    def write(self, b):
        raise OSError(28, "No space left on device")

    def flush(self):
        pass

    def close(self):
        pass


disk = os.path.join(td, "disk.ts")
open(disk, "wb").close()
out3 = os.path.join(td, "disk_rec.ts")
cp4 = dvr_mod.BufferTapeCopier(out3)
cp4.start(source=disk, at_end=False)
with cp4._lock:
    cp4._out = _BadOut()
with open(disk, "ab") as f:
    f.write(ts(b"G", 3))
check("a write failure marks the recording failed (never silent)",
      wait_for(lambda: cp4.failed)
      and "write failed" in (cp4.error or ""),
      str(cp4.error))
cp4.stop()

# ----------------------------------------------------------------------------
# [2] the DVR axis follows the DISPLAY's buffer file (generation base)
# ----------------------------------------------------------------------------
print("[2] DVR seeks/reopens use the file that holds the position")


class FakeVLC:
    def __init__(self):
        self.commanded = []
        self.t = 0.0
        self.state = "playing"

    def play_at(self, url, t, record_path=None, timeshift=False):
        self.commanded.append(("play_at", url, float(t)))
        self.t = float(t)

    def set_time(self, ms):
        self.commanded.append(("set_time", ms / 1000.0))
        self.t = ms / 1000.0

    def get_time(self):
        return int(self.t * 1000)

    def is_playing(self):
        return self.state == "playing"

    def state_name(self):
        return self.state

    def set_rate(self, r):
        pass

    def set_mute(self, on):
        pass

    def set_volume(self, v):
        pass

    def set_spu(self, tid):
        pass

    def is_mute(self):
        return False

    def resume(self):
        pass

    def stop_and_release(self):
        pass

    def video_size(self):
        return (1280, 720)

    def spu_tracks(self):
        return []

    def active_spu(self):
        return -1

    def get_length(self):
        return 0

    def play(self, url, timeshift=False):
        self.commanded.append(("play", url))


class FakeDVR:
    def __init__(self, path):
        self.file_path = path
        self.running = True

    def buffer_file(self):
        return self.file_path

    def stop(self, delete=False):
        pass

    def safe_stop(self, delete=True):
        pass


fd, cfg_path = tempfile.mkstemp(suffix=".json")
os.close(fd)
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
view = PlayerView(Config({}, Path(cfg_path)))
view.vlc = FakeVLC()
view.current = {"kind": "live", "url": "http://x/s.ts", "title": "DWTS"}

g0 = os.path.join(td, "buffer.ts")
g1 = os.path.join(td, "buffer_2.ts")
open(g0, "wb").write(ts(b"0", 2))
open(g1, "wb").write(ts(b"1", 2))
view._mode = "chase"
view.dvr = FakeDVR(g1)
view._buf_gens = [(g0, 0.0), (g1, 1000.0)]
view._dvr_gen_base = 1000.0
view._disp_gen = (g0, 0.0)          # the viewer sat 1 000 s behind live
view._cap_div_s = 0.0
view._cap_div_ok = False
view._frontier_s = lambda: 1200.0
view._cap_edge_s = lambda: 1200.0
view._safe_seek_target = lambda t: float(t)
view._cap_clock_s = 800.0
view._cap_backlog_s = 400.0
view._vid_s = 800.0

check("a position in the retired file selects that file",
      view._gen_for_content(500.0) == (g0, 0.0))
check("a position past the rotation selects the live file",
      view._gen_for_content(1150.0) == (g1, 1000.0))
check("a position AT the new base lands on the new file's head",
      view._gen_for_content(999.5) == (g1, 1000.0))
check("the axis mapping follows the DISPLAY's file, not the recorder's",
      view._axis_div() == 0.0)

view.vlc.commanded.clear()
view._reopen_display(at=500.0)
got = view.vlc.commanded
check("a reopen of a retired position opens the retired FILE there",
      got and got[-1][0] == "play_at" and got[-1][1] == g0
      and abs(got[-1][2] - 500.0) < 0.01, got)
check("...and the display generation follows it",
      view._disp_gen == (g0, 0.0))

view.vlc.commanded.clear()
view._chase_seek(1150.0)            # live-file position, viewer on g0
got = view.vlc.commanded
check("a seek to the live file reopens it (set_time cannot cross files)",
      got and got[-1][0] == "play_at" and got[-1][1] == g1
      and abs(got[-1][2] - 150.0) < 0.01, got)
check("the display generation follows the cross-file seek",
      view._disp_gen == (g1, 1000.0))
check("...and its mapping is the new file's base",
      view._axis_div() == -1000.0)

view.vlc.commanded.clear()
view._chase_seek(1100.0)            # inside the file already open
got = view.vlc.commanded
check("a seek inside the open file uses set_time with its base",
      got and got[-1][0] == "set_time" and abs(got[-1][1] - 100.0) < 0.01,
      got)

# the scrubber speaks CONTENT (its range is the frontier): a release must
# not be mapped through the axis div a second time (that sent every drag
# to the live edge once the base was ~10 000 s).
view.vlc.commanded.clear()
view.slider.setRange(0, 1200000)
view.slider.setValue(1100000)
view._on_slider_released()
got = view.vlc.commanded
check("a scrubber release seeks the content position it shows",
      got and got[-1][0] == "set_time" and abs(got[-1][1] - 100.0) < 0.01,
      got)

# PCR edge calibration: the pin is segment-local; the generation base must
# be added or the edge collapses to the viewer's position (DWTS).
view._pcr_join_base = 1000.0
view._cc_join_app_s = 0.0
view._cap_clock_s = 900.0
view._cap_backlog_s = 100.0
view._frontier_s = lambda: 1015.0    # the write head really is ~1010
edge_before = view._cap_edge_s()
view._cc_calibrate_edge(10.0, 1010.0)   # head is 10 s past the join pin
edge_after = view._cap_clock_s + (view._cap_backlog_s or 0.0)
check("PCR edge calibration keeps the generation base",
      edge_after > 1005.0 and view._cap_backlog_s > 90.0,
      "edge %.1f -> %.1f backlog %.1f"
      % (edge_before, edge_after, view._cap_backlog_s))

# a renumbered PCR axis (splice / provider reconnect) is re-pinned at the
# write head instead of dragging the edge a whole programme away
view._frontier_s = lambda: 5000.0
view._sync_pcr_join = (256, 1000.0)
view._cc_calibrate_edge(10.0, 1010.0)
check("a renumbered PCR axis re-pins the join at the write head",
      view._sync_pcr_join == (256, 1010.0)
      and abs(view._cc_join_app_s - 4000.0) < 0.01,
      "%s app=%.1f" % (view._sync_pcr_join, view._cc_join_app_s))

view._cc_head_pcr = (1010.0, pv.now_s())
view._sync_pcr_join = (256, 1000.0)
view._pcr_join_base = 1000.0
view._cc_join_app_s = 0.0
check("head-ahead math is on the absolute content axis too",
      abs(view._head_ahead_s(900.0) - 110.0) < 0.01,
      view._head_ahead_s(900.0))

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
try:
    view.stop()
except Exception:
    pass
