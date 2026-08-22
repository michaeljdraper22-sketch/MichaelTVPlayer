# P1 report — surgical bug batch (three parallel subagents)

Package WP1 from `subtitle_attack_plan.md`. Three subagents ran concurrently
in one working tree on separate files (WP0 merged in the tree, uncommitted,
as expected). Every fix has a fail-before/pass-after regression (rule zero).
Details: `p1a_report.md`, `p1b_report.md`, `p1c_report.md`.

## The six confirmed surgical bugs — all fixed

| # | Bug | Fix (owner) |
|---|---|---|
| 1 | Overlay delay sign inverted (`text_at(t + delay)`) | 1a |
| 2 | CueStore eviction orphans `_seen` → rewinds blank | 1b |
| 3 | MKV tail region (last ~2.5 MB) never tapped | 1c |
| 4 | Stale queued relay cues/`failed` land in the next movie | 1a |
| 5 | ASS bare-fixed-fields fallback dead code | 1c |
| 6 | `lang_matches` substring false positive ("Non-English") | 1c |

Plus the brief's extras: 1a's `_caption_tick` once-per-distinct-error
logging; 1c's MKV TimecodeScale + MP4 elst media_time timing, `_acquire`
`_alive` check, `server_close()`; 1c probed the async-startup-failure path
(VLC recovers to a clean terminal state — documented, no handback needed).

## 1a — player_view (delay sign, stale-relay race, tick logging)

- **Delay sign**: `_caption_tick` now queries `text_at(t - delay_ms/1000)` —
  positive = later, agreeing with `config.py`, the +/- tooltip, and VLC's
  `video_set_spu_delay` path. The bug-codifying overlay test was replaced by
  a directional probe (+2 s holds the cue past its true window); harness
  scenario f's check flipped from WP0's direction-neutral probe to
  DIRECTIONAL (+1.5 s must paint the cue active 1.5 s EARLIER). Fail-before:
  overlay FAIL + scenario f 6/7; after: all ok, f 7/7.
- **Stale-relay race**: new `_cap_relay_live()` guard (sender identity, with
  `_cap_relay_gen`/`_session` generation fallback) on `_on_vod_cue` and
  `_cap_relay_failed`; generation set at each attach in `_effective_url`.
  Probe finding: `_stop_profanity` never disconnected `failed` at all — now
  it does. Regression `[5c]` models the emit-racing-disconnect window from a
  worker thread: store stays clean, no phantom mute window, no `_cap_fail`
  latch. 3 FAILs before, all ok after.
- **`_caption_tick` bare `except: pass`**: errors now log once per distinct
  error (set bounded at 64, reset per media), still non-fatal; painting
  resumes after. FAIL before (0 records) → ok after.

## 1b — CueStore (eviction/dedupe coherence, >60 s cues)

- **Eviction orphans `_seen`**: eviction now goes through `_evict_one()`,
  dropping the oldest-ARRIVED cue (new `_order` deque) and its `_seen` key in
  the same step, so `_seen` always equals what is stored. Judgment call (flagged
  in `p1b_report.md`): a `_seen` prune alone cannot pass the acceptance — at
  cap, start-order eviction makes a re-received rewound cue evict ITSELF, so
  eviction became arrival-order; forward flow (arrival == start order) is
  bit-identical to the old front-truncate. 4 rewind FAILs before → ok after;
  bounded-store/dedupe/shift/clear controls green throughout.
- **`text_at` abandoned active >60 s cues**: the fixed `start < t - 60` break
  is now `start < t - grace - _max_span` (`_max_span` bounds every stored
  window, O(1) maintenance) — provably safe for any still-active cue, and
  TIGHTER than 60 s in the common all-short-cues case. Overlap policy
  documented + pinned. 3 FAILs before (0→90 s cue blanked from t≈60 under a
  dead newer cue) → ok after.

## 1c — VOD splitter / subtitle extraction

