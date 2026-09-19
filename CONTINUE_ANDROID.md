# MichaelTV — Android continuation handoff

> Paste this file (or point the new session at it) to continue the Android
> work. Written 2026-09-09 after the first working, release-signed,
> emulator-verified APK shipped. Everything here is verified unless marked
> UNVERIFIED. Read the SAFETY RULES before doing anything.

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

## Current release state (tag v2.1)

- `MichaelTV-2.1.zip` + `.sha256` — latest Windows build (the in-app
  updater matches `.zip` assets only; APKs on the same release are ignored
  by it — verified in `src/updater.py`).
- `MichaelTV-2.1.1.apk` — the Android app. Version 2.1.1/versionCode 211:
  `prepare_core.py` read the working tree's `APP_VERSION` which the
  PARALLEL SESSION had bumped to 2.1.1 (see warning below). sha256
  `554417e357a247f4edbe4c695959072a2a716571d3bee36d843ad77795f86484`.
- Emulator test verified green on the previous 2.1 build (Settings screen
  renders real Python data; Live TV shows its designed
  "No Xtream account configured" prompt; zero PyException in logcat).
  A playback leg (public Big Buck Bunny mp4 via `autoplay_test_url`,
  two screenshots 12 s apart must DIFFER) was added for 2.1.1.

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

1. **The user reports the app "doesn't really work" on their phone.**
   ROOT CAUSE CONFIRMED (2026-09-09, from the local machine's facts):
   out of the box the phone app queries PLAIN torrentio — every entry is
   torrent-only and greyed (no local Stremio server exists on a phone).
   The desktop player's own settings.json carries only plain torrentio
   too (verified with a redacted check) and no Xtream creds — the desktop
   flows that actually play work either through handoff URLs from the
   desktop Stremio app (which embed the debrid resolve links) or through
   the LOCAL Stremio server at 127.0.0.1:11470; neither exists on the
   phone. The keyed addon URL lives in the desktop Stremio profile
   leveldb (src/stremio_profile.discover_stream_addons, imported via the
   Stremio dialog's Import button).
   THE IMMEDIATE USER FIX (no code): configure torrentio with their
   Torbox API key in any browser (torrentio's setup page), copy the
   resulting addon URL (it embeds the key), paste it into the phone
   app's Settings — then every stream comes back as a direct debrid
   link that plays in-app.
   Code fixes worth building, in order:
   a. A friendlier empty state: when no keyed addon is configured, show
      a first-run banner explaining the above instead of a list of greyed
      rows.
   b. FLAGSHIP: config export from the Windows app (QR code / share
      text with addon URLs sourced from discover_stream_addons +
      settings) — requires src/ changes, a build.bat run, and the user's
      go-ahead since it touches the Windows program.
   c. Playback failures on real-device WebViews (mkv/HLS support varies):
      the external-player button covers this; consider detecting error
      events and prompting it automatically.
   d. Crashes on their specific device — get adb logcat output or the
      on-screen diagnostic text (it now names the exception).
   NEVER commit/upload the user's real addon URLs or creds — the URLs
   embed the debrid API key (repo is public).

2. **Playback verification on the emulator is BLOCKED (2026-09-19).**
   Status: the player screen opens via autoplay_test_url, the <video>
   element fires its error event ("Unable to play media."), and the
   frames-must-differ verdict correctly fails the run — the safety net
   works, playback itself does not start on the emulator. Ruled out:
   -no-audio (removed, same result) and codec errors (logcat clean).
   Prime suspect: the app runs ARM-TRANSLATED on the x86_64 image, and
   Chromium's WebView renderer/media stack runs partly inside the app
   process, where translation can break media init. Real arm64 phones
   with hardware codecs likely do NOT share this limitation.
   Unblock options for the next session, best first:
   a. CI-only x86_64 build: in the test job, sed app/build.gradle's
      abiFilters to "x86_64", build :app:assembleDebug with the runner's
      preinstalled JDK 17 + SDK (download gradle-8.4-bin.zip, run gradle
      with JAVA_HOME set; Chaquopy buildPython: setup-python 3.11), and
      test THAT APK — no translation involved.
   b. User installs BlueStacks locally (their choice) with ADB enabled;
      drive it via adb connect 127.0.0.1:5555 — still quiet, no OS input
      automation needed.
   c. The user's real phone over USB with adb logcat for ground truth.
   ALSO: the test's search leg is flaky — the app sometimes exits during
   the drive (04_search screenshots show the launcher). Re-focus with
   am start before EACH leg and consider dump-asserting the screen name.
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
  google_apis x86_64 images run arm64-only APKs via ARM translation and
  allow `adb root` (needed to push a settings.json into the app's private
  files dir after `am force-stop`).
- **Windows updater safety:** it picks the first `.zip` asset containing
  "michaeltv" and the first `.sha256` asset — an `.apk` asset is safe,
  but never upload an `.apk.sha256` (it could be picked as the zip's
  checksum).
- **Version flow:** `prepare_core.py` derives versionName/versionCode from
  `src/config.py` APP_VERSION ("2.1.1" → 211). versionCode must only ever
  increase.

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
