# WP2 — Live-edge wedge cluster: design doc (PHASE 1 — awaiting approval)

**Written:** 2026-08-22. Owner: WP2 session. WP0/WP1 merged (6248965).
**Status:** design only — nothing implemented. Reply `proceed with option X`
to start Phase 2 (implementation + D1 + D2, scenarios g/h, harness ×2,
offline suites, `p2_report.md`).

Evidence base (all re-verified against the current tree and raw logs today):

- `%APPDATA%\MichaelTVPlayer\sync_debug.log` (2026-08-21 E2E night,
  0.17x-delivery regime) — XPORT/XPORT1s rows below.
- `%APPDATA%\MichaelTVPlayer\player.log` — rescue/revive rows.
- `sync_diag_e2e_live.out` — 6/10 checks, the 4 FAILs.
- Code: `src/ui/player_view.py` (line numbers current as of today).

---

## 1. The wedge cluster, precisely (verified timeline)

All times 2026-08-21, from `sync_debug.log`:

| wall | event | raw | frontier | cc clock | note |
|---|---|---|---|---|---|
| 17:36:52 | `pause` | 94.18 | 57.39 | 102.70 | clock already **8.52 s ahead of raw** |
| 17:37:52 | `resume` → XPORT1s | 94.18 | 57.39 | 102.70 | raw did not move; VLC reports playing |
| 17:39:52 | `jump_live`/`jump_edge` target 94.18, safe 89.18, vlc_t 86.63 → XPORT1s | 94.18 | 57.39 | 102.70 | **set_time no-op** |
| 17:42:22 | `chase_seek` target −17.30, safe 0.00 → XPORT1s | 94.18 | 57.39 | 102.70 | **set_time no-op** |
| 17:42:45 | `chase_seek` target 222.70, safe 89.18 → XPORT1s | 94.18 | 57.39 | 102.70 | **set_time no-op** |

Supporting facts:

- `player.log`: the **last** chase rescue of the night fired at 15:58:41 with
  `frontier − current = 214.2 − 156.2 = +58 s` (the viewer BEHIND the
  frontier — the only signature the rescue can see). Zero rescue rows during
  the 17:36–17:43 wedge. Structurally: at the true edge the viewer sits PAST
  the under-credited frontier, so `frontier − current` is negative
  (`player_view.py:2513` requires `> 10`).
- The revive inside `_chase_seek` (`player_view.py:1568`) triggers only when
  `state ∈ {ended, stopped, error}`. The wedged player reports **playing**
  the whole time (`is_playing()` true), so every transport press funnels into
  the `set_time` branch (`:1572`) and no-ops.
- The dead-reckoned caption clock (`_caption_clock_s`, `:3063`) integrates
  `dt × rate` of **wall** time while "playing" unless raw has been frozen
  **6 s continuously** (`_CC_STALL_FREEZE_S`, `:100`). The 0.17x night fed
  sub-6-s freeze/thaw trickles, so the stall branch never engaged and the
  clock walked 8.5 s ahead of the frames actually on screen.
- E2E irony: the `jump-live lands at the TRUE edge` check **passed
  vacuously** — raw equaled the target because it was frozen at exactly the
  buffer tail (94.18 ≈ edge 94.18), not because the jump landed. The driver
  (`sync_stage3_run.py:233`) samples raw 3 s after the jump and accepts
  "already there". The four real FAILs: cold-join paint, scrub-back landing
  (landed 102.7 vs want −14.3), silent-stop stretch 14.1 s, paint ratio
  363/5120.
- Frontier axis is unreliable in **both** directions: it under-credits the
  cold burst (never credits content that landed before the first growth
  sighting — tonight −37 s) yet is wall-anchored per sighting
  (`_note_dvr_data` credits min(15, wall-gap) on every growth sighting,
  `:1215-1227`), so a slow-delivery trickle makes it **over**-credit new
  content, and a long provider stall freezes it entirely. Any wedge test
  keyed on the frontier inherits both failure directions. The PCR head
  probe (`_cc_probe_head_pcr`, throttled 4/s) is the only content-true
  write-head signal, already maintained by `_cc_edge_probe_tick`.