- **MKV tail hole**: new one-shot tail-harvest thread parses the prefetched
  `_tail` when track metadata exists (re-runs on language switch), anchored
  on a validated cluster header via new `_snap_cluster` (a mid-element tail
  boundary can otherwise trip the parser's stream-skip and swallow the whole
  region). `_tap_read` rewritten as a region-stitching reader with hole
  refusal — the old slice math served WRONG bytes for mid-hole reads (latent
  MP4 bug). Fixture: 4.7 MB MKV, late cues inside the 2.5 MB tail; full-file
  play AND a direct seek-into-tail GET tap all late cues at correct times,
  one provider connection.
- **ASS bare fixed-fields**: two patterns now — the real 10-field bare
  Dialogue line (empty Name) AND the 9-field matroska shape the old regex
  matched accidentally. Both ffmpeg-shaped fixtures reduce to clean text.
- **`lang_matches`**: ≥4-char hints match whitespace words (edge punctuation
  stripped): "english" no longer matches "Non-English Comments"; short codes
  keep alias behavior.
- **Timing**: MKV TimecodeScale parsed from Info and honored everywhere
  (incl. mid-stream parsers via new ctor arg + relay snapshot); MP4 elst
  media_time honored (skipped span dropped, rest shifted). Fixtures are
  in-place binary patches of ffmpeg outputs (ffmpeg can't emit either
  natively). Non-default scale shifts cues correctly through the live relay
  tap; default/zero-value files bit-identical.
- **Optional**: `_acquire` refuses after `stop()` (fail-before proven);
  `server_close()` added (hygiene, no fail-before possible); async-startup
  probe → VLC recovers by itself (`Opening → Ended` in 0.5 s after relay
  `failed`), documented and skipped per brief.

## Orchestrator gate (merged tree)

**Offline suites** (`.venv\Scripts\python.exe -X utf8 <test>.py`):

| suite | result |
|---|---|
| test_caption_overlay | 135 passed, 0 failed |
| test_cuestore (NEW) | 28 passed, 0 failed |
| test_vod_splitter | all 108 checks passed |
| test_profanity | all 54 checks passed |
| test_fixes | 51 passed, 0 failed |
| test_always_chase | 44 passed, 0 failed |
| test_dvr_e2e | 12 passed, 0 failed (see flake note) |
| test_subtitles | all 22 checks passed |
| test_sub_settings | all 37 checks passed |
| test_bundled_ccx | all 9 checks passed |
| test_overlay_focus | all 12 checks passed |
| test_tab_resize | 16 passed, 0 failed |
| test_temp_cleanup | all 6 checks passed |

**Adversarial harness**: `test_sync_adversarial.py --quick` → **31/31
passed** (mechanism 27/27, data-limited 4/4), including the new directional
check `f: +1.5 s delay paints the cue active 1.5 s EARLIER` — the end-to-end
re-confirmation required after the production sign flip.

**Ownership check**: modified files are exactly the WP0 baseline set
(`src/ui/player_view.py`, `sync_stage3_run.py`, `test_sync_adversarial.py`)
plus the three briefs' owned files (`src/ui/caption_overlay.py`,
`src/vod_splitter.py`, `src/mkv_subs.py`, `src/mp4_subs.py`,
`test_caption_overlay.py`, `test_vod_splitter.py`, new `test_cuestore.py`).
`sync_stage3_run.py` is byte-identical to the WP0 baseline; the deltas in
`player_view.py` (161 diff lines) and `test_sync_adversarial.py` (26 diff
lines) are confined to 1a's areas. Reports: `p1a/b/c_report.md`.

**test_dvr_e2e flake note**: this suite plays a REAL provider live channel
(muted/minimized per the standing rule). 7 runs total: 6 green (12/12),
1 run failed 2 checks — under concurrent harness CPU load; failing check
names not captured. Attribution: environmental, not P1 — no P1-changed code
path executes in this suite (live chase, captions never engaged, no VOD
relay), the suite's tolerances exist precisely for provider delivery
weather, and the final dedicated run (machine idle) was green. If it recurs,
capture the FAILED lines before investigating.

## Not done / deferred

- Nothing from the briefs. 1c's async-startup handback intentionally not
  added (VLC recovers alone — evidence in `p1c_report.md`).
- Live re-verification of the tail/delay/stale-cue fixes on real VOD assets
  belongs to WP5 (verification night).
