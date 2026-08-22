# WP0 report — make the tests able to fail

**Session:** 2026-08-21 (fresh, per the attack plan's operating procedure).
**Scope:** `test_sync_adversarial.py`, a no-behavior-change `now_s()` clock
seam in `src/ui/player_view.py`, check labeling in `sync_stage3_run.py`,
plus the mutation driver `p0_mutations.py` and the sync-trace wrapper
`p0_diag.py`. No production behavior changed; no live/network tests; all
runs headless (offscreen Qt, muted, no window).

## 1. The two proven defects, confirmed and root-caused

**Defect 1 — saturated metric.** `Harness.truth_cue()` only returned
released cues that already COVERED the display position (grace
[s−0.3, e+0.55]), so `err = disp − clamp(disp, s, e)` was bounded ≤ 0.55 s
by construction. Every historical "p95 0.29/0.37" was saturation. The
recorded baseline (reproduced bit-for-bit at session start:
`sync_diag_adversarial_quick.out`, `p0_diag_a_run.out`) was 30/31 with the
wedge scenario's displacement completely invisible (post-wedge "p95 0.32
PASS" while the anchor/store sat displaced).

**Defect 2 — pacing-dependent watchdog check.** The scenario-a check
"watchdog fired during the freeze" compared `rebase_count` before/after a
snapshot taken AFTER the freeze run had already ended — the watchdog's
fire DURING the freeze was already counted in both sides (today's sync
trace, `p0_sync_diag.log` line 1441: `REBASE why=watchdog shift=-3.83`
inside the freeze). Passing therefore required a SECOND fire in the
recovery window, which depends on where CCX's real-time parse head sits
relative to the virtual clock when the wedge lands — i.e. on real
CCExtractor pacing. Recorded 31/31 vs 30/31-twice was this coin flip.

## 2. The new painted-cue metric (design)

Per sampled tick (10 Hz) where the overlay has lines:

- **Match:** the painted lines (lowercased, exactly what
  `CueStore.text_at` returns — the profanity filter is off in the harness)
  are looked up in an incrementally-built index of RELEASED cues keyed by
  their paintable screen (`visible_lines(text)[-3:]`).
- **Score:** `perr = min over matching cues of |disp − clamp(disp, s, e)|`
  — UNBOUNDED. `disp = clock − m0r` puts the display position on the RAW
  content axis (see calibration below). Multiple matches (roll-up screens
  repeat lines): minimum error wins; the sample counts as *ambiguous* in
  diagnostics (steady-state ambiguity is high by nature: 60–75% of
  samples; it does not weaken the gate because min-over-matches is an
  upper bound on the true error only when a true match exists — verified
  by mutation m1, which pushes every sample to ~5 s despite ambiguity).
- **Exact-window (primary text assertion):** a sample passes when its
  best match sits within `_EXACT_TOL_S = 1.0` s of the display position —
  beyond that the painted cue is from the wrong window. The retired ±3-s
  neighbor/substring acceptance is kept as a diagnostic counter only
  (`diag[neighbor=...]` in every report line).
- **Stop metric (unchanged contract):** truth-active := a released cue
  covers the display position; painting nothing while truth-active grows
  `stop_run` (the watchdog contract). `truth_cue()` survives solely as
  this gate.
- **Calibration, unbiased:** `m0` (clock→app-visible axis, feeds the stop
  gate) and `m0r` (clock→raw axis, feeds `perr`) are medians over samples
  where a cue window COVERS the clock. The retired scan took the first
  window within ±4 s from the newest end — with roll-up windows narrower
  than the gaps between them, that picked a neighbor systematically AHEAD
  of the clock and biased m0 by −1.1…−1.5 s (measured: old scan −1.47,
  covering scan −0.17). Harmless while saturated; fatal now.
- **Warped axis (scenario e):** `CueQueue` now records every release on
  both axes — `released` (times the app saw; warped under WarpQueue) and
  `raw_released` (true content windows). Scoring against the raw axis is
  what makes e meaningful: the app-visible warped numbers are meaningless
  as positions (scoring against them produced p50≈46 s of pure artifact).

Aggregate gates: `p95(perr) ≤ 1.5 s` where the regime allows it,
`exact_rate ≥ 85%` (steady), `within_rate(1.5) ≥ 85%` (outcome phrasing:
"correct within N s"), plus scenario-specific bounds below.

## 3. Outcome assertions replace mechanism counts

- **Scenario a (template):** the fault is now applied deterministically at
  a fixed virtual moment (drift ends at the wedge; the 6-s release freeze
  is unsampled fault-injection). The retired watchdog-count check is
  replaced by outcome assertions with a backlog-aware deadline (recovery
  = anchor re-settle + the viewer walking its live-edge BACKLOG off the
  cues stored under the displaced axis):
  - no truth-active blank beyond the watchdog window after data returns;
  - when recovery ran a REBASE (the store-re-axing path — watchdog or
    anchor snap), the walk-off through the pre-fault region must paint
    the right text ≥ 70% of the time (a rebase that leaves the store on
    the old axis — mutation m3 — fails exactly here);
  - the healing tail (15 s) ≥ 60% within 1.5 s;
  - the verify window (next 10 s) fully correct: p95 ≤ 1.5 and ≥ 85%
    within 1.5 s.
  When NO rebase fired (the anchor only EWMA-crawled back), the pre-fault
  region keeps the displaced axis until walked off — a mixed-axis store,
  WP3's target — so WP0 demands forward correctness only and the walk-off
  numbers stay in the report as the WP3 acceptance baseline. Recovery by
  ANY internal path satisfies the check.
- **Scenario f:** "rebase round-tripped (forced + snap-back)" (mechanism
  count) replaced by the store-coherence outcome (p95 + exact rate after a
  2-s healing allowance for the snap-back flush). Added a steady-segment
  max gate (p95 ≤ 1.5, max ≤ 2.5) — the check that catches transient
  displacement that p95 averages away.
- **Scenario d:** whole-run numbers are REPORTED (the burst transient is
  real production behavior — see findings); the assertion is
  re-settle-and-stay-put on a quiescent 1:1 feed (settle detected ON the
  metric, bounded at 150 s — the burst backlog drains at CCX's real-time
  parse pace, so no fixed virtual delay is deterministic).
- **Scenario c:** "after lag recovery" is now literal — settle detection
  on the metric itself, then a 12-s stay-put window (p95 ≤ 2.0: the
  post-swing snap-rebase wander is real, quoted, and WP3's to tighten).

## 4. The `now_s()` clock seam

`player_view.now_s()` (module-level, `return time.time()`) is the single
wall-clock read for every timing gate in the pipeline — all 14
`time.time()` sites converted (caption clock, watchdog, DVR content
crediting, probe throttle, rescue/reopen cooldowns, arrival anchoring).
The harness rebinds `pv_mod.now_s = lambda: VT.t`; the retired
module-level `time` proxy (`_TimeProxy`) is deleted.

**Seam A/B proof (quick mode, original harness):** with the harness
changes stashed, the ORIGINAL harness (which proxies `pv_mod.time`)
running against the seamed player_view reproduced the recorded pre-seam
baseline scenario-by-scenario, near line-for-line — a: m0=−1.47 (143
samples), drift p50 0.16 / p95 0.37, the same watchdog-check FAIL
(rebases 1→1); d: gap 27.0 s, maxstop 5.1, rebases 4, p95 0.37
(`p0_seam_ab.out` vs `sync_diag_adversarial_quick.out`). `_note_dvr_data`
crediting is untouched and identical. The seam is behavior-neutral.

## 5. L(t) profiles and regime tags

- `LagProfile` formalizes the scripted CCX lag: scenarios declare
  `lag_const(1.5)`, `lag_swell_then_recede(...)` (c: 1→20 s swell, hold,
  0.6 s/s drain — the rate the original script applied per 0.1-s tick;
  my first draft at 0.06 s/s never recovered inside the scenario and was
  corrected), and print their profile in the scenario header.
- `check(..., kind="mechanism"|"data-limited")`: mechanism checks must
  pass in every regime; data-limited checks quote their gate (c's blank
  bound vs the L swell; d's re-settle bound vs the burst cadence). The
  final tally reports both counters.
- `sync_stage3_run.py`'s `check()` gained the same labeling (default
  mechanism — a semantic no-op for existing checks; P2/P5 tag theirs).

## 6. Scenario f's delay check is direction-neutral

The old check asserted `shifted == text_at(clock + 1.5)` — codifying the
current (wrong-per-plan) sign. The new check seeks a probe position where
the active cue differs BOTH 1.5 s earlier AND 1.5 s later, then asserts
that +1.5 s of delay_ms CHANGES the painted cue — whichever sign the
implementation applies. The sign fix belongs to WP1a.

## 7. Mutation tests — the harness can fail (proof)

Driver: `p0_mutations.py` (in-process monkeypatches; the working tree was
never modified — there is nothing to revert). Each mutation ran the
affected scenarios in `--quick` mode on the FINAL harness:

| Mutation | Implementation | Caught by (new checks) |
|---|---|---|
| **m1** anchor+store displacement: `view._cc_off += 5` + store/filter shift at a fixed virtual moment (first anchor flush + 45 s), HELD by biasing `_cap_edge_s` +5 so every re-derived target stays displaced | 7 failures across a+f: drift p95 5.30 / exact 0/577; post-wedge heal 0% of 144, verify p95 6.29; f steady max 5.65; scrubbed p95 5.15 / exact 0/163; post-rebase p95 4.64 |
| **m2** constant +3 s caption-clock skew (`_caption_clock_s` + 3) | b: p95 3.46; silent-stop 8.8 s (the skew also blanks at pause boundaries) |
| **m3** stale store after rebase (`_cc_rebase` no longer shifts `_cap_cues`) | a: the rebase-path walk-off gate — 56% within 1.5 s vs ≥70% (heal and verify stay clean at 100%/0.69: exactly the mixed-axis signature, the viewer walks through the stale region while forward cues are correct). Note: m3 is a no-op in the mode/phase where no rebase fires (full-duration a runs the EWMA-crawl path); it is exercised in quick mode where the watchdog fires — stated here so nobody "fixes" the mutation's scenario choice later. |

Note on m1's "held" design: an un-held one-shot +5 shift with data flowing
self-heals via anchor-snap within one cue flush (~0.2 s) — that is GOOD
production behavior, and scenario a's release freeze exists precisely to
hold its wedge. A mutation representing a shippable bug must be held the
same way; the one-shot variant is documented as self-healing.

## 8. Findings the new metric exposed immediately (feeds WP2/WP3)

The retired metric hid real behavior; the first honest runs measured:

- **Post-wedge residual:** after the anchor+store wedge recovers via the
  watchdog rebase, captions settle ~1.0–1.3 s off truth and stay there
  (quick runs: verify p50 ~1.0; recent runs with the longer settle land
  p50 0.07 / p95 0.69). Old metric called this "p95 0.32".
- **Wedge recovery is path- and content-dependent:** in quick mode the
  wedge blanks the display (sparse roll-up at that content phase) → the
  watchdog fires during the freeze → the store is re-axed and the walk-off
  is clean. At full duration the wedge lands in dense roll-up — painting
  never blanks, the watchdog never fires, and the anchor EWMA-crawls back
  over ~30–40 s (the correction never exceeds the 4-s snap threshold
  because the target falls in parallel) while the viewer, ~36 s behind the
  head, keeps painting cues stored under the displaced axis until the
  backlog is walked off. The pre-fault region stays on the old axis
  (mixed-axis store — the WP3 acceptance target). This is why the
  post-wedge deadline scales with backlog and the walk-off gate applies
  only when a rebase ran.
- **Burst displacement:** scenario d's whole-run p95 is ~15 s (max 22.5):
  each burst's release dump inflates the L EWMA to ~10 s and the pin runs
  low by that amount until CCX (real-time) has parsed the backlog —
  rebases at ±14…24 s chase it (whole-run line in every d report).
- **2x-axis sawtooth:** under a warped caption axis the anchor target
  falls ~1 s/s, the EWMA trails it, and `_CC_REBASE_S` snaps containment —
  a 0…5.7 s sawtooth at ~2 snaps/10 s (e: p50 2.00, p95 4.73, 11
  rebases). Sub-1.5 s positions need WP3's lead compensation; e now
  asserts the containment contract (p95 ≤ 6, exact within ±8 ≥ 85%).
- **Post-swing wander:** after c's L swing the recovered tail wanders
  1.2–1.8 s with continued snap-rebasing (the live "23 snap-rebases in
  START" symptom, reproduced offline). c's recovered gate is 2.0 s and
  quotes the rebase count; the tighten-the-wander work is WP3's.

## 9. Determinism proof — final runs

All three runs on the FINAL harness, same working tree, sequential:

| Run | Result |
|---|---|
| `--quick` #1 (`p0_final_quick1.out`) | **31/31** (mechanism 27/27, data-limited 4/4) |
| `--quick` #2 (`p0_final_quick2.out`) | **31/31** — check-by-check IDENTICAL to #1 (diff of all 31 verdict lines is empty) |
| full mode (`p0_final_full.out`) | **31/31** (mechanism 27/27, data-limited 4/4) |

Cross-mode agreement: every check passes in BOTH modes with the same
verdict, scenario by scenario. The detail numbers differ by design (full
runs 3x longer timelines, different loop phases, bigger backlogs) and the
scenario-a recovery exercises BOTH internal paths across modes — quick:
watchdog rebase fires (store re-axed; walk-off 100% within 1.5 s, gated);
full: no rebase, EWMA crawl (walk-off 10%, ungated, reported as the WP3
mixed-axis baseline; heal 100%, verify p95 0.27). A check that can only
pass one recovery path would have failed one of the modes.

## 10. Artifacts

- `p0_final_quick1.out`, `p0_final_quick2.out`, `p0_final_full.out` —
  the three confirmation runs.
- `p0_seam_ab.out` — seam A/B (original harness + seam vs the recorded
  pre-seam baseline).
- `p0_mut_m1/m2/m3.out` — mutation runs on the final harness.
- `p0_mutations.py` — the mutation driver (kept; re-run after any harness
  or pipeline change: `.venv\Scripts\python.exe -X utf8 p0_mutations.py m1 a,f`).
- `p0_diag.py` — sync-decision-trace wrapper (attaches a file handler to
  `mtp.sync` and runs the harness; the harness itself never configures
  logging — MTP_SYNC_LOG alone does nothing for it).

## 11. Not done / left to later packages (by design)

- The delay-sign fix (WP1a) — scenario f is direction-neutral.
- Anchor/store coherence mechanics, L lead compensation, wedge cluster
  (WP2/WP3) — the harness now measures them honestly; the transient
  numbers above are the acceptance baselines to beat.
- Live/network testing (WP5) — everything here is offline.
