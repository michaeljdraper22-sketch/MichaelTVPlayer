# -*- coding: utf-8 -*-
r"""WP3 retune: anchor/store coherence + L-compensation comparison.

Replays the pinned 2026-08-21 diagnosis matrix (``sync_retune_input.log``,
the 02:36 NFL matrix — the only one logged with per-cue L/innov fields:
3,558 target rows / 249 arrival batches across START..SPEED1X, L 0.9-31.3,
41 anchor-snap rebases in START alone) batch-by-batch through the WP3
candidate mechanisms:

  (a) STORE coherence — what already-stored cues do when a small
      (<= _CC_REBASE_S) anchor correction is accepted:
        A0     LANDED: stored cues keep their PIN-TIME positions; the
               store moves only on rebase snaps. (The harness's raw-
               window ground truth settled this: whole-store shifts on
               small corrections drag correctly-pinned cues with the
               regime swing — scenario i measured p95 7.9-11.2 for the
               shifting candidates vs 0.19 for A0.)
        A1     shift the store on every accepted EWMA step (rejected)
        A2pb   per-batch deadband + snap (rejected: steady noise pierces
               a small deadband every batch)
        A3     cumulative-debt deadband (rejected: the harness showed
               the debt shifts drag to-be-viewed cues by the swing)
  (b) L tracking — what lag estimate the PIN uses:
        Tinst  LANDED: the FRESH per-batch sample (head_rel - end at the
               flush). Smoothing L before pinning compounds the EWMA
               transient — p3_repro_osc.py measured a 10.6 s pin error
               on the first batch after a delivery pause through a fast
               L jump; the anchor's own a=0.5 EWMA provides smoothing.
        T0/T1/T1b/T1m  plain EWMA (0.18 current / 0.35 / 0.50 / +med3)
        T2m/T3m         lead-compensated / adaptive-alpha (the plan's
               candidates — both LOSE: per-batch speech-skew noise,
               p95 3.6 s, dwarfs the ramp signal and the derivative
               terms amplify it). The L EWMA itself stays at a=0.35 for
               D1 landing / no-probe fallback.
  (c) rebase policy:
        cur     pre-WP3: any |gap| > 4 s snaps (35-50 noise round-trips
                per session on the corpus)
        v2      LANDED: snap only when the correction is real — huge
                (> 8 s), persistent (2 consecutive OOB batches), or on a
                STABLE target (|target - prev| <= 0.8: a lone spike
                MOVES the target; the snap-back after a wrong forced
                rebase re-asserts a steady one — scenario f's contract)

Scores per combination:
  * ramp pin error  |L_est - L_true| where |regime slope| >= 0.1 s/s;
  * steady painted  SYNTHETIC steady-regime replay (constant L_true,
    measured cadence + measured sample noise): painted p95/max vs TRUE
    windows, store shifts and window travel — the JITTER criterion;
  * scrub coherence per-checkpoint (every 30 s) p95 / worst-checkpoint
    p95 of |displayed - pin-time-truth| over every stored cue. NOTE the
    corpus reference (pin with a +-12 s rolling-median L_true) is a
    proxy; the harness's raw cx windows are the real ground truth and
    overruled the store-policy ordering here (see (a)).

Usage:
  .venv\\Scripts\\python.exe -X utf8 sync_stage3_retune.py [corpus.log]
"""
import os
import re
import statistics
import sys
from collections import deque

LOG = sys.argv[1] if len(sys.argv) > 1 else "sync_retune_input.log"

CUE = re.compile(
    r"^(\d\d):(\d\d):(\d\d)\.(\d\d\d) CUE cx=\[[\d.]+\.\.([\d.]+)\] "
    r"adv=\S+ el=[\d.]+ \S+ \| off=([\d.\-]+(?:->[\d.\-]+)?) "
    r"fr=[\d.]+ cc=[\d.]+ raw=[\d.\-]+ lead=\S+ innov=([\d.\-]+) \| "
    r"pcr=[\d.]+ head_rel=[\d.]+ L=([\d.]+) lag_ewma=([\d.\-]+)")
TICK = re.compile(
    r"^(\d\d):(\d\d):(\d\d)\.(\d\d\d) TICK .*backlog=([\d.\-]+) "
    r"edge=([\d.\-]+)")
