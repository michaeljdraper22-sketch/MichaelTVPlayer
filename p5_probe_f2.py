# -*- coding: utf-8 -*-
"""Probe 2: for a far-match sample, dump ALL released windows for the
painted key, the store cue's full text, and the earliest raw releases."""
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
                cue = None
                for s, e, txt in reversed(self.view._cap_cues.cues):
                    if s <= clock <= e + 0.25:
                        cue = (s, e, txt)
                        break
                done.append(1)
                print(f"\n*** P2 disp={disp:.2f} err={err:.2f}", flush=True)
                print(f"stored cue window=({cue[0]:.2f},{cue[1]:.2f}) "
                      f"fulltext={cue[2]!r}", flush=True)
                print(f"all wins for painted key: "
                      f"{[(round(s,1), round(e,1)) for s, e in wins][:12]}",
                      flush=True)
                q = self.queue
                print(f"raw_released n={len(q.raw_released)} "
                      f"pending n={len(q.pending)}", flush=True)
                print("first 6 raw releases:", flush=True)
                for s, e, txt in q.raw_released[:6]:
                    print(f"  ({s:.2f},{e:.2f}) {txt[:70]!r}", flush=True)
                print("raw releases with window end < 20:", flush=True)
                n = 0
                for s, e, txt in q.raw_released:
                    if e < 20.0 and n < 10:
                        print(f"  ({s:.2f},{e:.2f}) {txt[:70]!r}", flush=True)
                        n += 1
                if not n:
                    print("  (NONE — no raw release ends before t=20!)",
                          flush=True)
    except Exception as exc:  # noqa: BLE001
        print("probe2 error:", repr(exc), flush=True)
        done.append(1)


A.Harness.sample = probe
A.main()
