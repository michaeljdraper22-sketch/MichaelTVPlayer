# -*- coding: utf-8 -*-
"""Diagnostic probe (P5, throwaway): wrap Harness.sample during
scenario f and dump every painted sample whose best exact-tuple match
is far away — what was painted, from which stored cue, and where the
matching released windows actually sit."""
import os
import sys

sys.argv = ["x", "--quick", "--only:f"]
os.environ["MTP_ADV_DIAG"] = "1"

import test_sync_adversarial as A  # noqa: E402

orig_sample = A.Harness.sample
DUMPS = []


def probe_sample(self, t):
    orig_sample(self, t)
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
            if err > 10.0 and len(DUMPS) < 12:
                near = sorted(wins, key=lambda w: abs(disp - w[0]))[:3]
                # which stored cue did text_at use?
                cue = None
                for s, e, txt in reversed(self.view._cap_cues.cues):
                    if s <= clock <= e + 0.25:
                        cue = (s, e, txt[-60:])
                        break
                DUMPS.append(
                    f"t={t:7.1f} disp={disp:7.2f} err={err:6.2f} "
                    f"stored_cue={cue and (round(cue[0],1), round(cue[1],1))} "
                    f"near_wins={[(round(s,1), round(e,1)) for s, e in near]} "
                    f"painted={key}")
    except Exception as exc:  # noqa: BLE001
        DUMPS.append(f"probe error: {exc!r}")


A.Harness.sample = probe_sample

# announce ourselves between scenario prints
print("*** PROBE: dumping far-match painted samples ***", flush=True)
A.main()
print("*** PROBE DUMPS ***", flush=True)
for d in DUMPS:
    print(d, flush=True)
