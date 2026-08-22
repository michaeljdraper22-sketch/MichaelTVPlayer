# Live-caption sync — stage 3 report (hardening + verification)

**Date:** 2026-08-21 · **Channel:** US: NFL NETWORK HD · **Code:**
`src/ui/player_view.py`, `src/live_cc.py`, `src/profanity.py`,
`src/ui/caption_overlay.py`, `src/logging_setup.py`. Artifacts:
`test_sync_adversarial.py` (offline harness), `sync_stage3_run.py`
(E2E driver), `sync_stage3_analyze.py` (log analyzer),
`sync_stage3_retune.py` (constant retune sim), `sync_stage3_harness_full*.out`
(raw harness output). All live runs muted, minimized, never focused.

## 1. Offline adversarial regression harness (spec item 1)

`test_sync_adversarial.py` runs the REAL caption pipeline (installed
CCExtractor tailing a DVR buffer grown from the real NFL Network
recording, looped with PCR/PTS restamping so the PCR axis stays
monotonic for minutes) against a MOCK display player on a virtual
clock, replaying the exact stage-1/2 failure sequences. Headless:
offscreen Qt, no window, no audio, no focus, `--quick` (0.35x) and
full-duration modes, `--only:a,b,...` for iteration.

Six scenarios with truth-gated assertions (display error p95 ≤ 1.5 s
against the mock clock's true content, silent-stop windows, scrub
coherence, text locality):

| Scenario | Result (full matrix) |
|---|---|
| a cold join + growing divergence + wedged anchor | 8/8 — drift p95 0.29 s; wedge (anchor+store displaced 5 s, cues frozen) recovered by the watchdog, rebases 0→1, stretch 0 s |
| b pause 60 s → resume +25 s renumber step ×3 | 5/5 — div accumulates +74.7 (~75), p95 0.29, maxstop 1.8 s |
| c jump-to-live with L swelling 1→20 s | 4/4 — landed 5.0 s behind the true edge (edge 72.0, head 72.1), p95 0.30, blanks data-limited ≤ 2.4 s |
| d provider bursts (30 s at once after 30 s stalls; frontier under-credits 59 s) | 4/4 — p95 0.30, maxstop 6.4 s, 3 watchdog fires (+9.0/+28.1/+22.2 s one-shot corrections) |
| e CCX caption axis at 2x wall | 4/4 — p95 0.29, text locality 81% |
| f scrub back 2 min, forced rebase coherence, live delay_ms | 7/7 — scrub exact, scrubbed region p95 0.29 text 100%, forced ±6 s rebase round-trips, delay_ms repaints at t+1.5 s |

**Final: 31/31 checks (full duration) and 31/31 (quick).** Raw logs:
`sync_stage3_harness_full2.out` (final) vs `sync_stage3_harness_full.out`
(the stage-3 starting point, 29/31 — d at 8.4 s stop / f blank 30 s).

### The two failures the harness caught (and their fixes)

- **f: post-rebase 30-s blank.** A forced anchor displacement of −6 s
  after a scrub left the store 6 s off the clock; the anchor-snap
  existed but converged too slowly to re-cover the viewer (old α=0.35
  per-cue EWMA + per-cue decisions). Fixed by the deferred anchor +
  `_CC_ANCHOR_ALPHA = 0.50` (below) — the first fresh cue after the
  displacement now snaps the anchor back within one arrival flush
  (~4 s worst case, observed 0.7-s display stop).
- **d: 8.4-s silent stop.** Two-fire watchdog sequences: the fire
  target used the L EWMA, which is mid-transient exactly when the
  watchdog fires (after a burst), so correction #1 landed short
  (+2.7 s) and correction #2 was gated by the 8-s cooldown. Fixed by
  deriving the fire target from the INSTANTANEOUS probe lag
  (`head_rel − last cue end`) — burst-immune, current, and ~0 during
  genuine speech pauses — and widening the arrival window
  `_CC_WATCH_CUE_S` 10→20 s so fires are not blocked while CCX chews
  a landed burst (30 s of 4K content ≈ 20–30 s of parse silence).
  Result: one-shot corrections (+9…+28 s), maxstop 6.4 s.

## 2. Constant retune (spec item 2)

`sync_stage3_retune.py` replays the stage-2 definitive matrix's
per-cue anchor targets (recovered from the `innov`/`off` columns of
the mtp.sync log) against alternative α values. The stage-2 build ran
α=0.35 per-cue with a persistent signed bias (captions late) in every
phase; α=0.50 halves it, and stage 3's DEFERRED anchor (one clean
newest-cue sample per arrival flush instead of per-cue) removed the
queue-skew jitter that low α protected against. Landed:
`_CC_ANCHOR_ALPHA = 0.50` per flush decision.

Live verification (tonight's E2E, a harsher L regime than stage 2's):
signed anchor innovation ≈ 0 in every phase (START +0.01, PAUSE +0.58,
RESUME −0.08, JUMPLIVE +0.10, SCRUBBACK +0.37) — steady-state lead is
centered on 0. No residual lead constant is warranted (a −0.15 s lead
was simulated and made bias worse in 8/9 phases).

