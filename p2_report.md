# WP2 — Live-edge wedge cluster: implementation report

**Package:** WP2 (option 1 approved 2026-08-22: the full stack (a)+(b)+(c),
plus product decisions D1 + D2). Design: `p2_design.md` (approved as-is;
deviations during implementation listed in §3 — all evidence-driven, none
change the approved contracts).

**Outcome:** all three measured symptoms of the wedge cluster now have a
mechanism with a fail-before proof: `test_sync_adversarial.py` 46/46 in
`--quick` twice (was 31 checks + 4 live-E2E FAILs on the 2026-08-21 night),
all 14 offline suites green, 6/6 mutation proofs, new `test_wedge_cluster.py`
23/23.

---

## 1. What landed

`src/ui/player_view.py` (+412/−79 against 6248965):

| Mechanism | Where | What it does |
|---|---|---|
| **(a) seek-verify-escalate** | `_arm_seek_verify` / `_verify_seek` / `_chase_revive` | every chase `set_time` arms a verify `(target, vlc_t, deadline, armed_at)`; `_tick` checks raw reached the target on the content axis by a target-proportional deadline (`min(6, 1.5+0.15·|jump|)`, tol 2.0) and escalates to the local-file `play_at` revive on a confirmed no-op. Backoff ladder 5→15→30 s (decays after 90 s clean), shares `_REOPEN_COOLDOWN_S`. |
| **(b) PCR-head wedge rescue** | `_head_ahead_s` + the `_tick` rescue signature | "data ahead" now measured from the PCR write head (`head_rel + join_app − current`), not the frontier — reachable at the true edge past the under-credited frontier (the 2026-08-21 geometry where the old `frontier − current > 10` was negative all night). Legacy frontier fallback while PCR/join pins are unavailable. Movement threshold is rate-aware (`max(0.02, 0.5·rate·dt)`) — the flat 0.05 read 0.125x slow-mo as frozen. |
| **(c) freeze-aware clock** | `_trickle_test` / `_raw_win_rate` + `_caption_clock_s` | `_tick` keeps a rolling 3-s `(now, raw)` window; raw advancing < 0.3·rate·wall while "playing" holds the caption clock AND `_vid_s` (branch `trickle`). Anti-lead clamp: `clock ≤ raw_content + 1.0` while playing (suspended while a seek verify is pending). The live-edge backlog advances with **delivered** data during holds (`_raw_win_rate`), not wall — see §3.1. |
| **D1 adaptive jump-to-live** | `_chase_jump_back_s` + `_chase_seek`/`_cap_seed_transport`/FF catch-up | LIVE lands `max(5, L+3)` behind the edge while the measured L > 8 s, true edge (−5) otherwise. The landing gap now seeds the backlog for every transport (the constant-5 seed double-counted the gap). The FF catch-up reset compares against the true edge, not the frontier. |
| **D2 near-play join** | `_cc_join_byte` + `_start_cc_when_buffer` | the CC reader joins ~8 s behind the playback position at ANY frontier (the `frontier >= 90` gate is deleted); `vid_s` is clamped to the frontier so a viewer past the under-credited frontier cannot overshoot the file tail. |

Pure helpers (`_chase_jump_back_s`, `_cc_join_byte`) are module-level and
unit-tested directly; every new gate reads the clock through the `now_s()`
seam (WP0), so the harness drives them on virtual time.

## 2. Proof (rule zero: everything below FAILED before its fix)

### 2.1 Mutation proofs (each detection disabled surgically → its scenario fails)

