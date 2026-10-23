# -*- coding: utf-8 -*-
"""WP2 wedge-cluster unit tests (standalone, offscreen Qt — no pytest).

Covers the pure pieces of the live-edge wedge cluster so the harness
scenarios (g/h) stay about outcomes while the MATH gets pinned here:

  * D1  _chase_jump_back_s — the adaptive jump-to-live formula
  * D2  _cc_join_byte — near-play join at ANY frontier (the >=90 s gate
    is gone) incl. the past-the-under-credited-frontier clamp
  * (a) seek-verify deadline arithmetic (target-proportional, capped) and
    the escalation backoff ladder / strike decay
  * (b) _head_ahead_s — PCR-axis data-ahead with the legacy frontier
    fallback; the rate-aware movement threshold keeps 0.125x slow-mo
    from reading as frozen
  * (c) _trickle_test / _raw_win_rate — the freeze-aware clock window

Run: .venv\\Scripts\\python.exe -X utf8 test_wedge_cluster.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5 import QtWidgets  # noqa: E402

import src.ui.player_view as pv  # noqa: E402
from src.ui.player_view import (  # noqa: E402
    PlayerView, _chase_jump_back_s, _cc_join_byte,
    _SEEK_VERIFY_BASE_S, _SEEK_VERIFY_PROP_S, _SEEK_VERIFY_MAX_S,
    _SEEK_ESC_BACKOFF_S, _CC_ADAPTIVE_MIN_L_S, _CHASE_SAFETY_S,
    _CC_JOIN_BACK_S, _CC_TRICKLE_WIN_S, _WEDGE_DATA_AHEAD_S,
    _HOLD_GIVE_UP_S, _CHASE_OPEN_DEAD_S)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "FAIL ") + name
          + (f"   [{detail}]" if detail else ""), flush=True)


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

# ----------------------------------------------------------------------------
# D1: adaptive jump-to-live
# ----------------------------------------------------------------------------
check("D1: no measured L / low L -> true edge (back = 5)",
      _chase_jump_back_s(None) == _CHASE_SAFETY_S
      and _chase_jump_back_s(0.0) == _CHASE_SAFETY_S
      and _chase_jump_back_s(_CC_ADAPTIVE_MIN_L_S) == _CHASE_SAFETY_S,
      f"back(None)={_chase_jump_back_s(None)} "
      f"back(8)={_chase_jump_back_s(_CC_ADAPTIVE_MIN_L_S)}")
check("D1: high L -> max(5, L+3) behind the head",
      _chase_jump_back_s(20.0) == 23.0
      and _chase_jump_back_s(240.0) == 243.0
      and _chase_jump_back_s(2.9) == 5.0,   # L+3 < 5 floors at the safety
      f"back(20)={_chase_jump_back_s(20.0)} back(240)="
      f"{_chase_jump_back_s(240.0)}")
check("D1: just above the threshold adapts (8.1 -> 11.1)",
      abs(_chase_jump_back_s(_CC_ADAPTIVE_MIN_L_S + 0.1)
          - (_CC_ADAPTIVE_MIN_L_S + 0.1 + 3.0)) < 1e-9,
      f"back(8.1)={_chase_jump_back_s(_CC_ADAPTIVE_MIN_L_S + 0.1)}")

# ----------------------------------------------------------------------------
# D2: near-play join at ANY frontier
# ----------------------------------------------------------------------------
sz = 1_000_000
j = _cc_join_byte(sz, 400.0, 390.0)
check("D2: mid-show engage joins ~8 s behind the position (fr 400)",
      sz * (390.0 - _CC_JOIN_BACK_S) / 400.0 - 188 <= j
      <= sz * (390.0 - _CC_JOIN_BACK_S) / 400.0,
      f"join={j} (~{j / sz * 400:.1f} s)")
j2 = _cc_join_byte(sz, 20.0, 15.0)     # old gate: join 0 below frontier 90
check("D2: the old >=90 s frontier gate is GONE (fr 20 joins near play)",
      j2 >= sz * (15.0 - _CC_JOIN_BACK_S) / 20.0 - 188,
      f"join={j2} (~{j2 / sz * 20:.1f} s; byte-0 would be 0)")
j3 = _cc_join_byte(sz, 57.0, 94.0)     # viewer past the under-credited fr
check("D2: viewer past the frontier clamps to the frontier (no tail "
      "overshoot)", j3 <= sz - 188 and j3 < sz * 0.9,
      f"join={j3}")
check("D2: degenerate inputs -> byte 0",
      _cc_join_byte(100, 20.0, 15.0) == 0
      and _cc_join_byte(sz, 0.0, 5.0) == 0
      and _cc_join_byte(sz, 20.0, 3.0) == 0,   # position inside the join pad
      "")

# ----------------------------------------------------------------------------
# (a) seek-verify: deadline arithmetic + escalation ladder (pure parts)
# ----------------------------------------------------------------------------
def deadline_for(jump):
    return min(_SEEK_VERIFY_MAX_S,
               _SEEK_VERIFY_BASE_S + _SEEK_VERIFY_PROP_S * jump)


check("(a): small seeks verify fast (base window)",
      abs(deadline_for(0.0) - _SEEK_VERIFY_BASE_S) < 1e-9,
      f"{deadline_for(0.0):.2f}s")
check("(a): deadline grows with jump distance, capped",
      abs(deadline_for(10.0) - 3.0) < 1e-9
      and abs(deadline_for(60.0) - 6.0) < 1e-9
      and abs(deadline_for(600.0) - _SEEK_VERIFY_MAX_S) < 1e-9,
      f"10s->{deadline_for(10.0):.1f} 60s->{deadline_for(60.0):.1f} "
      f"600s->{deadline_for(600.0):.1f}")
check("(a): backoff ladder is strictly increasing and bounded",
      _SEEK_ESC_BACKOFF_S[0] < _SEEK_ESC_BACKOFF_S[1]
      < _SEEK_ESC_BACKOFF_S[-1] and _SEEK_ESC_BACKOFF_S[0] >= 5.0,
      f"{_SEEK_ESC_BACKOFF_S}")

# ----------------------------------------------------------------------------
# view-level checks (offscreen Qt, fake player/DVR — no CCX, no network)
# ----------------------------------------------------------------------------
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
        self.state = "playing"


class FakeDVR:
    def __init__(self, path):
        self.file_path = path
        self.running = False   # no real recorder: the stall watchdog skips

    def buffer_file(self):
        return self.file_path

    def stop(self, delete=False):
        pass

    def safe_stop(self, delete=True):
        pass


import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
tmp = tempfile.mkdtemp(prefix="mtp_wedge_")
bufpath = os.path.join(tmp, "buffer.ts")
Path(bufpath).write_bytes(b"\x47" * 188 * 10)

cfg_path = os.path.join(tmp, "cfg.json")
Path(cfg_path).touch()
from src.config import Config  # noqa: E402
cfg = Config({}, Path(cfg_path))
view = PlayerView(cfg)
view.vlc = FakeVLC()
view.current = {"kind": "live", "url": "http://x/s.ts", "title": "wedge"}
view._mode = "chase"
view.dvr = FakeDVR(bufpath)

VT = {"t": 1_000_000.0}
pv.now_s = lambda: VT["t"]          # the sanctioned seam


def esc_ladder():
    """Drive _verify_seek past the deadline N times; return the play_at
    targets (one per escalation, cooldowns respected via VT jumps)."""
    view.vlc.commanded.clear()
    outs = []
    for k in range(4):
        VT["t"] += 30.0                       # far past every cooldown
        view._seek_verify = (50.0 + k, 50.0 + k,
                             VT["t"] - 1.0, VT["t"] - 10.0,
                             -1.0, True, False)
        view._verify_seek(VT["t"], 999.0)     # raw nowhere near target
        pas = [c for c in view.vlc.commanded if c[0] == "play_at"]
        if pas:
            outs.append(pas[-1][1])
    return outs


# a seek that no-ops escalates to the play_at revive at the target
view._last_reopen = 0.0
view._seek_esc_strikes = 0
view._seek_esc_ok_at = 0.0
view._cap_div_s = 0.0
view._cap_div_ok = False
ladder = esc_ladder()
check("(a): a confirmed no-op escalates to play_at at the seek target",
      len(ladder) >= 1 and abs(ladder[0] - 50.0) < 1e-9,
      f"play_at targets: {ladder}")

# the strike ladder limits escalation frequency: after a strike the next
# one waits for its backoff even with the deadline long past
view._last_reopen = 0.0
view._seek_esc_strikes = 0
view._seek_esc_ok_at = 0.0
view.vlc.commanded.clear()
VT["t"] += 100.0
view._seek_verify = (10.0, 10.0, VT["t"] - 1.0, VT["t"] - 10.0, -1.0, True, False)
view._verify_seek(VT["t"], 999.0)             # strike 1 -> play_at
n_after_1 = len([c for c in view.vlc.commanded if c[0] == "play_at"])
VT["t"] += 3.0                                # inside the 5 s reopen cd
view._seek_verify = (20.0, 20.0, VT["t"] - 1.0, VT["t"] - 10.0, -1.0, True, False)
view._verify_seek(VT["t"], 999.0)
n_after_2 = len([c for c in view.vlc.commanded if c[0] == "play_at"])
check("(a): escalation respects the reopen cooldown (no burst)",
      n_after_1 == 1 and n_after_2 == 1,
      f"play_at after 1st={n_after_1} after cd-violating 2nd={n_after_2}")

# a clean verify decays the strike ladder
view._seek_verify = None
view._seek_esc_strikes = 2
view._seek_esc_clean = VT["t"] - 5.0
VT["t"] += 100.0                              # > _SEEK_ESC_DECAY_S
view._verify_seek(VT["t"], 0.0)
check("(a): clean verifies decay the strike ladder",
      view._seek_esc_strikes == 0,
      f"strikes={view._seek_esc_strikes}")

# ----------------------------------------------------------------------------
# (b): head-ahead on the PCR axis + legacy fallback + slow-mo movement
# ----------------------------------------------------------------------------
view._cc_head_pcr = (180.0, VT["t"])          # PCR head (abs), fresh:
#                                            # head_rel 80 + join_app 2 = 82
view._sync_pcr_join = (100, 100.0)            # join PCR pinned at 100
view._cc_join_app_s = 2.0                     # join byte sits at content 2
view._vid_s = 50.0
check("(b): head_ahead uses the PCR content axis (head - current)",
      abs(view._head_ahead_s(50.0) - 32.0) < 1e-9,
      f"ahead={view._head_ahead_s(50.0)} want 32")
check("(b): head_ahead > data-ahead threshold at 32 (would rescue)",
      view._head_ahead_s(50.0) > _WEDGE_DATA_AHEAD_S, "")
view._cc_head_pcr = None                      # PCR pins unavailable
view._dvr_content_s = 40.0
view._dvr_base = 0.0
check("(b): without PCR pins it falls back to the legacy frontier gap",
      abs(view._head_ahead_s(50.0) - (-10.0)) < 1e-9,
      f"ahead={view._head_ahead_s(50.0)} want -10 (negative = unreachable)")
view._cc_head_pcr = (180.0, VT["t"])          # restore for nothing further;
#                                            # (b)'s PCR branch is covered

# rate-aware movement: drive the REAL tick at 0.125x — raw advancing
# exactly rate*dt per tick must refresh _raw_change_wall (a flat 0.05 s
# threshold read legit slow-mo as frozen and would reopen it every
# cooldown once data ran ahead — the tick's actual behavior is asserted,
# not the formula, so a mutated threshold cannot self-neutralize)
view._rate = 0.125
view._chase_started = True
view._tick_t = None
view._last_raw = None
view._raw_change_wall = 0.0
view._raw_win = []
view._trickle_hold = False
# seed a healthy DVR content clock: _note_dvr_data's first-sighting path
# would zero _dvr_content_s (frontier 0 -> raw insane -> no movement ever)
view._dvr_first_data = VT["t"] - 300.0
view._dvr_content_s = 300.0
view._dvr_base = 0.0
view._dvr_size = os.path.getsize(bufpath)
view._dvr_last_growth = VT["t"]
VT["t"] += 1.0
view.vlc.t = 100.0
view._tick()                      # baseline tick: seeds _last_raw
w0 = view._raw_change_wall
VT["t"] += 0.4
view.vlc.t = 100.05               # exactly rate*dt of advance
view._tick()
check("(b): 0.125x slow-mo counts as movement in the tick (no false "
      "wedge)", view._raw_change_wall > w0,
      f"wall {w0:.2f} -> {view._raw_change_wall:.2f} "
      f"(advance 0.05 s/tick)")

# ----------------------------------------------------------------------------
# (c): trickle window verdict + measured raw rate
# ----------------------------------------------------------------------------
view._raw_win = [(VT["t"] - _CC_TRICKLE_WIN_S, 10.0),
                 (VT["t"] - 2.0, 10.3),
                 (VT["t"], 10.4)]
view._rate = 1.0
check("(c): a 0.2x trickle over the window -> hold",
      view._trickle_test(VT["t"], True) is True,
      f"raw_adv=0.4 wall=3.0 -> ratio 0.13")
view._raw_win = [(VT["t"] - _CC_TRICKLE_WIN_S, 10.0),
                 (VT["t"], 13.0)]
check("(c): healthy 1:1 playback -> no hold",
      view._trickle_test(VT["t"], True) is False,
      "raw_adv=3.0 wall=3.0")
check("(c): paused / not playing -> never holds",
      view._trickle_test(VT["t"], False) is False
      and (view.__setattr__("_chase_paused", True),
           view._trickle_test(VT["t"], True) is False)[-1],
      "")
view._chase_paused = False
view._raw_win = [(VT["t"] - _CC_TRICKLE_WIN_S, 10.0), (VT["t"], 10.4)]
check("(c): measured raw window rate ~0.13x",
      abs(view._raw_win_rate(VT["t"]) - 0.4 / 3.0) < 0.01,
      f"rate={view._raw_win_rate(VT['t']):.3f}")
view._raw_win = [(VT["t"] - _CC_TRICKLE_WIN_S, 10.0), (VT["t"], 30.0)]
check("(c): raw rate clamps at the playback rate",
      abs(view._raw_win_rate(VT["t"]) - 1.0) < 1e-9,
      f"rate={view._raw_win_rate(VT['t']):.3f}")
view._raw_win = [(VT["t"] - 1.0, None), (VT["t"], 10.0)]
check("(c): insane readings fall back to the playback rate",
      abs(view._raw_win_rate(VT["t"]) - 1.0) < 1e-9, "")

# ----------------------------------------------------------------------------
# tail-hold at the write head (2026-10-11 14:44 NFL Network incident: the
# rescue reopened 2-3 s behind the end point every cooldown and the viewer
# watched the same seconds replay forever). The tick's actual behavior is
# asserted: eat-to-head HOLDS, resumes FORWARD from the freeze point once
# data exists past it, gives up to the plain-live fallback when it doesn't.
# ----------------------------------------------------------------------------
view._mode = "chase"
view.dvr = FakeDVR(bufpath)
view._rate = 1.0
view._chase_started = True
view._chase_paused = False
view._chase_hold = None
view._seek_verify = None
view._stall_ticks = 5            # past the 3-tick not-playing gate
view._last_reopen = 0.0
view._tick_t = None
view._vid_s = 349.0              # where the player ate to the file end
view._last_raw = 349.0
view._raw_change_wall = 0.0
view._raw_win = []
view._trickle_hold = False
view._dvr_first_data = VT["t"] - 300.0
view._dvr_content_s = 40.0       # frontier 340 (under-credits, like live)
view._dvr_base = 300.0
view._dvr_size = os.path.getsize(bufpath)
view._dvr_last_growth = VT["t"]
view._cap_clock_s = 349.0
view._cap_wall = VT["t"]
view._cap_div_s = 0.0
view._cap_div_ok = False
view._cc_head_pcr = (350.0, VT["t"])   # content head 252 -> BEHIND freeze
view._sync_pcr_join = (100, 100.0)     # (a real provider trickle)
view._cc_join_app_s = 2.0
view.vlc.state = "ended"
view.vlc.paused = False
view.vlc.t = 349.0
view.vlc.commanded.clear()
VT["t"] += 1.0
view._tick()                     # the rescue fires: tail-eat branch
check("tail-hold: eat-to-head enters the hold, never reopens",
      view._chase_hold is not None
      and abs(view._chase_hold[0] - 349.0) < 0.5
      and not [c for c in view.vlc.commanded
               if c[0] in ("play_at", "set_time")],
      f"hold={view._chase_hold} cmd={view.vlc.commanded}")
check("tail-hold: the DVR pill explains the freeze",
      bool(view._dvr_status.text()), f"text={view._dvr_status.text()!r}")

# holding ticks with still no data: no transport commands, still holding
view.vlc.commanded.clear()
VT["t"] += 3.0
view._tick()
VT["t"] += 3.0
view._tick()
check("tail-hold: a starved hold waits silently (no rewind)",
      view._chase_hold is not None
      and not [c for c in view.vlc.commanded
               if c[0] in ("play_at", "set_time")],
      f"hold={view._chase_hold} cmd={view.vlc.commanded}")

# the burst lands: 53 s of data ahead of the freeze point -> resume AT the
# freeze point (forward, no rewind)
view._cc_head_pcr = (500.0, VT["t"])   # content head 402 vs freeze 349
view.vlc.commanded.clear()
VT["t"] += 0.4
view._tick()
pas = [c for c in view.vlc.commanded if c[0] == "play_at"]
check("tail-hold: data past the freeze point resumes AT it (forward)",
      view._chase_hold is None and len(pas) == 1
      and abs(pas[0][1] - 349.0) < 0.5,
      f"play_at={pas} hold={view._chase_hold}")
check("tail-hold: the pill clears on resume",
      not view._dvr_status.isVisible(), f"visible={view._dvr_status.isVisible()}")

# a user transport during the hold clears it (rewind/LIVE/play win)
view._mode = "chase"
view.dvr = FakeDVR(bufpath)
view._chase_hold = [349.0, VT["t"]]
view._chase_fail_count = 0
view.vlc.state = "ended"
view.vlc.commanded.clear()
view._chase_seek(100.0)
check("tail-hold: a user seek cancels the hold",
      view._chase_hold is None
      and any(c[0] == "play_at" for c in view.vlc.commanded),
      f"hold={view._chase_hold}")

# no data for _HOLD_GIVE_UP_S: give up to the plain-live fallback ladder
view._mode = "chase"
view.dvr = FakeDVR(bufpath)
view._chase_hold = [349.0, VT["t"] - _HOLD_GIVE_UP_S - 1.0]
view._chase_fail_count = 0
view._cc_head_pcr = (350.0, VT["t"] - 10.0)   # starved again (no burst)
view.vlc.state = "ended"
view.vlc.commanded.clear()
view._chase_hold_tick(VT["t"])
check("tail-hold: no data within the give-up window -> plain-live fallback",
      view._chase_hold is None and view._mode == "live" and view.dvr is None
      and any(c[0] == "play" for c in view.vlc.commanded),
      f"mode={view._mode} dvr={view.dvr} cmd={view.vlc.commanded}")

# a paused hold must not resume under the user
view._mode = "chase"
view.dvr = FakeDVR(bufpath)
view._chase_hold = [349.0, VT["t"]]
view._chase_paused = True
view._cc_head_pcr = (500.0, VT["t"])
view._chase_hold_tick(VT["t"])
check("tail-hold: paused outranks the hold (no resume under the user)",
      view._chase_hold is not None,
      f"hold={view._chase_hold}")
view._chase_paused = False

# ----------------------------------------------------------------------------
# axis-divergence poisoning (2026-10-11 15:03 incident): a lone garbage
# get_time spike (transient during a reopen) captured div=+1495 — every
# later play_at start-time doubled (seeking past the write head: instant
# end, rescue storm) and a pause/play revive converted raw 0 through the
# poisoned div into a NEGATIVE content position, replaying the game from
# 0:00. A renumber is now only trusted after a ~1:1 continuation, and a
# transport seed voids the old div entirely.
# ----------------------------------------------------------------------------
view._mode = "chase"
view.dvr = FakeDVR(bufpath)
view._rate = 1.0
view._chase_paused = False
view._trickle_hold = False
view._seek_verify = None
view.vlc.state = "playing"
view.vlc.paused = False


def clock_seed(pos):
    view._cap_clock_s = pos
    view._cap_raw_s = pos
    view._cap_raw_clock = pos
    view._cap_wall = VT["t"]
    view._cap_raw_wall = VT["t"]
    view._cap_div_s = 0.0
    view._cap_div_ok = False
    view._cap_renum_pend = None
    view.vlc.t = pos


# spike while PLAYING, then the old axis resumes: no div may be captured
clock_seed(100.0)
VT["t"] += 0.4
view.vlc.t = 2992.0                     # the garbage spike
view._caption_clock_s()
VT["t"] += 0.4
view.vlc.t = 100.8                      # old axis continues (spike was a lie)
view._caption_clock_s()
check("div: an unconfirmed raw spike never poisons the axis",
      not view._cap_div_ok and abs(view._cap_vlc_time_for(150.0)
                                   - 150.0) < 1e-9,
      f"div_ok={view._cap_div_ok} div={view._cap_div_s}")

# spike while NOT playing (ended/opening get_time lies): never even pending
clock_seed(100.0)
view.vlc.state = "ended"
VT["t"] += 0.4
view.vlc.t = 2992.0
view._caption_clock_s()
check("div: a jump while not playing is never axis evidence",
      view._cap_renum_pend is None and not view._cap_div_ok,
      f"pend={view._cap_renum_pend} div_ok={view._cap_div_ok}")
view.vlc.state = "playing"

# a REAL renumber (jump, then ~1:1 continuation) is confirmed and converts
clock_seed(100.0)
VT["t"] += 0.4
view.vlc.t = 2592.0                     # the jump
view._caption_clock_s()
VT["t"] += 0.4
view.vlc.t = 2592.4                     # continues 1:1 from the new number
view._caption_clock_s()
check("div: a confirmed renumber captures the axis offset",
      view._cap_div_ok and abs(view._cap_div_s - 2492.0) < 1.0,
      f"div_ok={view._cap_div_ok} div={view._cap_div_s}")
check("div: seeks convert through a confirmed offset",
      abs(view._cap_vlc_time_for(150.0) - (150.0 + view._cap_div_s))
      < 1e-9, "")

# a transport seed voids even a previously captured div (the open lands
# raw at the commanded number; a stale div must not double the next
# play_at start-time: live-seen 1495 -> 2991 past the write head)
view._cap_seed_transport(50.0)
check("div: a transport seed voids the stale axis divergence",
      not view._cap_div_ok and view._cap_div_s == 0.0
      and view._cap_renum_pend is None,
      f"div_ok={view._cap_div_ok} div={view._cap_div_s} "
      f"pend={view._cap_renum_pend}")

# the pause/play revive on an ended player converts raw 0 through whatever
# div exists: it must clamp to the buffer start (>= 0), never negative
view._cap_div_s = 1495.7                # worst case: div poisoned anyway
view._cap_div_ok = True
view.vlc.state = "ended"
view.vlc.t = 0.0
view._mode = "chase"
view.dvr = FakeDVR(bufpath)
view._chase_hold = None
view._chase_fail_count = 0
view.vlc.commanded.clear()
view._toggle_pause()                    # ended -> play revives via seek
pas = [c for c in view.vlc.commanded if c[0] == "play_at"]
check("div: the ended-player play revive never seeks negative",
      len(pas) == 1 and pas[0][1] >= 0.0,
      f"play_at={pas}")

# ----------------------------------------------------------------------------
# dead-open rescue (2026-10-11 15:05:36: a reopen landed past the write
# head and ENDED instantly — never a playing tick, so _chase_started
# stayed False and the stall rescue (which requires it) was structurally
# disarmed: the screen sat dead in silence, "stopped for no reason")
# ----------------------------------------------------------------------------
view._mode = "chase"
view.dvr = FakeDVR(bufpath)
view._chase_hold = None
view._chase_started = False         # the open never played a tick
view._chase_paused = False
view._stall_ticks = 0
view._seek_verify = None
view._reopen_repeats = 0
view._reopen_last_at = -1e9
view._reopen_last_t = 0.0
view._last_reopen = VT["t"] - _CHASE_OPEN_DEAD_S - 1.0
view._vid_s = 200.0
view._cap_clock_s = 200.0
view._cap_wall = VT["t"]
view._cap_div_s = 0.0
view._cap_div_ok = False
view._cap_renum_pend = None
view._dvr_first_data = VT["t"] - 300.0
view._dvr_content_s = 40.0
view._dvr_base = 300.0
view._dvr_size = os.path.getsize(bufpath)
view._dvr_last_growth = VT["t"]
view._cc_head_pcr = None
view.vlc.state = "ended"
view.vlc.t = 0.0
view._tick_t = None
view._last_raw = None
view._raw_change_wall = 0.0
view._raw_win = []
view._trickle_hold = False
view.vlc.commanded.clear()
VT["t"] += 1.0
view._tick()
pas = [c for c in view.vlc.commanded if c[0] == "play_at"]
# 2026-10-19: the reopen anchor is nudged _RESCUE_GOP_NUDGE_S forward
# past the damaged GOP (issue #12's replay-per-cut) — never backward
check("dead-open: an open that never plays gets rescued (nudged past the GOP)",
      len(pas) == 1 and abs(pas[0][1] - 203.0) < 1.0,
      f"play_at={pas}")

# a FRESH open still inside the dead-open window must not be disturbed
view._chase_started = False
view._last_reopen = VT["t"] - 3.0
view.vlc.commanded.clear()
VT["t"] += 0.4
view._tick()
pas = [c for c in view.vlc.commanded if c[0] == "play_at"]
check("dead-open: a fresh open within the window is left alone",
      not pas, f"play_at={pas}")

# ----------------------------------------------------------------------------
try:
    view.stop()
except Exception:
    pass
view.deleteLater()
app.processEvents()

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed", flush=True)
for f in FAIL:
    print("  FAILED:", f, flush=True)
sys.exit(1 if FAIL else 0)
