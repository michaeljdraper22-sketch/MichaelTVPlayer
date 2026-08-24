# WP4b — vendored a streaming-capable CCExtractor (D3)

**Date:** 2026-08-23 · **Scope:** `vendor/`, `src/live_cc.py`,
`MichaelTVPlayer.spec`, `test_bundled_ccx.py`, `test_caption_overlay.py`
(one guard). Everything offline/headless on the repo's local test
recording (`TV Recordings/US_NFL_NETWORK_HD_20260820_171631.ts`, 54 MB,
~70 s, CEA-608 in the H.264 SEI **and** a DVB-subtitle track).

## Outcome

Zero-install machines now get live captions through a vendored
CCExtractor **0.96.6** (win portable, minimal runtime subset, 165.1 MiB
in `vendor/`): the 0.88 fail-fast is gone, the bundled binary streams
SRT on stdout while its stdin is an open growing stream, and a
simulated zero-install engage delivers cues through the real `CCSource`
pipeline. Acceptance is fail-before/pass-after in
`p4b_stream_before.out` / `p4b_stream_after.out`.

Three things beyond the prompt's script were forced by measurements:

1. The modern build **requires `--no-codec dvbsub`** on these streams
   (without it CCX OCRs DVB bitmaps and — with no `tessdata` vendored —
   finds "no captions" at all). `ccx_args()` now returns one modern
   form for the bundled AND the installed binary.
2. A **latent production bug surfaced**: `CCSource._read_stdout` used
   buffered `read(4096)`, which blocks until the full 4096 bytes
   arrive; the old DVB-OCR output density masked it, the clean 608
   output (~2 KB / 13 s) starved cue delivery in ~20 s lumps. Fixed
   with `read1(4096)` (single raw read). The sync harness wedged
   before this fix (`p4b_harness_smoke.out`) and passes after
   (`p4b_harness_smoke2.out`, scenario a 7/7).
3. The **0.88 historical record was wrong** in one detail: 0.88 *does*
   stream through pipes with its legacy single-dash flags (4,504 SRT
   bytes after 2 MiB fed, stdin open — reproduced twice). The "30 MB
   piped, 0 B out until close" measurement does not replicate through
   Python pipes; it most likely observed stdout redirected to a *file*
   (fully buffered). 0.88 still cannot serve the modern pipeline — it
   rejects `--stdin`/`--no-codec` outright (`Parameter --stdin not
   understood`, rc=4) — so the vendoring was needed regardless.

## 1. Inventory and sizes

Source: official `CCExtractor.0.96.6_win_portable.zip` (GitHub release
v0.96.6, x64, 88,026,547 bytes, SHA-256
`c9b14cd356c7ada66b294426ba25c52d7654396a31b8b115b789e4d5e73f62bc`;
binary self-reports 0.96.5 / git `185631d`, byte-identical to the
local MSI install). The zip is a Flutter-GUI distribution, 221 MB
uncompressed:

| Not vendored | size | why |
|---|---|---|
| avcodec-60.dll → *(kept — see below)* | 74.7 MB | load-required |
| flutter_windows.dll + data/ + ccxgui.exe + 6 plugin DLLs | ~43 MB | GUI only |
| tessdata/ (eng + osd) | 14.7 MB | only for DVB-OCR — the app forces `--no-codec dvbsub` |
| msvcp140.dll, vcruntime140_1.dll | 0.6 MB | not in the import closure |

**Vendored set (173,079,680 bytes ≈ 165.1 MiB):** `ccextractorwinfull.exe`
(32.1 MB) + `libgpac.dll` (11.8 MB) + the ffmpeg set the loader pulls
through libgpac's static imports: `avcodec-60` (74.7 MB),
`avformat-60` (17.3), `avfilter-9` (25.4), `avdevice-60` (4.6),
`avutil-58` (2.3), `swscale-7` (0.64), `swresample-4` (0.64),
`postproc-57` (0.09), plus `libcryptoMD` (2.4), `libsslMD` (0.49),
`OpenSVCDecoder` (0.37), `vcruntime140` (0.12). Per-file SHA-256s in
`vendor/CCEXTRACTOR-VENDORED.txt`.

