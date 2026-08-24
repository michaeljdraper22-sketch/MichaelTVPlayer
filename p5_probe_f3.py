# -*- coding: utf-8 -*-
"""Probe 3: at the far-match sample, dump the store's earliest cues, the
app's cc source type, stash size, and store shift history if any."""
import os
import sys

sys.argv = ["x", "--quick", "--only:f"]
import test_sync_adversarial as A  # noqa: E402

orig = A.Harness.sample
done = []


def probe(self, t):
    orig(self, t)
    if done:
        return
    try:
        clock = self.view._cap_clock_s
        painted = [ln for ln in self.view._cap_wid._lines if ln.strip()]
        if not painted:
            return
        m0r = self.m0r if self.m0r is not None else (self.m0 or 0.0)
        disp = clock - m0r
        key = tuple(ln.lower() for ln in painted)
        self._sync_release_index()
        wins = self._rl_idx.get(key, ())
        if wins:
            err = min(abs(disp - min(max(disp, s), e)) for s, e in wins)
            if err > 10.0:
                done.append(1)
                v = self.view
                print(f"\n*** P3 disp={disp:.2f} err={err:.2f} "
                      f"clock={clock:.2f}", flush=True)
                print(f"cc_source type: {type(v._cc_source)}", flush=True)
                print(f"stash n={len(v._cc_stash) if v._cc_stash else 0} "
                      f"join_byte={getattr(v, '_cc_join_byte', None)} "
                      f"join_app_s={getattr(v, '_cc_join_app_s', None)}",
                      flush=True)
                cs = v._cap_cues
                print(f"store n={len(cs.cues)} — earliest 8:", flush=True)
                for s, e, txt in cs.cues[:8]:
                    print(f"  ({s:.2f},{e:.2f}) {txt[:60]!r}", flush=True)
                # store cues with start < 10: how many / what text
                early = [(s, e, txt) for s, e, txt in cs.cues if s < 10.0]
                print(f"store cues with start<10: n={len(early)}", flush=True)
                rel_early = [r for r in self.queue.raw_released if r[0] < 10.0]
                print(f"raw releases with start<10: n={len(rel_early)}",
                      flush=True)
    except Exception as exc:  # noqa: BLE001
        print("probe3 error:", repr(exc), flush=True)
        done.append(1)


A.Harness.sample = probe
A.main()
