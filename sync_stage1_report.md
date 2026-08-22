# Live-caption sync — stage 1 measurement report

**Date:** 2026-08-20 · **Channel:** US: NFL NETWORK HD · **Session:** 11.5-min
scripted run (cold join 3 min → pause 60 s → resume 2 min → jump-live 3 min →
scrub ∓120 s → jump-begin 1 min → 2x/1x), plus a 2.5-min feed-position probe
run. Muted, minimized, background. No sync behavior changed — logging only.

**Artifacts**
- Instrumentation (env-gated, off by default): `src/ui/player_view.py`,
  `src/live_cc.py` (PCR probes), `src/logging_setup.py` (`mtp.sync` logger →
  `%APPDATA%\MichaelTVPlayer\sync_debug.log`, 8 MB rotating).
- Enable with `MTP_SYNC_LOG=1` before launch. Driver: `sync_stage1_run.py`;
  analyzer: `sync_stage1_analyze.py`. Raw log kept (2.6 MB).

## The four clocks on one buffer file (measured)

| Axis | Source | Behavior measured |
|---|---|---|
| **PCR head** | newest PCR in last 256 KB of buffer (content-exact edge) | advances 0.91–1.00 s per wall s, bursty with VLC flushes |
| **Frontier** `_frontier_s` | wall time credited between growth sightings | advances 0.95–1.00 s/s; **under-credits content by a constant 20–35 s** established in the first seconds (CDN cold burst: ~24–34 s of content arrives in ~3 s, credited as ~1 s). 15-s cap never fired. |
| **CCX cue-end axis** | newest cue end emitted by CCExtractor | advances 0.78–1.04 s/s **relative to the PCR head — nonstationary**: 0.78x during the full 13-min run (growing lag), ≈0.94–1.0x in the 2.5-min probe run |
| **VLC get_time** | display player | **1.00 s/s wall at 5-s granularity in continuous play**; freezes while paused; 2.0x at 2x speed; at 100-ms granularity it is a staircase (frozen 5–6 s, then +3–6 s jumps) |

Exonerated by direct test: CCX speed (45.7x realtime on a ready file,
full-timeline SRT coverage), pipe throughput (327 MB/s through the exact
256-KB-chunk/pipe topology), and the tail feed (run 2: `feed_behind` = 0–3 MB
steady after the cold burst drains). In-file timestamps are internally
consistent: video-PTS/PCR slope ratio 1.0000, drift +0.43±0.05 s constant;
ffprobe first-PTS audio-vs-video = **−21 ms** (container A/V alignment clean —
matches "A/V stays synced").

## The measured L(t) — CCX's true lag behind the write head

`L = (tail PCR − join PCR) − newest CCX cue end` (same raw-PTS family; the
tail-PCR probe validated — sub-20 ms, single PCR PID 100).

- **13-min run:** L grew **36 s → 129 s** (~+0.15 s/s overall; +0.21 s/s while
  playing, fell during the pause as the caption axis caught up while the
  network slowed). Not constant, not small — `_CC_LAG_S = 1.0` is wrong by
  1.5–2 orders of magnitude by end of session.
- **Probe run:** cold burst gives L≈22 initially, drains to **0.1–7 s** and
  holds (feed current). So L's steady-state value is **session-dependent**
  (provider caption timing is nonstationary); in the long run it drifted
  without bound.

## The one identity that explains everything

With `frErr = frontier − PCR head` (−33.5 ± 1.5 s, constant, set at cold
burst) the anchor's EWMA offset satisfies **`off ≈ L + frErr`** — verified
across all 13 minutes (e.g. final tick: off +90.7, L 126.1, frErr −34.6).
Captions are mapped as `mapped = cx + off`, so the display error versus the
video is exactly `off`: every second of L growth or frErr shift goes straight
into caption misplacement. The anchor pins the *newest* cue at `frontier − 1`
on a frontier axis that is 20–35 s of content behind the true write head.

## Symptom → axis-pair mapping

**(a) Cold join ~6 s behind, then stop.** At first fresh cue frErr ≈ −30 and
CCX is replaying burst backlog (L ≈ 30+). `off` starts +1.8 and the fresh-check
passes **everything** (6109/6109 cues "fresh" — the per-cue advance vs elapsed
guard cannot catch catch-up when individual roll-up windows are 0.03–0.4 s
wide; 185 cues/20 s all "fresh"). As off passes ≈6 captions sit 6 s late, then
the two errors race: L growing (13-min run: captions ever-later, +0.2 s/s) or
L draining faster than frErr (probe run: off flips negative to −14…−27,
captions run *early*). "Stop displaying": the EWMA moves `off` by whole
seconds between consecutive cues whose windows are sub-second — mapped windows
separate and the clock crosses gaps with nothing active (measured 19.3-s
display stop at the end of phase A).

