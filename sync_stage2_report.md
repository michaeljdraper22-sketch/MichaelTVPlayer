# Live-caption sync — stage 2 report (single-axis refactor)

**Date:** 2026-08-21 · **Channel:** US: NFL NETWORK HD · **Code:**
`src/ui/player_view.py` (+ `CueStore.shift` in `caption_overlay.py`,
`ProfanityEngine.shift_windows` in `profanity.py`). Test artifacts:
`sync_stage2_run.py` (matrix driver), `sync_stage2_wedge.py` (rescue
verification), `sync_stage2_analyze.py` (analyzer), `sync_stage2_*.log`
(raw logs). All live runs muted, minimized, never focused.

## What was built (spec items 1–7)

1. **Dead-reckoned caption clock** (`_caption_clock_s`): seeded at every
   transport event (the `_chase_seek`/`_reopen_display` set_time target
   IS the clock, by construction — `_cap_seed_transport`), advanced
   `wall × rate` while playing, held while paused/stalled. The old
   snap-to-raw + `min(raw, frontier)` + reject-backward logic is gone.
   Raw `get_time` now contributes only **outlier-rejected delta nudges**:
   when raw advanced ≈ rate × wall within ±2 s over a reading-change
   window, the residual folds in; bigger jumps are treated as PTS
   renumbering (clock keeps, divergence `raw − clock` recorded for seek
   conversion); a reading frozen >6 s while "playing" is a stall (clock
   holds; a catch-up jump matching the frozen span snaps back onto it).
   Loose sanity bound only: `clock ∈ [0, frontier + 120]`.
2. **Measured CCX lag** (was hardcoded 1 s): every cue arrival probes
   the tail PCR (throttled 4/s, ~20 ms) and updates
   `L = head_PCR − join_PCR − cue_end`, EWMA-smoothed (α 0.18), samples
   clamped [0, 240] s. A 2-s timer keeps probing between cues. The
   newest FRESH cue pins at `edge − L`.
3. **Dead-reckoned live-edge backlog**: `edge = clock + backlog`; seeded
   at engage (≈ chase_delay behind the frontier-estimated edge),
   reseeded to `_CHASE_SAFETY_S` at jump-to-live, preserved across
   scrubs. Grows 1:1 wall while paused/stalled, shrinks by
   (rate−1)·dt while playing. The edge is kept honest against the PCR
   head (α 0.5 pull, hard snap beyond 2.5 s); **CDN bursts move the head
   and L together, so an edge snap advances the lag EWMA by the same
   amount — the pin `edge − L` is burst-immune.** `_frontier_s()` is
   demoted to seek-safety/data-existence only.
4. **Snap-and-rebase** (`_cc_rebase`): a fresh cue's implied correction
   > 4 s sets the offset immediately (no EWMA crawl) AND shifts every
   stored CueStore window and filter mute window by the same delta, so a
   scrub back shows coherently placed captions.
5. **Caption-stopped watchdog**: cues arriving within 10 s but no window
   intersecting the clock for > 5 s (and the derived correction ≥ 1 s —
   smaller ones are normal roll-up speech gaps) rebases the anchor from
   the newest cue (`edge − L − end`) and logs at WARNING. 8-s cooldown.
6. **Transport conversion**: content-axis targets convert to VLC
   set_time numbers via the measured divergence (`raw − clock`, EWMA'd
   from trusted deltas, snapped at renumbering). `_jump_live` targets
   the TRUE edge (`edge − 5`, bypassing the frontier clamp; the buffer
   really holds the cold-burst content the frontier never credited).
7. **VOD untouched**: the VOD branch of `_caption_clock_s` is the
   unchanged raw passthrough; every new mechanism is gated on
   `mode == "chase" and dvr`; relay cues never touch the live anchor;
   `test_vod_splitter.py` passes 80/80 and the VOD sections of the
   caption tests are unchanged.