Derivation: the exe's only non-system import is `libgpac.dll`; libgpac
statically imports the whole ffmpeg set (Windows loads static imports
eagerly). Verified empirically: exe alone → exit 127; minus
`libsslMD.dll` → 127; minus `avfilter-9.dll` → 127; full set → runs.
(`vcruntime140.dll` also resolves from System32 on this machine but is
vendored for true zero-install machines.) This *is* the minimal
runnable subset of the official distribution — the prompt's "exe +
required ffmpeg/etc DLLs" — but it is 165 MiB, not smaller: no official
leaner CLI artifact exists for Windows in this release (checked the
release's asset list; the MSI carries the same files, byte-identical).

## 2. The `--no-codec dvbsub` discovery (load-bearing)

On this ATSC-style stream the modern build's default behavior is to
latch onto the PMT's DVB-subtitle PID and OCR the bitmaps:

| mode | captions | time (70 s TS) | output shape |
|---|---|---|---|
| default, no `tessdata` | **none** ("No captions were found", rc=10, 2-3 s bail) | — | — |
| default, with `tessdata` | OCR'd | **70-88 s (~1x realtime!)** | 704 micro-cues, 32-ms windows, font tags, OCR errors ("! MEAN" for "I MEAN") |
| `--no-codec dvbsub` | CEA-608 from SEI (`NAL_type_7: 88`) | **2 s (~35x)** | ~50 natural cues, same text as 0.88's output |

Same content, different axes: the vendored set ships no `tessdata`, so
without the flag the bundled binary is useless on exactly the app's
target streams. `ccx_args()` therefore returns
`["-in=ts","-srt","-utf8","--stdin","--stdout","--no-codec","dvbsub"]`
for everyone. Note for P5: **the user-installed 0.96.5 has been running
the OCR path in every live session to date** (same default behavior
with the MSI's tessdata present) — the "~1x parse speed is physics"
note in the attack plan was actually OCR cost. With this change the
installed path also switches to the 608 SEI path (~35x faster, cleaner
cues, no `&gt;&gt;` HTML entities — those came from the OCR output;
`SrtParser` strips the font tags either way). Accepted tradeoff:
streams carrying captions *only* as DVB bitmaps (non-US providers) lose
OCR captions; an installed CCX older than the `--no-codec` flag
rejects it and the app falls back to VLC rendering (same graceful
degradation the 0.88 fail-fast had).

## 3. Streaming acceptance (fail-before / pass-after)

The regression lives in `test_bundled_ccx.py` (15 checks):
a growing-stream probe (1 MiB appends, 50 ms pauses, stdin never
closed, byte-at-a-time reader so partial buffering can't fake a
result) plus a **zero-install engage** — `find_ccextractor` pinned to
the bundled path, a temp DVR buffer seeded at 4 MiB and grown while
`CCSource` tails it — requiring `start()` to succeed and cues to arrive.

- **Before** (`p4b_stream_before.out`, old tree): 6 checks FAIL (exe,
  DLL set, discovery path, args x2, runtime presence), plus the
  captured old behavior: `start() -> False | failed ->
  ['bundled CCExtractor 0.88 cannot stream']`.
- **After** (`p4b_stream_after.out`): all 15 pass — first SRT on
  stdout after 2 MiB fed with stdin open; zero-install engage delivers
  cues (31 on the recorded run) with zero failure signals.
- Cross-checks: mid-file join (3.1 MB offset, no PAT at head) also
  streams with the new args; the second repo recording (94 MB) yields
  6,003 bytes of clean 608 SRT (`NAL_type_7: 216`) in 2 s.

## 4. The `read(4096)` → `read1(4096)` fix

First harness smoke after the rewiring wedged: CCX alive, zero cues,
calibration grinding (`p4b_harness_smoke.out`). Standalone `CCSource`
repros were green, isolating the difference to output *density*:
`BufferedReader.read(4096)` blocks until the full count. OCR output
(~25 KB / 13 s) filled blocks in seconds; clean 608 output (~2 KB /
13 s) makes the reader stall in ~20 s lumps. The starve-guard's own
comment ("CCX's stdout flushes in ~4 KB blocks") was calibrated on OCR
density. `read1(4096)` returns whatever one raw read gives. After the
fix: harness `--quick --only:a` **7/7** and `--only:g` **6/6**
(`p4b_harness_smoke2.out` holds the scenario-a run). Full-harness
re-baselining stays with P5 — cue cadence changed for the installed
path too (see §2).

## 5. Build / spec deltas

`MichaelTVPlayer.spec`: the single `vendor/ccextractorwin.exe` datum
became an explicit 16-entry vendor list (14 binaries + GPL text +
provenance file), all in the `vendor` tree of the bundle. Verified
with a real build (`p4b_build.out`, separate work/dist paths so the
existing `dist/` was untouched): build succeeds;
`dist_p4b/MichaelTV.exe` = **163,069,426 bytes vs the previous
42,265,943** (+120.8 MB — the 165 MiB payload compresses ~5:4 in the
onefile). `pyi-archive_viewer -l` confirms all 16 vendor entries
inside the bundle with correct uncompressed sizes; the DLLs land next
to the exe in `_MEIPASS\vendor\`, satisfying the loader's
exe-dir-first search in every layout. License: the portable zip ships
no license file, so `vendor/COPYING-ccextractor.txt` keeps the GPL-2
text and `vendor/CCEXTRACTOR-VENDORED.txt` documents provenance,
inventory, SHA-256s, and the GPL-2.0+ status.

## 6. Test results (final tree)

| suite | result |
|---|---|
| `test_bundled_ccx.py` | all 15 checks pass (twice) |
| `test_fixes.py` | 51 passed, 0 failed |
| `test_profanity.py` | all 54 checks pass |
| `test_caption_overlay.py` | 138 passed, 0 failed (incl. the flipped "bundled CCX accepted for live streaming" guard) |
| `test_sync_adversarial.py --quick --only:a` | 7/7 (post read1-fix) |
| `test_sync_adversarial.py --quick --only:g` | 6/6 |

`test_profanity` has a pre-existing intermittent exit-time segfault
under offscreen Qt (~1 in 3 runs, *after* printing results) —
reproduced with a pristine `live_cc.py`, i.e. not from this package.

## 7. Deviations from the prompt, honestly

- The vendored subset is 165 MiB, not "small": it is the true minimum
  for the official binary; recorded above so the size decision is
  reviewable. Git adds ~90 MB compressed objects; dist grows +121 MB.
- `--no-codec dvbsub` is a behavior change for the *installed* CCX
  path too (unified args), justified in §2; DVB-only-stream OCR loss
  accepted (US 608 target); flagged for P5's live verification.
- The `read1` fix (§4) went slightly beyond the named file scope of
  wiring, but without it the package's own acceptance (live cues
  through the pipeline) fails at real 608 data rates.
- 0.88's pipe-streaming correction (§0/outcome 3) is recorded for the
  attack-plan history; the fail-fast's *conclusion* (0.88 unusable)
  stands via flag rejection, not via buffering.