**(b) Pause + resume: brief accuracy, then captions race ahead.** The caption
clock is **acquitted** — the decision trace shows clean `hold` while paused
(542 holds, clock frozen at 179.5 exactly like raw), clean snap/integrate
after (one accepted −2.19 s backward snap; zero `ahead>fr+30` and zero
`back<-10` rejections all session — the snap guard never fired). The convict
is the anchor: during the pause CCX keeps producing and `off` re-anchors
(−9.4 s during the 13-min run's pause; −6 in the probe run), then keeps
sliding through the post-pause catch-up transient (+13 over the first resume
minute in the probe run) — the mapped windows slide *under* the playing clock,
so the text on screen advances faster than the video ("accurate for a few
seconds" = pre-drift windows still active; "run ahead rapidly" = EWMA sliding
windows forward through the transient). Structural amplifier: after resume
playback is permanently 60 s behind the frontier (both advance 1 s/s; the gap
never closes), so the newest cue is anchored 60 s in the clock's future
(measured `lead` avg +59.9 s through RESUME).

**(c) Jump-to-live: captions vanish, return wrong.** The seek itself is
exact — commanded 361.07, raw 356.84 one second later (clamped −5 by
`_CHASE_SAFETY_S`), same for scrub (411.7→412.5) and jump-begin (0→0.81).
But it lands on the frontier axis, 33–35 s of content behind the true PCR
edge, and the cue store around the landing zone holds cues mapped with
minutes-old `off` values (their text is 30–100 s stale; L was 63–105 s in
this phase). Captions disappear until fresh cues arrive — and fresh cues map
at `fr−1`, i.e. their windows sit just ahead of the clock (JUMPLIVE `lead`
avg +2.3 s) while their text is L seconds old: caption text racing ahead of
the displayed content.

**(d) Scrub / jump-to-beginning worst of all.** By then off is at its session
maximum (+70…+96). Early-buffer cues were mapped when off was small, recent
ones when off was huge: **the same content minute is mapped at positions up to
~580 s apart depending on arrival time** (JUMPBEGIN `lead` avg +578 s).
Replaying from 0 shows the old-mapped windows (error = arrival-time off, a few
to 30 s late) then thins out; scrubbing lands in incoherent mapping territory.
The store has no consistent timeline — divergence growth has already
shredded it.

**Events that pump the divergence:** (1) the cold burst (sets frErr −20…−35 s
instantly, never corrected); (2) provider caption-axis rate ≠ PCR rate (grows
L at ~0.2 s/s in the long run — the dominant pump); (3) every pause (off
re-anchors while CCX catch-up transients flow); (4) every transport event
(plays back content whose cues were mapped under an older off).

## Frames-play-1:1 assumption (stage-2 dependency) — CONFIRMED

At 5-s tick granularity, raw get_time deltas equal wall-time deltas in every
continuous-play window (no >1.5-s deviations across START/RESUME/JUMPLIVE);
deviations appear only at PAUSE (0 vs 5 s, correct) and SPEED2X (2x, correct).
At 100-ms granularity get_time is a frozen/+3–6-s staircase — wall-time
integration between snaps already handles this, but a stage-2 snap-clamp
should note raw can also step *backward* up to 10 s silently (observed −2.19).

## Stage-2 validations delivered

1. **Tail-PCR probe works** (acceptance ✓): content-exact write-head edge,
   sub-20 ms, robust alignment; `PCRJOIN` reference pins CCX's axis origin.
2. **Lag sensor works** (acceptance ✓): L(t) measured per cue arrival and per
   5-s tick, same raw-PTS family as cue ends. Anchoring to
   `pcr_head − L_measured − cx` would have kept every caption of both runs
   within ~±2 s of correct (L's intra-second jitter), independent of the
   frontier's wall-crediting error and of provider CC-timing drift.
3. **Frontier should be PCR-based**: frErr (−20…−35 s) is pure measurement
   error of the wall-credited clock; the probe replaces it exactly.
4. **Open item:** the 13-min run's caption-end axis ran 0.78x the PCR head
   with CCX, pipe, and feed all exonerated — i.e. the caption data's own
   timestamps rode behind the video/PCR axis in that session (provider-side,
   nonstationary; absent in the probe run). The `fsize/feed/feed_behind` tick
   fields (added mid-study) should stay instrumented to confirm on stage-2
   runs.

## Misc
- 15-s DVR credit cap: never engaged (largest flush gap ≪ 15 s) — not a
  factor in these symptoms today.
- VOD unaffected by construction (relay cue times on the file's own cluster
  timecodes; same-axis, no anchoring).
- VLC decoder warnings during the run (D3D11VA alloc retries, PSI
  discontinuity notices) — cosmetic; playback never stalled.
