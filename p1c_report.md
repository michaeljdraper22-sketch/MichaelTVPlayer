# P1c report — VOD splitter / subtitle-extraction surgical fixes

**Subagent 1c of WP1.** Owned files: `src/vod_splitter.py`, `src/mkv_subs.py`,
`src/mp4_subs.py`, `test_vod_splitter.py`. Nothing else touched (WP0 stays
uncommitted in the tree as expected; concurrent 1a/1b edits left alone).

**Suite: `test_vod_splitter.py` — 108/108 checks green, run twice
consecutively (determinism check).** Rule zero held for every fix: all new
regressions were written first and provably FAILED before their fix (one run,
12 failures, zero pre-existing checks broken), then passed after.

Fail-before run (before any src change):

```
FAILED 12: ['ffmpeg field-style payload reduced to its text',
 '(a) full-file play taps the tail-region cues',
 "(a) tail cue times on the file's own clock",
 '(b) seek into the tail region keeps captions tapped',
 'hole between head and tail returns EMPTY, not wrong bytes',
 '_acquire refused after stop() (no dead connection)',
 'bare fixed-field dialogue (empty Name) reduced to its text',
 'bare fixed-field text keeps its commas',
 "'english' does NOT match 'Non-English Comments'",
 'non-default TimecodeScale (2x) shifts cue times',
 'scale honored through the live relay tap',
 'elst media_time (0.7 s skip) shifts cue times earlier']
```

After: `all 108 checks passed` (twice).

---

## Fix 1 — MKV captions stop in the last ~2.5 MB (the tail hole)

**Diagnosis confirmed.** VLC's reads of the tail region are served straight
from `_tail` (`vod_splitter.py` `_stream_out` tail branch) and never enter
`read_cache`; the MKV tap (`_tap_cache`) parsed only the cache. Two shapes:
(a) full-file play — the cache window stops growing exactly at `_tail_base`,
so the parser never sees the final clusters; (b) a seek landing INSIDE the
tail region is served from the prefetch with no provider acquire, so the
window never rebases and the tap stays anchored elsewhere entirely.

**What changed (`src/vod_splitter.py`):**
- New one-shot **tail harvest** thread (`_tap_tail_harvest`, :870): when the
  tap loop first has track metadata + a prefetched tail (:791-813), a
  mid-stream parser seeded with the metadata (and TimecodeScale) parses the
  whole `_tail` region and emits its cues — cluster timecodes are absolute,
  so times land on the file's own clock; re-emitted cues dedupe downstream on
  (start, text). Re-runs on a CC-menu language switch (`_tail_harvested`
  reset in the `_tap_restart` branch, :725). The relay-side mechanism is a
  harvest rather than extending the sequential feed because of shape (b): a
  sequential parser cannot jump the uncached hole between frontier and tail,
  and rebasing the window on tail GETs would corrupt the cache when VLC's
  startup Cues-index request arrives (analyzed, rejected).
- New `_snap_cluster` (:97): the tail boundary starts MID-element, and the
  garbage size decode there can exceed 1 MB, tripping the parser's
  stream-skip path which then silently swallows the whole region (this made
  the first post-fix run pass by luck of the noise bytes — caught and fixed
  on the second run). The harvest now anchors on a validated cluster header
  (magic + size that fits + next header at the cluster's declared end).
- `_tap_read` rewritten (:646): region-stitching reader — cache, then
  prefetched head, then prefetched tail, stitched only where contiguous
  (preserves the MP4 sample straddle contract), and a read starting in (or
  running into) an uncached hole now returns the readable prefix instead of
  the old code's WRONG BYTES (the old slice math mapped a mid-hole offset to
  the START of `_tail` — latent for MP4 `extract`, which never requests
  holes, but a real wrong-bytes bug).
- MKV head side: no new mechanism needed — the head ride caches its bytes
  (offset-0) and VLC's walk GET caches them (resume), both parsed by the
  existing sequential path; the MP4 tap's head fallback exists for
  random-access (faststart moov), which the sequential MKV tap doesn't do.

**Evidence:** fixture `build/split_test_tail.mkv` — 4.7 MB noise+CBR MKV
(one 90 s encode, ~4 s) with a t=1 s cue inside the 512 KB head-ride zone
and t=60/63/86 s cues inside the 2.5 MB tail prefetch, served over the
Range-capable local HTTP provider. Checks: (a) full-file GET through the
relay → byte fidelity + all three late cues tapped with times 60.0/63.0/86.0
±0.5 s + early cue still tapped + one provider connection; (b) fresh relay,
a single seek GET 1 MB from EOF (served from the prefetch, provider_opens
stays 1) → late cues still tapped. Plus `_tap_read` units: head/tail serves,
hole refusal, cache→tail straddle stitch.

## Fix 2 — ASS "bare fixed-fields" fallback dead code

