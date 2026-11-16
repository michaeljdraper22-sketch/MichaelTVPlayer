# -*- coding: utf-8 -*-
"""Unseekable DVR buffer: the 2-second replay loop (2026-11-14 field report).

"mtv is having a stroke" — the picture played ~2 s, froze 8 s, and repeated
every ~11 s on a live channel. The log showed rescues at
``play_at 627.5s`` / ``693.4s`` / ``899.0s`` all opening ``start=19.9s`` and
landing ``raw=21.6``, plus "seek verify: play_at landed at 0.0s".

Root cause (measured with probe_chase_wedge.py / probe_chase_seek.py against
the real temp buffer): after the recorder's watchdog rotated the buffer
(``buffer.ts`` -> ``buffer_2.ts``), the fresh sibling held a video PTS
discontinuity near its head (22.005 -> 20.520 s). libvlc then could not seek
in it at all — ``:start-time`` was dropped (landed at 0.0) and ``set_time``
snapped straight back — so every rescue reopened at the file start, replayed
the same couple of seconds, and wedged again. The verify ladder revived into
that buffer forever; nothing ever concluded "this buffer cannot be seeked".

Fix (two detectors, one escape):
  1. verify strikes — a play_at that lands at the file start for a deep
     target, in EITHER form: raw parked small at the deadline, or the
     advancing "landed at 0.0s — correcting with set_time" branch
     (2026-11-14 17:30: every rescue re-armed a fresh verify, so the
     deadline ladder never ran);
  2. head-pinned reopens — the axis/GOP mapping lands the reopen near the
     file HEAD (vlc seconds) while the content target is deep
     (2026-11-14 noon: the poisoned div made the verify read CLEAN, so
     only the reopen site could see it).
Three strikes of either hand the buffer to the plain-live fallback
(fresh buffer near the live edge) instead of reviving into it forever.

Run:  .venv\\Scripts\\python.exe test_unseekable_buffer.py
"""
import copy
import inspect
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config, DEFAULTS  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import (PlayerView, _SEEK_ZERO_STRIKES)  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          ("   [" + detail + "]" if detail else ""))


class _StubVlc:
    def __init__(self):
        self.set_times = []
        self.play_ats = []

    def set_time(self, ms):
        self.set_times.append(ms)

    def play_at(self, url, t, **kw):
        self.play_ats.append((url, t))


class _StubDvr:
    def buffer_file(self):
        return "C:\\fake\\buffer.ts"


class _Harness:
    """PlayerView with the chase machinery stubbed: we drive _verify_seek
    directly and only observe which escape it chooses."""

    def __init__(self, view):
        self.view = view
        self.fallbacks = []
        self.revives = []
        view._fallback_from_chase = lambda waited: self.fallbacks.append(
            waited)
        view._chase_revive = lambda t, v, why: self.revives.append(
            (t, v, why))

    def arm(self, target, fix_tried=True, raw_pre=-1.0):
        v = self.view
        v._seek_verify = (float(target), float(target), 0.0, 0.0,
                          float(raw_pre), fix_tried, True)
        v._seek_esc_ok_at = 0.0
        v._last_reopen = 0.0
        v._chase_paused = False
        v._seek_esc_strikes = 0

    def verify(self, raw):
        self.view._verify_seek(pv_mod.now_s(), raw)