Three user-visible symptoms, three distinct defects:

1. **"Playback freezes at live"** — wedge occurs (VLC demuxer-blocked at the
   tail of the growing file while reporting playing); no autonomous path can
   see it at the true edge.
2. **"Skips stop working"** — every user transport funnels through
   `set_time`, which no-ops in that state; nothing verifies a seek landed.
3. **"Captions run ahead"** — the caption clock (and `_vid_s`) integrate
   wall time through sub-6-s freeze/thaw trickles.

---

## 2. Options evaluated

### (a) Seek-verify-escalate

**Mechanism.** `_chase_seek`'s set_time branch arms a verify record
`(target_content, vlc_t, deadline)`; the chase branch of `_tick` (2.5 Hz,
always running) checks it:

- **verify pass** — `|_cap_content_for_raw(raw) − target| ≤ tol` → clear.
- **verify fail at deadline** — escalate to the *existing* revive path
  (local `play_at` at the target: `_cap_seed_transport(target)` +
  `vlc.play_at(buf, vlc_t)` + rate/volume pokes). Local file operation; the
  single provider connection is never touched.
- Deadline is **target-proportional** (VLC may legally take >1.5 s on a big
  demux jump): `deadline = min(6.0, 1.5 + 0.15·|vlc_t − raw_pre|)`.
- **Hysteresis / anti-reopen-loop:** one escalation per armed seek; escalations
  share `_REOPEN_COOLDOWN_S` (5 s) and add a strike/backoff ladder
  (5 → 15 → 30 s) that decays after 90 s of clean verifies. A `play_at` that
  itself fails to land within `2 × deadline` counts the next strike. Verify
  state is last-wins (a new `_chase_seek` re-arms — user intent = latest) and
  is torn down on session/mode change (`play_media`, `_reopen_display`
  generation bump).
- Production already has the diagnostic shape (`XPORT` raw_pre + `XPORT1s`
  1 s later); this makes the same measurement *actionable*.

**Why keep it:** it is the only option that guarantees the **user's press**
works ("skips stop working" is the interaction-failure symptom). (b) heals
the same wedge but only after its own freeze window; a viewer who presses
LIVE/−60s should not wait for that.

**Failure modes.**
- *Legal slow seek misread as no-op* → one needless `play_at` (~0.5 s
  hiccup). Mitigated by the proportional deadline; worst case is bounded and
  rare (local-file seeks land ≪1 s).
- *Reopen loop* (revive target lands in a data hole, player re-stalls,
  rescue re-fires): backoff ladder + `_REOPEN_COOLDOWN_S`; reopens have
  empirically revived the player (player.log rows), so loops require a
  broken buffer file, which no policy can fix — the ladder caps the damage
  at one attempt per ≤30 s.
- *Verify confused by PTS renumber mid-window* → verify compares on the
  content axis via the tracked divergence; a renumber folds into `div` on
  the next clock pass, and tol 2.0 s matches `_CC_SYNC_TOL_S`'s class.
- *Clock already seeded wrong between no-op and escalate* — the seek seeds
  the clock at the target by construction, so during the ≤ deadline window
  anchors may pin ~(`wedge − target`) off; bounded by the deadline (≤6 s)
  and fully re-seeded by the revive's `_cap_seed_transport`.

### (b) Wedge detection independent of the frontier

**Mechanism.** Keep the `_tick` rescue's structure, replace the
data-availability test with the PCR content axis:

```
head_ahead = (head_pcr − join_pcr) + _cc_join_app_s − current
fire iff: raw_frozen > 8 s  AND  playing  AND  head_ahead > 10 s
         (fallback while PCR/join pins unavailable: frontier − current > 10,
          today's behavior)
```

`head_pcr` is already cached at 4/s (`_cc_head_pcr`); `current` and the
join-app refinement are content-axis. This is exactly the quantity
`_cc_calibrate_edge` already trusts for the live edge.

Two required corrections to the existing frozen test:

- **Rate-aware movement threshold.** `_tick` refreshes `_raw_change_wall`
  only when `|raw − _last_raw| > 0.05` (`:2482-2484`) — at 0.125x slow-mo a
  tick advances ~0.05 s, so legit slow playback reads as frozen and (with
  data ahead > 10 s, which slow-mo guarantees within ~12 s) the rescue would
  reopen every cooldown. Refresh when
  `|Δraw| > max(0.05, 0.5 · dt_tick · rate)`.
- The existing 8-s freeze window stays (it correctly ignored the wedged
  player's 1 ms oscillation).

**Why keep it:** the only option that heals the wedge **without user
interaction** ("playback freezes at live" while nobody touches anything —
the actual night: the wedge persisted through pause/resume and two seeks).
Also kills the false-residue the *old* frontier test produces under
slow-delivery (see scenario g: on current code the over-crediting frontier
makes the old rescue fire spurious reopens every cooldown).

**Failure modes.**
- *PCR probe failure / join pin missing / mid-session join not yet refined*
  → falls back to the frontier test (today's behavior; no regression).
- *Genuine provider stall* (no data ahead) → correctly silent (this is the
  h-starved sub-case; assert it in scenario h).
- *Slow-mo false positive* → closed by the rate-aware threshold above.
- *0.2x trickle* → raw freezes ≈4.3 s between appends (<8 s) and
  `head_ahead ≈ 1 s` (<10) → silent on both conditions.

### (c) Freeze-aware clock

**Mechanism.** Two layers in chase mode:

1. **Ratio hold.** `_tick` maintains a rolling `(now, raw)` window
   (3 s, ~8 samples at 2.5 Hz — `_tick` always runs in chase, so this works
   with captions OFF, unlike `_caption_tick`); expose
   `trickle_hold = playing and raw_adv < 0.3 · (rate · wall_adv)` over the
   window. While held, `_caption_clock_s`'s `wall_adv` is 0 (new branch
   `trickle`, sibling of `stall`) and `_tick` does not integrate `_vid_s`.
   On thaw the existing machinery already handles the catch-up: small
   trickle jumps fold (`|residual| ≤ 2`), real ≥6 s underruns take the
   `stallsnap` branch.
2. **Anti-lead clamp.** While playing, clamp
   `clock ≤ _cap_content_for_raw(raw) + 1.0 s`. Raw is the displayed truth;
   the clock may lag it (data trickle) but must never lead it by more than
   the fold granularity. The clamp is div-aware (renumber-safe: `div`
   updates before the next integration) and skipped while paused (the
   pause-holds contract, E2E "clock holds while paused") and when raw is
   unreadable.

The live-edge estimate stays honest through all of this because the backlog
carries the growth: `edge = clock + backlog`, and the backlog integration
(`:3128-3134`) already grows 1:1 with wall while data flows — clamping the
clock does not corrupt the edge, it stops the *anchor pin* (`edge − L`)
from running hot.

**Why keep it:** the only option that fixes the caption symptom itself
("captions run ahead" — measured 8.5 s). Also keeps the seeded-clock error
after a no-op seek bounded (clamps back to the frozen frame's position
until the revive re-seeds).

**Failure modes.**
- *Over-hold on transient raw jitter* (get_time hiccups during heavy seek):
  holds ≤ window (3 s), folds on recovery; transient caption lag bounded by
  the window.
- *4x FF*: ratio = raw_adv/(rate·wall) ≈ 1 → no hold; clamp inert (clock ≈
  raw). 0.125x: ratio ≈ 1 → no hold.
- *Wedge (raw frozen ≥ window)*: hold + clamp → clock parks at the frozen
  frame — captions match what is on screen; the anchor stays sane until
  (a)/(b) revive.
- *Interaction with scenario a's slow drift (speed_warp)*: ratio ≈ 0.9995 →
  no hold; unchanged behavior.

### (d) Product interplay — D1 / D2 (already approved; designed here)

**D1 adaptive jump-to-live.** New constants
`_CC_ADAPTIVE_MIN_L_S = 8.0`, `_CC_ADAPTIVE_PAD_S = 3.0`. Helper:

```
back = _CHASE_SAFETY_S if (L := self._cc_lag) is None or L <= 8.0
       else max(_CHASE_SAFETY_S, L + 3.0)
target = clamp(edge − back,  0,  frontier + 120)      # _chase_seek :1558
```

Three coupled edits that must land together:

- `_chase_seek`'s `jump_live` branch uses `back` (was hardcoded
  `edge − _CHASE_SAFETY_S`).
