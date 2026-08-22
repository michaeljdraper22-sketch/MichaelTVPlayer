# -*- coding: utf-8 -*-
r"""Stage-3 constant retune: what anchor alpha centers steady-state lead?

Replays the stage-2 definitive matrix (mtp.sync log, alpha=0.25 per-cue
EWMA + snap rebases) against alternative _CC_ANCHOR_ALPHA values and an
optional fixed lead compensation, reporting per-phase bias of the anchor
off vs. the burst-immune pin target (edge - L - end).

Sign convention: off is ADDED to cue times, so off > target paints
captions LATE; innovation = target - off > 0 means the anchor sits EARLY.

Usage:
  .venv\\Scripts\\python.exe -X utf8 sync_stage3_retune.py [sync_debug.log]
"""
import os
import re
import sys
from statistics import median

LOG = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("APPDATA") or "", "MichaelTVPlayer", "sync_debug.log")

CUE = re.compile(
    r"^(\d\d):(\d\d):(\d\d)\.(\d\d\d) CUE cx=\[[\d.]+\.\.([\d.]+)\] "
    r"adv=\S+ el=[\d.]+ \S+ \| off=([\d.\-]+(?:->[\d.\-]+)?) "
    r"fr=[\d.]+ cc=[\d.]+ raw=[\d.\-]+ lead=\S+ innov=([\d.\-]+) ")
PHASE = re.compile(r"^(?:\d\d:){2}\d\d\.\d\d\d PHASE (\w+)")
REBASE = re.compile(r"^(?:\d\d:){2}\d\d\.\d\d\d REBASE")


def tsec(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]


def simulate(cues, alpha, lead_comp=0.0):
    """cues: list of (phase, target, was_rebase). Returns per-phase bias
    samples of (off - target_true) where target_true excludes lead_comp
    (the compensation shifts the TARGET the anchor chases)."""
    off = None
    out = {}
    for phase, target, rebased in cues:
        t_true = target
        t_chase = target - lead_comp
        if off is None or rebased:
            off = t_chase
        else:
            off += (t_chase - off) * alpha
        out.setdefault(phase, []).append(off - t_true)
    return out


def report(tag, phases, order):
    print(f"\n== alpha/lead = {tag} ==")
    print(f"{'phase':<10} {'n':>5} {'bias p50':>9} {'bias mean':>10} "
          f"{'|bias| p95':>11}")
    for ph in order:
        v = phases.get(ph) or []
        sv = sorted(v)
        mean = sum(v) / len(v) if v else float("nan")
        print(f"{ph:<10} {len(v):>5} {pct(sv, 0.5):>+9.2f} {mean:>+10.2f} "
              f"{pct(sorted(abs(x) for x in v), 0.95):>11.2f}")


def main():
    cues = []
    phase = "?"
    alphas = set()
    for line in open(LOG, encoding="utf-8", errors="replace"):
        m = PHASE.match(line)
        if m:
            phase = m.group(1)
            continue
        if REBASE.match(line):
            # next CUE row's off reflects the snap: mark it
            cues.append(("__rebase__", 0.0, True))
            continue
        m = CUE.match(line)
        if not m:
            continue
        end = float(m.group(5))
        offv = m.group(6)
        innov = m.group(7)
        if innov == "-" or "->" not in offv:
            continue
        a, b = (float(x) for x in offv.split("->"))
        innov = float(innov)
        # the producing run's alpha (median-checked below)
        if abs(innov) > 0.3:
            alphas.add(round((b - a) / innov, 2))
        target = a + innov          # off_before + (target - off_before)
        if cues and cues[-1][0] == "__rebase__":
            cues[-1] = (phase, target, True)
        else:
            cues.append((phase, target, False))
    cues = [c for c in cues if c[0] != "__rebase__"]
    if not cues:
        print("no parsable CUE rows")
        return 1
    order = []
    for ph, _, _ in cues:
        if ph not in order:
            order.append(ph)
    prod_alpha = median(sorted(alphas)) if alphas else float("nan")
    print(f"parsed {len(cues)} cue targets across phases: {order}")
    print(f"producing run's effective alpha (median of steps): "
          f"{prod_alpha:.2f}")

    for alpha in (0.14, 0.18, 0.25, 0.35):
        report(f"alpha={alpha:.2f}", simulate(cues, alpha), order)
    # lead compensation candidates against the measured steady bias
    report("alpha=0.18 lead=-0.15", simulate(cues, 0.18, -0.15), order)
    report("alpha=0.18 lead=+0.15", simulate(cues, 0.18, 0.15), order)
    return 0


if __name__ == "__main__":
    sys.exit(main())