def main():
    app = (QtWidgets.QApplication.instance()
           or QtWidgets.QApplication(sys.argv))
    _ = app
    cfg = Config(copy.deepcopy(DEFAULTS),
                 Path(tempfile.gettempdir()) / "mtp_unseekable_settings.json")
    view = PlayerView(cfg)
    view._mode = "chase"
    h = _Harness(view)

    print("[1] three start-landings for a deep target -> plain-live fallback")
    for i in range(_SEEK_ZERO_STRIKES - 1):
        h.arm(890.0)
        h.verify(0.8 + 0.1 * i)     # the wedge: almost at the file start
        check(f"strike {i + 1}: accumulating, no fallback yet",
              not h.fallbacks, f"fallbacks={h.fallbacks}")
    h.arm(890.0)
    h.verify(0.8 + 0.1 * (_SEEK_ZERO_STRIKES - 1))
    check("fallback fired exactly once after %d strikes"
          % _SEEK_ZERO_STRIKES, len(h.fallbacks) == 1,
          f"fallbacks={h.fallbacks}")
    check("counter reset after handing over",
          view._seek_zero_landings == 0,
          str(view._seek_zero_landings))

    print("\n[2] one clean landing resets the ladder")
    h.fallbacks.clear()
    view._seek_zero_landings = 0
    for _ in range(_SEEK_ZERO_STRIKES - 1):
        h.arm(890.0)
        h.verify(0.9)
    check("two strikes recorded",
          view._seek_zero_landings == _SEEK_ZERO_STRIKES - 1,
          str(view._seek_zero_landings))
    h.arm(890.0)
    h.verify(890.4)                 # the demuxer did honour the seek
    check("clean verify clears the strikes",
          view._seek_zero_landings == 0, str(view._seek_zero_landings))
    h.arm(890.0)
    h.verify(0.9)
    check("later miss starts over, no fallback",
          view._seek_zero_landings == 1 and not h.fallbacks,
          f"n={view._seek_zero_landings} fallbacks={h.fallbacks}")

    print("\n[3] a near-start target is a normal miss, not the signature")
    h.fallbacks.clear()
    h.revives.clear()
    for _ in range(_SEEK_ZERO_STRIKES + 2):
        h.arm(5.0)                  # a deliberate rewind to the top
        h.verify(0.5)
    check("no fallback for a target near the start", not h.fallbacks,
          f"fallbacks={h.fallbacks}")
    check("still escalates normally", len(h.revives) == _SEEK_ZERO_STRIKES + 2,
          f"revives={len(h.revives)}")

    print("\n[4] a wrong-but-mid-file landing is not the signature")
    h.fallbacks.clear()
    h.revives.clear()
    view._seek_zero_landings = 0
    for _ in range(_SEEK_ZERO_STRIKES + 1):
        h.arm(890.0)
        h.verify(40.0)              # wrong, but clearly inside the file
    check("no fallback for mid-file landings", not h.fallbacks,
          f"fallbacks={h.fallbacks}")
    check("counter never armed", view._seek_zero_landings == 0,
          str(view._seek_zero_landings))

    print("\n[5] 'buffer is unseekable' wording is in the log path")
    src = inspect.getsource(PlayerView._verify_seek)
    check("reason logged for the field",
          "buffer is unseekable" in src)
    check("escape uses the existing ladder",
          "_fallback_from_chase" in src)

    print("\n[6] advancing variant (2026-11-14 17:30): the 'landed at "
          "0.0s'\n    corrective branch must count strikes too")
    view.dvr = _StubDvr()
    stub_vlc = _StubVlc()
    view.vlc = stub_vlc
    h.fallbacks.clear()
    view._seek_zero_landings = 0
    for i in range(_SEEK_ZERO_STRIKES - 1):
        h.arm(890.0, fix_tried=False, raw_pre=-1.0)
        h.verify(0.0)                # opened at the file head, advancing
        check(f"cycle {i + 1}: corrective set_time issued, no fallback",
              len(stub_vlc.set_times) == i + 1 and not h.fallbacks,
              f"set_times={len(stub_vlc.set_times)} "
              f"fallbacks={len(h.fallbacks)}")
    h.arm(890.0, fix_tried=False, raw_pre=-1.0)
    h.verify(0.0)
    check("third head-landing escapes without another futile set_time",
          len(stub_vlc.set_times) == _SEEK_ZERO_STRIKES - 1
          and len(h.fallbacks) == 1,
          f"set_times={len(stub_vlc.set_times)} "
          f"fallbacks={len(h.fallbacks)}")
    check("counter reset after the escape",
          view._seek_zero_landings == 0, str(view._seek_zero_landings))

    print("\n[7] head-pinned reopens (2026-11-14 noon: verify read CLEAN, "
          "only the\n    reopen site sees the poison)")
    h.fallbacks.clear()
    stub_vlc.play_ats.clear()
    view._reopen_head_pins = 0
    view._cap_div_s = -607.6         # poisoned axis: content 627 -> vlc ~20
    view._cap_div_ok = True
    view._last_reopen = 0.0
    view._poke_audio = lambda: None
    view._poke_rate = lambda: None
    for i in range(_SEEK_ZERO_STRIKES - 1):
        view._cap_div_s = -607.6     # the fold re-poisons between cycles
        view._cap_div_ok = True
        r = view._reopen_display(at=627.5)
        check(f"pinned reopen {i + 1} still plays, no fallback",
              r is True and not h.fallbacks and len(stub_vlc.play_ats)
              == i + 1, f"r={r} fallbacks={len(h.fallbacks)}")
    view._cap_div_s = -607.6
    view._cap_div_ok = True
    r = view._reopen_display(at=627.5)
    check("third pinned reopen falls back without playing into it",
          r is False and len(h.fallbacks) == 1
          and len(stub_vlc.play_ats) == _SEEK_ZERO_STRIKES - 1,
          f"r={r} fallbacks={len(h.fallbacks)} "
          f"plays={len(stub_vlc.play_ats)}")
    check("pin counter reset after the escape",
          view._reopen_head_pins == 0, str(view._reopen_head_pins))
    view._cap_div_s = 0.0            # healthy axis: deep content -> deep vlc
    h.fallbacks.clear()
    for _ in range(_SEEK_ZERO_STRIKES + 2):
        view._reopen_display(at=600.0)
    check("healthy deep reopens never fall back",
          not h.fallbacks and view._reopen_head_pins == 0,
          f"fallbacks={len(h.fallbacks)} "
          f"pins={view._reopen_head_pins}")

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