- `_cap_seed_transport(jump_live=True)` seeds `backlog = back` (was
  `= _CHASE_SAFETY_S`, `:1396`) — otherwise `edge = clock + backlog`
  double-counts the landing gap and every downstream anchor/edge
  calibration is wrong by `back − 5`.
- The FF catch-up reset (`:2544-2546`) compares `frontier − current` →
  becomes `edge − current`. With adaptive landing the viewer normally sits
  `back` behind the TRUE head while the frontier under-credits; the old
  form would either fire instantly (frontier below current) or never
  (frontier over-crediting), resetting FF at the wrong moment.

Rationale for `L + 3`: landing exactly at `edge − L` puts the newest cue at
the playhead with zero cushion for EWMA error/jitter; 3 s of pad keeps first
paints immediate without reintroducing the wedge zone. L is the existing
per-batch EWMA (`_CC_LAG_ALPHA` 0.18); WP3 may improve the estimator — D1
only consumes it. Failure mode: L drains fast, EWMA still high → "live" sits
~L+3 back for a few EWMA time constants (visible but benign; captions
timely, which is the point).

**D2 near-play caption join.** Delete the
`frontier >= _CC_JOIN_MIN_FRONTIER_S` gate (`:3749`) — the join byte is
computed from the playback position at ANY frontier. Extract the arithmetic
into a pure helper for testability:

```
def _cc_join_byte(size, frontier, vid_s) -> int:
    target_s = max(0.0, min(vid_s, frontier) - _CC_JOIN_BACK_S)
    join = int(size * target_s / max(frontier, 1.0))
    return max(0, min(join - join % 188, size - 188))
```

- The `min(vid_s, frontier)` guard is NEW and required: after a true-edge
  landing the viewer sits past the under-credited frontier, and the old
  formula would compute `target_s > frontier` → join clamped to the file
  tail.
- `_CC_JOIN_MIN_FRONTIER_S` (90) is removed. At tiny frontiers the formula
  degenerates to join ≈ 0 naturally (the old "byte-0 is instant anyway").
- Consequence: most engages are now mid-session joins → the
  `_cc_join_app_s` refinement path (`_cc_refine_join_app`) becomes the
  common path (it needs `seg_s > 5` of growth; during a 0.2x trickle that's
  ~25 s wall, during which edge calibration politely waits — acceptable,
  noted).
- Harness impact: none — the harness engages CC at frontier ≈12 s with
  `vid ≈ 4` → join byte 0, same as today. Proof moves to a unit test of the
  helper (frontier 20/vid 15 → join ≈ 7/20 of file; frontier 400/vid 390 →
  ≈ 382/400; vid past frontier → clamped sane).

---

## 3. Recommendation — Option 1: the full stack (a) + (b) + (c) [+ D1 + D2]

The three mechanisms are layered defenses with disjoint false-positive
risks, and each owns one measured symptom:

| symptom | owner | backup |
|---|---|---|
| playback freezes at live (no interaction) | (b) PCR-head rescue | D1 reduces exposure |
| skips/transport presses dead | (a) verify-escalate | (b) within its window |
| captions run ahead of speech | (c) trickle-hold + clamp | (a)/(b) revive playback so raw resumes |

Any subset leaves a measured failure unfixed or slows its recovery:

- **Option 2 — (b) + (c) only** (drop per-seek verify): a user press during
  the wedge still no-ops for up to (rescue window + cooldown) ≈ 13 s and,
  worse, *silently* — the seek target was consumed, `set_time` swallowed.
  "Skips stop working" was the symptom the user actually reported first.
- **Option 3 — (a) only**: heals presses but not the unattended freeze, and
  leaves the caption clock integrating through trickles (the 8.5 s lead)
  — the anchor pin stays hot all night even with perfect transport.
