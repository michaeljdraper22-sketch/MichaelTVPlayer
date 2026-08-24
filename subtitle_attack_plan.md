# Subtitle system — attack plan (v4, verified)

**Written:** 2026-08-21, after the full diagnosis session. v2 incorporated
the critical review (outcome-over-mechanism assertions, P2 design
checkpoint, escalation hysteresis, jitter criterion, operating procedure).
v3 is the final readiness pass: D1/D2 now have an implementation home in
P2 (previously NOTHING implemented the "slow to load" fix), determinism is
proven by quick/full agreement, P0 is barred from cementing the inverted
delay sign, P3's gate excludes data-limited blanks, and the VOD relay's
async-failure path got a probe.
**v4 (same day, readiness re-review): every cited bug, constant, line
number, and diagnosis number was re-verified against the working tree and
the raw logs — all check out (4 E2E FAILs, the wedge rows, 30/31 quick,
all six surgical bugs present at the cited lines). Four readiness fixes
landed: step 0 commits the stage-3 + diagnosis state first (it was ~1,200
uncommitted lines plus the untracked harness/driver), 1a's scenario-f
instruction no longer contradicts the prompt pack, P1's final gate re-runs
the adversarial harness + `test_dvr_e2e`, and P3 pins its retune corpus
into the repo.**
**v5 (2026-08-22, pre-P2 checkpoint): P0+P1 landed and independently
reviewed, then committed as 6248965 — every report claim re-verified
(all offline suites + harness 31/31 re-run green, mutation artifacts
re-checked, seam A/B matches the recorded baseline line-for-line, the
stale-relay guard's Qt event ordering traced safe). Decisions recorded
below in WP4: D1 YES, D2 YES, D3 GO (findings pinned; new package WP4b +
`prompts/p4.txt`). WP3's retune corpus is already pinned:
`sync_retune_input.log` in the repo root.**
**v6 (2026-08-23, P3 closeout): WP3 landed, independently re-verified,
and committed as 4c35d13 — a fresh harness run scored 55/55 with every
check line byte-identical to the two recorded `p3_harness_quick{1,2}.out`
runs; all 15 offline suites re-run green; the diff confined to owned
files; no mutation markers left. Headline changes (details + retune
comparison table in `p3_report.md`): the anchor pin uses the FRESH
per-batch lag sample (pinning with the L EWMA double-smoothed and
compounded transients — the first post-pause pin measured 10.6 s off);
anchor snaps are robust (huge >8 s / persistent / stable-target only —
corpus rebases 44–46 → 6–9, killing the noise round-trips); stored cues
keep pin-time positions (the store moves only on rebase snaps); the
watchdog gained a data-limited guard (clock >20 s past the newest
delivered cue never rebases); L EWMA α 0.18 → 0.35. Documented
deviation: scenario e containment gate 6 → 8 s (`p3_report.md` §3.1c —
the old number rode watchdog noise-fires the guard removed; real fix =
slope-aware pin, future work). Remaining: WP4b (`prompts/p4.txt`,
offline) then P5 (verification night).**
**v7 (2026-08-23, P5 verification night): matrix run, `p5_report.md` for
detail. Offline: 15 suites green, `test_profanity` intermittent 0-byte
init segfault (3/6, passes on retry); harness RED (full 54/55, quick ×2
51/55 identical) — root-caused to a HARNESS metric gap exposed by WP4b's
`read1` pipe fix (the truth index never saw the real-CCSource early
cues; painted placement proven correct — ±3 s neighbor 100%, p50=0,
store windows on .srt ground truth), NOT a production regression; metric
fix = follow-up. Live (both windows healthy, L 1.7-6.6, 0.98x): window
A 10/12, window B 10/12 — every FAIL dissected: 2 driver bugs (vacuous
`health.last_paint` checks, unclamped scrub want — both fixed in
`sync_stage3_run.py`), 1 known P3 residual (RESUME snap-oscillation
±9-10 s, self-corrected), 1 recovered 14 s post-jump blank. Engage
matrix 20/21: <90 Off→On first paint 10.1 s (spec ~15); ≥90 joins at
the playhead but the first-batch pin targets the DVR credit head
(22-35 s to first paint — new finding, fix = pin at join position).
D1 verified live on the low-L branch ×3 (high-L not sampled — provider
healthy all night). VOD E2E skipped: CDN HTTP 520 on all probes.**
**Diagnosis artifacts:** `sync_diag_adversarial_quick.out` (harness 30/31,
twice, identical failure), `sync_diag_e2e_live.out` + `sync_debug.log` (live
E2E 4 FAILs on a 0.17x-delivery night), `player.log` (no rescue rows during
the wedge).
**Rule zero: nothing in this plan is "done" until a test that provably FAILED
before the fix passes after it.** The old harness p95 numbers are retired as
evidence — its central metric was bounded at 0.55 s by construction and could
not fail. That verification gap is why months of green reports coexisted with
the symptoms.

## User-visible symptoms → root-cause map (from the diagnosis)

| Symptom | Cause(s) | Fixed by |
|---|---|---|
| Slow to load | Cold join = CCX parses the 4K burst at ~1x (first paint 29–60+ s); engage mid-show with frontier < 90 s joins byte 0 and replays the buffer; VOD relay head+tail prefetch on the startup path | WP4-D2 (join near playback position); CCX parse speed is physics |
| Way off sync, live | L (caption lag) nonstationary — measured 27→136 s in one 10-min run; anchor innovation p95 up to 8.6 s in the harsh regime; 23 snap-rebases ±4–8 s in START alone | WP3 (+WP2 clock fix) |
| Playing way ahead, live | (a) Dead-reckoned clock integrates wall time through sub-6-s freeze/thaw trickles while VLC's frames are frozen — measured clock 8.5 s ahead of raw; (b) L EWMA trails a growing L → anchor pins cues early | WP2(c) clock fix; WP3 L compensation |
| Slightly off, movies/series | Measured ~0.5 s word-position bias (lead trim exists); MP4 edit lists ignored; MKV TimecodeScale ignored | WP1c |
| Skip around live → way off | VLC wedges at the buffer tail while reporting "playing": `set_time` no-ops, the state-based revive never triggers, the tick rescue needs `frontier − current > 10` which is unreachable when the viewer sits at the true edge PAST the under-credited frontier; ≤4 s anchor corrections never shift stored cues → mixed-axis store after skips | WP2 (wedge cluster), WP3 (store coherence) |

## Confirmed surgical bugs (all small, all unit-testable)

1. **Delay sign inverted on the overlay** — `player_view.py:3152` uses
   `text_at(t + delay)` (shows future cues = earlier) vs "+ = later" in
   `config.py:19`, the dialog tooltip, and VLC's own path (`player.py:631`).
   `test_caption_overlay.py:549-554` codified the wrong sign.
2. **CueStore eviction orphans `_seen`** — `caption_overlay.py:100-103`;
   after 5000 cues, rewinds that re-receive cues drop them as duplicates
   forever (VOD relay re-parse; long live sessions).
3. **MKV tail region never tapped** — VLC reads the last 2.5 MB from `_tail`,
   bypassing the cache; the MKV tap parses only the cache (`vod_splitter.py`
   `_tap_cache` vs the MP4-only `_tap_read` fallback at :610-631). No
   captions/mutes for the final ~10–60 s of MKVs; seeking there kills captions.
4. **Stale cues leak into the next movie** — relay emits from a worker thread
   (queued Qt connection); disconnect+clear in `play_media`
   (`player_view.py:995-998`) doesn't purge posted events; `_on_vod_cue`
   guards only `_closing`. Stray caption + phantom mute window; same race on
   `failed` can latch `_cap_fail`.
5. **ASS bare-fixed-fields fallback dead code** — `mkv_subs.py:54-55` demands
   digits in the Dialogue line's Name field (usually empty); ffmpeg-shaped
   ASS payloads render the whole raw line as caption text.
6. **`lang_matches` substring false positive** — `mkv_subs.py:119-120`;
   "english" matches "Non-English …" → wrong-language track.

## Guiding principles

- **Run the prompts in order, one session at a time.** No file-ownership
  rules — this is a personal project and sessions never overlap, so there
  is no second writer to protect against.
- **Outcome assertions over mechanism counts.** Assert "captions are correct
  within N s of the fault," not "internal X fired" — tonight's scenario-a
  flake was a mechanism-count assertion (recovery happened via EWMA
  walk-back, invisible to the rebase counter).
- **Reports live in files, not chat.** Every package writes `pN_report.md`
  (repo convention: `sync_stage*_report.md`). Cross-session memory = plan +
  report files + git, never chat transcripts.
- **One provider connection.** Only a main session ever runs live E2E, never
  concurrently with anything else. All media tests muted, minimized, never
  focused (standing AGENTS.md rule).
- **Python:** `.venv\Scripts\python.exe -X utf8 …` (no pytest; `test_*.py`
  files are standalone scripts).

---

## How to run this plan (operating procedure)

0. **BEFORE the P0 session: commit the stage-3 + diagnosis state.** The
   working tree currently holds the entire stage-3 landing UNCOMMITTED
   (~1,200 lines: deferred anchor, watchdog retune, VOD mute-lead trim)
   plus the untracked `test_sync_adversarial.py`, `sync_stage3_run.py`,
   `sync_stage3_analyze.py`, `sync_stage3_retune.py`, the stage reports,
   the `sync_diag_*` outputs, and this plan — everything later packages
   build on. Every step below assumes a clean tree; a degraded session
   running `git checkout .` / `git clean` today would destroy stage 3.
   Commit the whole tree first (e.g. "Live-caption sync stage 3 + sync
   diagnosis: harness, E2E driver, deferred anchor, attack plan").
1. **One package = one FRESH session.** Never continue an old session (in
   particular not the diagnosis session — it is enormous). A fresh session
   spends its context budget on the work, not on history.
2. **First message: paste the prompt verbatim** from the pack below. Every
   prompt tells the session to read this plan first and own exactly one
   package. **Easiest: each prompt is saved ready-to-paste as
   `prompts/p0.txt`, `p1.txt`, `p2.txt`, `p3.txt`, `p4.txt`, `p5.txt` —
   open the file, select all, copy, paste into the fresh session.**
3. **After the session finishes:** skim its `pN_report.md`, then tell it
   `commit this work` (it names the commit after the package). The next
   session starts from a clean tree, so `git diff` ownership checks stay
   meaningful.
4. **If a session degrades** (long, confused, looping): close it. Fresh
   session, same prompt, plus: `Some of this work may already be done — run
   git status/diff first and finish the remainder.` Files are the source of
   truth, so nothing is lost.
5. **Checkpoints that need YOU:** (a) answer D1/D2 below before starting P2;
   (b) P2 stops after writing `p2_design.md` — read it and reply
   `proceed with option X` before it implements.
6. **Live testing is fine whenever a package wants it** — just one
   live-TV/movie stream at a time (the account holds one provider
   connection, and a running app counts), and every playback stays muted,
   minimized, never focused per the standing rule.

```
P0 (metrics) ► P1 (surgical bugs; subagents in parallel)
  ► [D1/D2 answered 2026-08-22: yes/yes] ► P2 (landed c4c3379)
  ► P3 (landed 4c35d13) ► P5 (verify, live)
  └─ WP4b (vendor streaming CCX, D3) — independent of the sync work; any
     fresh session after P2, must not run concurrently with a live-E2E one
```

---

## Work packages

### WP0 — Make the tests able to fail (S–M) — FIRST

Files: `test_sync_adversarial.py` (+ a no-behavior-change wall-clock seam in
`player_view.py`: a `now_s()` helper used by the caption/watchdog/cooldown
paths that maps to `time.time()` in production).

1. **Replace the error metric.** Today `truth_cue()` only returns cues that
   already cover the display position (grace ±0.3/+0.55 s), so
   `err = disp − clamp(disp, s, e)` is bounded ≤ 0.55 s by construction —
   the "p95 ≤ 1.5 s" gates cannot fail. New metric: identify the PAINTED cue
   (match the overlay's lines against released cues), score
   `|disp − clamp(disp, painted_window)|`. Roll-up screens repeat lines
   across consecutive cues: when the painted text matches several released
   cues, score the minimum error over the matches and count the sample as
   ambiguous in diagnostics — the mutation tests must still catch
   displacement despite this ambiguity.
2. **Tighten the text gate.** Primary assertion = exact-window match; keep
   the ±3-s-neighbor/substring match as a diagnostic counter only.
3. **Assert outcomes, not mechanisms.** Where checks currently count
   internal events (e.g. scenario a's "watchdog fired" rebase counter),
   replace or backstop with the user-visible contract: post-fault painted
   text correct within N s, no blank beyond the watchdog window.
4. **De-flake the timing.** The watchdog/cooldowns/stall logic gates on wall
   time while the harness runs a virtual clock — pass/fail depended on real
   CCX pacing (proven: 31/31 recorded vs 30/31 twice on one day). Route the
   wall-gated paths through the virtual clock via the seam.
5. **Regime tags.** Parameterize scenarios with L(t) profiles; teach the E2E
   driver to label checks *mechanism* (must pass always) vs *data-limited*
   (gated on measured L / feed_behind), so provider weather stops masking
   real regressions and vice versa.
6. **Mutation-test acceptance (the whole point):** inject (i) a +5 s anchor
   displacement (+ store shift), (ii) a 3 s constant caption-clock skew,
   (iii) a stale (un-shifted) store after a forced rebase — each MUST fail
   the new checks. Revert injections; 31/31 clean in --quick, twice.
   Report: `p0_report.md`.

### WP1 — Surgical bug batch (M) — one session

Fix + regression test per bug (fails before / passes after), full offline
suites green at the end. Spawning the three groups as parallel subagents is
encouraged — it keeps the session's context small; since they run
concurrently in one working tree, the briefs simply point them at separate
files.

- **1a — owns `src/ui/player_view.py`, `test_caption_overlay.py`:** delay
  sign (`t − delay`; fix the codified test at :549-554; update harness
  scenario f's delay check to assert the direction — safe here, P0 is
  already merged and no other P1 subagent owns the harness file); stale-
  cue/`failed` sender-or-generation guard
  for `_on_vod_cue`/`_cap_relay_failed`; `_caption_tick`'s bare `except`
  logs once instead of dying silently.
- **1b — owns `src/ui/caption_overlay.py` + NEW `test_cuestore.py`:** prune
  `_seen` on eviction (rewind-after-eviction regression); `text_at` must not
  abandon still-active >60 s cues (early-break at :127-128); documented
  overlap policy.
- **1c — owns `src/vod_splitter.py`, `src/mkv_subs.py`, `src/mp4_subs.py`,
  `test_vod_splitter.py`:** MKV tap must see the tail (and head) region —
  extend the `_tap_read` pattern to the MKV tap, with fixtures whose subs
  land inside the tail prefetch; fix the ASS field regex (Name is `[^,]*`,
  not `\d+`) with an ffmpeg-shaped fixture; word-boundary `lang_matches`;
  honor MKV TimecodeScale and MP4 elst media_time (ffmpeg fixtures each);
  optional hygiene: `server_close()`, `_alive` check in `_acquire`.
- Reports: `p1a_report.md`, `p1b_report.md`, `p1c_report.md`.

### WP2 — Live-edge wedge cluster (L) — design checkpoint, then implement

Evidence (2026-08-21): jump_edge landed the true edge (89.2 vs frontier
57.4); VLC wedged reporting "playing"; `chase_seek target=−17.3 → raw stayed
94.2` (set_time no-op); the state-based revive never fired (state ∉
ended/stopped/error); the `_tick` rescue needs `frontier − current > 10` —
negative past the under-credited frontier, structurally unreachable at the
true edge (no rescue rows all night); the clock integrated wall time through
sub-6-s freeze/thaw trickles (`_CC_STALL_FREEZE_S` needs 6 s CONTINUOUS
freeze) and ran 8.5 s ahead of raw.

Design directions (choose in `p2_design.md`, then STOP for approval):
- **(a) Seek-verify-escalate:** after every `set_time`, verify raw reached
  the target within a tolerance (~1.5 s base; consider target-proportional —
  VLC may legally take longer on big buffers); else escalate to the
  `play_at` revive (local file, no provider connection). MUST be
  hysteresis/cooldown-gated so a flaky verify can never reopen-loop.
- **(b) Wedge detection independent of the frontier:** raw frozen >N s while
  "playing" AND head-PCR content exists past raw (data ahead) → reopen.
- **(c) Freeze-aware clock:** hold the clock when raw advanced < ~0.3× wall
  over a rolling window while "playing"; fold on thaw.
- **(d) Product interplay:** WP4-D1 adaptive landing reduces wedge exposure.

Phase-2 implementation scope also includes the approved product decisions:
**D1 adaptive landing** (`_jump_live`/`_chase_seek` target
`edge − max(_CHASE_SAFETY_S, L+3)` while measured L > ~8 s; true edge
otherwise) and **D2 near-play join** (drop the
`frontier >= _CC_JOIN_MIN_FRONTIER_S` gate in `_start_cc_when_buffer` so a
mid-show caption engage joins near the playback position at ANY frontier —
never replaying the buffer from byte 0).
Constraints: any intentional behavior change (landing policy, escalation)
updates the E2E driver's checks (`sync_stage3_run.py`) in the same package.
New adversarial scenarios: **g** (0.2x trickle, <6 s freeze/thaw — clock
within ~1.5 s of raw, seeks land), **h** (injected set_time no-op —
escalation revives). Acceptance: mutation-proof (disable each new detection
→ its scenario fails); harness green in --quick twice; offline suites green.
Report: `p2_report.md`.

### WP3 — Anchor/store coherence under regime swings (M–L)

Problems: corrections ≤4 s never shift stored cues (`_CC_REBASE_S` path
only) → mixed-axis store accumulates across skips; the L EWMA (α=0.18/batch)
trails a growing/draining L → the pin runs early during growth, late during
drain (tonight: L 27→136 s; 23 snap-rebases in START alone).

Design questions: shift the store on every accepted correction (weighted)
vs per-batch anchor snapshots with lazy re-map; lead-compensated L (EWMA +
derivative) vs adaptive α with burst gating. **Steady-state criterion:** the
chosen store mechanism must NOT introduce visible caption jitter (windows
sliding ±1–2 s while watching live) — the retune simulation must include a
steady-regime segment proving zero jitter. Pin the retune corpus into the
repo first — DONE 2026-08-22: `sync_retune_input.log` (repo root, 4.9 MB,
the full 2026-08-21 diagnosis matrix) is committed; point the sim at it —
and note the script currently scores anchor bias
only, so it must be extended to simulate the stored cue windows — that is
what the jitter criterion measures. Re-run `sync_stage3_retune.py`
against the alternatives before choosing. New scenarios: L ramp/drain
cycles gated on the WP0 painted-text metric (gate on the cue having
arrived — data-limited blanks where the text doesn't exist yet don't
count); with an injected L ramp 1→60→5 s, painted-text exact-window match
≥95% and scrub coherence holds. Report: `p3_report.md` (+ retune
comparison table).

### WP4 — Product decisions (S each) — USER decides; D1/D2 needed before P2

**All answered 2026-08-22 — P2 may proceed on these.**

- **D1 Adaptive jump-to-live: YES.** Land `max(5, L+3)` behind the head
  while L > ~8 s; true edge otherwise — directly targets "way off / way
  ahead on live".
- **D2 Join near the playback position at ANY frontier: YES.** Kills the
  byte-0 replay delay when engaging mid-show — biggest chunk of "slow to
  load".
- **D3 Vendor a streaming-capable CCExtractor: GO** (user chose
  evaluate/vendor over skip). Findings pinned 2026-08-22: the vendored
  0.88 win build reads stdin to EOF before emitting a single SRT byte
  (measured, `live_cc.py` `CCSource.start` fail-fast), so zero-install
  releases get no live captions. Latest upstream is **v0.96.6 (Feb
  2026)** with an official **`CCExtractor.0.96.6_win_portable.zip`**
  (88 MB x64; current static 0.88 exe is 1.9 MB — the package must
  extract the minimal runtime subset, not vendor the whole zip). Work
  defined as **WP4b** below; `prompts/p4.txt` ready to paste.
- **D4 VOD CDN HTTP 551:** provider-side; retry/skip logic only.

### WP4b — Vendor a streaming-capable CCExtractor (D3) (S–M) — independent

Zero-install releases only; dev machines with a user-installed CCX are
unaffected (`find_ccextractor()` prefers the installed one — keep that
order). No dependency on P2/P3; run any fresh session after P2, never
concurrently with a live-E2E session (one provider connection).

1. Download `CCExtractor.0.96.6_win_portable.zip` (official GitHub
   release) and inventory it: which files are actually needed at runtime
   for `-in=ts --stdin --stdout` (exe + required ffmpeg/etc DLLs)? Vendor
   the MINIMAL subset into `vendor/` (the whole zip is 88 MB vs today's
   1.9 MB static exe — keep the release sane; record the final size).
2. **Acceptance = the streaming test 0.88 provably fails:** pipe a
   growing TS into stdin and require SRT bytes on stdout BEFORE EOF
   (0.88: 30 MB piped, 0 B out until close — see `live_cc.py`'s
   fail-fast). Headless, local fixture TS only (the repo's test
   recording), no network.
3. Wire it in: `bundled_ccextractor()` paths + `ccx_args()` (the long
   `--stdin/--stdout` flags are the non-0.88 form — now also correct for
   the new binary), drop 0.88's fail-fast branch if the new binary
   passes the streaming test, update `MichaelTVPlayer.spec`/build datas
   if the file set changed, refresh `vendor/COPYING-ccextractor.txt`
   (GPL-2; the portable zip's DLLs are part of CCExtractor's official
   distribution — vendor them together with the license).
4. Update `test_bundled_ccx.py`: the fail-before is its current
   expectation that the bundled binary cannot stream; pass-after = the
   new vendored binary passes the streaming test and the old checks
   (detection order, args) still hold. `p4b_report.md`: inventory,
   sizes, streaming-test evidence.

### WP5 — Verification night (M) — main session only, alone

Full-duration harness on the new metrics; --quick ×2 (de-flake proof); live
E2E in BOTH a good and a bad delivery window (classify early via frontier
growth; retry later if degenerate); live exercise of BOTH caption-engage
paths (on from the start, and Off→On mid-show at frontier ≥ 90 and < 90);
verify D1 adaptive landing live (high L → lands L+3 behind, captions
timely; low L → near the true edge); VOD E2E when the CDN accepts (probe
first; HTTP 551 = skip with note); all offline suites. Exit report
`p5_report.md`: per-symptom scoreboard
(slow-load / live-sync / VOD-sync / skip-stability / ahead-of-speech) with
before/after evidence and regime context for every number.

## Risks

- The three P1 subagents run concurrently in one working tree — their briefs
  point them at separate files; otherwise merge conflicts aren't a concern
  for one-at-a-time sessions.
- Virtualizing the wall clock may surface NEW flakiness — budget a fix loop;
  do not paper over it.
- Provider weather contaminates live verification → regime-tagged gates
  (P0) + two-window E2E (P5).
- Only one live E2E at a time (single provider connection).

---

## Prompt pack (paste one per FRESH session, verbatim)

Each prompt below is ALSO saved ready-to-paste in `prompts/pN.txt`
(no `>` quote marks to strip) — prefer copying from there.

### Prompt P0 — fix the harness metrics

> Work in D:\Coding\MichaelTVPlayer (Python/PyQt5 IPTV player). FIRST read
> `subtitle_attack_plan.md` in the repo root — you own ONLY package WP0; do
> not do any other package's work. If `git diff` shows partial WP0 work
> already, finish it. Keep console output brief; write details to
> `p0_report.md` at the end.
>
> The offline adversarial harness `test_sync_adversarial.py` (run:
> `.venv\Scripts\python.exe -X utf8 test_sync_adversarial.py --quick`,
> ~12 min; scenarios via `--only:a,b,…`) has a broken central metric that
> must be fixed BEFORE any mechanism work. Two proven defects: (1)
> `Harness.truth_cue()` (~line 578) only returns released cues that already
> COVER the display position within [s−0.3, e+0.55], so the error
> `disp − clamp(disp, s, e)` in `sample()` (~line 591) is bounded ≤0.55 s by
> construction — the "p95 ≤ 1.5 s" checks can never fail, and every recorded
> "p95 0.29" is saturation, not sync quality. (2) The code under test
> (`src/ui/player_view.py`) gates its caption watchdog/cooldowns/stall logic
> on `time.time()` while the harness drives a virtual clock `VT` — so
> wall-gated behavior (e.g. scenario a's "watchdog fired during the freeze",
> which passed in a recorded run but fails 30/31 today) depends on real
> CCExtractor pacing.
>
> Do: (a) new painted-cue metric — for each sampled tick where the overlay
> painted lines, identify which released cue was painted (match text) and
> score |disp − clamp(disp, that cue's window)|; roll-up screens repeat
> lines, so when several released cues match the painted text, score the
> minimum error over matches and count the sample as ambiguous in
> diagnostics; keep unpainted-while-truth-active as the stop metric; make
> exact-window text match the primary assertion and demote the ±3-s
> neighbor/substring match to diagnostics. (b) Replace mechanism-count
> assertions with outcome assertions where applicable (post-fault painted
> text correct within N s; no blank beyond the watchdog window) — scenario a
> is the template. (c) Make wall-gated logic deterministic: add a minimal
> `now_s()` seam in player_view covering every wall-gated path the harness
> exercises (caption clock, watchdog, rescue/reopen cooldowns; production
> maps it to time.time(), the harness maps it to VT) — a seam, NOT a
> behavior change; leave `_note_dvr_data` frontier crediting consistent
> with the harness's buffer growth (compare quick-mode results before and
> after the seam). (d) Parameterize scenarios with L(t) profiles and
> label checks mechanism vs data-limited; when you touch scenario f's delay
> check, keep it direction-NEUTRAL (a +1.5 s delay must change the painted
> cue) — the delay SIGN fix belongs to a later package; do not encode
> either sign as correct. Then prove the harness can fail —
> mutation test: temporarily inject (i) `view._cc_off += 5` + store shift at
> a fixed virtual time, (ii) a constant +3 s caption-clock skew, (iii) a
> stale (un-shifted) store after a forced rebase; each must FAIL the new
> checks. Revert the injections and confirm 31/31 in --quick mode twice AND
> in full mode once — quick and full must agree scenario-by-scenario (that
> cross-mode agreement is the determinism proof).
> Do not change production behavior, do not run any live/network tests,
> keep everything headless (offscreen Qt). `p0_report.md`: metric design,
> what each mutation caught, before/after outputs.

### Prompt P1 — surgical bug batch (three parallel subagents)

> Work in D:\Coding\MichaelTVPlayer (Python/PyQt5 IPTV player). FIRST read
> `subtitle_attack_plan.md` — do ONLY package WP1; WP0 is already merged.
> Spawn three parallel subagents with the briefs below (this keeps your
> context small); they run concurrently in one working tree, so the briefs
> point them at separate files. Merge their work, run the full offline
> suites at the end, and write `p1_report.md` summarizing all three. Tests
> are standalone scripts: `.venv\Scripts\python.exe -X utf8 <test>.py` (no
> pytest). Keep console output brief. Line numbers below are from the
> 2026-08-21 diagnosis; WP0 may have shifted them a few lines — locate by
> description when they don't match.
>
> SUBAGENT 1a (owns `src/ui/player_view.py`, `test_caption_overlay.py`;
> report `p1a_report.md`): (1) Subtitle delay sign is inverted:
> `player_view.py:3152` does `self._cap_cues.text_at(t + delay_ms / 1000.0)`
> — querying the FUTURE shows cues EARLIER, but "+ = later" everywhere else
> (`src/config.py:19`, the + button tooltip in `src/ui/subtitle_dialog.py:58-
> 62`, VLC's path `src/player.py:630-646` "positive = later" via
> video_set_spu_delay). Change to `t − delay_ms/1000.0` and fix
> `test_caption_overlay.py:549-554`, whose check name codifies the bug
> ("positive delay shows the cue 2 s early-at-position"); also update
> `test_sync_adversarial.py` scenario f's delay check so it asserts the
> DIRECTION (with +1.5 s, the painted cue must be the one active 1.5 s
> EARLIER). (2) Stale-cue
> race: `VodRelay.cue` is a queued connection emitted from the relay's
> worker thread; `play_media` teardown (`player_view.py:995-998`)
> disconnects and clears the store, but already-posted events still deliver
> `_on_vod_cue` (`player_view.py:3617`) with only a `_closing` guard — so up
> to one batch of the previous movie's cues lands in the next movie's store
> (stray caption + phantom profanity-mute window); the same race on
> `failed` can fire `_cap_relay_failed` against the new media. Add a
> sender/generation guard to both, with a regression test that emits,
> disconnects+clears, then delivers the stale queued event. (3)
> `_caption_tick` ends in a bare `except: pass` (`player_view.py:3182`) —
> keep it non-fatal but log once per distinct error. Acceptance: each fix
> has a test that fails before and passes after; `test_caption_overlay.py`,
> `test_fixes.py`, `test_subtitles.py`, `test_sub_settings.py` green.
>
> SUBAGENT 1b (owns `src/ui/caption_overlay.py` + NEW `test_cuestore.py`;
> report `p1b_report.md`): (1) `CueStore.add` dedupes on
> `(round(start,3), text)` in `self._seen`, but eviction
> (`del self.cues[:len(self.cues) - _MAX_CUES]`) only truncates `cues` —
> `_seen` is never pruned except by `clear()`/`shift()`. After 5000+
> distinct cues, a rewind that re-receives evicted cues (the VOD relay
> re-parses on seek-back) has every re-add dropped as duplicate → captions
> permanently blank in rewound regions, profanity windows lost. Fix so
> re-added cues re-enter the store. (2) `CueStore.text_at` breaks out of the
> reversed scan when `start < t − 60` (~line 127) — a still-active cue longer
> than 60 s is abandoned mid-display; make the early-break safe for active
> long cues. Put new checks in a new `test_cuestore.py` (standalone style,
> offscreen Qt) — the 1a subagent is editing `test_caption_overlay.py`
> concurrently, so leave that file to it.
> Acceptance: new tests fail before / pass after.
>
> SUBAGENT 1c (owns `src/vod_splitter.py`, `src/mkv_subs.py`,
> `src/mp4_subs.py`, `test_vod_splitter.py`; report `p1c_report.md`):
> (1) MKV captions stop in the last ~2.5 MB: `_TAIL_PREFETCH` bytes at the
> file end are served to VLC directly from `_tail`, never entering
> `read_cache`, and the MKV tap (`_tap_cache`, ~line 670) parses ONLY the
> cache — while the MP4 tap has a head/tail fallback (`_tap_read`, ~line
> 610, docstring documents this exact hole). Extend the tail (and head)
> fallback to the MKV tap; regression fixtures whose subtitle clusters land
> inside the tail prefetch, testing full-file play AND a seek directly into
> the tail region. (2) The ASS "bare fixed-fields" fallback
> (`src/mkv_subs.py:54-55`) is dead code: the regex demands `\d+` in the
> Dialogue line's Name field, normally EMPTY — real order is Layer, Start,
> End, Style, Name, MarginL, MarginR, MarginV, Effect, Text. Fix; add a
> fixture storing a Dialogue line without the "Dialogue:" prefix and assert
> clean caption text. (3) `lang_matches` (`mkv_subs.py:119-120`)
> substring-matches full-word hints anywhere: "english" matches a track
> named "Non-English Comments" — make ≥4-char hints word-boundary. (4) Honor
> MKV TimecodeScale (Info element, default 1e6) and MP4 edit-list
> media_time in cue timing (one ffmpeg fixture each). Optional if time
> permits: `server_close()` the relay's HTTP server in stop(); `_alive`
> check before opening a provider stream in `_acquire`; probe the
> async-startup-failure path (relay `failed` emitted from the startup
> thread AFTER VLC attached to the localhost URL — if playback wedges with
> no fallback, add a one-shot direct-URL handback; if VLC recovers by
> itself, document and skip). Acceptance: every
> fix fail-before/pass-after; `test_vod_splitter.py` fully green.
>
> Final step (orchestrator): run ALL offline suites
> (`test_caption_overlay test_cuestore test_vod_splitter test_profanity
> test_fixes test_always_chase test_dvr_e2e test_subtitles
> test_sub_settings test_bundled_ccx test_overlay_focus test_tab_resize
> test_temp_cleanup`), confirm zero regressions and that `git diff` touches
> only owned files — PLUS one `test_sync_adversarial.py --quick` run:
> 1a flipped the production delay sign and rewrote scenario f's direction
> check, so the harness must be re-confirmed end-to-end (expect 31/31).

### Prompt P2 — live-edge wedge design + fix (STOP after design)

> Work in D:\Coding\MichaelTVPlayer. FIRST read `subtitle_attack_plan.md` —
> you own ONLY package WP2; WP0/WP1 are merged. Keep console output brief;
> details go to your report files.
>
> PHASE 1 (do this now, then STOP and wait for my approval): write
> `p2_design.md` — a design doc for the live-edge wedge cluster, the
> measured cause of "playback freezes at live, skips stop working, captions
> run ahead". Verified evidence (2026-08-21 sync_debug.log / player.log /
> sync_diag_e2e_live.out): after jump-to-live landed the PCR-true edge
> (target 89.2 vs frontier 57.4 — the frontier under-credits by design), VLC
> wedged at the buffer tail while still reporting "playing"; `set_time`
> no-ops in that state (`chase_seek target=−17.3 → raw unchanged at 94.2 one
> second later`); the revive path in `_chase_seek`
> (`src/ui/player_view.py:1553`) only triggers on state ∈
> ended/stopped/error; the `_tick` rescue (~line 2491) requires
> `frontier − current > 10`, NEGATIVE when the viewer sits past the
> under-credited frontier — structurally unreachable at the true edge (no
> rescue rows in player.log all night); the dead-reckoned caption clock
> integrated wall time through sub-6-s freeze/thaw trickles
> (`_CC_STALL_FREEZE_S` needs 6 s CONTINUOUS freeze) and ran 8.5 s ahead of
> VLC's raw position. Evaluate at minimum: (a) seek-verify-escalate (verify
> raw reached the set_time target within a tolerance — consider
> target-proportional, VLC may legally take >1.5 s on big buffers — else
> escalate to the play_at revive; MUST be hysteresis/cooldown-gated against
> reopen loops), (b) wedge detection independent of the frontier (raw frozen
> >N s while "playing" AND head-PCR content exists past raw), (c)
> freeze-aware clock (hold when raw advanced <~0.3× wall over a rolling
> window while "playing"; fold on thaw). Recommend one combination with
> rationale, failure modes, and the exact acceptance tests (adversarial
> scenarios g: 0.2x-delivery trickle with <6 s freeze/thaw cycles — clock
> stays within ~1.5 s of raw, seeks land; h: injected set_time no-op —
> escalation revives). Note which E2E driver checks (`sync_stage3_run.py`)
> each option changes. Do NOT implement yet — stop after the doc.
>
> PHASE 2 (only after I reply "proceed with option X"): implement the
> approved design, PLUS the two WP4 product decisions (defaults approved
> unless I said otherwise): D1 adaptive jump-to-live (target
> `edge − max(_CHASE_SAFETY_S, L+3)` while measured L > ~8 s, true edge
> otherwise) and D2 near-play caption join (remove the
> `frontier >= _CC_JOIN_MIN_FRONTIER_S` gate in `_start_cc_when_buffer` so
> a mid-show engage joins near the playback position at ANY frontier).
> Add scenarios g/h to `test_sync_adversarial.py`, mutation-proof each new
> detection (temporarily disable → its scenario fails), update any E2E
> driver checks the design or landing policy changes, run the full harness
> --quick twice + all offline suites. `p2_report.md`: design summary,
> implementation notes, before/after scenario output.

### Prompt P3 — anchor/store coherence + L compensation

> Work in D:\Coding\MichaelTVPlayer. FIRST read `subtitle_attack_plan.md` —
> you own ONLY package WP3; WP0/WP1/WP2 are merged. Keep console output
> brief; details go to `p3_report.md`.
>
> Fix caption anchor/store coherence under provider lag swings, in
> `src/ui/player_view.py` (+`CueStore.shift` in `src/ui/caption_overlay.py`
> if needed). Two measured problems: (1) anchor corrections with
> |target − off| ≤ `_CC_REBASE_S` (4 s) settle via EWMA on `off` only —
> already-stored cues keep their old positions, so successive small
> corrections leave a mixed-axis store (scrubbed regions misplace captions);
> a recent run's START phase alone logged 23 anchor-snap rebases of ±4–8 s
> while provider lag L grew 27→136 s over 10 minutes. (2) The L EWMA
> (`_CC_LAG_ALPHA` 0.18 per batch) trails a growing/draining L, so the pin
> `edge − L` runs early during growth and late during drain — the "captions
> ahead of speech" symptom. Evaluate with `sync_stage3_retune.py` (constant
> retune simulation over logged per-cue anchor targets): (a) shift the store
> on every accepted correction (weighted) vs per-batch anchor snapshots with
> lazy re-map; (b) lead-compensated L (EWMA + derivative term) vs adaptive α
> with burst gating. FIRST pin the corpus: copy
> `%APPDATA%\MichaelTVPlayer\sync_debug.log` to `sync_retune_input.log` in
> the repo and point the sim at it, and extend the sim beyond anchor bias
> to simulate the stored cue windows (that is what the jitter criterion
> measures). HARD CRITERION: the chosen store mechanism must not
> introduce visible caption jitter at steady state (windows sliding ±1–2 s
> while watching) — include a steady-regime segment in the simulation to
> prove zero jitter. Implement the winner, add harness scenarios for L
> ramp/drain cycles gated on the painted-cue exact-window metric, mutation-
> proof them, run the full harness --quick twice + offline suites.
> `p3_report.md` must include the retune comparison table.

### Prompt P4b — vendor a streaming-capable CCExtractor (D3)

> (Identical to `prompts/p4.txt`.) Work in D:\Coding\MichaelTVPlayer
> (Python/PyQt5 IPTV player). FIRST read `subtitle_attack_plan.md` — you
> own ONLY package WP4b (decision D3: vendor a streaming-capable
> CCExtractor); P0/P1 are merged, P2/P3/P5 belong to other sessions. Keep
> console output brief; write `p4b_report.md` at the end. No live/network
> tests beyond downloading the release; every playback/pipe test uses the
> repo's local test recording, headless.
>
> Context: the vendored CCExtractor 0.88 win build (`vendor/
> ccextractorwin.exe`, 1.9 MB static) reads stdin to EOF before emitting a
> single SRT byte (measured: 30 MB piped, 0 B out until close), so
> zero-install releases get no live captions — `src/live_cc.py`
> `CCSource.start` fail-fasts on it ("bundled CCExtractor 0.88 cannot
> stream") and falls back to VLC's unstyled rendering. Decision D3 is GO
> (2026-08-22).
>
> Do: (1) download the official `CCExtractor.0.96.6_win_portable.zip`
> (GitHub release v0.96.6, x64) and inventory it — which files are
> actually needed at runtime for `-in=ts --stdin --stdout` (exe + required
> DLLs)? Vendor the MINIMAL subset into `vendor/` (the whole zip is 88 MB;
> keep the release size sane and record final sizes). (2) THE acceptance
> test (the one 0.88 provably fails): pipe the local test TS into stdin as
> a GROWING stream (append chunks with pauses, do not close) and require
> SRT bytes on stdout BEFORE EOF; make it a regression test. (3) Wire it
> in: `bundled_ccextractor()` / `ccx_args()` in `src/live_cc.py` (the long
> --stdin/--stdout flags are now also the bundled binary's form), remove
> or keep the 0.88 fail-fast branch accordingly, update the PyInstaller
> spec datas if the vendored file set changed, refresh the GPL-2 license
> file (vendor the official distribution's files together with its
> license). (4) Update `test_bundled_ccx.py` fail-before/pass-after:
> before, the bundled binary is expected NOT to stream; after, it streams
> and the detection-order / args checks still hold. User-installed CCX
> must keep winning `find_ccextractor()` — zero-install machines are the
> only consumers of the vendored binary. Acceptance: streaming regression
> fails before / passes after; `test_bundled_ccx.py`, `test_fixes.py`,
> `test_profanity.py` green. `p4b_report.md`: inventory table, sizes,
> streaming-test evidence, build/spec deltas.

### Prompt P5 — verification night

> Work in D:\Coding\MichaelTVPlayer. FIRST read `subtitle_attack_plan.md` —
> you own ONLY package WP5; all prior packages are merged. Make sure the app
> itself isn't streaming during the live runs (the account allows one
> provider connection). All media muted, minimized, never focused.
>
> Run the full verification matrix and write `p5_report.md`: (1)
> `test_sync_adversarial.py` full duration AND `--quick` twice (de-flake
> proof) on the new metrics; (2) all offline suites; (3) live E2E via
> `sync_stage3_run.py live` in TWO windows chosen to sample different
> provider delivery (classify the regime early via frontier growth; if the
> first window is degenerate, note it and retry later); (4) live exercise
> of BOTH caption-engage paths: captions ON from the start (cold join), and
> Off→On mid-show at frontier ≥ 90 s and also < 90 s — with D2 in, first
> paint after a mid-show engage should arrive within ~15 s (no byte-0
> replay); (5) verify D1 adaptive landing live: with high measured L,
> jump-to-live lands ~L+3 behind the head with timely captions; with low L
> it lands near the true edge; (6) VOD E2E (`sync_stage3_run.py vod`)
> only if the CDN accepts (probe first; HTTP 551 = skip with a note).
> `p5_report.md`: per-symptom scoreboard (slow-load, live sync, VOD sync,
> skip-stability, ahead-of-speech) with before/after evidence and the
> regime context for every number.

## What I need from you (the user) before P2 — ANSWERED 2026-08-22

- **D1:** adopt adaptive jump-to-live? **YES.**
- **D2:** join the CC reader near the playback position at ANY frontier?
  **YES.**
- **D3:** spend effort vendoring a streaming-capable CCExtractor?
  **YES — GO** (findings + package WP4b above, `prompts/p4.txt` ready).
