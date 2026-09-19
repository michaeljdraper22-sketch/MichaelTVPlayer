# MichaelTV — Android continuation handoff

> Paste this file (or point the new session at it) to continue the Android
> work. Written 2026-09-09 after the first working, release-signed,
> emulator-verified APK shipped; updated 2026-09-19 after the 2.1.2 APK
> (first-run setup banner) shipped and playback verification went green.
> Everything here is verified unless marked UNVERIFIED. Read the SAFETY
> RULES before doing anything.

## What exists (state as of the handoff)

Two Android app variants live in this repo; **only the second one ships**:

1. **`android/`** — Kivy + Buildozer variant. Complete app code, but its CI
   build is PARKED (`.github/workflows/android-build.yml`, manual dispatch
   only): 7 attempts, 7 distinct environment-era mismatches (libtool m4
   macros, NDK clang lib layouts, ffmpeg 7 removing `avfft.h`, bionic's
   API-26 `getgrent` guard, 2026 setuptools breaking the 2024 hostpython's
   distutils…). Full post-mortem in the workflow comments. Reviving it
   needs a locally-verified buildozer stack first; do NOT burn CI hours on
   it without one.

2. **`android-gradle/`** — **THE SHIPPING APP.** Gradle + Chaquopy 15.0.1
   (MIT, free) + AGP 8.1.4 + Gradle 8.4 + JDK 17 + Python 3.11 + WebView
   UI (no androidx). Embeds the Windows app's pure core verbatim:
   `prepare_core.py` copies `src/stremio.py`, `src/xtream.py`,
   `src/models.py` into `app/src/main/python/michaeltv_core/` at build
   time (git-ignored; the single source of truth stays `src/`).

   - UI: `app/src/main/assets/www/index.html` — one file, screens:
     Home/search, Detail (seasons/episodes), Streams (ranked list, same
     English-preference ordering as Windows), Player (`<video>` + external
     -player button), Settings (addon URLs, resolution pref, size demote,
     Xtream creds), Live TV (Xtream categories → channels).
   - Java: `MainActivity.java` — WebView + `JavaBridge`; every Python call
     goes through a **dedicated single-thread executor** (NOT the WebView
     JavaBridge thread — calls died there) and is wrapped in a Throwable
     catch that returns a diagnostic `{"error": "java <class>: <msg>"}`
     so failures NAME their cause on screen.
   - Python: `app/src/main/python/bridge.py` — `Bridge.rpc(cmd, argsJson)`
     returns JSON always, never raises. Commands: ping, get/set_settings,
     search, detail, streams_movie, streams_series, probe, xt_auth,
     xt_categories, xt_channels, xt_url.
   - Settings live in `getFilesDir()/settings.json`; the test-only
     `autoplay_test_url` key makes the app open the player on that URL at
     boot (used by CI; never shown in Settings UI).

## Key commands

```
python android-gradle/build_local.py        # full local build, ~1-4 min cold
                                            # (first run bootstraps .tools/,
                                            # ~1 GB, no admin). Produces
                                            # MichaelTV-<version>.apk
                                            # (release-signed) or -debug.apk
                                            # if no keystore.
gh workflow run "Android emulator test" --ref main     # cloud smoke test
gh run list --workflow=android-test.yml --limit 1
gh run download <run-id> -n android-test-artifacts -D .tmp-artifacts
# then READ .tmp-artifacts/shots/*.png with the image viewer and
# .tmp-artifacts/*.xml (uiautomator dumps = on-screen text)
gh release view v2.1    # release page state
```

The release APK install flow for the user: download from the release page,
allow "Install unknown apps", open. Same signing key (`michaeltv-release.jks`
+ `keystore.properties`, both git-ignored, generated on first build) means
updates install over the old copy — **never lose those two files**.

## Current release state (tag v2.1.2, published 2026-09-19)

- `MichaelTV-2.1.2.zip` + `.zip.sha256` — Windows build (parallel session's
  debrid auto-advance work; the in-app updater matches `.zip` assets only).
- `MichaelTV-2.1.2.apk` — the Android app, versionCode 212 (stamps from
  src/config.py APP_VERSION 2.1.2), release-signed with the same key, so it
  updates over the 2.1.1 install. sha256
  `0efc733bf703f7fa4514c077a4ebdccbae67bb7bdd2eab721189109190211ebf`.
  Carries the first-run setup banner + streams explainer (see priority 1a).
- The stale `MichaelTV-2.1.apk` asset (versionCode 21) was REMOVED from the
  release — Android refuses downgrade installs, so it was dead weight.
