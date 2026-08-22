# WP1a report — overlay delay sign, stale-relay race, silent tick errors

Owned files: `src/ui/player_view.py`, `test_caption_overlay.py`, and the
scenario-f delay check in `test_sync_adversarial.py`. Rule zero honored
for every fix: the regression tests were written first, run to FAIL
against the unfixed tree, then re-run to PASS after the fix.

## Fix 1 — subtitle delay sign inverted on the overlay

**What changed**
- `src/ui/player_view.py` `_caption_tick` (~line 3171, was 3161): the
  store query flipped from `text_at(t + delay_ms / 1000.0)` to
  `text_at(t - delay_ms / 1000.0)` — positive delay = cues later, now
  agreeing with `src/config.py:19`, the +/- tooltip
  (`src/ui/subtitle_dialog.py:58-62`) and VLC's own path
  (`src/player.py:630-646`, positive `video_set_spu_delay` = later).
  Docstring updated to state the sign.
- `test_caption_overlay.py` (~line 549): the old check
  "positive delay shows the cue 2 s early-at-position" codified the bug;
  replaced by an isolated probe cue (80-82 s) and three checks: at +2 s
  the true position paints nothing, the cue paints 2 s PAST its true
  window, and delay 0 restores the true window.
- `test_sync_adversarial.py` scenario f (~line 1229): the WP0
  direction-neutral probe is now DIRECTIONAL — with +1.5 s, the painted
  lines must equal `text_at(clock - 1.5)` (the cue active 1.5 s
  EARLIER) and differ from the delay-0 paint.

**Evidence**
- Before (old sign): `FAIL positive delay shows the cue 2 s past its
  true window`; scenario f alone (`--quick --only:f`): `FAIL f: +1.5 s
  delay paints the cue active 1.5 s EARLIER` (6/7).
- After: all three overlay checks `ok`; scenario f alone 7/7
  (`ok f: +1.5 s delay paints the cue active 1.5 s EARLIER`).

## Fix 2 — stale queued relay deliveries land in the next movie

**Mechanism (verified by probe, this PyQt5 build)**: `VodRelay.cue` /
`failed` are queued connections emitted from worker threads.
`_stop_profanity` (`player_view.py:~3954`) disconnected only `cue` —
`failed` stayed connected until object destruction — and `VodRelay.stop`
does not join the tap thread, so emissions in flight during
`play_media`'s teardown (disconnect + store clear + `_session` bump)
can be delivered after the NEXT media attached its own relay: stray
caption, phantom profanity-mute window, `_cap_fail` latched against a
healthy relay. (On this build `disconnect()` purges already-queued
events, so the surviving window is an emit racing the disconnect — the
test models that in-flight delivery by keeping the old connection
alive; on builds without the purge the exposure is wider.)

**What changed**
- `src/ui/player_view.py`:
  - new `_cap_relay_live()` (~line 3640): a delivery is accepted only
    when `sender() is self._vod_relay` (the relay the CURRENT media
    attached); with no sender (direct call / destroyed sender) it falls
    back to `_cap_relay_gen == _session`.
  - `_on_vod_cue` and `_cap_relay_failed` now guard on
    `not self._cap_relay_live()`.
  - `_cap_relay_gen` init in `__init__` (0, matching `_session = 0`) and
    set to `self._session` at every attach in `_effective_url`.
  - `_stop_profanity` now also disconnects `failed` (shrinks the
    never-disconnected window).
- `test_caption_overlay.py` new section `[5c]`: connects a stub relay A
  (cue+failed, queued from a worker thread), runs the play_media
  teardown mimic (overlay released, store+filter cleared, session bump,
  fresh relay B attached), then delivers A's stale cue+failed and
  asserts: store stays clean, no phantom mute window, no `_cap_fail`
  latch.

**Evidence**
- Before: `FAIL stale cue from the previous relay never lands in the
  store`, `FAIL stale cue opens no phantom profanity-mute window`,
  `FAIL stale relay failure does not latch against the new media`.
- After: all three `ok`.

## Fix 3 — `_caption_tick` bare `except: pass`

**What changed** — `src/ui/player_view.py` `_caption_tick` (~line 3200):
still non-fatal, but each DISTINCT error (key `TypeName:message`) logs
one warning ("captions: tick failed (...)"); identical repeats are
suppressed; a new distinct error logs once more. `_cap_tick_errs` set
in `__init__` (bounded: cleared when it exceeds 64 keys) and reset in
`play_media`'s teardown so a new media logs its own errors.

**Evidence** — new checks in `test_caption_overlay.py`: 3 identical
`RuntimeError("tick-boom")` ticks + 1 `KeyError("tick-other-boom")`
tick must produce exactly 2 warning records (one per distinct error),
and the tick must keep painting afterwards.
- Before: `FAIL caption tick errors logged once per distinct error`
  (0 records — fully silent).
- After: `ok caption tick errors logged once per distinct error`,
  `ok caption tick survives the errors (paints again after)`.

## Suites run (after all fixes)

| suite | result |
|---|---|
| `test_caption_overlay.py` | 135 passed, 0 failed (was 130/5 before the fixes) |
| `test_fixes.py` | 51 passed, 0 failed |
| `test_subtitles.py` | all 22 checks passed |
| `test_sub_settings.py` | all 37 checks passed |
| `test_profanity.py` (extra: direct `_on_vod_cue` path) | all 54 checks passed |
| `test_sync_adversarial.py --quick --only:f` | 7/7 (was 6/7 with old sign) |

Notes for the orchestrator: the full `--quick` harness re-run is left
to the package-level gate as planned; the sign flip touches scenario f
only (delay usage is confined to it — verified by grep). Concurrent
subagents 1b/1c were editing their own files throughout; this package
touched only the three owned files (probe scripts used during
diagnosis were deleted).