## 3. VOD profanity mute-lead trim (spec item 3)

`_VOD_MUTE_LEAD_S = 0.4`: the VOD filter path (`_on_vod_cue`) opens
each mute window 0.4 s EARLY — movies were measured to miss mutes by
~0.5 s (the word-position estimate inside a cue runs behind the
dialogue). Live behavior untouched (`lead_s=0.0` on the chase path;
the live lead concept was removed in stage 2). Tests: differential
lead check in `test_profanity.py` (54/54) and a window-position check
in `test_caption_overlay.py` (128/128).

## 4. Final end-to-end verification (spec item 4)

`sync_stage3_run.py live` — 10-min matrix on US: NFL NETWORK HD,
muted, minimized, never focused, one connection. Mechanism checks all
pass: chase engages at cold join; overlay + anchor engage; live
`delay_ms` repaints in place; clock holds through a 60-s pause (±1.5 s);
largest silent-stop with cue data present 5.2 s (contract ~5+3);
scrub back/forward land on target. Captions stayed in sync through
every phase (signed innovation ~0 above; PAINT rows continuous).

Tonight's provider regime was the harsh one (CCX lag L grew
26→76 s during the run — the stage-2 "late night" regime), which made
two DATA-side checks miss: first paint took just over the 60-s wait
(cold-join caption data arrives at L≈29 s and CCX chews 4K at ~1x),
and overall paint coverage was 57.7% vs the 60% gate — text that has
not been emitted yet cannot be painted; the harness's scenario c
covers exactly this regime deterministically (blank ≤ L+margin ✓).
Jump-to-live landed 19 s short of the true-edge target: VLC's demuxer
cache ended short of the file tail while the CDN stalled (playback
continued, captions synced on the content actually playing) — the
known ragged-edge behavior from stage 2, not a sync defect.

`sync_stage3_run.py vod` — one movie + one series episode through the
local relay with the filter on (exercising the 0.4-s trim), text track
picked through the real menu path: overlay engages via the relay,
relay produces cues, painted cue covers the clock, `delay_ms` shifts
captions live. **Provider-blocked tonight:** every VOD category
funnels through one CDN host (cf.534842.xyz) which is refusing this
client (HTTP 551 on direct probe; live-TV hosts work — the live matrix
above played fine). The driver now skips items that never open and
tries the next (3 per kind), and is ready to rerun for the spot-check
when the CDN recovers. The VOD path itself is verified tonight by
`test_vod_splitter.py` (80/80: real local relay, MKV/SRT + MP4/subrip
parsing, profanity windows) and `test_vod_series.py` (real playback),
plus two unit checks of the mute-lead trim.

## 5. Cleanup (spec item 5)

- `mtp.sync` diagnostics: handler attaches ONLY when `MTP_SYNC_LOG` is
  set (`logging_setup.py`); every call site is `_SYNC_ON`-guarded —
  normal runs log nothing and pay one boolean check.
- Stage-1/2 driver scaffolding removed; stage-3 keeps the harness +
  E2E driver + analyzer + retune sim as regression tools.

## Found & fixed along the way: bundled CCX cannot stream

The vendored CCExtractor 0.88 reads stdin TO EOF before emitting a
single SRT byte (measured: 30 MB piped, 0 B out until close) — it
cannot tail a growing DVR buffer, so the zero-install fallback would
silently produce no live captions (`failed` never fires; CCX just
idles). `CCSource.start` now refuses the bundled build with
`failed("bundled CCExtractor 0.88 cannot stream")` so the app falls
back to VLC caption rendering (checked in `test_caption_overlay.py`).
The harness pins the INSTALLED build (streams fine, ~1.1x realtime on
this 4K recording — the harness honestly paces near real time, ~35 min
full matrix; `--quick` ≈ 12 min).

## Suites

| Suite | Result |
|---|---|
| test_sync_adversarial.py (new) | 31/31 full, 31/31 quick |
| test_caption_overlay.py | 128/128 |
| test_always_chase.py | 44/44 |
| test_vod_splitter.py | 80/80 |
| test_dvr_e2e.py | 12/12 |
| test_profanity.py | 54/54 |
| test_fixes.py | 51/51 |
| test_bundled_ccx.py | 9/9 |
| test_sub_settings.py | 37/37 |
| test_subtitles.py | 22/22 |
| test_overlay_focus.py | 12/12 |
| test_tab_resize.py | 16/16 |
| test_temp_cleanup.py | 6/6 |

## Caveats (measured, not bugs)

- Harness duration is bound by the installed CCX's ~1.1x parse speed
  (the alternative — the vendored build — cannot stream at all).
- In L > ~30 s provider regimes, true-edge landings show late/blank
  captions until the pipeline catches up (data-limited; stage-2's
  standing offer stands: sit `max(5, L+3)` behind the head at
  jump-to-live if you'd trade edge-ness for caption timeliness).
- The E2E paint-ratio gate (≥60%) is regime-dependent; tonight's
  L 26→76 s session sat at 57.7% with every mechanism check green.
