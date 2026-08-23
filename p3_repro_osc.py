# -*- coding: utf-8 -*-
"""Fast WP3 repro: the scenario-i geometry against the REAL anchor code,
no CCX/starve-guard — synthetic cues on a scripted head/L(t).

Reproduces (or exonerates) the osc-phase p95 ~11 s painted displacement
with zero store shifts, in seconds instead of the 13-min harness run.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5 import QtWidgets  # noqa: E402

import src.ui.player_view as pv  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


class FakeVLC:
    t = 0.0

    def is_playing(self):
        return True

    def get_time(self):
        return int(self.t * 1000)

    def state_name(self):
        return "playing"


import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
tmp = tempfile.mkdtemp(prefix="mtp_repro_")
bufpath = os.path.join(tmp, "buffer.ts")
Path(bufpath).write_bytes(b"\x47" * 188 * 10)

from src.config import Config  # noqa: E402
cfg_path = os.path.join(tmp, "cfg.json")
Path(cfg_path).touch()
cfg = Config({}, Path(cfg_path))
view = PlayerView(cfg)
view.vlc = FakeVLC()

VT = {"t": 1_000_000.0}
pv.now_s = lambda: VT["t"]

view.current = {"kind": "live", "url": "http://x/s.ts", "title": "repro"}
view._mode = "chase"
view.dvr = type("D", (), {"file_path": bufpath,
                          "buffer_file": lambda s: bufpath,
                          "stop": lambda s, delete=False: None,
                          "safe_stop": lambda s, delete=True: None})()
view._cap_cues.clear()
view._cc_off = None
view._cc_prev_target = None
view._cc_oob_run = 0
view._cc_lag = None
view._cc_last_c = None
view._cc_last_t = 0.0
view._cc_last_arrival = 0.0
view._cc_last_active = 0.0
view._cc_last_watchfire = 0.0


def lag_cycles(el):
    if el < 220.0:
        return 1.0 + 59.0 * (el / 220.0)
    if el < 250.0:
        return 60.0
    if el < 450.0:
        return 60.0 - 55.0 * ((el - 250.0) / 200.0)
    if el < 600.0:
        m = (el - 450.0) % 50.0
        return 14.0 + 12.0 * (1.0 - abs(m - 25.0) / 25.0)
    return 5.0


head = 0.0
view._cap_edge_s = lambda: head          # perfect edge (isolate the anchor)
view._frontier_s = lambda: max(0.0, head)
view._cap_backlog_s = 63.0               # viewer backlog post-D1-jump
view._cap_clock_s = head - 63.0

CUE_CAD = 0.7
CUE_WIN = 3.0
pending = []          # synthetic cue ends not yet eligible
next_end = 0.0
last_flush = None
perr = []             # (el, err): painted window vs its OWN raw window
n_store_shifts = 0
el = 0.0
step = 0.1
flush_every = 2.5
seen_len = len(view._cap_cues.cues)
while el < 640.0:
    VT["t"] += step
    el += step
    head += step
    while next_end < head - 0.5:
        pending.append(next_end)
        next_end += CUE_CAD
    L = lag_cycles(el)
    if last_flush is None or VT["t"] - last_flush >= flush_every:
        last_flush = VT["t"]
        delivered = [e for e in pending if head - e >= L]
        pending = [e for e in pending if head - e < L]
        if delivered:
            for e in delivered:
                view._on_cc_cue(e - CUE_WIN, e, f"cue {e:.1f}")
            # inject the measured lag sample: head_rel - end = L(t)
            newest = delivered[-1]
            view._cc_pend = (newest, newest + L)
            off_pre = view._cc_off
            view._cc_flush_pending()
            if 440.0 <= el <= 475.0 or 495.0 <= el <= 510.0:
                pin_new = delivered[-1] + (view._cc_off or 0.0)
                pin_new_pre = delivered[-1] + (off_pre or 0.0)
                print(f"    el={el:6.1f} L={L:5.1f} Lest="
                      f"{(view._cc_lag if view._cc_lag is None else round(view._cc_lag, 2))!s:>6} "
                      f"newest={newest:7.2f} pin(true axis)={head - L:7.2f} "
                      f"pin_pre={pin_new_pre:7.2f} pin_post={pin_new:7.2f} "
                      f"err_pre={pin_new_pre - (head - L):+6.2f} "
                      f"err_post={pin_new - (head - L):+6.2f}", flush=True)
    view._cap_clock_s += step
    view.vlc.t += step
    view._caption_tick()
    if len(view._cap_cues.cues) != seen_len:
        seen_len = len(view._cap_cues.cues)
    # painted window vs its own raw window (the text carries the raw end)
    clock = view._cap_clock_s
    for s_c, e_c, text in reversed(view._cap_cues.cues):
        if s_c <= clock <= e_c + 0.25:
            raw_end = float(text.split()[1])
            center = (s_c + e_c) / 2.0
            perr.append((el, abs(center - (raw_end - CUE_WIN / 2.0))))
            break
        if s_c < clock - 30:
            break

import statistics  # noqa: E402


def phase_p95(lo, hi):
    errs = sorted(e for t, e in perr if lo <= t < hi)
    if not errs:
        return float("nan"), 0
    return errs[min(len(errs) - 1, int(len(errs) * .95))], len(errs)


for name, lo, hi in (("ramp", 5, 220), ("drain", 250, 450),
                     ("osc", 450, 600), ("settle", 600, 640)):
    p, n = phase_p95(lo, hi)
    print(f"{name:8s} p95={p:6.2f} n={n}")
all_e = sorted(e for _, e in perr)
print(f"overall p95={all_e[min(len(all_e)-1, int(len(all_e)*.95))]:.2f} "
      f"max={all_e[-1]:.2f} n={len(all_e)}")
print(f"final off={view._cc_off}, lag={view._cc_lag}")
