# P6 — Catch-Up window download: freeze root-caused + fixed, button restyle

User reports (8/25 follow-up): the windowed download button should be WHITE
at rest and GOLD only while engaged/downloading; and a window download froze
the stream once, with catch-up feeling generally less stable.

## Root cause of the freeze — measured, not guessed

The provider's timeshift backend REAPS SIBLING CONNECTIONS. Probes
(p6_concurrency_probe*.py, 2026-08-25):

- playback-paced connection alone: 200 MB, never cut,
- same connection + a concurrent window download (full speed OR throttled
  to 4 MB/s): one of the two connections is killed mid-body ~15-30 s in
  (4 kills / 6 trials; sometimes the download is the victim instead).

The old code had no recovery for that: the relay truncated the client
response, VLC froze/ended, catch-up has NO chase watchdog — the stream
stayed dead until the channel was changed (the player log shows exactly
this around the 10:40 download: program switched twice within 24 s of
"download finished ok=True"). FileDownloader additionally treated an
early EOF as SUCCESS — a killed download produced a truncated .ts
reported as a finished download.

## Fixes

1. **CatchupRelay byte-exact resume** (src/catchup_relay.py): an EOF/reset
   short of the promised Content-Length is bridged by re-dialing the
   provider from the exact next byte and continuing the SAME client
   response — VLC never notices the kill. Retries are progress-driven:
   they continue while dials deliver bytes, give up after 4 consecutive
   zero-byte dials or a starved trickle (6 dials < 64 KB). A failed
   re-dial counts as a zero-delivery attempt under the same policy.

2. **FileDownloader resume + truncation detection** (src/ui/worker.py):
   kills mid-body (IncompleteRead/reset/EOF) re-dial with
   `Range: bytes={done}-` and append; progress-driven stalls policy as
   above; a server that answers a resume with 200 fails LOUDLY instead
   of corrupting the file. Applies to VOD downloads too.

3. **Catch-up stall watchdog** (player_view._catchup_watchdog): if the
   relay could not bridge (provider gone), VLC is left frozen/ended —
   now `_tick` reopens the stream at the tracked position (frozen-clock
   detection needs a clock that moved at least once; a dead player needs
   4 ticks; the program-end margin never rescues; 5 s reopen cooldown;
   position corrected back by the freeze duration).

4. **Button restyle** (icons.py + player_view): `download_window()` is
   WHITE at rest like every other glyph, GOLD while the window markers
   are engaged, and a pinned-disabled GOLD while a download is in flight
   (an explicit Disabled pixmap keeps it from graying out).

## Verification

- Offline: test_catchup.py 96/96 (relay resume + give-up, downloader
  resume + refused-resume, icon states, watchdog frozen/stopped/cooldown/
  natural-end). Neighbors: vod_splitter 114, fixes 51, dvr_e2e 12,
  tab_resize 16, startup_defaults 15, vod_series live playback OK.
- LIVE (p6_live_resume_probe.py, p6_live_resume2.out): real relay + real
  headless VLC playing a past program WHILE FileDownloader pulls a 5-min
  window — download 193/193 MB ok, VLC clock advanced 4.3s -> 32.2s
  during and 34.3s -> 48.1s after: PROBE PASS.

## Unrelated finding (provider-side, 8/25)

Channel 395713 (NFL NETWORK HD) serves data that VLC's TS muxer writes as
ZERO sout bytes (direct reads deliver fine) — it starved test_dvr_e2e
identically on committed code. The e2e channel was switched to 1031378
(same network, muxes fine). Symptomatic of the "generally less stable"
provider days; the sibling-kill hardening above is the app-side answer.

## Scratch files

- p6_concurrency_probe{,2,3}.py — the kill/throttle/control experiments.
- p6_live_resume_probe.py (+ p6_live_resume2.out) — end-to-end proof.