- **Option 1 — (a)+(b)+(c)**: all three symptoms covered at their source;
  the only added interactions are the shared cooldown/backoff (a)↔(b) —
  which is *required* anyway to prevent reopen ping-pong — and (c)'s clamp
  making (b)'s `current` (via `_vid_s`) and the clock agree with the frozen
  frame, so the rescue revives *where the viewer actually was*.

Cost/risk is modest: ~120 lines in `player_view.py` plus tests, all in
chase-gated paths; VOD/live-plain modes untouched.

---

## 4. New constants (single table — Phase 2 implements exactly these)

| constant | value | where | rationale |
|---|---|---|---|
| `_SEEK_VERIFY_BASE_S` | 1.5 | (a) | legal local-file seeks land <1 s (XPORT1s evidence) |
| `_SEEK_VERIFY_PROP_S` | 0.15 | (a) | per |Δ| s of jump — demux slack for big seeks |
| `_SEEK_VERIFY_MAX_S` | 6.0 | (a) | cap; no-op verdict at 1 s is unambiguous, 6 s is generous |
| `_SEEK_VERIFY_TOL_S` | 2.0 | (a) | arrival tolerance, `_CC_SYNC_TOL_S` class |
| `_SEEK_ESC_BACKOFF_S` | (5, 15, 30) | (a) | strike ladder; decays after 90 s clean |
| `_WEDGE_DATA_AHEAD_S` | 10.0 | (b) | replaces `frontier − current > 10` on the PCR axis |
| `_RAW_MOVE_FRAC` | 0.5 | (b) | rate-aware movement threshold (0.125x slow-mo fix) |
| `_CC_TRICKLE_WIN_S` | 3.0 | (c) | rolling window; < freeze/thaw cycle (~5 s), > raw jitter |
| `_CC_TRICKLE_RATIO` | 0.3 | (c) | 0.2x trickle holds, 1x/4x/0.125x playback never does |
| `_CC_LEAD_MAX_S` | 1.0 | (c) | anti-lead clamp; contract is 1.5, clamp sits under it |
| `_CC_ADAPTIVE_MIN_L_S` | 8.0 | D1 | approved threshold ("while measured L > ~8 s") |
| `_CC_ADAPTIVE_PAD_S` | 3.0 | D1 | cushion over `edge − L` for EWMA error |

All new gates read the clock through the `now_s()` seam (WP0), so the
harness drives them on virtual time deterministically. Window/deque
maintenance lives in `_tick` (always running in chase), consumed by
`_caption_clock_s` — captions OFF must not disable wedge/trickle logic.

---

## 5. Acceptance tests (exact)

### Harness: scenario g — 0.2x-delivery trickle, sub-6-s freeze/thaw

**Staging.** `fresh_harness(CueQueue(lag_const(1.5)))`; custom growth: every
5.0 VT s append 1.0 s of content (avg 0.2x; raw freezes ≈4.3 s between
appends — never reaching the 6-s continuous stall). Note the harness's
frontier will *over*-credit (wall-anchored per sighting) — realistic per
`_note_dvr_data` and deliberately kept: it is what makes the OLD rescue
false-fire. Mid-trickle, one `_seek_ms(-30000)` then `_seek_ms(+30000)`
while the trickle continues, then a 1:1 recovery tail
(`run_until_settled` + 12 s).

**Checks.**
- **g1 [mechanism]** `caption clock tracks raw within 1.5 s through the
  trickle (p95) AND never leads > 1.5 s (p100)` — sample
  `view._cap_clock_s − view._cap_content_for_raw(raw)` at 10 Hz while
  playing. *Fails on current code* (clock integrates ~0.8 s/s of wall ahead).
- **g2 [mechanism]** `mid-trickle seeks land`: after each seek,
  `|cap_clock − want| ≤ 3 s` and raw moved to the converted target within
  `_SEEK_VERIFY_MAX_S + 2` VT s.
- **g3 [data-limited]** `painted cues correct while cues flow`:
  p95 ≤ 1.5 s, exact-window ≥ 85% over the trickle (cue flow is starved by
  design; gate on samples existing).
