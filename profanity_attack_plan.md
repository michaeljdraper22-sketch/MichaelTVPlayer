# Profanity-mute timing — attack plan (v2)

**Written:** 2026-08-23, after the subtitle campaign (P0–P5) landed and the
exe was rebuilt. Subtitle SYNC is now good (user: "perfectly synced with
movies"), which isolates the remaining complaint to the FILTER's mute
timing: mutes miss the word by ~0.5–1 s. Same doctrine as the subtitle
plan: nothing is "done" until a check that provably FAILED before the fix
passes after it, and every number comes from a measurement, not a guess.
**v2 (same day, user input): the misses are on MOVIES & SERIES — the VOD
relay path. Live mutes are not a reported problem. Critical path is now
WP1 → WP2 → WP4 (measure → fix VOD → verify+ship); WP3 (live) is
DEFERRED until live misses are ever reported. Working assumption for
direction: late-on leaks (the in-code measurement at `player_view.py:240`
says "the word already audible as the window opened") — WP1's data
confirms or corrects this.**

## Where the miss comes from (code-anchored error budget)

The mute chain is: cue (start, end, text) → word position estimated by
**character share** inside the cue (`windows_from_cues`,
`src/profanity.py:217-235`) → pads `pad_before_s` 0.12 / `pad_after_s`
0.25 added in `evaluate()` (`src/profanity.py:336-343`, driven by the
100 ms caption tick, `player_view.py:4339-4343`) →
`set_filter_mute` → `audio_set_mute` (`src/player.py:579-601`).

| # | Error source | Path | Est. size | Direction |
|---|---|---|---|---|
| 1 | **Char-share word estimate** — real speech rate doesn't follow character count; a 4–6 s cue can place a word ±1 s off. A constant lead can't fix scatter | both; worst on long cues | ±0.3–1.5 s | both |
| 2 | **VLC audio-output latency** — `audio_set_mute` takes effect after the aout buffer; nothing compensates (`audio_get_delay` never called) | both | ~0.1–0.3 s | late-on |
| 3 | **100 ms tick granularity** | both | ≤0.1 s | late-on |
| 4 | **Live lead is 0.0** — `_on_cc_cue` passes `lead_s=0.0` on the theory that arrival-anchored windows are already display times (`player_view.py:4222-4223`); but display-sync tolerance is ±1 s, mute needs ±0.3 s, and captioner lag is 1–3 s VARIABLE | live | mean ~1.5 s? | late-on (to be measured) |
| 5 | **Anchor transients** — rebase/snap corrections shift windows (`shift_windows`, `src/profanity.py:325-333`); a mid-word shift can un-mute early or mute the wrong speech | live | p95 1–3 s transient | both |
| 6 | **Pads too small for the estimate error** — 0.12/0.25 covers none of #1 | both | — | leaks |

Existing band-aids that admit the problem:
- `player_view.py:240-246` `_VOD_MUTE_LEAD_S = 0.4` — "movies were
  measured to miss mutes by ~0.5 s (the word already audible as the
  window opened)". A constant shifts the mean; the scatter remains.
- `config.py:41` `lead_ms` (default 1500) is a **dead knob**: BOTH call
  sites pass explicit leads (VOD the 0.4 constant, live 0.0), so the
  config value never reaches the engine. Either wire it honestly or
  remove it — WP3 decides on data.

**Policy decision (default, revisit if the user says otherwise):** a
profanity filter's job is to never leak; clipping ~1 s of adjacent clean
dialogue is the acceptable cost. All tuning biases toward cover-fully:
pads asymmetric, generous before/after, tuned from measured error
distributions (WP1), not hand-picked.

## Guiding principles (carried from the subtitle plan)

- **Rule zero:** nothing here is "done" until a check that provably
  FAILED before the fix passes after it.
- **Outcome assertions over mechanisms:** assert "the mute command fires
  within N ms of the word's known time" / "no leak at p95", not
  "function X was called".
- **Reports live in files** (`profN_report.md`); cross-session memory =
  plan + report files + git.
- **One package = one FRESH session**, prompt pasted verbatim from
  `prompts/profN.txt`. Python: `.venv\Scripts\python.exe -X utf8 …`
  (no pytest; `test_*.py` are standalone). Headless/offscreen Qt.
- **One provider connection;** live samples only in a main session, one
  stream at a time, every playback muted + offscreen + never focused.
- **Ear ground truth beats simulation:** the user's labeled words during
  WP4 verification are the acceptance of record; fixtures only
  regression-guard the mechanics.

## Immediate user-side mitigation (no code, today)

`%APPDATA%\MichaelTVPlayer\settings.json` → `"profanity"`:
`pad_before_ms: 700`, `pad_after_ms: 900` (pads ARE wired into
`evaluate()`). This stops the audible leaks immediately at the cost of
~0.7/0.9 s over-mute per word. `sync_ms` shifts everything (+ later /
− earlier). `lead_ms` currently does nothing (dead knob, #4 above).
The plan's WP2 replaces this blunt fix with measured, duration-aware
values as defaults.

---

## Work packages

### WP1 — Make the miss measurable (S–M) — FIRST

**VOD is the reported miss path (v2): the tone fixture + one real
movie's prof_timing.log are the deliverables that matter; keep the
live/vod tag and live instrumentation anyway (they are cheap and WP3
may return).**

Files: `src/profanity.py` (timing log), `src/player.py` (aout probe),
`src/ui/player_view.py` (log wiring), NEW `test_profanity_timing.py`.

1. **Per-mute timing log.** One line per window open/close to
   `%APPDATA%\MichaelTVPlayer\prof_timing.log`: wall time, caption-clock
   `t`, window `(s, e)`, the source cue `(start, end, text)` with the
   matched word span, path tag (live/vod), and `audio_get_delay()` ms
   (probe availability once per mute; degrade gracefully if the call
   fails). One real evening of watching then yields dozens of samples.
2. **Tone fixture = exact ground truth.** Build (ffmpeg, via
   `profanity.find_ffmpeg()`) a short MP4/MKV: a 300 ms tone burst at a
   KNOWN instant + an embedded text-track cue containing a bad word at a
   known character offset, swept across cue lengths 1–6 s and word
   positions (start/middle/end). Play it through the REAL relay + VLC +
   engine offscreen (position as clock, no audio device needed for the
   command-timing check) and measure when `set_filter_mute(True)` fires
   vs the tone's known instant. This pins error terms #2 + #3 exactly
   and #1 for synthetic cues; make it a permanent regression suite.
3. **Prove it fails today:** with current constants the fixture must
   show the ~0.5–1 s miss (it will — that's the point); record the
   numbers.
4. **Real-session pull:** after instrumentation lands, grab one evening
   of the user's `prof_timing.log` and decompose: estimator error vs
   aout delay vs tick, live vs VOD separately. This answers the open
   questions without guessing: (a) does the live anchor absorb
   captioner lag or not (#4); (b) is the live miss bigger than VOD's;
   (c) actual error distribution width for pad sizing.
Report: `prof1_report.md` (error decomposition table, fixture design,
fail-today evidence).

### WP2 — VOD: replace the guess (M) — THE CENTRAL FIX

*This is the package the whole plan exists for (v2): the user-reported
misses are movies/series, and this package replaces the estimate that
causes them.*

Files: `src/profanity.py`, `src/ui/player_view.py` (VOD call site),
`test_profanity_timing.py`, `test_profanity.py`.

1. **Syllable-weighted word timing.** In `windows_from_cues`, weight
   each word by vowel-cluster count (≈ spoken duration) instead of raw
   character count; digits/punctuation fall back to char share. Keep it
   deterministic and pure — unit-testable against constructed cues.
2. **Duration-adaptive asymmetric pads.** Estimation error scales with
   cue duration: `pad_before = a + b·cue_dur`, `pad_after = c + d·cue_dur`
   with constants FIT from WP1's measured distribution (target: zero
   leaks at p95, over-mute p95 ≤ ~1 s). Defaults live in
   `src/config.py`; the user's manual pads still override.
3. **Absorb `_VOD_MUTE_LEAD_S`** into the fitted model (its 0.4 s is a
   mean-shift; the model subsumes it) — delete the constant, keep a
   comment pointing at the WP1 numbers.
4. Acceptance: WP1's fixture suite goes red→green (fail-before with
   reverted estimator, pass-after); over-mute and leak numbers from the
   WP1 corpus reported; all offline suites green.
Report: `prof2_report.md`.

### WP3 — Live: honest lead + rebase-safe mutes (M) — DEFERRED (v2)

**DEFERRED 2026-08-23: the user reports misses on movies/series only.
Run this package only if/when live misses are reported. (The dead
`lead_ms` knob cleanup rides along here whenever it runs.)**

Files: `src/profanity.py`, `src/ui/player_view.py`, harness/driver as
needed.

1. **DECIDE on WP1 data (record the decision here):**
   - **D1:** if the live anchor already places windows at speech time
     (miss distribution ≈ VOD's), delete the `lead_ms` knob entirely
     and treat live like WP2 with slightly wider pads;
   - **D2:** if live misses late by a consistent mean (anchor places
     windows at caption-broadcast time), wire the knob: `_on_cc_cue`
     passes the configured lead (new default from the measured mean),
     exposed honestly in config, documented in the dialog tooltip.
2. **Rebase-safe mutes.** `shift_windows` currently translates windows
   on anchor rebases — verify the mute side specifically: a shift
   landing mid-mute must never un-mute (extend, don't truncate: if a
   window was open at shift time, keep audio muted until the shifted
   window's end or the original end, whichever is later). Add an
   adversarial-harness scenario: forced rebase mid-word → the mute
   survives the shift; mutation-proof it (disable the guard → fail).
3. **Live pads:** roll-up cues span several sentences — wider
   uncertainty than VOD; size from WP1's live distribution.
4. One live sample with the timing log (main session, one connection,
   muted/offscreen) confirming the log records the fix.
Report: `prof3_report.md` (decision D1/D2 + evidence).

### WP4 — Verification night + ship (S–M)

1. **Ear ground truth:** the user watches TWO movies/series episodes
   (the reported path; a live show optional) with the filter on and
   notes 5–10 words by ear ("mute was late / early / clean"); correlate
   with `prof_timing.log` — ≥90% of labeled words fully covered, zero
   audible leaks is the pass bar.
2. All offline suites + the timing fixture suite green; commit; rebuild
   `dist\MichaelTV.exe` (spec unchanged since WP4b); smoke-launch
   minimized, confirm alive, close.
3. User watches normally for a few days; residual complaints become
   labeled samples for a v2 tuning pass (constants only).
Report: `prof4_report.md` (per-word label table, verdict).

## Risks

- `audio_get_delay()` may return 0/unavailable on some aout modules —
  probe, fall back to a measured constant from the tone fixture.
- The tone fixture measures command timing, not audible timing; the aout
  term bridges them and is bounded (~0.3 s) — ear labels in WP4 are the
  final arbiter.
- Over-muting annoys if pads overshoot — the fit targets leak-zero at
  the SMALLEST pads that achieve it, and the user's config overrides
  remain in effect.

## Prompt pack (paste one per FRESH session, verbatim)

Also saved in `prompts/prof1.txt` … `prof4.txt`.

> **prof1:** Work in D:\Coding\MichaelTVPlayer (Python/PyQt5 IPTV player).
> FIRST read `profanity_attack_plan.md` — you own ONLY package WP1; do
> not do WP2/WP3 work. Headless/offscreen only, no live streams (VOD is
> the reported miss path — the tone fixture + the measurement layer are
> the deliverables). Tests:
> `.venv\Scripts\python.exe -X utf8 <test>.py`. Keep console output
> brief; details to `prof1_report.md`. Build the measurement layer the
> rest of the plan depends on: (1) per-mute timing log in
> `src/profanity.py` + `player_view.py` wiring (fields per the plan's
> WP1.1, written to %APPDATA%\MichaelTVPlayer\prof_timing.log, one line
> per window open/close, live/vod tag); (2) probe `audio_get_delay()`
> once per mute in `src/player.py`, degrade gracefully; (3) NEW
> `test_profanity_timing.py`: ffmpeg tone-burst fixtures (300 ms tone at
> a known instant + embedded text cue with a bad word at a known char
> offset; sweep cue lengths 1–6 s × word positions start/middle/end;
> build via profanity.find_ffmpeg(), skip with a note if absent) played
> through the REAL VodRelay + VLC + engine offscreen — assert when
> set_filter_mute(True) fires vs the tone's known instant; the suite
> MUST show today's ~0.5–1 s miss (record it — that is the fail-before);
> (4) run test_profanity.py + test_caption_overlay.py, zero
> regressions. prof1_report.md: error decomposition table, fixture
> design, the fail-today numbers.

> **prof2:** Work in D:\Coding\MichaelTVPlayer. FIRST read
> `profanity_attack_plan.md` — you own ONLY package WP2; WP1 is merged.
> Headless/offscreen only. (1) Replace char-share word timing in
> `windows_from_cues` (src/profanity.py) with syllable-weighted timing
> (vowel clusters; digits/punct fallback char share) — deterministic,
> pure, unit-tested; (2) duration-adaptive asymmetric pads
> (pad_before = a + b·cue_dur, pad_after = c + d·cue_dur) with
> constants fit from WP1's measured distribution in prof1_report.md —
> target zero leaks at p95, over-mute p95 ≤ ~1 s; defaults into
> src/config.py, user's manual pad settings still override; (3) absorb
> _VOD_MUTE_LEAD_S into the model and delete the constant with a comment
> pointing at the WP1 numbers; (4) acceptance: the WP1 fixture suite
> red→green (revert the estimator temporarily to prove red), leak and
> over-mute numbers from the WP1 corpus in the report, all offline
> suites green. prof2_report.md.

> **prof3:** Work in D:\Coding\MichaelTVPlayer. FIRST read
> `profanity_attack_plan.md` — you own ONLY package WP3; WP1/WP2 merged.
> Record decision D1 or D2 (plan §WP3.1) with the WP1 evidence BEFORE
> implementing: either delete the dead lead_ms knob or wire it live with
> a measured default. Implement the rebase-safe mute guard
> (extend-don't-truncate across shift_windows when a window is open at
> shift time), add the forced-rebase-mid-word adversarial scenario,
> mutation-proof it, size live pads from WP1's live distribution. One
> live sample with the timing log on (main session, one provider
> connection, muted + offscreen + never focused) confirming the log
> records the fix. prof3_report.md: the decision + evidence + scenario
> output.

> **prof4:** Work in D:\Coding\MichaelTVPlayer. FIRST read
> `profanity_attack_plan.md` — you own ONLY package WP4; WP1/WP2 merged
> (WP3 is DEFERRED — skip it). (1) After the USER's labeled viewing
> session (their 5–10 ear-labeled words on movies/series; live
> optional), correlate labels against
> prof_timing.log — ≥90% fully covered, zero audible leaks is the pass
> bar; (2) all offline suites + the timing fixture green; commit the
> package; (3) rebuild dist\MichaelTV.exe
> (.venv\Scripts\pyinstaller --noconfirm MichaelTVPlayer.spec, then
> robocopy vlc dist\vlc /E), verify the vendor payload with
> pyi-archive_viewer, smoke-launch minimized (never focused, no audio),
> confirm alive in player.log, close. prof4_report.md: per-word label
> table + verdict.

## What I need from you (the user)

1. **Where does it miss** — ANSWERED 2026-08-23: **movies & series**
   (the VOD relay path). Live deferred accordingly (WP3).
2. **Which direction** — still open; working assumption is late-on
   leaks (per the in-code measurement note at `player_view.py:240`).
   WP1's data confirms or corrects; say so anytime if it's the
   opposite (mutes starting early / clipping clean speech).
3. **WP4 labels:** during the verification viewing, note 5–10 words by
   ear (late / early / clean) — those labels are the acceptance of
   record.