- Cloud emulator test GREEN on the exact HEAD that shipped: banner, Settings,
  Live TV, search ("Bluey" via typed query), and PLAYBACK in both H.264 and
  VP9 (frames-differ verdict). See priority 2 — the old "playback blocked"
  conclusion is RETIRED.

## ⚠️ PARALLEL SESSION WARNING

Another ZCode session works in this repo and has UNCOMMITTED changes in
`src/stremio.py` and `src/ui/player_view.py` (and an APP_VERSION bump to
2.1.1). Before ANY commit: `git status` and stage only YOUR files by exact
path. Never `git add -A`. Never revert their files. If you change anything
bundled into the Windows exe (`src/`, `main.py`, spec) you MUST run
`cmd //c build.bat` (~3 min) per AGENTS.md — but so far the Android work
has needed ZERO src/ changes; keep it that way where possible.

## SAFETY RULES (from the user, hard requirements)

- The user watches TV on this machine: never steal focus, never launch
  visible windows, keep audio silent, prefer headless verification.
- **NEVER use OS-level input automation** (SendInput/pyautogui/computer-
  use/keybd_event). A past incident left their keyboard/mouse broken.
  Driving the CLOUD emulator via adb/CI is fine (it is inside GitHub's VM).
  Local BlueStacks only if the user installs/enables it themselves; then
  use `adb connect 127.0.0.1:5555` and drive it purely via adb.
- If `dist\MichaelTV.exe` is running and a build needs it closed, ASK
  first — they are often watching.
- Sound cues per `C:\Users\micha\.zcode\AGENTS.md` (task start/complete/
  blocked .bat files) — they are REQUIRED every session.

## TOP PRIORITIES (in order)

1. **The phone "doesn't really work" report — mitigation SHIPPED (2.1.2),
   user fix still needed once.** Root cause (confirmed 2026-09-09): out of
   the box the phone app queries PLAIN torrentio (src/stremio.py:971
   fallback) — every entry is torrent-only and greyed; the keyed addon URL
   lives only in the desktop Stremio profile. DONE in 2.1.2: the Home
   screen carries a setup banner (shown while no addon URL has "=" in its
   path — the debrid-key signature — and no Xtream creds) walking through
   the torrentio+Torbox fix, and an all-torrent-only Streams list explains
   itself. The user still has to do the one-time paste: configure torrentio
   with their Torbox key in any browser, copy the addon URL, paste into the
   phone app's Settings — then every stream is a direct debrid link that
   plays in-app. Remaining code fixes, in order:
   a. FLAGSHIP: config export from the Windows app (QR code / share text
      with addon URLs sourced from discover_stream_addons + settings) —
      requires src/ changes, a build.bat run, and the user's go-ahead.
   b. Playback failures on real-device WebViews (mkv/HLS support varies):
      the external-player button covers this; consider detecting error
      events and prompting it automatically.
   c. Real-device verification (adb logcat from the user's phone) — the
      emulator path is now fully green, so any remaining failure is
      device-specific.
   NEVER commit/upload the user's real addon URLs or creds — the URLs
   embed the debrid API key (repo is public).

2. **Playback verification: RESOLVED (2026-09-19).** The 2.1.1-era
   "playback blocked on the emulator / ARM-translation suspect" was WRONG:
   the Big Buck Bunny sample URL
   (commondatastorage gtv-videos-bucket) now returns 403 Forbidden — the
   video element failed to LOAD (broken-media badge, controls at 0:00,
   zero codec errors in logcat). The test now:
   - builds an x86_64 DEBUG APK from HEAD on the runner (sed abiFilters,
     setup-python 3.11 buildPython, gradle 8.4 dist, prepare_core,
     assembleDebug) — tests HEAD natively instead of the release APK;
   - plays two verified-live public samples via autoplay_test_url
     (H.264: mdn.github.io/shared-assets/videos/flower.mp4; VP9:
     upload.wikimedia.org ... Schlossbergbahn.webm.480p.vp9.webm) and
     passes if either advances — both advanced on the green run, so the
     emulator WebView has full codec support and the app's whole
     autoplay→<video>→render pipeline is proven.
   NEVER reuse a sample URL without a fresh HEAD check (they rot).
3. **Never put the user's addon URLs / Xtream creds in the public repo or
   CI** — the addon URLs embed their debrid API keys. Test with public
   URLs only; inject real creds only via a local emulator/adb push.
4. Player parity (later): subtitles, resume positions, next-stream button
   (the logic is all in `stremio.py`, mirrored from Windows), search
   history.
5. Parked: Kivy variant revival (see workflow comments for the failure
   history before trying).

## Hard-won lessons (do not relearn these)

- **Chaquopy:** `PyObject.call(...)` invokes the object itself;
  `callAttr("name", ...)` calls the method. The first APK shipped with
  `.call("rpc", ...)` → `'bridge' object is not callable` on EVERY rpc.
- **WebView JS bridge:** JavascriptInterface methods must never throw —
  catch Throwable and return `{"error": "java ..."}` instead; run Python
  on your own executor thread, not the JavaBridge thread.
- **YAML + heredocs:** a heredoc terminator at column 0 in the FILE ends
  the YAML block scalar (indent it — YAML strips the common indent, so
  bash still sees it at column 0). Conversely, the local Bash tool eats
  one escape layer in `<< 'HEREDOC'` python bodies — verify written bytes
  (`cat -A`) before trusting an in-file patch, or use index-based line
  edits.
- **Emulator on GitHub runners:** pin `ANDROID_AVD_HOME=$HOME/.android/avd`
  for BOTH avdmanager and emulator (the runner's avdmanager writes
  elsewhere by default and the emulator dies with "Unknown AVD name");
  bound `adb wait-for-device` with `timeout 300` (it hangs forever);
  google_apis x86_64 images allow `adb root` (needed to push a
  settings.json into the app's private files dir after `am force-stop`).
  The whole build+boot+drive pipeline runs in ~6 min — fast iteration.
- **GMS kills bound apps (root cause of the historical drive flakiness):**
  when com.google.android.gms.persistent dies on the emulator,
  ActivityManager kills every app bound to its FontsProvider — including
  this WebView app ("depends on provider ... in dying proc"). Survive it
  by refocusing with `am start` on EVERY retry iteration (relaunches a
  dead task, no-ops a live one) and by re-establishing UI state (the
  search leg retries type→tap→poll as a unit; a kill mid-RPC loses the
  JS state along with the in-flight search).
- **uiautomator vs WebView, four traps:** (1) right after a cold start the
  WebView exposes only a bare `NAF` node — the DOM reaches the a11y tree
  seconds later, so dump-with-retry, never dump once at a fixed sleep;
  (2) piping `exec-out uiautomator dump /dev/tty` silently swallows
  "could not get idle state" failures — dump via /sdcard + adb pull
  (rm the stale file first!) so failures are visible and retryable;
  (3) the query field's placeholder is NOT exposed as text (EditText
  shows text="" when empty) so placeholder-based tapping can never focus
  it — tap coordinates from the field's observed a11y bounds
  ([35,347][811,457] on the pixel_5 AVD; remember the +136px status-bar
  offset when converting CSS layout math to device px; density is 440
  → 2.75, not the pixel_5 spec's 2.625); (4) assert on strings that
  exist ONLY inside the app's WebView — "MichaelTV" also matches the
  launcher icon label.
- **Sample media URLs rot:** the gtv-videos-bucket Big Buck Bunny mp4
  started returning 403 and cost multiple debugging rounds that wrongly
  blamed ARM translation and codecs. HEAD-check every sample URL before
  a run (and prefer mdn.github.io / upload.wikimedia.org, which are
  stable CDNs).
- **Windows updater safety:** it picks the first `.zip` asset containing
  "michaeltv" and the first `.sha256` asset — an `.apk` asset is safe,
  but never upload an `.apk.sha256` (it could be picked as the zip's
  checksum).
- **Version flow:** `prepare_core.py` derives versionName/versionCode from
  `src/config.py` APP_VERSION ("2.1.2" → 212). versionCode must only ever
  increase; a release page must never carry an APK with a versionCode
  lower than one already shipped (Android refuses downgrade installs —
  the stale MichaelTV-2.1.apk asset was removed for this reason).

## Files that matter

| Path | Role |
|---|---|
| `android-gradle/app/src/main/java/org/michaeltv/app/MainActivity.java` | WebView + JavaBridge + executor |
| `android-gradle/app/src/main/python/bridge.py` | 12-command JSON bridge over the shared core |
| `android-gradle/app/src/main/assets/www/index.html` | The whole UI (single file, inline JS/CSS) |
| `android-gradle/build_local.py` | One-command Windows build (tools, keystore, gradle, APK) |
| `android-gradle/prepare_core.py` | Copies src core + stamps version |
| `.github/workflows/android-test.yml` | Cloud emulator smoke + playback test |
| `scripts/tap_text.py` | uiautomator-driven tap helper for the test |
| `.github/workflows/android-build.yml` | PARKED Kivy CI (post-mortem inside) |
| `android/` | Parked Kivy variant source |
