# WP1 subagent 1b — CueStore fixes (caption_overlay.py)

Owned files: `src/ui/caption_overlay.py`, NEW `test_cuestore.py`.
Rule zero honored: the new suite was written FIRST and run against the
unfixed code (7 FAILs, evidence below), then the fix landed and the same
suite went 28/28.

## Fix 1 — eviction orphaned `_seen` (rewind-after-eviction)

**What changed** (`src/ui/caption_overlay.py`):

- `add()` (:96-120) now appends the cue's exact `(start, text)` to a new
  arrival-order deque `self._order`, and evicts via `_evict_one()`
  instead of `del self.cues[:len(self.cues) - _MAX_CUES]`.
- New `_evict_one()` (:122-139): drops the oldest-**arrived** cue —
  located in the start-sorted list by `bisect_left(cues, (s,))` plus a
  short equal-start scan — and discards its key from `self._seen` in the
  same step, so `_seen` always equals the keys of what is actually
  stored.
- `clear()`/`shift()` reset/rebuild `_order` alongside `_seen`
  (:90-94, :141-154).

**Why eviction order changed too (not just the prune):** with the store
at cap, evicting by START makes a re-received rewound cue (older start)
sort to the front and evict *itself* on arrival — a `_seen` prune alone
leaves deep rewinds permanently blank, and the package's acceptance
regression ("fill past `_MAX_CUES`, evict, re-add an evicted cue, assert
queryable via `text_at`") cannot pass. Arrival-order eviction keeps the
re-received cue and drops the oldest stale arrival instead. In the
forward live/VOD flow arrival order IS start order, so evictions there
are identical to the old front-truncate (the existing "store bounded" /
"oldest evicted, newest kept" checks in `test_caption_overlay.py` [1]
encode exactly this and still hold — verified by trace; the file itself
is 1a's and was not run mid-flight).

**Fail before / pass after** (`test_cuestore.py` [2]):

```
FAIL deep rewind: evicted cue re-enters the store          -> ok
FAIL deep rewind: evicted cue queryable again via text_at  -> ok
FAIL shallow rewind: just-evicted cue queryable again      -> ok
FAIL _seen matches exactly what is stored (pruned in step) -> ok
```

Controls that must NOT change stayed green both runs: store bounded
(5300 adds -> exactly 5000), oldest evicted / newest kept, in-store
dedupe still drops re-received stored cues, shift() dedupe coherence,
clear() resets the dedupe memory.

## Fix 2 — `text_at` abandoned still-active >60 s cues

**What changed** (`text_at`, `src/ui/caption_overlay.py:156-180`):

- New `self._max_span` (maintained O(1) in `add()`; reset on `clear()`;
  unchanged by `shift()` — clamping only shrinks windows) bounds every
  stored `end - start`.
- The backward scan's early break changed from the fixed
  `start < t - 60.0` to `start < t - _CUE_GRACE_S - self._max_span`.
  Cues are sorted by start but ENDS are not; an active cue `L` satisfies
  `start_L >= t - grace - max_span`, so no newer cue can trip the break
  before `L` is examined — provably safe, and in the common all-short-
  cues case the horizon is TIGHTER than 60 s (scan stops a few seconds
  back instead of 60).
- Overlap policy documented in the docstring (newest covering cue wins;
  greatest start, ties -> latest arrival) and pinned by tests.

**Fail before / pass after** (`test_cuestore.py` [3]):

```
FAIL long cue still paints under a dead newer cue (regression) -> ok
FAIL long cue paints at start + 70 ... up to end + grace      -> ok
FAIL deep long cue found under many newer cues                -> ok
```

Failure mechanism (reproduced before the fix): a dead short cue whose
start is >60 s behind `t` but NEWER-start than the long cue tripped the
break first, so the still-active 0->90 s cue was never examined and the
region went blank from t~60 on. Controls green both runs: lone long cue,
short-cue window, grace expiry at end+0.25, newest-wins.

## Tests run

| suite | result |
|---|---|
| `test_cuestore.py` (NEW, this package) | 28 passed / 0 failed, exit 0 — run twice, deterministic |
| `test_profanity.py` (bystander sanity) | all 54 checks passed |
| `test_fixes.py` (bystander sanity) | 51 passed / 0 failed |

Not run by me, by design: `test_caption_overlay.py` (1a editing it
concurrently — its [1] section's CueStore checks were verified by trace
to be unaffected), `test_vod_splitter.py` (1c editing), and the
adversarial harness (orchestrator runs it at merge; CueStore's public
surface — `add/clear/shift/text_at`, `cues` as sorted 3-tuples — is
unchanged, and `sync_stage3_run.py`/`test_sync_adversarial.py` only use
that surface).

## Not completed / notes

- Nothing outstanding in the brief. One judgment call to flag: fixing
  the rewind bug required changing the eviction policy (arrival order)
  in addition to the suggested `_seen` prune — rationale above; the
  forward-flow behavior is bit-identical to before.