| mutation (temporary) | result | caught by |
|---|---|---|
| **m-a** verify measures but never escalates | **h1 FAIL** — "escalation" arrived at +8.3 s (that was (b)'s rescue; the 6-s deadline was missed) | `h: user seek escalates within deadline+2` |
| **m-b** PCR head test disabled (legacy frontier only) | **h0 FAIL** — no autonomous rescue at all: the viewer sits past the under-credited frontier where `frontier − current` is negative — the exact 2026-08-21 structural blindness | `h0: rescued autonomously <= 15 VT s` |
| **m-c** freeze-aware clock disabled (no hold, no clamp) | **g1 FAIL** — p95 2.00, max lead 3.90 over the trickle (the wall-integrating clock runs ahead; the live night measured 8.5 s) | `g: clock tracks raw within ~1.5 s` |
| **m-rate** flat 0.05 movement threshold | **unit FAIL** — `_raw_change_wall` never refreshes at 0.125x (tick-driven check, not formula recomputation) | `test_wedge_cluster.py` slow-mo check |
| **m-D1** adaptive landing forced to `edge − 5` | **c FAIL ×2** — landed `edge−5` but the (independent, inlined) expectation says `edge−16.5` (L 13.5 at the jump); backlog seeded 5 vs 16.5 | scenario c's two D1 checks |
| **m-D2** `frontier >= 90` gate re-added | **unit FAIL** — join byte 0 at frontier 20 | `test_wedge_cluster.py` D2 check |

All mutations reverted; source verified clean (`MUTATION` absent).

Two check-strengthening lessons worth keeping: the first versions of the
m-D1 and m-rate checks **self-neutralized** (they recomputed the production
formula — a mutated formula mutated the expectation too). Both now assert
behavior: the D1 expectation is inlined in the harness and the E2E driver,
and the slow-mo check drives the real `_tick`.

### 2.2 Harness — `test_sync_adversarial.py --quick`, full matrix

* **Run 1: 46/46** (mechanism 39/39, data-limited 7/7).
* **Run 2: 46/46** — every mechanism check line byte-identical to run 1;
  the only diffs are h3's data-limited detail numbers (p95 0.63 vs 1.03,
  settle 16 vs 72 s — both well inside the gates; `run_until_settled`
  detects on the metric and rides real-CCX pacing by design, the P0
  precedent). Evidence: `p2_harness_quick{1,2}.out`.

New scenario outputs (run 1):

```
== scenario g: 0.2x-delivery trickle, <6 s freeze/thaw cycles ==
  g: steady pre-trickle:       p95=0.53 exact=37/37
  g: trickle (incl. seek):     p95=1.24 exact=286/327
  g: post-trickle recovery:    p95=0.78 exact=118/118
  ok  g: caption clock tracks raw within ~1.5 s   [n=414 p95=1.00 max_lead=1.00]
  ok  g: mid-trickle seek lands ~30 s behind the DISPLAYED position
  ok  g: painted displacement contained (p95 <= 2.5 — L-drain residual is WP3's feed)
  ok  g: no spurious reopen during the trickle (wedge rescue quiet)
  ok  g: pipeline re-settles after the trickle
  ok  g: no pipeline exceptions

== scenario h: injected set_time no-op (wedge at the true edge) ==
  ok  h0: wedged player at the true edge rescued autonomously
         [revived after 8.6s (wedge@85.6 fr=70.9 head=89.6)]
  ok  h1: user seek escalates within deadline+2   [escalated after 6.3s]
  ok  h2: escalated play_at lands the SEEK TARGET  [raw@esc=41.7 want~41.6]
  ok  h-starved: no autonomous reopen while no data is ahead
  ok  h-starved: user seek still escalates and lands
  ok  h: bounded rescue attempts (3 play_at, no loop)
  ok  h3: painted cues re-settle after the revives [p95=0.63]
  ok  h: no pipeline exceptions
```

Scenario c's jump check is now D1-aware and independent of the production
helper: `L@jump=13.5 → back=16.5`, landed `edge−16.5` with the backlog
seeded exactly 16.5 (the no-double-count clause).

Pre-existing scenarios a/b/d/e/f unchanged in intent; their numbers moved
within their gates (a drift p95 1.48 ≤ 1.5, b p95 0.85, d quiescent 0.78,
e contained 4.73 ≤ 6, f steady 0.54/scrub 0.50) — full outputs in
`p2_harness_quick1.out` / `p2_harness_quick2.out`.

### 2.3 Offline suites (all green)

