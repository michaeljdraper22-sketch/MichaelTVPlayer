# WP5 — Verification night report

**Date:** 2026-08-23 (evening) · **Scope:** full verification matrix per
`subtitle_attack_plan.md` WP5. Tree = `8dad09e` + the uncommitted WP4b
landing (vendored CCX 0.96.6 subset, `ccx_args` modern form, `read1`
pipe fix — `p4b_report.md`). Courtesies held throughout: one provider
stream at a time, every playback muted + offscreen, never focused.

## Verdict up front

| Area | Result |
|---|---|
| Offline suites (16) | **15 green + `test_profanity`**: intermittent 0-byte init segfault (3 crashes / 6 runs, always at interpreter init, every completed run 54/54 green) — environment flake, follow-up #4 |
| Adversarial harness | **RED — 54/55 full, 51/55 quick ×2 (identical)**. Root-caused tonight to a **harness metric gap** exposed by WP4b's `read1` pipe fix, NOT a caption-placement regression (proof below). Deterministic per mode |
| Live E2E window A (healthy regime) | **10/12**; both FAILs dissected: one vacuous driver check (fixed in `sync_stage3_run.py`), one real-but-recovered 14 s post-jump blank |
| Live E2E window B (healthy regime, 40 min later) | **10/12** with the fixed driver (cold-join paint now PASSES); both FAILs dissected: one driver formula bug (unclamped scrub want — fixed), one real known transient (resume snap-oscillation 8.8-10.2 s, self-corrected — P3's documented residual) |
| Caption engage (cold / mid-show ≥90 / <90) | **20/21** on the rerun matrix: cold join paints; <90 engage first paint **10.1 s ✓ spec**; ≥90 engage joins at the playhead but first paint 22-35 s (new pin-overshoot finding, §3) |
| D1 adaptive landing live | **Verified on the low-L branch in 3 live samples** (L 2.2–4.3 → back=5, land within tolerance, captions timely). High-L branch not sampled live tonight (provider healthy both windows); covered offline |
| VOD E2E | **SKIPPED** — CDN refusing (HTTP 520 on all 8 candidates, provider-side; the D4 retry/skip class) |

## 0. Harness regression — attribution, mechanism, verdict

**Observation.** Full matrix 54/55 (only `f: rebased store stays
coherent` FAIL, p95=5.82); quick ×2 51/55 **byte-identical to each
other** (f ×3 at p95 76.93/78.66/113-208, g ×1 at 4.21/4.31). P3's
recorded quick runs (`p3_harness_quick{1,2}.out`) were 55/55 on the
same scenarios — the tree changed only by WP4b since.

**Attribution (A/B).** Reverted `src/live_cc.py` to the committed P3
version → `--only:f --quick` **7/7** (p95 0.17-0.18) twice; restored
the WP4b version → fails again. The regression enters through WP4b's
`live_cc.py` (the `read(4096)`→`read1(4096)` stdout fix and/or the
`--no-codec dvbsub` arg form).

**Mechanism (probed, `p5_probe_f{,2,3}.out`).** The harness app runs a
REAL `CCSource` on the looped recording; the painted-cue metric's truth
index only contains cues the lag-gated `CueQueue` released — and the
queue's first release is at t=10.51 (early-buffer cues are never
released). With the old 4 KB blocking `read`, the real path's early
deliveries were lumped/delayed past the sampled segments, so the store
and the index agreed where it mattered. With `read1`, the real path
delivers every cue promptly — the store now holds cues (e.g. the .srt's
cue 1 at window (2.5,4.4), exact ground-truth text) that the metric's
index lacks nearby, so painted text can only tuple-match loop copies
~72-77 s away → p95 77. The full-matrix post-rebase variant (5.82) is
the same min-over-matches inflation through the forced −6 s shift; a
standalone full `--only:f` passes 7/7 because the matrix shares
scenario a's axis calibration.