**Extra hardening found by the live matrix:** landing at the true edge
means VLC rides the ragged end of the growing buffer; when the provider
throttles, VLC's demuxer hits EOF and wedges (state can be Ended or
demuxer-blocked-"playing"; `set_time` no-ops — the classic
"ran into the end and stopped"). A merged **stuck-player rescue** in
`_tick` now covers both signatures (not-playing × 3 ticks, or raw frozen
> 8 s jitter-proofed with data piled 10+ s past us) and reopens the
buffer at the caption clock's position. Also `_vid_s`/`_safe_seek_target`
allow the frontier+60 true-edge landing zone (capped, so a bad edge
estimate can't stall-loop).

## Before → after (log-verified)

"Before" = stage-1 measurements (same channel, 2026-08-20). "After" =
stage-2 matrices (2026-08-21, three full cold matrices + one Off→On +
one rescue-verification run; provider caption-lag regime varied from
L≈6–25 s early in the night to L≈45–139 s late — far harsher than
stage-1's sessions).

| Symptom (stage 1) | Before | After |
|---|---|---|
| Pause/resume captions race ahead | `off` re-anchors during pause (+9.4 s), post-resume lead avg +59.9 s | clock holds exactly (CLOCK hold rows; pause check ok); anchor pinned at true positions; lead = backlog − L (geometry), no EWMA slide under the clock |
| Jump-to-live lands 33–35 s behind the true edge | commanded frontier−5, raw lands there — 33 s of content short of the head | lands at PCR-calibrated edge − 5: raw 505.4 vs frontier 479.4 (26 s PAST the frontier = the true edge); 3-min post-jump playback keeps advancing (rescue reopens any wedge in ~1–2 s) |
| Seeks | exact (unchanged) — but slider/FF used raw-axis numbers | every seek log-verified exact (target vs raw 1 s later ≤ ~1 s), now on one axis incl. renumbering conversion; scrub ±120 s lands within ±1 s |
| Captions stop displaying (19.3-s stop; 6109/6109 "fresh") | store shredded by divergent offsets; same content minute mapped across ~580 s | one coherent timeline (rebases shift the whole store); stops only when the data itself is late (see caveats) |
| Anchor display error = off (36→129 s over 13 min) | frontier-pinned, drifts with L and frErr | pin at `edge − L` with measured L; **innovation p50 0.1–0.4 s, p95 1.4–2.0 s** across every scenario incl. immediately after transport events |

## Acceptance matrix (definitive run `sync_stage2_v3`, all fixes in)

Driver checks **8/8** (chase engages; overlay at cold join; clock holds
while paused; jump-live lands at the TRUE edge; clock still advancing
3 min after jump-live; scrub −120 s lands; jump-begin near 0; 2x
advances ~2x wall). `test_dvr_e2e.py` right after: **12/12**.

Anchor innovation on fresh cues (|target − current off|; p50 = the
anchor's cue-to-cue convergence):

| Phase | n | p50 | p95 | max |
|---|---|---|---|---|
| START (cold join, 5 min) | 2291 | 0.17 | 1.58 | 7.9 |
| PAUSE | 322 | 0.24 | 2.03 | 6.7 |
| RESUME | 783 | 0.14 | **1.34** | 5.2 |
| JUMPLIVE (3 min) | 1373 | 0.14 | **1.41** | 7.5 |
| SCRUBBACK | 74 | 0.25 | 4.25 | 23.0 |
| SCRUBFWD | 182 | 0.17 | **0.95** | 3.8 |
| JUMPBEGIN (1 min) | 347 | 0.23 | 1.91 | 6.9 |
| SPEED2X (30 s) | 251 | 0.16 | 1.56 | 5.3 |
| SPEED1X | 139 | 0.14 | **1.42** | 4.9 |

Read: the anchor is converged (p50 ≈ 0.15 s) through every scenario
including immediately after transport events; the p95 tail (1.3–2.0 in
transient phases) is per-cue queue-skew in the L sample, not drift —
each cue's pin carries the skew of "probe now vs cue emitted up to ~1 s
ago." SCRUBBACK's single 23-s outlier is one cue arriving exactly on an
edge snap (burst landing); the next cues converge immediately.

Lead geometry (newest-cue mapped_end − clock = backlog − L, by
construction): JUMPLIVE avg **+0.6 s** (viewer at edge−5, L≈5.7: the
newest caption is AT the playhead — "blank ~L s then on-time" realized;
PAINT rows show continuous painting from t≈508 right after the jump);
PAUSE/RESUME grow exactly with the paused seconds; JUMPBEGIN **+701 s**
(the whole buffer's timeline is coherent after rebases — a scrub from 0
crosses correctly-placed captions, vs stage 1's same-minute-mapped-580 s
apart).

Caption display stops: 23 stop-ticks in 13.5 min, all during SCRUBFWD
(12–17 s, CCX hiccup) and JUMPBEGIN (~58 s — the buffer's first minute
genuinely has no cues yet; CCX hadn't caught up when that content was
live). No mid-session stop ever exceeded those. The caption-stopped
watchdog fired only on real gaps (derived shift < 1 s → correctly
suppressed as speech pauses); the stuck-player RESCUE reopened 4 wedges
across the night within ~1–2 s each (player.log "chase rescue" rows).

Off→On mid-session (separate `offon` run): with the profanity filter
enabled in config the CC reader runs from t=0 even with captions off,
so turning captions On mid-show is instant and the anchor was already
warm (anchored ≤ 60 s check ok; innovation p95 1.40 in that phase).
The mid-buffer CCX JOIN path (join > 0) was therefore not exercised
live on these runs (the reader joined at byte 0); its join-position
refinement (`_cc_refine_join_app`, byte-rate derived from the same PCR
probes) is covered by design review only.

## Suites

- `test_caption_overlay.py` **123/123** (anchor section updated to the
  new contract: snap-and-rebase beyond 4 s + EWMA inside; clock
  delta-fold and renumbering-divergence checks added)
- `test_always_chase.py` **44/44**
- `test_vod_splitter.py` **80/80**
- `test_dvr_e2e.py` **12/12** (live-edge check updated to the
  PCR-calibrated edge per the stage-2 design)

## Caveats / provider-limited behavior (measured, not bugs)

- **L is nonstationary and sometimes huge.** Late on 2026-08-21 the
  provider's caption axis ran L≈45–139 s and CCX's feed fell ~97 MB
  behind on this 4K HEVC stream (CCX parses ~1× realtime live on it).
  At a true-edge landing with L > ~10 s, caption text for the newest
  ~L seconds does not exist yet: jump-to-live shows late/blank captions
  until the pipeline catches up — data-limited, not a sync error. The
  spec's "~L s of blank then on-time" holds exactly when CCX keeps up
  (feed_behind ≈ 0); when it can't, nothing can display text that was
  never emitted. (If you'd rather jump-to-live sit `max(5, L+3)` behind
  the head so captions are always timely, that's a one-line change in
  `_jump_live` — say the word.)
- Innovation p95 sits at 1.4–2.0 s in the worst phases (p50 ≈ 0.15 s):
  the residual is per-cue queue-skew in the L sample (σ ≈ 1 s), not
  anchor drift — the anchor itself is converged cue-to-cue.
- The cold-join first-minute blank tracks how fast CCX chews the cold
  burst (feed_behind): ~10–15 s in the good regime, ~50 s in the L≈50
  regime.