`test_caption_overlay` 135/0 (one test updated, see §3.4) · `test_cuestore`
28/0 · `test_vod_splitter` 108 ✓ · `test_profanity` 54 ✓ · `test_fixes`
51/0 · `test_always_chase` 44/0 (incl. "LIVE lands edge − 5 s" — L low ⇒
true edge, unchanged) · `test_dvr_e2e` 12/0 · `test_subtitles` 22 ✓ ·
`test_sub_settings` 37 ✓ · `test_bundled_ccx` 9 ✓ · `test_overlay_focus`
12 ✓ · `test_tab_resize` 16/0 · `test_temp_cleanup` 6 ✓ ·
**`test_wedge_cluster` (NEW) 23/23** — D1 formula, D2 join (incl.
past-frontier clamp), deadline arithmetic, escalation ladder + cooldown +
strike decay, head-ahead PCR axis + legacy fallback, tick-driven slow-mo
movement, trickle verdict + raw-rate measurement.

## 3. Implementation notes & deviations from the design doc

All deviations are additions the evidence forced during landing; the
approved contracts (checks, thresholds, failure modes) are unchanged.

1. **Trickle-aware backlog advance** (§(c) extension). First cut kept the
   backlog growing at wall rate (1x) while the clock held — on a 0.2x night
   the edge estimate then ran ~5x fast, the PCR edge-snap sawtoothed
   (measured: repeated `EDGESNAP err≈−3`, backlog 0.01→1.38 per cycle) and
   dragged every anchor pin with it. Now `backlog += raw_rate·dt −
   clock_adv` while trickling (`_raw_win_rate`, clamped to the playback
   rate): the edge advances with DELIVERED data. Pauses still grow it 1:1
   (trickling is False — the viewer genuinely falls behind).
2. **Fold-expectation redefinition** in `_caption_clock_s`: `expected` is
   now "how far the CLOCK believes frames played since raw last moved"
   (`(prev_clock − _cap_raw_clock) + wall_adv`), not `d_wall·rate`. The old
   wall-based expectation made every thaw after a held stretch (stall,
   trickle, pause) read as a PTS renumber. Baseline `_cap_raw_clock` is
   stored after all clock mutations in the tick. Verified against every
   existing scenario: b's +25 renumber div still accumulates to ~75; d's
   burst-thaws fold instead of mislabeling as renums.
3. **h0 staging is wall-neutral across modes**: the burst is
   `stall + 15` content s and the deficit is waited out explicitly, so
   `grow_1to1` actually appends during the wedge window in BOTH `--quick`
   and full (a fixed +60 burst on a DUR-scaled 21-s stall left the head
   39 s past the wall axis in quick — growth silently stalled and the
   first h0 run tested a frozen head).
4. **`test_caption_overlay` fold test setup updated**: the old setup parked
   the clock ~5 s past the raw reading; the anti-lead clamp now (correctly)
   clamps that state, so the test parks the clock coherently before
   exercising the fold. The fold itself is unchanged.
5. **Check independence**: D1's expectation is inlined in the harness and
   the E2E driver (not imported from the production helper) after m-D1
   self-neutralized the first version. Same principle as WP0's
   outcome-over-mechanism rule.
6. **h3 self-calibration**: h's post-revive recovery is measured on its own
   re-calibrated axis (`h.calibrate(20)` after settling). The absolute
   offset vs scenario a's axis (~1.7 s, matrix mode) is L-EWMA inertia
   through the stall/burst/wedge history — WP3's target (see §5), the same
   class as g3's residual. `run_until_settled` + a tight 12-s window (p95
   ≤ 1.5) still proves re-settling.

## 4. E2E driver (`sync_stage3_run.py`) — updated per design §6

* **Rewritten** `jump-live lands per the adaptive policy (raw moved/tracks)`:
  expected landing = `edge_pre − back` with `back` inlined; raw must MOVE
  from `raw_pre` (or track forward) — closes the vacuous pass where a
  player frozen exactly at the target satisfied the old check.