**Why this is not a user-visible regression.** For every failing sample
in every failing segment: the painted text is verbatim present in the
released set within ±3 s (`diag[neighbor=…]` 100% — 834/834, 300/300),
p50 of the painted error is 0.00, the anchor is healthy (off≈0, L≈1.45,
rebases=0 in the scrubbed segment), and the store windows sit exactly on
the .srt ground truth (`p5_probe_f2.out`). Captions paint the right
words at the right time; the metric's exact-tuple match is defeated by
duplicate text across loop iterations plus real-path cues the index
never saw.

**Follow-up (not tonight):** the harness metric must index the union of
queue-released AND real-path-delivered cues (or the store feed must be
single-sourced). Scenario g's 4.21/4.31 vs the 2.5 gate shows the same
inflation signature and is expected to clear with the metric fix; it is
data-limited-tagged. Until then, harness greens on f/g require the
`live_cc.py` A/B caveat.

## 1. Offline matrix

- Suites: `test_always_chase` 44, `test_anchor_store` 14/14,
  `test_bundled_ccx` 15, `test_caption_overlay` 138, `test_cuestore` 28,
  `test_dvr_e2e` rc0, `test_fixes` 51, `test_overlay_focus` 12,
  `test_sub_settings` 37, `test_subtitles` 22,
  `test_tab_resize` 16, `test_temp_cleanup` 6, `test_vod_series` rc0,
  `test_vod_splitter` rc0, `test_wedge_cluster` 23/23 — all green.