**Diagnosis refined.** The old single regex
`^\d+,[^,]*,[^,]*,[^,]*,\d+,\d+,\d+,[^,]*,` is dead for a bare FULL Dialogue
line (10 fields: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,
Text) because `\d+` lands on the normally-EMPTY Name — exactly as diagnosed.
But it accidentally matches the OTHER real shape: ffmpeg/matroska stores ASS
events with the prefix AND the timestamps stripped (9 fields: ReadOrder,
Layer,Style,Name,margins,Effect,Text — verified by dumping a real ffmpeg
`-c:s ass` MKV's payloads). The original suite fixture was that 9-field
shape, which is why the bug hid.

**What changed (`src/mkv_subs.py` :62-67, :185-190):** two patterns tried in
order — `_BARE_DIALOGUE_RE` (10 fields, Start/End required clock-shaped,
`split(",",9)[-1]`) then `_MATROSKA_FIELDS_RE` (the old 9-field pattern,
`split(",",8)[-1]`). Docstrings updated; `flatten_ass_text` now documents
all four payload shapes.

**Evidence:** `[3b]` keeps the 9-field (matroska) check and adds the 10-field
check; `[7]` adds the two 10-field regression checks (empty Name, commas in
Text) — both FAILED before (raw line leaked as caption text), pass after.

## Fix 3 — `lang_matches` substring false positive

**What changed (`src/mkv_subs.py` :121-139):** ≥4-char hints now match
whitespace-delimited WORDS (edge punctuation stripped, so "Signs (English)"
matches; hyphenated compounds stay one word, so "Non-English Comments" does
not). Short hints/international codes keep the alias-table behavior, now
also run over the stripped tokens. No other caller's semantics changed
(`test_caption_overlay.py`'s lang checks all use the alias path — verified).

**Evidence:** `[7b]`: `'english' does NOT match 'Non-English Comments'`
FAILED before (returned True), passes after; true positives
("English Comments", "(English)", alias pairs) pass before and after.

## Fix 4a — MKV TimecodeScale

**What changed (`src/mkv_subs.py`):** `MkvSubParser` now parses the Info
element's TimecodeScale (default 1e6 ns; `_parse_info` :385, hooked at
:300); cluster/block timecodes (:480) and BlockDurations (:508) scale by
`scale_ns/1e9`. Mid-stream parsers (rebase/restart/harvest/backfill joins
past Info) take the scale via a new `timecode_scale_ns` ctor arg; the relay
snapshots `parser_scale_ns` from the head ride and the tap loop and seeds
every fresh parser (`vod_splitter.py` :222, :404, :740-773, :812, :846).

**Evidence:** ffmpeg cannot mux a non-default scale, so the fixture is the
base MKV with `TimestampScale` patched 1e6→2e6 ns in place (same 3-byte
width): `[8]` unit (1 s SRT cue must land at 2.0 s end 6.0 s — was 1.0/3.0)
and a live relay run over the local HTTP provider (scale honored through
the tap). Default-scale timing unchanged (guard check at 1.0 s ±0.05).

## Fix 4b — MP4 edit-list media_time

**What changed (`src/mp4_subs.py`):** `_Trak.elst_media` (:110); `_parse_trak`
collects `edts`, new `_parse_edts` (:369) honors the first non-empty edit's
media_time (v0/v1, signed; empty edits media_time=-1 read as no skip);
`_expand` (:475) drops samples inside the skipped span and shifts the rest
earlier by `media_time/timescale` (:495-496). media_time=0 files are
bit-identical in behavior (guard check).

**Evidence:** ffmpeg always writes media_time=0, so the fixture is a plain
mov_text MP4 with the TEXT trak's elst media_time patched to 0.7 s worth of
media ticks (timescale-read from mdhd — the tx3g trak runs at 1 MHz, not
1 kHz; same field width, no size changes). `[9]`: the 1 s cue must land at
0.3/2.3 s — was 1.0/3.0; unpatched plain MP4 still 1.0/3.0.

## Optional items

- **`_acquire` `_alive` check** — DONE (`vod_splitter.py` :531): a stopped
  relay opens no provider stream. Fail-before/pass-after via `[6c]`
  (`_acquire` after `stop()` returned a live stream and bumped
  `provider_opens` before; returns None now).
- **`server_close()` in `stop()`** — DONE (:417, after `shutdown()`): frees
  the listening socket deterministically instead of relying on GC. Hygiene
  only: a port-check test passes before AND after (CPython's refcount close
  frees it promptly either way), so no fail-before proof is possible; suite
  green confirms no regression.
- **Async-startup-failure probe** — PROBED, VLC recovers by itself; no
  handback added (documented, skipped per brief). Offline localhost
  simulation: provider serves only the 12-byte probe then resets every
  connection (tail prefetch + 3 acquire attempts fail). With VLC attached to
  the relay URL (muted `VLCPlayer(volume=0)`, no window bound, silent
  color-bar fixture, minimized/never focused per standing rule):
  `relay.failed("provider open failed")` fired from the startup thread at
  t=3.1 s, VLC's handler served HTTP 503 once `_startup_failed` latched, and
  VLC went `State.Opening → State.Ended` by t=3.6 s, `is_playing()=False` —
  a clean terminal state, no wedge. The UI-side stale-`failed` handling is
  1a's sender/generation guard; nothing for the relay to hand back.

## Notes / deviations

- The brief's "extend `_tap_read` to the MKV tap" is implemented as the
  harvest + snap mechanism (outcome-equivalent for full-file play, and the
  only thing that can cover seek-into-tail, which a sequential feed cannot
  jump to); `_tap_read` itself was rewritten for hole safety and remains the
  MP4 tap's reader.
- Two fixture-build subtleties worth remembering: ffmpeg's mov_text trak
  timescale is 1 MHz (patch values must be derived from mdhd, not assumed),
  and the elst builder must write the patched bytearray back to the file
  (first version patched in memory only — caught by the failing check).
- `test_vod_splitter.py` grew from 96 to 108 checks; new sections [6]/[6b]/
  [6c]/[7]/[7b]/[8]/[9] plus two fixture checks folded into [3b]. Suite
  runtime ~4 min (one extra 90 s encode + tail pulls).