* **Rewritten** `scrub back lands ~120 s behind (clock AND raw reaches it)`:
  adds a `wait_until` raw-landing assertion (≤ 4 s, 8 s window) — the (a)
  mechanism's live-observable half.
* **New** `no unrecovered raw freeze > 15 s while PCR data ahead` —
  driver-side monitor, computed independently of the app's rescue.
* **New** `caption clock never leads VLC raw > 1.5 s while playing` —
  the direct encoding of the 8.5-s-ahead night.
* Health.tick extended accordingly (raw-movement tracker + head-ahead
  monitor + lead sampler); cold-join checks unchanged (D2 changes the
  join byte, not engagement; the live mid-show Off→On exercise belongs to
  P5 per the plan).

Live E2E itself is P5's package (one provider connection, both delivery
windows) — the driver is ready for it.

## 5. Findings that feed WP3 (L tracking)

The deepest finding of this package: **the anchor's residual error under
regime changes is L-EWMA inertia, not clock error.** Concretely:

* In the harness's 1:1 phases the starve guard lets real CCX trail the
  feed by up to 8 content s, so the L EWMA legitimately settles ~5-6 and
  the anchor *compensates* (painted stays correct — Δ ≈ L). In the trickle
  regime the release spacing narrows to ~2 while L drains at 0.18/batch
  with a batch only every ~10 VT s: the pin runs early by (L − Δ) until it
  drains. Measured: standalone g trickle p95 2.13; matrix (younger buffer,
  different L history) 1.24 — same code, different L history.
* A 120-s trickle lead-in waiting for L ≤ 2.8 made it WORSE (rebase war,
  perr 10+): you cannot wait out the EWMA inside the trickle; the fix is
  WP3's lead-compensated / adaptive-α L, not pacing.
* h's stall/burst/wedge history leaves the same inertia (h3's absolute
  offset ≈ 1.7 s vs a's axis).

So: g3's containment gate (p95 ≤ 2.5, data-limited, WP3-tagged) and h3's
self-calibrated axis are the honest boundaries of THIS package; the clock
mechanism itself (g1: p95 1.00, max lead exactly the 1.0 clamp) is exact.

**Measurement gotcha for future sessions:** the harness never calls
`setup_logging()`, so its `mtp.sync` INFO rows go nowhere —
`%APPDATA%\MichaelTVPlayer\sync_debug.log` still holds the 2026-08-21
night (I briefly analyzed stale rows as if they were harness output; the
clock-time overlap made it convincing). Harness-side diagnosis must use
the DIAG/HDBG env-gated prints or add a handler.

## 6. Known limitations

* `(b)`'s full fidelity needs the CC pipeline alive (PCR join pins); with
  captions AND filter off, `_sync_pcr_join` is absent → legacy frontier
  fallback (today's behavior). Documented in `_head_ahead_s`.
* If real VLC ever ignored `play_at` too, the ladder caps attempts at one
  per ≤30 s — no livelock, recovery falls to a channel change (design §8).
* `_vid_s` can overshoot raw by ≤ the trickle-window fill time (~2-3 s)
  before the verdict engages — bounded, and immaterial to the rescue's
  10-s head-ahead threshold.
* The dead `stallsnap` branch (`frozen_for` is 0 inside the raw-change
  path, so its `> 6 s` condition can never hold there) predates WP2 and
  is untouched; noted for WP3's store/anchor work.

## 7. Files touched

* `src/ui/player_view.py` — all mechanisms (§1) + state resets in
  `__init__`/`play_media`/`stop`.
* `test_sync_adversarial.py` — FakeVLC `wedge()` mode; scenarios g, h;
  scenario c D1-aware (inlined expectation); DIAG gains hold/bl/rawrate.
* `sync_stage3_run.py` — §4.
* `test_caption_overlay.py` — fold-test setup made clamp-coherent (§3.4).
* `test_wedge_cluster.py` — NEW unit home (23 checks).
* `p2_design.md` (phase 1 artifact), `p2_harness_quick{1,2}.out` (evidence).