- `test_profanity`: **intermittent 0-byte startup segfault** — 3 crashes
  in 6 runs tonight (always rc=139 with zero output, i.e. the
  interpreter dies at init before the first check; every non-crashing
  run is fully green, 54/54, three times). First observed in the
  16-suite chain, later standalone — correlates with heavy VLC/live
  usage earlier in the session, never mid-suite. Not a test-logic
  regression; needs one look (follow-up #4).
- Harness: `p5_harness_full.out` 54/55, `p5_harness_quick{1,2}.out`
  51/55 identical (de-flake proof holds — the failures are
  deterministic, see §0).

## 2. Live windows

### Window A — 20:34, healthy delivery (L 2.1–5.6 s, growth 0.98×)

`p5_live_windowA.out` + `p5_sync_windowA.log` + analyzer: frontier
3.7→170.8 s over 170 s wall (0.98×). 111 cues flowed in START from
t+6 s; anchor pinned 10 s after CC start (off 0.00 at 20:34:22);
playback moving 20:34:48; **captions painting continuously from
20:34:48** (`since_show=0.0-0.1` every tick — first paint ≈ 35 s after
phase start = VLC startup ~25 s + first cue at content 3.6 s).
- `captions paint at the cold join` **FAIL = driver bug**: it tested
  `health.last_paint`, which is only ever set inside `health_pump()` —
  which starts AFTER the check. Vacuously false since P2 rewrote the
  driver (the 2026-08-21 diagnosis run failed the same way). Fixed in
  `sync_stage3_run.py` (tests `view._cap_wid._lines`).
- `no silent-stop > 8 s` **FAIL, real, recovered**: 19 stop events,
  max 14.0 s — all three >8 s stop-ticks sit in the 30 s after JUMPLIVE
  (raw axis renumbered ~−30 s; 6 watchdog rebases re-pinned; captions
  back and stable). Baseline that night: a 7-minute wedge with zero
  recovery and a 7.1% paint rate. Freeze-watch max 2.0 s (gate 15);
  `max_lead=1.00 s` (gate 1.5; diagnosis baseline 8.5 s ahead).
- D1 low-L branch: L=2.2 → back=5 → want 389.2 vs edge 394.2, landed
  and tracking (raw 544.6→279.1 through the −110 raw divergence).
- Scrub −120 s: landed 416.4 vs want 419.2, clock AND raw reached it.
- START rebases: **0** (diagnosis baseline: 23 snap-rebases in START);
  8 rebases total across the whole run, all watchdog/snap events during
  JUMPLIVE/SCRUBFWD. Anchor innovation p95 ≤ 3.05 except one SCRUBFWD
  8.39 snap (signed means ≈ 0 — no bias).

### Window B — 21:14 (40 min after A), healthy delivery again (L 1.7–6.6 s, growth 0.98×)

`p5_live_windowB.out` + `p5_sync_windowB.log`: frontier 1.75→172.97
over 175 s. **10/12 with the fixed driver** — the repaired cold-join
paint check now PASSES (captions painting well inside the 60 s wait).

- D1 low-L branch again: L=4.2 → back=5 → want 383.2 vs edge 388.2,
  landed and tracking (raw 383.2→383.7).
- Display stops: max 2.9 s (6 stops) — better than window A; paint
  93.5% of samples.
- `scrub back lands` **FAIL = driver formula bug, fixed**: the clock
  sat at 84.8 s (the content axis re-seeded after the post-jump raw
  renumbering: raw had grown to 518.7, off_v +420-510) so
  `want = s0−117` went to −32.2 while the app correctly clamped the
  seek to the buffer head (landed 2.8). The clamp now mirrors the
  harness's scenario-f check. The app-side behavior was right
  throughout: the seek-forward after the clamp landed 142.8 vs 140.7,
  SCRUBFWD anchor innovation p95 0.85, captions re-anchored within
  13 s of the clamp (the only >8 s stop-tick with data).
- `caption clock never leads raw > 1.5 s` **FAIL — real, known,
  self-corrected**: right after RESUME the anchor snap-oscillated
  −10.23 → 0.00 → −8.81 → 0.00 (two full round-trips; the store
  shifted with each snap). Max measured lead 8.81 s. This is exactly
  the residual P3 documented when widening scenario e's containment
  gate 6→8 s ("the old number rode watchdog noise-fires the guard
  removed; real fix = slope-aware pin, future work" — `p3_report.md`
  §3.1c); live measured 10.23 s once, transient, zero bias after
  (signed means ≈ 0 in every phase).
- START: 0 rebases again, anchor innovation p95 1.17, 286 cues flowed.

## 3. Caption-engage paths (live)

All three paths exercised live on US: NFL NETWORK HD (`p5_engage_run2.out`,
`p5_sync_engage2.log`; first run `p5_engage_run.out`/`p5_sync_engage.log`
superseded by the rerun — its MIDLT90 phase accidentally tested the
sticky-captions-across-channel-change path instead, see below).

| Path | Result |
|---|---|
| **Cold join, captions ON from start** (window A START) | Cues flow t+6 s, anchor pinned t+10 s (off 0.00), playback moving t+35 s, captions painting continuously from then (`since_show=0.0-0.1`); the apparent FAIL was the vacuous driver check (fixed) |
| **Off→On mid-show, frontier ≥ 90 s** (engage at 94.4-95.3, two samples) | Reader genuinely off before engage; `join_byte=46,425,284` / `41,041,152` (D2: joins at the playhead, never byte 0); cues with playhead-matching text in **0.6-0.7 s**; first anchor 0.6-0.7 s; **first paint 22.1 s / 35.3 s** — over the ~15 s target, cause = the pin overshoot below; once painted, cue sits exactly at the playhead (dist 0.1 s), zero stops after |
| **Off→On mid-show, frontier < 90 s** (engage at 48.0) | Reader genuinely off; `join_byte=19,075,608` (D2 at ANY frontier ✓); anchor **0.38 s**; **first paint 10.1 s — within the ~15 s target**; cue at playhead (dist 0.1 s); no stop > 0.8 s |

Product-behavior note confirmed on the way: captions stay WANTED
across a channel change (the first run's "fresh" session auto-engaged a
reader at byte 0 within seconds of chase entry — correct sticky-ON
behavior, but it meant that phase tested a second cold join; the rerun
disengages explicitly first).

### The mid-show-join pin overshoot (new finding, quantified)

MID90 (engage at frontier 95.3): `join_byte=41,041,152` (D2 works — the
reader joined at the playhead, not byte 0), cues with playhead-matching
text flowed within **0.7 s** (CCX sprinted the already-written buffer —
`feed_behind=0` within 2.5 s; parse speed is not the bottleneck), first
anchor 0.7 s — but **first paint 35.3 s**. Cause chain from
`p5_sync_engage.log`: the first post-join ANCHOR pinned `target=96.00`
≈ the DVR wall-credit (≈ frontier), placing the first cue window at
content 113.7 while the playhead was at 79; paint waited for the clock
to walk ~35 s into the window. The same flush carried an `EDGESNAP
err=+50.34` and no `PCRJOIN` row exists for the 41 MB join (the
join-probe/logging path for mid-show joins has a gap). **D2 changed the
join byte but the first-batch pin still uses the credit axis — only
correct for cold joins.** Cost model: blank ≈ backlog + L + ε (measured
35.3 s at backlog ≈ 17 + L ≈ 3; a byte-0 replay at this frontier would
have cost ~85-95 s — D2 still halves-to-thirds the wait, and the
correct pin would make it ≈ 5-10 s). Fix is P6 work: pin the first
post-join batch at the join content position (or block the first-flush
edge snap until the join probe resolves).

## 4. D1 adaptive landing (live)

Three live samples, all on the **low-L branch** (L ≤ 8 → back =
`_CHASE_SAFETY_S` = 5, land near the true edge):

| Sample | L | back | want vs edge | Outcome |
|---|---|---|---|---|
| Window A JUMPLIVE | 2.2 | 5 | 389.2 vs 394.2 | landed, raw moved/tracked through a −110 s raw divergence |
| Engage run 1 D1 | 4.3 | 5 | 116.8 vs 121.8 | landed (raw 116.8, tracking), captions already painting (t=0.0 s) |
| Engage run 2 D1 | ~4.9-6.9 | 5 | — | landed, paint immediate, max stop 0.8 s |

The high-L branch (back = L+3) was **not sampled live tonight** — both
windows measured L 1.4-6.9 s, never crossing the 8 s adaptive
threshold. Offline coverage: the inlined-formula checks in the driver
+ P2's harness scenarios exercise the landing policy; the formula
itself (`max(_CHASE_SAFETY_S, L+3)`) is driver-inlined so it cannot
self-neutralize. A high-L live sample remains open for the next
degenerate-delivery window.

## 5. VOD E2E — SKIPPED (CDN down, provider-side)

Probed before any playback attempt (`p5_vod_probe.py`): all 8
candidates (6 movies across 3 categories + 2 series episodes) returned
**HTTP 520** with an HTML error body — including a plain GET without a
Range header (7,331 bytes of error page, no media bytes). This is the
D4 "provider-side; retry/skip logic only" case (tonight's status is 520
rather than 551, same refusal class — the whole VOD CDN is down, not a
per-item failure). VOD behavior is therefore covered only by the
offline evidence: `test_vod_splitter`, `test_vod_series`, `test_fixes`
(rc0/51 green tonight) and P1c's fail-before/pass-after fixtures
(`p1c_report.md`); the live VOD relay path stays unverified tonight —
re-run `sync_stage3_run.py vod` when the CDN accepts.

## 6. Per-symptom scoreboard

| Symptom | Before (2026-08-21 diagnosis + reports) | After (tonight, regime noted) |
|---|---|---|
| Slow to load | Cold join first paint 29-60+ s (CCX ~1x on the 4K burst); mid-show engage at frontier <90 s replayed the whole buffer from byte 0 | Cold join (healthy window): captions painting ~35 s after press-play (VLC startup dominates; cues+anchor ready at t+10 s). Mid-show ≥90: join at playhead + cues 0.6-0.7 s, but pin overshoot delays first paint to 22-35 s (finding §3). Mid-show <90: join at playhead, anchor 0.4 s, **first paint 10.1 s — meets the ~15 s target** |
| Way off sync, live | L 27→136 s in 10 min; anchor innovation p95 8.6 s; 23 snap-rebases in START; E2E "coherent" FAILs; 14.1 s stops ×10; paint 7.1% | Healthy windows ×2 (L 1.7-6.6, 0.98×): 0 START rebases both, anchor innovation p95 ≤3.05, signed bias ≈0, paint 81.6-93.5%, stops ≤14 s only post-jump/scrub-clamp (recovered). Degenerate regime NOT sampled tonight — both windows healthy; the harsh-regime guarantees rest on the harness (scenarios a/e/g/i) |
| Playing way ahead, live | Caption clock 8.5 s ahead of raw through freeze/thaw | Window A `max_lead=1.00 s` ✓. Window B hit 8.81 s once — the RESUME snap-oscillation (−10.2→0→−8.8→0, self-corrected), P3's documented residual (real fix = slope-aware pin) |
| Skip around live → way off | Wedge at true edge: set_time no-ops ×4, raw pinned 94.18 for 7 min, no rescue; scrub mislanded (102.7 vs −14.3) | Both windows: scrub lands on target (or correctly clamps at the buffer head; the one "FAIL" was the driver's unclamped expectation), jump lands per D1 policy and tracks through raw renumbering (off_v ±420-510 handled), no unrecovered freeze (max 2.0/8.3 s vs 15 gate), post-jump/scrub re-anchor ≤14 s blank |
| Slightly off, movies/series | ~0.5 s word bias; elst/TimecodeScale ignored; ASS fallback broken; MKV tail hole; delay sign inverted | P1 landed (six bugs, fail-before/pass-after in `p1{a,b,c}_report.md`); tonight's live VOD verification skipped — CDN refusing (HTTP 520, §5); offline VOD suites green (`test_vod_splitter`, `test_vod_series`, `test_fixes`) |

## 7. Driver/tooling deltas made tonight (P5-owned)

- `sync_stage3_run.py`: fixed two vacuous checks (`health.last_paint`
  tested before any `health_pump` — cold-join paint + VOD paint) and
  the unclamped scrub-back `want` (clamps at the buffer head like the
  harness's scenario-f check). No acceptance semantics loosened — all
  three fixes make previously-vacuous/miscomputed checks measurable.
- `p5_engage_run.py` (new): the two mid-show Off→On engage cases +
  a live D1 sample, with join-byte/anchor/first-paint/pin-overshoot
  instrumentation.
- `p5_vod_probe.py` (new): CDN 551-class probe.
- `p5_probe_f{,2,3}.py` (new, throwaway diagnostics): the harness-metric
  investigation of §0 — their outputs are the evidence cited there.
- No production code touched.

## 8. Artifacts

Run outputs: `p5_offline_chain.out` (+ `p5_out_test_*.txt`),
`p5_harness_full.out`, `p5_harness_quick{1,2}.out`, `p5_diag_f_quick.out`,
`p5_diag_f_full.out`, `p5_ab_f_p3livecc.out`, `p5_probe_f{,2,3}.out`,
`p5_live_window{A,B}.out`, `p5_engage_run{,2}.out`; sync logs:
`p5_sync_window{A,B}.log`, `p5_sync_engage{,2}.log`; saved diff for the
A/B: `p4b_live_cc.patch` (= the working tree's WP4b `live_cc.py` delta).

## 9. Follow-ups for the next package

1. Harness metric: index real-path deliveries (§0) — unblocks f/g;
   re-run the full matrix + quick ×2 after.
2. Mid-show join pin: use the join content position for the first
   batch (§3); also close the PCRJOIN logging/probe gap for join>0.
   Expected effect: ≥90-engage first paint 22-35 s → ~5-10 s.
3. Post-jump/scrub re-anchor up to ~14 s blank (watchdog rebases after
   raw renumbering) vs the driver's 8 s stop gate — tighten the re-pin
   or widen the gate with cause.
4. RESUME snap-oscillation ±9-10 s (window B; P3's documented residual)
   — the slope-aware pin remains the real fix.
5. `test_profanity` 0-byte init segfault (3/6 tonight, passes on retry)
   — one look at Qt/VLC init under handle/GDI pressure.
6. Live-verify the D1 high-L branch and the VOD relay path when the
   provider offers those conditions (high-L window; CDN accepting).
