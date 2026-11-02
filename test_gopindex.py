# -*- coding: utf-8 -*-
"""GOP-index + anchored-seek unit tests (standalone, offscreen Qt).

Two layers:

  * src.gopindex parsing/indexing — synthetic TS packets for the header
    math, then the REAL provider recording (TV Recordings/...) for the
    full pipeline: every IDR must match the ffprobe keyframe list to
    within 50 ms (measured worst case: 0.8 ms), anchor_for must pick the
    right GOP, incremental scans of a growing file must equal one full
    scan, and a replaced (shrunk) file must reset.
  * PlayerView anchored transports — offscreen view with the wedge-suite
    FakeVLC/FakeDVR harness pointed at the real recording's index:
    _chase_seek must command the ANCHORED vlc time (IDR - guard, not the
    raw slider target) and seed/verify the anchored content position;
    the scrub drag preview must be throttled and anchored too.

Run: .venv\\Scripts\\python.exe -X utf8 test_gopindex.py
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.gopindex import (  # noqa: E402
    GopIndex, _packet_pid, _packet_pusi, _payload_offset, _packet_pcr,
    _pes_pts, _TS)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "FAIL ") + name
          + (f"   [{detail}]" if detail else ""), flush=True)


def ts_packet(pid=0x100, pusi=False, payload=b"", af=None):
    """Synthetic TS packet; ``af`` = raw adaptation bytes (for PCR)."""
    b = bytearray(b"\x47")
    b.append((0x40 if pusi else 0) | ((pid >> 8) & 0x1F))
    b.append(pid & 0xFF)
    afc = 0
    if af is not None:
        afc |= 2
    if payload:
        afc |= 1
    b.append(afc << 4)
    if af is not None:
        b.append(len(af))
        b += af
    b += payload
    b += b"\xff" * (_TS - len(b))   # stuffing
    return bytes(b)


def pes_video(pts90k, payload):
    """PES header + payload for a video stream id (0xE0)."""
    p = pts90k & ((1 << 33) - 1)
    b0 = 0x20 | ((p >> 29) & 0x0E) | 0x01
    b1 = (p >> 22) & 0xFF
    b2 = ((p >> 14) & 0xFE) | 0x01
    b3 = (p >> 7) & 0xFF
    b4 = ((p << 1) & 0xFE) | 0x01
    hdr = bytes((0x00, 0x00, 0x01, 0xE0))
    body = bytes((0x00, 0x00,          # PES packet length (unbounded ok)
                  0x00, 0x80, 5,       # flags1, flags2 (PTS), hdrlen
                  b0, b1, b2, b3, b4))
    return hdr + body + payload


# ----------------------------------------------------------------------------
# TS/PES header math on synthetic packets
# ----------------------------------------------------------------------------
pkt = ts_packet(pid=0x100, pusi=True,
                payload=pes_video(90000 * 7, b"\x00\x00\x01\x65\x88"))
check("packet pid/pusi", _packet_pid(pkt) == 0x100 and _packet_pusi(pkt))
check("payload offset skips header", _payload_offset(pkt) == 4)
check("pes pts parsed", _pes_pts(pkt[4:]) == 90000 * 7,
      str(_pes_pts(pkt[4:])))

af = bytearray(b"\x00" * 7)
af[0] = 0x10                                        # PCR flag
pcr_base = 123456789
af[1] = (pcr_base >> 25) & 0xFF
af[2] = (pcr_base >> 17) & 0xFF
af[3] = (pcr_base >> 9) & 0xFF
af[4] = (pcr_base >> 1) & 0xFF
af[5] = ((pcr_base & 1) << 7) | 0                   # ext hi (0) + reserved
af[6] = 0                                           # ext lo
pkt2 = ts_packet(pid=0x100, af=bytes(af), payload=b"\x01\x02")
r = _packet_pcr(pkt2)
check("pcr parsed from adaptation field",
      r is not None and r[0] == 0x100 and abs(r[1] - pcr_base) < 1,
      str(r))

# ----------------------------------------------------------------------------
# GopIndex over the real recording (ffprobe-verified keyframe list)
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
REC = os.path.join(HERE, "TV Recordings",
                   "US_NFL_NETWORK_HD_20260827_191626.ts")
FFPROBE_IDRS = [0.433, 2.936, 5.438, 7.941, 10.443, 12.946, 15.448,
                17.951, 20.453, 22.956, 25.458, 27.961, 30.463, 32.966,
                35.468, 37.971, 40.473, 42.976, 45.478, 47.981, 50.483,
                52.986, 55.488, 57.790, 60.293, 60.493, 62.996, 65.498,
                68.001, 70.503, 73.006, 75.508, 78.011, 80.513, 83.016,
                85.084, 85.518, 88.021, 90.523, 93.026, 95.528, 98.031]

gi = None
if os.path.exists(REC):
    gi = GopIndex(REC)
    n = gi.scan() + gi.scan()
    idrs = gi.idrs()
    check("real file: every ffprobe IDR found (42)",
          n == len(FFPROBE_IDRS) == len(idrs),
          f"indexed={len(idrs)} scanned_new={n}")
    worst = max((abs(raw - FFPROBE_IDRS[k])
                 for k, (raw, _o) in enumerate(idrs[:len(FFPROBE_IDRS)])),
                default=99)
    check("real file: IDR times match ffprobe <50ms (worst 0.8ms)",
          worst < 0.05, f"worst={worst*1000:.1f}ms")
    check("anchor_for picks the IDR at/below the target",
          abs(gi.anchor_for(31.0) - 30.463) < 0.02
          and abs(gi.anchor_for(30.40) - 27.961) < 0.02)
    check("anchor_for None before the first IDR / beyond max_back",
          gi.anchor_for(0.2) is None and gi.anchor_for(110.0) is None
          and abs(gi.anchor_for(99.0) - 98.031) < 0.02)

    # growing-file equivalence: index half, append the rest, scan again
    tmp = tempfile.mkdtemp(prefix="mtp_gop_")
    try:
        half = os.path.join(tmp, "buffer.ts")
        full = open(REC, "rb").read()
        with open(half, "wb") as f:
            f.write(full[:len(full) // 2])
        gi2 = GopIndex(half)
        gi2.scan()
        with open(half, "ab") as f:
            f.write(full[len(full) // 2:])
        gi2.scan()
        gi2.scan()
        a1 = [round(r, 3) for r, _ in gi2.idrs()]
        a2 = [round(r, 3) for r, _ in gi.idrs()]
        check("growing file: incremental == full scan",
              a1[:len(a1) // 2 * 2] == a2[:len(a1)],
              f"inc={len(a1)} full={len(a2)}")
        # replaced (shrunk) file resets cleanly
        with open(half, "wb") as f:
            f.write(full[:_TS * 3000])
        gi2.scan()
        check("shrunk file: index resets", gi2._pos == 0
              or len(gi2.idrs()) <= 1, f"pos={gi2._pos}")

        # tail-first: a long buffer indexes its NEWEST region on the first
        # scan (rewinds target near the head), history fills in after
        big = os.path.join(tmp, "long.ts")
        pad = (b"\x47" + b"\x00" * 187) * ((30 * 1024 * 1024) // 188)
        with open(big, "wb") as f:
            f.write(full + pad + full)
        gi3 = GopIndex(big)
        gi3.scan()
        check("long buffer: tail IDRs on the FIRST scan",
              len(gi3.idrs()) > 40
              and abs(gi3.anchor_for(31.0) - 30.463) < 0.05,
              f"first-scan idrs={len(gi3.idrs())}")
        for _ in range(8):
            gi3.scan()
        check("long buffer: forward fill completes (42 x 2 copies)",
              len(gi3.idrs()) == 84, f"idrs={len(gi3.idrs())}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
else:
    print("  (recording missing — real-file GopIndex checks skipped)",
          flush=True)

# ----------------------------------------------------------------------------
# view-level: anchored chase transports (FakeVLC + the real index)
# ----------------------------------------------------------------------------
from PyQt5 import QtWidgets  # noqa: E402  (offscreen)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

import src.ui.player_view as pv  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402


class FakeVLC:
    def __init__(self):
        self.commanded = []
        self.t = 0.0
        self.state = "playing"
        self.paused = False

    def play_at(self, url, t, record_path=None, timeshift=False):
        self.commanded.append(("play_at", t))
        self.t = float(t)

    def set_time(self, ms):
        self.commanded.append(("set_time", ms / 1000.0))
        self.t = ms / 1000.0

    def get_time(self):
        return int(self.t * 1000)

    def is_playing(self):
        return not self.paused and self.state == "playing"

    def state_name(self):
        return self.state

    def set_rate(self, r):
        pass

    def resume(self):
        self.paused = False

    def set_volume(self, v):
        pass

    def set_mute(self, on):
        pass

    def set_spu(self, tid):
        pass

    def stop_and_release(self):
        pass

    def video_size(self):
        return (1280, 720)

    def spu_tracks(self):
        return []

    def active_spu(self):
        return -1

    def is_mute(self):
        return False

    def get_length(self):
        return 0

    def play(self, url, timeshift=False):
        self.commanded.append(("play", url))


class FakeDVR:
    def __init__(self, path):
        self.file_path = path
        self.running = False

    def buffer_file(self):
        return self.file_path

    def stop(self, delete=False):
        pass

    def safe_stop(self, delete=True):
        pass


tmp = tempfile.mkdtemp(prefix="mtp_gop_view_")
cfg_path = os.path.join(tmp, "cfg.json")
open(cfg_path, "w").close()
from src.config import Config  # noqa: E402
from pathlib import Path  # noqa: E402

cfg = Config({}, Path(cfg_path))
view = PlayerView(cfg)
view.vlc = FakeVLC()
view.current = {"kind": "live", "url": "http://x/s.ts", "title": "gop"}
view._mode = "chase"
view.dvr = FakeDVR(REC if os.path.exists(REC) else os.path.join(tmp, "b.ts"))
# pinned clocks: div=0 (raw axis == content axis), frontier at 200s
view._frontier_s = lambda: 200.0
view._cap_edge_s = lambda: 200.0
VT = {"t": 1_000_000.0}
pv.now_s = lambda: VT["t"]

if gi is not None:
    view._gop = gi
    view._gop_path = REC

    # a mid-GOP seek target must command the IDR below it minus the guard
    view.vlc.commanded.clear()
    view._chase_seek(31.0)
    want = 30.463 - pv._GOP_ANCHOR_GUARD_S
    got = [c for c in view.vlc.commanded if c[0] == "set_time"]
    check("chase seek anchors to IDR-0.35",
          got and abs(got[0][1] - want) < 0.02,
          f"commanded={got[0][1] if got else None} want={want:.3f}")
    v = view._seek_verify
    check("seek verify seeded at the ANCHORED target",
          v is not None and abs(v[0] - want) < 0.02,
          f"verify target={v[0] if v else None}")

    # jump-to-live anchors too (target = edge - 5 = 195 -> IDR 194.9xx
    # does not exist in a 100s file: falls back EXACT, no wrong anchor)
    view.vlc.commanded.clear()
    view._chase_seek(0.0, jump_live=True)
    got = [c for c in view.vlc.commanded if c[0] in ("set_time", "play_at")]
    check("jump-to-live beyond indexed range stays exact",
          got and abs(got[0][1] - 195.0) < 0.01,
          f"commanded={got[0][1] if got else None}")

    # no index -> legacy behavior byte-for-byte (exact target)
    view._gop = None
    view.vlc.commanded.clear()
    view._chase_seek(31.0)
    got = [c for c in view.vlc.commanded if c[0] == "set_time"]
    check("no index: unanchored exact seek (legacy)",
          got and abs(got[0][1] - 31.0) < 0.01,
          f"commanded={got[0][1] if got else None}")
    view._gop = gi

    # scrub drag preview: throttled + anchored
    view._seeking = False
    view._scrub_prev_t = 0.0
    view.slider.setRange(0, 100000)
    view.slider._win_mode = False
    view.slider.setValue(31000)
    view.vlc.commanded.clear()
    view._on_slider_moved(31000)
    got = [c for c in view.vlc.commanded if c[0] == "set_time"]
    check("drag preview seeks anchored once",
          len(got) == 1 and abs(got[0][1] - want) < 0.02,
          f"commanded={[g[1] for g in got]}")
    view._on_slider_moved(32000)
    got = [c for c in view.vlc.commanded if c[0] == "set_time"]
    check("drag preview throttled within 0.35s", len(got) == 1)
    VT["t"] += 1.0
    view.slider.setValue(33000)
    view._on_slider_moved(33000)
    got = [c for c in view.vlc.commanded if c[0] == "set_time"]
    check("drag preview resumes after the throttle window", len(got) == 2)
    view._on_slider_released()
    check("release re-arms seek verify (anchored)",
          view._seek_verify is not None
          and abs(view._seek_verify[0]
                  - (32.966 - pv._GOP_ANCHOR_GUARD_S)) < 0.02,
          f"target={view._seek_verify[0] if view._seek_verify else None}")
    check("release clears seeking + resets preview throttle",
          view._seeking is False and view._scrub_prev_t == 0.0)
else:
    print("  (recording missing — view-level anchor checks skipped)",
          flush=True)

view._gop_teardown()
try:
    view.stop()
except Exception:
    pass

print(f"\n{len(PASS)} passed, {len(FAIL)} failed", flush=True)
sys.exit(1 if FAIL else 0)
