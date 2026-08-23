# -*- coding: utf-8 -*-
"""WP3 anchor/store coherence unit tests (standalone, offscreen Qt).

Pins the DECISION TABLE of `_cc_flush_pending` (the WP3 design: lag EWMA
a=0.35, robust anchor-snap with a stable-target rule, pin-time store
positions) so the harness scenario (i) stays about outcomes while the
arithmetic is verified here:

  * robust snap — a LONE out-of-band gap (4-8 s) whose target MOVED
    (sample spike) rides the EWMA path (no rebase, store unmoved); a
    second consecutive one snaps; a huge gap (> _CC_REBASE_HARD_S)
    snaps immediately; a STABLE target 4-8 s away snaps at once (the
    snap-back after a wrong forced rebase — scenario f's contract)
  * store policy — cues keep their PIN-TIME positions: sustained small
    drift and zero-mean wobble NEVER move the store (the harness's
    raw-window truth shows shifts only drag correctly-pinned cues);
    rebase snaps slide everything (coherent before and after)
  * lag EWMA a=0.35 — one batch after an L step 5 -> 30 the estimate
    already covers >= 35% of the step (the WP3 retune; 0.18 covered 18%)
  * watchdog data-limited guard — the clock far past the newest
    delivered cue must not rebase (nothing can cover it); a true
    divergence near the newest cue still does

Run: .venv\\Scripts\\python.exe -X utf8 test_anchor_store.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5 import QtWidgets  # noqa: E402

import src.ui.player_view as pv  # noqa: E402
from src.ui.player_view import (  # noqa: E402
    PlayerView, _CC_ANCHOR_ALPHA, _CC_LAG_ALPHA, _CC_REBASE_HARD_S,
    _CC_REBASE_S, _CC_REBASE_STABLE_S)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"   [{detail}]" if detail else ""), flush=True)


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


class FakeVLC:
    def is_playing(self):
        return True

    def state_name(self):
        return "playing"

    def get_time(self):
        return 0

    def stop_and_release(self):
        pass


class FakeDVR:
    def __init__(self, path):
        self.file_path = path

    def buffer_file(self):
        return self.file_path

    def stop(self, delete=False):
        pass

    def safe_stop(self, delete=True):
        pass


import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
tmp = tempfile.mkdtemp(prefix="mtp_anchor_")
bufpath = os.path.join(tmp, "buffer.ts")
Path(bufpath).write_bytes(b"\x47" * 188 * 10)

from src.config import Config  # noqa: E402
cfg_path = os.path.join(tmp, "cfg.json")
Path(cfg_path).touch()
cfg = Config({}, Path(cfg_path))
view = PlayerView(cfg)
view.vlc = FakeVLC()
view.current = {"kind": "live", "url": "http://x/s.ts", "title": "anchor"}
view._mode = "chase"
view.dvr = FakeDVR(bufpath)

VT = {"t": 1_000_000.0}
pv.now_s = lambda: VT["t"]          # the sanctioned clock seam

# fixed world: edge = clock + backlog; targets crafted via (end, lag)
EDGE = 1000.0
view._cap_clock_s = 900.0
view._cap_backlog_s = 100.0

rebases = []
_real_rebase = view._cc_rebase


def counting_rebase(target_off, why):
    rebases.append((target_off, why))
    _real_rebase(target_off, why)


view._cc_rebase = counting_rebase


def flush(end, lag, head_rel=None):
    """One anchor batch: newest cue ends at `end`, measured lag sample
    `head_rel - end` (None = no probe -> _cc_lag untouched), pin target
    = edge - lag_est - end."""
    view._cc_lag = lag
    view._cc_pend = (end, None if head_rel is None
                     else end + lag)   # head_rel giving lag as the sample
    view._cc_flush_pending()


def seed(off):
    """First anchor: pins off exactly (fast-start path)."""
    view._cap_cues.clear()
    rebases.clear()
    view._cc_off = None
    view._cc_prev_target = None
    view._cc_oob_run = 0
    view._cc_lag = None
    end = EDGE - off            # target = edge - 0 - end = off (lag 0)
    view._cc_pend = (end, end)
    view._cc_flush_pending()
    assert abs(view._cc_off - off) < 1e-9, view._cc_off


# target for a batch that wants the anchor at `want`: end = edge - want
def batch(want, lag=0.0):
    flush(EDGE - want, lag)


# ----------------------------------------------------------------------------
# robust anchor-snap: lone spike rides the EWMA, confirmed/huge snaps
# ----------------------------------------------------------------------------
seed(100.0)
view._cap_cues.add(50.0, 52.0, "c1")     # a stored cue to watch
rebases.clear()
batch(106.0)                             # +6: lone out-of-band spike
check("snap: lone 4-8 s gap does NOT rebase (rides the EWMA)",
      not rebases and abs(view._cc_off - (100.0 + 6.0 * _CC_ANCHOR_ALPHA))
      < 1e-9,
      f"off={view._cc_off:.2f} rebases={len(rebases)}")
check("snap: lone spike leaves the STORE unmoved",
      view._cap_cues.cues[0][:2] == (50.0, 52.0),
      f"cues={view._cap_cues.cues}")
batch(100.0)                             # spike reverses (in-band now)
batch(100.0)
batch(100.0)
check("snap: the spike round-trips with no rebase at all",
      not rebases and abs(view._cc_off - 100.0) < 1.5,
      f"off={view._cc_off:.2f} rebases={len(rebases)}")

seed(100.0)
rebases.clear()
batch(106.0)                             # first out-of-band batch (off->103)
batch(108.0)                             # still >4 past the moved off: confirmed
check("snap: SECOND consecutive out-of-band batch snaps",
      len(rebases) == 1 and abs(view._cc_off - 108.0) < 1e-9,
      f"rebases={len(rebases)} off={view._cc_off:.2f}")
check("snap: the confirmed snap resets the out-of-band run",
      view._cc_oob_run == 0 and abs(view._cc_off - 108.0) < 1e-9,
      f"oob={view._cc_oob_run} off={view._cc_off:.2f}")

seed(100.0)
rebases.clear()
batch(112.0)                             # > HARD: real jump, immediate
check("snap: gap > _CC_REBASE_HARD_S snaps on the FIRST batch",
      len(rebases) == 1 and abs(view._cc_off - 112.0) < 1e-9,
      f"rebases={len(rebases)} off={view._cc_off:.2f} "
      f"hard={_CC_REBASE_HARD_S}")

# ----------------------------------------------------------------------------
# store policy: pin-time positions + stable-target snap
# ----------------------------------------------------------------------------
seed(100.0)
view._cap_cues.add(50.0, 52.0, "c1")
rebases.clear()
pin_pos = None
for k in range(1, 30):
    batch(100.0 + 2.0 * k)               # sustained +2/batch drift
    if pin_pos is None:
        pin_pos = view._cap_cues.cues[0][:2]
check("store: sustained SMALL drift never moves stored cues "
      "(pin-time positions are true)",
      not rebases
      and view._cap_cues.cues[0][:2] == pin_pos,
      f"cue={view._cap_cues.cues[0][:2]} pin={pin_pos} "
      f"rebases={len(rebases)}")

seed(100.0)
rebases.clear()
for k in range(60):                      # zero-mean wobble +-2 s
    batch(100.0 + (2.0 if k % 2 == 0 else -2.0))
check("store: 60 batches of +-2 s wobble never shift the store "
      "(zero steady-state jitter)",
      not rebases,
      f"rebases={len(rebases)}")

# stable-target snap: a wrong forced rebase leaves the anchor 6 s from a
# ROCK-STABLE true target — the first fresh flush must snap back and
# slide the store (scenario f's contract; a lone spike would NOT: its
# target moved, see the earlier snap checks)
seed(100.0)
view._cap_cues.add(50.0, 52.0, "c1")
batch(99.0)                              # establish the stable true target
off_pre = view._cc_off                   # 99.5 (one EWMA step from 100)
view._cc_rebase(off_pre - 6.0, "wrong-forced")   # store displaced -6
check("stable-snap: the forced displacement shifted the store",
      abs(view._cap_cues.cues[0][0] - 44.0) < 1e-9,
      f"cue={view._cap_cues.cues[0][:2]}")
batch(99.0)                              # target 99 again: stable vs prev
check("stable-snap: a stable target 5-6 s away snaps back immediately "
      "(no EWMA crawl)",
      len(rebases) == 2 and abs(view._cc_off - 99.0) < 1e-9,
      f"rebases={len(rebases)} (forced + snap-back) "
      f"off={view._cc_off:.2f}")
check("stable-snap: the store slides back with it (coherent timeline)",
      abs(view._cap_cues.cues[0][0] - 49.5) < 1e-9,
      f"cue={view._cap_cues.cues[0][:2]} (pin 50, forced -6, snap +5.5)")

# ----------------------------------------------------------------------------
# lag EWMA retune (a=0.35): one batch after an L step covers >= 35%
# ----------------------------------------------------------------------------
seed(100.0)
view._cc_lag = 5.0
# (end, head_rel) so lag_now = head_rel - end = 30
end = EDGE - 100.0 - 5.0
view._cc_pend = (end, end + 30.0)
view._cc_flush_pending()
check("lag: EWMA a=0.35 covers >= 35% of an L step in ONE batch",
      view._cc_lag >= 5.0 + 0.35 * 25.0 - 1e-9,
      f"L={view._cc_lag:.2f} (a={_CC_LAG_ALPHA}; step 5->30)")

# ----------------------------------------------------------------------------
# watchdog data-limited guard: the clock far PAST the newest delivered cue
# means the caption hasn't left the pipeline yet — rebasing cannot cover it
# ----------------------------------------------------------------------------
seed(100.0)
view._cap_cues.clear()
view._cap_cues.add(50.0, 52.0, "c1")
rebases.clear()
view._cc_last_c = 92.0            # newest cue's cx end (pinned ~92 + off)
view._cc_off = 8.0                # newest_end = 100
view._cc_store_base = 8.0
view._cc_lag = 5.0
view._cap_clock_s = 140.0         # 40 s past the newest window
view._cap_backlog_s = 200.0       # edge = 340 -> target = 340 - 5 - 92 = 243
view._cc_last_watchfire = 0.0
view._cc_last_active = 0.0
pv_now = pv.now_s
pv.now_s = lambda: 1000.0
view._cc_watchdog_fire(1000.0)
check("watchdog: data-limited (clock 40 s past the newest cue) does "
      "NOT rebase",
      not rebases and view._cc_off == 8.0
      and view._cap_cues.cues == [(50.0, 52.0, "c1")],
      f"rebases={len(rebases)} off={view._cc_off}")
view._cap_clock_s = 104.0         # 4 s past: a REAL divergence shape
target_near = 104.0 + 200.0 - 5.0 - 92.0   # edge - lag - last_c
view._cc_last_watchfire = 0.0
view._cc_last_active = 0.0
view._cc_watchdog_fire(1000.0)
check("watchdog: clock NEAR the newest cue still rebases (true "
      "divergence recovered)",
      len(rebases) == 1 and abs(view._cc_off - target_near) < 1e-9
      and abs(view._cap_cues.cues[0][0]
              - (50.0 + target_near - 8.0)) < 1e-9,
      f"rebases={len(rebases)} off={view._cc_off:.1f} "
      f"cue={view._cap_cues.cues[0][:2]}")
pv.now_s = pv_now

# ----------------------------------------------------------------------------
print()
print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
for f in FAIL:
    print("  FAILED:", f)
view.stop()
view.deleteLater()
sys.exit(1 if FAIL else 0)
