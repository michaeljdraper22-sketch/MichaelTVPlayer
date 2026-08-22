# -*- coding: utf-8 -*-
"""WP0 mutation tests: prove the new harness checks CAN fail.

Three injected defects, each an in-process monkeypatch of the code under
test (no source edits; the working tree stays clean):

  m1  anchor+store displacement, held: at a fixed virtual moment (45 s
      after scenario start) view._cc_off += 5 with the cue store and
      filter windows shifted +5 in lockstep (the scenario-a wedge, now
      mid-steady-state), and every subsequent live-edge read is biased
      +5 so the anchor KEEPS re-deriving the displaced target — the
      model of a shipped mis-anchor. (An un-held one-shot shift self-
      heals via anchor-snap within one cue flush when data flows —
      scenario a's release freeze exists precisely to hold its wedge;
      a mutation must be held the same way to represent a shippable
      bug.)
      Expect: every painted-cue p95 / exact-window / steady-max check
      fails (~5 s displacement).

  m2  constant +3 s caption-clock skew: the clock the overlay keys on
      runs 3 s ahead of the displayed position.
      Expect: every painted-cue check fails (~3 s displacement).

  m3  stale store after a rebase: _cc_rebase updates the anchor but
      never shifts the stored cues (mixed-axis store — the WP3 bug
      class).
      Expect: scenario a's post-wedge outcome checks fail (the
      watchdog's corrective rebase leaves the wedged store in place);
      scenario f documents which regions see it.

Usage:
  .venv\\Scripts\\python.exe -X utf8 p0_mutations.py m1 a,f
  .venv\\Scripts\\python.exe -X utf8 p0_mutations.py m2 b
  .venv\\Scripts\\python.exe -X utf8 p0_mutations.py m3 a,f
Exits nonzero iff the run recorded failures (a mutation that passes
everything is a harness defect).
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MUT = sys.argv[1] if len(sys.argv) > 1 else "m1"
ONLY = sys.argv[2] if len(sys.argv) > 2 else "f"
sys.argv = [sys.argv[0], "--quick", f"--only:{ONLY}"]

from src.ui import player_view as pv  # noqa: E402
import test_sync_adversarial as harn  # noqa: E402

STATE = {"on": False, "t_fix": None}

if MUT == "m1":
    _orig_edge = pv.PlayerView._cap_edge_s
    _orig_flush = pv.PlayerView._cc_flush_pending

    def edge_patched(self):
        v = _orig_edge(self)
        return v + 5.0 if STATE["on"] else v

    def flush_patched(self):
        if STATE["t_fix"] is None:
            # fixed virtual moment, anchored at this scenario's first
            # anchor flush (setup() re-randomizes the VT base)
            STATE["t_fix"] = harn.VT.t + 45.0
        if not STATE["on"] and harn.VT.t >= STATE["t_fix"]:
            STATE["on"] = True
            self._cc_off = (self._cc_off or 0.0) + 5.0
            self._cap_cues.shift(5.0)
            self._filter_engine.shift_windows(5.0)
            print("  [m1] +5 s anchor/store displacement injected at "
                  f"VT={harn.VT.t:.1f} (held via edge bias)", flush=True)
        _orig_flush(self)

    pv.PlayerView._cap_edge_s = edge_patched
    pv.PlayerView._cc_flush_pending = flush_patched
elif MUT == "m2":
    _orig_clock = pv.PlayerView._caption_clock_s

    def clock_patched(self):
        return _orig_clock(self) + 3.0

    pv.PlayerView._caption_clock_s = clock_patched
    print("  [m2] constant +3 s caption-clock skew injected", flush=True)
elif MUT == "m3":
    _orig_rebase = pv.PlayerView._cc_rebase

    def rebase_patched(self, target_off, why):
        # the original, minus the cue-store shift
        shift = target_off - (self._cc_off if self._cc_off is not None
                              else 0.0)
        self._cc_off = target_off
        self._filter_engine.shift_windows(shift)

    pv.PlayerView._cc_rebase = rebase_patched
    print("  [m3] rebases no longer shift the cue store (stale store)",
          flush=True)
else:
    print(f"unknown mutation {MUT!r}")
    sys.exit(2)

rc = harn.main()
print(f"mutation {MUT} on scenarios {ONLY}: "
      f"{len(harn.PASS)} passed, {len(harn.FAIL)} failed "
      f"(exit {rc})", flush=True)
sys.exit(rc)