PHASE = re.compile(r"^(?:\d\d:){2}\d\d\.\d\d\d PHASE (\w+)")

BATCH_GAP_S = 0.6        # arrival rows within this gap = one flush batch
REBASE_S = 4.0           # _CC_REBASE_S: beyond this the anchor snaps
ANCHOR_ALPHA = 0.50      # _CC_ANCHOR_ALPHA: weighted step per batch
LAG_MAX_S = 240.0
TRUE_WIN_S = 12.0        # +/- s of batch samples in the L_true median
SLOPE_WIN_S = 12.0       # central median-difference span for the slope
RAMP_SLOPE = 0.10        # |slope| >= this (s/s) = ramp regime
STEADY_SLOPE = 0.05      # |slope| < this = steady-ish regime
REPLAY_S = 300.0         # synthetic steady replay length
CUE_WIN_S = 2.5          # synthetic speech-like cue window
CHKPT_S = 30.0           # scrub-coherence checkpoint cadence


def tsec(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def pct(vals, p):
    if not vals:
        return float("nan")
    sv = sorted(vals)
    return sv[min(len(sv) - 1, int(len(sv) * p))]


def median_win(pts, t, win):
    vals = [v for tt, v in pts if t - win <= tt <= t + win]
    if not vals:                     # sequence edges: nearest sample wins
        vals = [min(pts, key=lambda p: abs(p[0] - t))[1]]
    return statistics.median(vals)


# ----------------------------------------------------------------------------
# corpus
# ----------------------------------------------------------------------------
class Batch:
    __slots__ = ("t", "phase", "end", "L", "edge")

    def __init__(self, t, phase, end, L, edge):
        self.t = t                # wall time of the batch's first row
        self.phase = phase
        self.end = end            # newest cue's cx end
        self.L = L                # instant lag sample of that cue
        self.edge = edge          # write-head estimate (nearest TICK row)


def load_corpus(path):
    rows = []
    ticks = []
    phase = "?"
    for line in open(path, encoding="utf-8", errors="replace"):
        m = PHASE.match(line)
        if m:
            phase = m.group(1)
            continue
        m = TICK.match(line)
        if m:
            ticks.append((tsec(*m.group(1, 2, 3, 4)), float(m.group(6))))
            continue
        m = CUE.match(line)
        if not m:
            continue
        try:
            float(m.group(7))      # innov=- rows carry no target sample
        except ValueError:
            continue
        rows.append((tsec(*m.group(1, 2, 3, 4)), float(m.group(5)),
                     float(m.group(8)), phase))
    if not rows or not ticks:
        raise SystemExit("corpus parse failed: %d rows %d ticks"
                         % (len(rows), len(ticks)))
    groups = []
    cur = [rows[0]]
    for r in rows[1:]:
        if r[0] - cur[-1][0] > BATCH_GAP_S:
            groups.append(cur)
            cur = [r]
        else:
            cur.append(r)
    groups.append(cur)
    batches = []
    for g in groups:
        newest = max(g, key=lambda r: r[1])
        # write head from the nearest TICK row (reconstructing it from the
        # per-row target identity breaks at phase transitions)
        edge = min(ticks, key=lambda tk: abs(tk[0] - g[0][0]))[1]
        batches.append(Batch(g[0][0], g[0][3], newest[1], newest[2], edge))
    return batches


def regime(batches):
    """L_true = rolling median of batch samples; slope = central median
    difference; steady residuals = the sample noise pool."""
    pts = [(b.t, b.L) for b in batches]
    truths = [median_win(pts, b.t, TRUE_WIN_S) for b in batches]
    slopes = []
    for b in batches:
        pre = median_win(pts, b.t - TRUE_WIN_S - SLOPE_WIN_S, SLOPE_WIN_S)
        post = median_win(pts, b.t + TRUE_WIN_S + SLOPE_WIN_S, SLOPE_WIN_S)
        slopes.append((post - pre) / (2.0 * (TRUE_WIN_S + SLOPE_WIN_S)))
    residuals = [b.L - tr for b, tr, s in zip(batches, truths, slopes)
                 if abs(s) < STEADY_SLOPE]
    return truths, slopes, residuals


# ----------------------------------------------------------------------------
# (b) L trackers
# ----------------------------------------------------------------------------
class Tracker:
    def __init__(self, kind, kd=2.0, gain=3.0, base_alpha=0.18,
                 trend_alpha=0.25, gate=False):
        self.kind, self.kd, self.gain = kind, kd, gain
        self.base_alpha, self.trend_alpha, self.gate = \
            base_alpha, trend_alpha, gate
        self.reset()

    def reset(self):
        self.est = None
        self.base = None
        self.trend = 0.0
        self.prev_fed = None
        self.win = deque(maxlen=3)

    def step(self, sample, dt):
        fed = sample
        if self.gate:
            self.win.append(sample)
            fed = sorted(self.win)[len(self.win) // 2]
        if self.base is None:
            self.base = self.est = self.prev_fed = fed
            return self.est
        self.base += (fed - self.base) * self.base_alpha
        rate = (fed - self.prev_fed) / dt if dt > 0.0 else 0.0
        self.trend += (rate - self.trend) * self.trend_alpha
        self.prev_fed = fed
        if self.kind == "ewma":
            self.est = self.base
        elif self.kind == "instant":
            self.est = max(0.0, min(LAG_MAX_S, fed))   # WP3 pin policy
        elif self.kind == "lead":
            self.est = max(0.0, min(LAG_MAX_S, self.base + self.kd * self.trend))
        elif self.kind == "adapt":
            alpha = min(1.0, self.base_alpha + self.gain * abs(self.trend))
            self.est = max(0.0, min(LAG_MAX_S,
                                    self.est + (fed - self.est) * alpha))
        else:
            raise ValueError(self.kind)
        return self.est


def run_tracker(tr, batches):
    ests = []
    prev_t = batches[0].t
    for b in batches:
        ests.append(tr.step(b.L, b.t - prev_t))
        prev_t = b.t
    return ests


# ----------------------------------------------------------------------------
# (a) store mechanisms
# ----------------------------------------------------------------------------
class AnchorStore:
    """Anchor decision per batch + the store policy. Records every cue's
    initial position and every whole-store shift, so the displayed
    position at any later checkpoint can be reconstructed.

    rebase_policy:
      "cur"    pre-WP3: |gap| > REBASE_S snaps immediately (single-batch
               noise spikes round-trip the store)
      "robust" snap only on 2 consecutive OOB batches or > 2x REBASE_S
      "v2"     LANDED: robust + the STABLE-TARGET rule — snap also when
               the target itself is stable (|target - prev| <= 0.8): a
               lone spike MOVES the target, a genuine correction (incl.
               the snap-back after a wrong forced rebase) re-asserts a
               steady one
      "none"   no snap at all (diagnostic bound)"""

    def __init__(self, mech, deadband=3.0, rebase_policy="cur"):
        self.mech = mech            # A0 | A1 | A2pb | A3
        self.D = deadband
        self.rebase_policy = rebase_policy
        self.reset()

    def reset(self):
        self.off = None
        self.shift_base = None      # off at the last whole-store shift
        self.shifts = []            # (t, delta)
        self.rebases = 0
        self.accepts = 0
        self._oob_run = 0           # consecutive out-of-band gaps
        self._prev_target = None

    def _shift(self, t, delta):
        self.shifts.append((t, delta))
        self.shift_base = self.off

    def decide(self, t, target):
        if self.off is None:
            self.off = target
            self.shift_base = target
            self._prev_target = target
            self._oob_run = 0
            return
        gap = target - self.off
        snap = False
        if abs(gap) > REBASE_S:
            self._oob_run += 1
            stable = self._prev_target is not None \
                and abs(target - self._prev_target) <= 0.8
            if self.rebase_policy == "cur" \
                    or abs(gap) > 2.0 * REBASE_S \
                    or (self.rebase_policy == "robust"
                        and self._oob_run >= 2) \
                    or (self.rebase_policy == "v2"
                        and (self._oob_run >= 2 or stable)):
                snap = True
        else:
            self._oob_run = 0
        self._prev_target = target
        if snap:
            self.off = target
            self._shift(t, gap)
            self.rebases += 1
            self._oob_run = 0
            return
        # small correction (or a spike riding the EWMA path): the anchor
        # EWMA always moves; the STORE policy decides whether cues follow
        if self.mech == "A0":
            self.off += gap * ANCHOR_ALPHA
        elif self.mech in ("A1", "A4"):
            self.off += gap * ANCHOR_ALPHA
            self._shift(t, gap if self.mech == "A4"
                        else gap * ANCHOR_ALPHA)
            self.accepts += 1
        elif self.mech == "A2pb":
            self.off += gap * ANCHOR_ALPHA
            if abs(self.off - self.shift_base) > self.D:
                self._shift(t, self.off - self.shift_base)
                self.accepts += 1
        elif self.mech == "A3":
            self.off += gap * ANCHOR_ALPHA
            debt = self.off - self.shift_base
            if abs(debt) > self.D:
                self._shift(t, debt)
                self.accepts += 1
        else:
            raise ValueError(self.mech)


def simulate(batches, ests, off_true, mech, D, rebase_policy="cur"):
    """Run the anchor/store policy over the corpus. Returns per-batch
    (off_at_store, off_after) plus the shift log."""
    an = AnchorStore(mech, D, rebase_policy)
    offs_store = []
    offs_after = []
    for b, le, ot in zip(batches, ests, off_true):
        offs_store.append(an.off if an.off is not None else ot)
        target = b.edge - le - b.end
        an.decide(b.t, target)
        offs_after.append(an.off)
    return an, offs_store


def scrub_coherence(batches, off_true, an, offs_store, mask=None):
    errs, worst_cp = [], []
    t0, t_end = batches[0].t, batches[-1].t
    ct = t0 + CHKPT_S
    while ct <= t_end:
        cp = []
        for i, (b, ot, o0) in enumerate(zip(batches, off_true, offs_store)):
            if b.t > ct - CHKPT_S:
                break
            if mask is not None and not mask[i]:
                continue
            disp = b.end + o0 + sum(d for t, d in an.shifts if b.t < t <= ct)
            cp.append(abs(disp - (b.end + ot)))
        if cp:
            errs.extend(cp)
            worst_cp.append(pct(cp, 0.95))
        ct += CHKPT_S
    return errs, (max(worst_cp) if worst_cp else float("nan"))


def phase_mask(batches, guard_s=15.0):
    """True where the batch sits > guard_s from any phase transition —
    transition batches (PAUSE/RESUME/JUMPLIVE/SCRUB) move the true pin
    wholesale for a few batches whatever the mechanism; the mixed-axis
    question lives in the quiet stretches."""
    mask = []
    boundaries = [batches[0].t]
    for i in range(1, len(batches)):
        if batches[i].phase != batches[i - 1].phase:
            boundaries.append(batches[i].t)
    for b in batches:
        ok = all(abs(b.t - bt) > guard_s for bt in boundaries)
        mask.append(ok)
    return mask


def round_trips(shifts, window_s=20.0):
    """Consecutive whole-store shifts within window_s that undo each
    other (opposite signs) — noise rebases the policy should not fire."""
    n = 0
    for (t1, d1), (t2, d2) in zip(shifts, shifts[1:]):
        if t2 - t1 <= window_s and d1 * d2 < 0.0:
            n += 1
    return n


def replay_steady(batches, truths, residuals, mech, D, tr_spec,
                  rebase_policy="cur"):
    """Synthetic steady regime: constant pipeline lag L0, measured cadence,
    speech-skew sample noise (the residual pool), one speech-like cue per
    batch, a display clock running L0+8 s behind the write head (the
    viewer's backlog — the newest cue's window only reaches the clock
    ~L0 s after its batch).

    Construction (matches production exactly): the batch's newest cue has
    cx_end = edge - C - L_sample, so its TRUE app-axis window ends at
    edge - L_sample; the anchor target = C + (L_sample - L_est); cues are
    pinned at the anchor off (the a=0.5 EWMA over targets, as _on_cc_cue
    maps cx + off). Pinned-vs-true = off - C, and STORE SHIFTS move the
    pinned windows after the fact — the jitter criterion scores the
    painted cue (newest stored window covering the clock) against its
    TRUE window, exactly like the harness's painted-cue metric."""
    cadence = statistics.median([batches[i + 1].t - batches[i].t
                                 for i in range(len(batches) - 1)])
    L0 = statistics.median(truths)
    res_pool = residuals or [0.0]
    n = max(2, int(REPLAY_S / cadence))
    bt0 = batches[0].t
    sim_t = [bt0 + i * cadence for i in range(n)]
    sim_L = [L0 + res_pool[(i * 7) % len(res_pool)] for i in range(n)]
    tr = tr_spec()
    ests = [tr.step(L, cadence) for L in sim_L]
    C = 0.0
    view_backlog = L0 + 8.0
    off = None
    shift_base = None
    wins = []                       # [s, e, s_true, e_true] app axis
    shifts = 0
    travel = 0.0
    painted_err = []
    flap = 0
    last_id = None
    last_switch_t = -10.0
    b_idx = 0
    oob_run = 0
    step = 0.1
    for i in range(int(REPLAY_S / step)):
        t = bt0 + i * step
        clock = i * step - view_backlog
        while b_idx < n and sim_t[b_idx] <= t:
            elapsed = b_idx * cadence
            L_sample = sim_L[b_idx]
            est = ests[b_idx]
            # this batch's cue: cx end maps to a true window ending
            # L_sample behind the head; pinned at the CURRENT anchor off
            e_true = elapsed - L_sample
            pin_off = off if off is not None else C
            cx_end = elapsed - C - L_sample
            wins.append([cx_end + pin_off - CUE_WIN_S, cx_end + pin_off,
                         e_true - CUE_WIN_S, e_true])
            target = C + L_sample - est
            if off is None:
                off = target
                shift_base = target
                prev_target = target
            else:
                gap = target - off
                snap = False
                if abs(gap) > REBASE_S:
                    oob_run += 1
                    stable = prev_target is not None \
                        and abs(target - prev_target) <= 0.8
                    if rebase_policy == "cur" or abs(gap) > 2.0 * REBASE_S \
                            or (rebase_policy == "robust" and oob_run >= 2) \
                            or (rebase_policy == "v2"
                                and (oob_run >= 2 or stable)):
                        snap = True
                else:
                    oob_run = 0
                prev_target = target
                if snap:
                    off = target
                    for w in wins:
                        w[0] += gap
                        w[1] += gap
                    shift_base = off
                    shifts += 1
                    travel += abs(gap)
                    oob_run = 0
                else:
                    off += gap * ANCHOR_ALPHA
                    debt = off - shift_base
                    d = 0.0
                    if mech in ("A1", "A4"):
                        d = gap if mech == "A4" else gap * ANCHOR_ALPHA
                    elif mech == "A2pb" and abs(off - shift_base) > D:
                        d = off - shift_base
                    elif mech == "A3" and abs(debt) > D:
                        d = debt
                    if d:
                        for w in wins:
                            w[0] += d
                            w[1] += d
                        shift_base = off
                        shifts += 1
                        travel += abs(d)
            b_idx += 1
        # display: newest stored window covering the clock (roll-up)
        active = None
        for w in wins:
            if w[0] <= clock <= w[1]:
                active = w
        if active is not None:
            painted_err.append(abs(clock - min(max(clock, active[2]),
                                               active[3])))
            if active is not last_id:
                if t - last_switch_t < 1.0 and last_id is not None:
                    flap += 1
                last_id = active
                last_switch_t = t
    return {"shifts": shifts, "travel": travel,
            "p95": pct(painted_err, 0.95),
            "max": max(painted_err or [0.0]),
            "flap": flap, "n": len(painted_err)}


# ----------------------------------------------------------------------------
# driver
# ----------------------------------------------------------------------------
def main():
    batches = load_corpus(LOG)
    truths, slopes, residuals = regime(batches)
    Ls = [b.L for b in batches]
    n_ramp = sum(1 for s in slopes if abs(s) >= RAMP_SLOPE)
    n_steady = sum(1 for s in slopes if abs(s) < STEADY_SLOPE)
    print(f"corpus: {len(batches)} batches / "
          f"{batches[-1].t - batches[0].t:.0f} s; L p50="
          f"{pct(Ls, .5):.1f} p95={pct(Ls, .95):.1f} max={max(Ls):.1f}")
    print(f"regime: ramp={n_ramp} steady-ish={n_steady} batches; "
          f"steady residual pool n={len(residuals)} "
          f"p50={pct([abs(r) for r in residuals], .5):.2f} "
          f"p95={pct([abs(r) for r in residuals], .95):.2f} s")

    trackers = [
        ("T0  a=0.18 (current)", lambda: Tracker("ewma")),
        ("T1  a=0.35", lambda: Tracker("ewma", base_alpha=0.35)),
        ("T1b a=0.50", lambda: Tracker("ewma", base_alpha=0.50)),
        ("T1m a=0.35 med3",
         lambda: Tracker("ewma", base_alpha=0.35, gate=True)),
        ("T2m lead kd=2 med3",
         lambda: Tracker("lead", kd=2.0, gate=True)),
        ("T3m adapt g=3 med3",
         lambda: Tracker("adapt", gain=3.0, gate=True)),
        ("T3m adapt g=6 med3",
         lambda: Tracker("adapt", gain=6.0, gate=True)),
        ("Tinst pin=L_sample (LANDED)",
         lambda: Tracker("instant")),
    ]
    print("\n== (b) L trackers: pin error |L_est - L_true| (s) ==")
    print(f"{'tracker':<24}{'ramp p50':>9}{'ramp p95':>9}{'steady p95':>11}")
    tr_ests = {}
    for name, fac in trackers:
        ests = run_tracker(fac(), batches)
        tr_ests[name] = ests
        ramp = [abs(e - u) for e, u, s in zip(ests, truths, slopes)
                if abs(s) >= RAMP_SLOPE]
        steady = [abs(e - u) for e, u, s in zip(ests, truths, slopes)
                  if abs(s) < STEADY_SLOPE]
        print(f"{name:<24}{pct(ramp, .5):>9.2f}{pct(ramp, .95):>9.2f}"
              f"{pct(steady, .95):>11.2f}")

    mechs = [
        ("A0  current", "A0", 0.0),
        ("A1  shift-every", "A1", 0.0),
        ("A2pb deadband .75", "A2pb", 0.75),
        ("A3  debt 2.0", "A3", 2.0),
        ("A3  debt 3.0", "A3", 3.0),
        ("A3  debt 4.0", "A3", 4.0),
        ("A4  pin-exact", "A4", 0.0),
    ]
    off_true = [b.edge - u - b.end for b, u in zip(batches, truths)]
    mask = phase_mask(batches)
    print(f"\nscrub metric: {sum(mask)}/{len(mask)} batches outside "
          f"phase-transition guards")
    fac_by_name = dict(trackers)
    for name, ests in tr_ests.items():
        for rp in ("cur", "v2"):
            print(f"\n== (a) store mechanisms under {name} "
                  f"[rebase: {rp}] ==")
            print(f"{'mechanism':<20}{'shifts':>7}{'travel':>8}"
                  f"{'st.p95':>7}{'st.max':>7}{'flap':>5} | "
                  f"{'scrub p95':>9}{'quiet p95':>9}{'worst':>7}"
                  f"{'rt':>4}{'reb':>4}{'acc':>4}")
            for mname, mech, D in mechs:
                an, offs_store = simulate(batches, ests, off_true, mech, D,
                                          rebase_policy=rp)
                errs, worst = scrub_coherence(batches, off_true, an,
                                              offs_store)
                qerrs, _ = scrub_coherence(batches, off_true, an,
                                           offs_store, mask=mask)
                r = replay_steady(batches, truths, residuals, mech, D,
                                  fac_by_name[name], rebase_policy=rp)
                print(f"{mname:<20}{r['shifts']:>7}{r['travel']:>8.2f}"
                      f"{r['p95']:>7.2f}{r['max']:>7.2f}{r['flap']:>5} | "
                      f"{pct(errs, .95):>9.2f}{pct(qerrs, .95):>9.2f}"
                      f"{worst:>7.2f}{round_trips(an.shifts):>4}"
                      f"{an.rebases:>4}{an.accepts:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