- **g4 [mechanism]** `no spurious reopen during the trickle`: zero
  `play_at` in `fake.commanded` during the trickle phase (the old
  frontier-based rescue false-fires here within ~13 s — this check fails on
  current code too, proving (b)'s selectivity).
- **g5 [mechanism]** no pipeline exceptions; post-trickle recovery
  re-settles (`run_until_settled` < 150 s).

### Harness: scenario h — injected set_time no-op (the exact night)

**Staging.** FakeVLC gains a `wedge` mode: `wedged=True` → `set_time`
records the command but does not move; `get_time()` returns the frozen
wedge position; `is_playing()` stays True; `play_at` **clears** the wedge
(a revive works). Phases:
1. **Burst under-credit** (scenario-d cadence, ~60 s) then `_jump_live` —
   lands at the TRUE edge, past the under-credited frontier (mirrors
   raw 94.2 / fr 57.4).
2. **h0 autonomous revive [mechanism — proves (b)]**: engage wedge, keep
   1:1 growth; PCR head runs ahead while `frontier − current` stays
   negative. Assert a `play_at` arrives within ≤ 15 VT s and raw then
   advances. *Mutation m-b (head-ahead test disabled) fails this — the
   legacy frontier test is negative and cannot fire.*
3. **h1/h2 interactive escalate [mechanism — proves (a)]**: re-engage
   wedge; `_seek_ms(-60000)`. Assert the escalation `play_at` lands within
   `deadline + 2` VT s and `|cap_content_for_raw(raw) − target| ≤ 3`.
   *Mutation m-a (escalation disabled) fails h2 — (b)'s revive, if it
   fires at all, revives at the clock position, not the user's −60 s
   target.*
4. **h-starved negative [mechanism]**: engage wedge, STOP growth (provider
   stall; no data ahead). Assert **no** autonomous `play_at` for ≥ 12 VT s
   (no false fire), clock held (captions match the frozen frame), and a
   user seek still escalates and lands (data exists behind in the file).
5. **h-loop bound [mechanism]**: total `play_at` count across the scenario
   ≤ 3 (revive + escalations) — no reopen loop.
6. **h3 [data-limited]** post-revive painted cues re-settle
   (exact-window ≥ 85% within the recovery window).

### Mutation proofs (rule zero)

| mutation (temporary) | must fail |
|---|---|
| (c) disabled: no ratio-hold, no anti-lead clamp | g1 |
| (b) disabled: head-ahead test removed (not reverted to frontier) | h0 |
| (a) disabled: verify logs but never escalates | h2 (and h-starved's seek) |
| (b)+(a) rate-aware threshold reverted to flat 0.05 | new slow-mo unit check (0.125x, data ahead → no rescue for 20 VT s) |
| D1 landing forced to `edge − 5` regardless of L | scenario c's jump check (below) + new E2E formula |
| D2 gate re-added (`frontier >= 90`) | join-byte unit test at frontier 20 |

Revert each; full harness 31+/31+ green in `--quick` **twice**.

### Existing checks that MUST be updated (not new failures)

- **Scenario c** (`test_sync_adversarial.py:1070`): with D1, the jump at
  L≈20 must land at `true_head − max(5, L+3)` = `−23 s`, and
  `_cap_backlog_s` must seed to the landing gap (`|backlog − 23| ≤ 2` —
  the edge-integrity clause; a backlog still seeded at 5 fails it). The
  tolerance stays ±3 on the clock.
- **E2E driver** — see §6.

### Offline suites

All existing suites re-run green (`test_caption_overlay test_cuestore
test_vod_splitter test_profanity test_fixes test_always_chase test_dvr_e2e
test_subtitles test_sub_settings test_bundled_ccx test_overlay_focus
test_tab_resize test_temp_cleanup`). New unit home: `test_wedge_cluster.py`
(seek-verify deadline math, head-ahead computation incl. join-app fallback,
ratio-window logic, `_cc_join_byte` helper incl. the past-frontier clamp —
pure helpers where possible, offscreen Qt for the integration pieces).

---

## 6. E2E driver deltas per option (`sync_stage3_run.py`)

| check | change | option |
|---|---|---|
| `jump-live lands at the TRUE edge` | **rewritten**: expected = `edge_pre − max(5, L+3 if L_pre>8 else 5)`; assert raw **moved** from raw_pre AND reached expected within 8 s (closes the vacuous-pass hole) | D1 + (a) |
| `scrub back lands ~120 s behind` | keep clock formula; **add** raw-landing assertion: `\|cap_content_for_raw(raw) − want\| ≤ 4` within 8 s | (a) |
| NEW `no unrecovered raw freeze > 15 s while PCR data ahead` | driver-side monitor (raw changes + its own head probe via `view._cc_probe_head_pcr()` read-only) — independent of the mechanism under test | (b) |
| NEW `caption clock never leads raw > 1.5 s while playing (p100)` | sampler in `Health.tick` — the direct encoding of the 8.5-s-ahead night | (c) |
| `clock holds while paused` | unchanged — (c) must preserve it (clamp is playing-gated) | (c) guard |
| cold-join checks (`anchor ≤ 90 s`, `captions paint at cold join`) | unchanged; CCSTART `join_byte` may now be >0 at small frontier — no check depends on 0 | D2 |
| mid-show Off→On engage | NOT added live here (P5 owns the live exercise); D2's proof in Phase 2 is the unit test + harness non-regression | D2 |

Mechanism/data-limited labeling per WP0: the two NEW checks and the two
rewritten landing checks are **mechanism**; paint-ratio and silent-stop
stay as-is (silent-stop is data-limited by nature tonight).

---

## 7. Implementation sketch (Phase 2 scope guard)

`src/ui/player_view.py` only (plus the two test files + driver):

1. Constants §4; state: `_seek_verify` tuple-or-None, `_seek_esc` strikes/
   next-allowed, `_raw_win` deque, `_trickle_hold` flag; reset all in
   `play_media`/`_reset` paths alongside `_last_reopen`.
2. `_chase_seek`: arm verify (not-down branch); jump_live branch uses
   adaptive `back`; revive branch factors into `_chase_revive(target,
   vlc_t, why)` reused by (a)'s escalation and (b)'s rescue.
3. `_tick` chase branch: rate-aware movement refresh; head-ahead
   computation with legacy fallback; rescue condition swap; FF catch-up on
   `edge − current`; trickle window maintenance; `_vid_s` respects
   `_trickle_hold`.
4. `_caption_clock_s`: `trickle` branch (wall_adv=0 under hold); anti-lead
   clamp (playing-gated, div-aware, raw≥0-gated).
5. `_cap_seed_transport`: `jump_live` seeds the actual landing gap.
6. `_start_cc_when_buffer`: D2 — `_cc_join_byte` helper, gate removed.
7. `_tick`/`_sync_transport`: extend the rescue log row with
   `head_ahead=`; `XPORT` gains `esc=` when an escalation fires.

Explicitly NOT in this package: L lead-compensation / store-shift policy
(WP3), CCX vendoring (WP4b), any change to VOD/plain-live paths, any change
to `_CC_STALL_FREEZE_S`/stallsnap semantics (the ≥6 s branch remains for
real underruns).

---

## 8. Residual risks

- **Virtualizing new windows may surface flakiness** (WP0's known risk
  class): the trickle window runs on `_tick`'s 2.5 Hz, sampled by a 10 Hz
  caption clock — off-by-one-window transients must fold, not latch; the
  g1 p100 lead bound is the tripwire.
- **FakeVLC wedge fidelity**: `wedged` models "demuxer-blocked while
  playing"; if real VLC also ignores `play_at` in some states, the ladder
  caps attempts at 30 s spacing — livelock impossible, but recovery on
  that pathological variant falls to a channel change (documented, not
  solved here).
- **D1 + frontier+120 clamp**: an absurdly high L (EWMA run-away, capped at
  `_CC_LAG_MAX_S` 240) could push the adaptive target behind the buffer
  start — `max(0, …)` and `_safe_seek_target` already bound it.
