# Task: convert this machine's daily MichaelTV run from the built EXE to a source launch (dev mode)

Work in `D:\Coding\MichaelTVPlayer`. The user wants to iterate on the app
without a 3-minute `build.bat` after every change, while keeping every
existing behavior. The analysis is DONE (2026-11-01/02, evidence in the
repo — summary below); your job is to implement, verify, and flip this
machine over. **Do not put the switch in the app's own UI — the user
explicitly rejected an in-app "launch mode" dialog. A small script or
checklist-style tool is the right shape.**

## Goals (the user's words, condensed)

- Watch TV through a launcher that runs the source tree, so code changes
  take effect on the next app relaunch — no EXE rebuild per iteration.
- Keep ALL current functionality, especially the Stremio **"Play in
  MichaelTV"** button behaving exactly as it does today.
- EXE and APK releases still get built and shipped to GitHub **only when
  the user asks**.
- The switch must be reversible (back to the dist EXE) and must not need
  admin.

## What the analysis already established (don't redo it)

The codebase is dual-mode by design — every Windows integration has a
`sys.frozen` branch with a working source-run fallback:

- `src/streampatch.py` — the Stremio button. server.js is patched with ONE
  existing file path (Stremio's `fs.existsSync` gate; launch is
  `child.exec`, a plain shell string). Dev runs already auto-write
  `stremio-handoff-dev.cmd` relaying into `python main.py %*` and re-point
  server.js on every app start (the flip between dist/dev targets is
  self-healing — see `is_patched()`).
- `src/fileassoc.py` — `.m3u` + video "Open with" ProgId. Re-registers
  every startup; `_launch_command()` already emits
  `"python" "main.py" "%1"` for source runs.
- `src/singleinst.py` — named local socket `"MichaelTVPlayer-single"`,
  shared by both modes (dev + exe instances even forward args to each
  other; no double-instance risk while mixing).
- `src/ui/stremio_dialog.py` — the elevated `--apply-stremio-patch`
  one-shot already has a dev branch (`python.exe "main.py" --apply-…`).
- `main.py` `_setup_bundled_vlc()` / `_install_dir()` — repo `vlc\` is
  found when running from source; `src/live_cc.py` finds the vendored
  CCExtractor in `vendor\`.
- `src/updater.py` — self-update refuses gracefully from source runs
  ("no MichaelTV.exe beside the running app"); releases unaffected.

### The four real gaps (the work)

1. **Console python leaks.** Everything registers `sys.executable` — if
   the app is launched with console `python.exe`, a console window
   attaches for the whole session (Stremio's `child.exec` of a `.cmd`
   waits on it). ALWAYS launch via `.venv\Scripts\pythonw.exe`, and make
   the Stremio handoff a **hidden wscript `.vbs` relay** instead of the
   `.cmd` (`WScript.Shell.Run "…pythonw…main.py… args", 0, False`) —
   zero flash. The old `.cmd` approach is what `streampatch._dev_launcher()`
   writes today; upgrade it, and remember server.js needs the patched
   path to remain ONE existing file (a `.vbs` qualifies).
2. **Shortcuts.** No code creates them; the user's Start-menu/desktop
   `.lnk`s point at `dist\MichaelTV.exe` (currently found:
   `Desktop\MichaelTV.lnk`). Repoint to Target
   `…\.venv\Scripts\pythonw.exe`, Arguments `"<repo>\main.py"`,
   WorkingDirectory `<repo>`, Icon `assets\icon.ico` (PowerShell
   `WScript.Shell` COM works — round-trip verified 2026-11-01).
3. **Icon regression.** `fileassoc` registers `sys.executable + ",0"` as
   the ProgId icon → Python's feather in every "Open with" menu in dev
   mode. Point `DefaultIcon` at `assets\icon.ico` when present.
4. **Process rules.** Update `AGENTS.md`: in dev mode "ship it" = tell
   the user to relaunch (no build); dist/auto keeps the current
   build-always rule; releases on request only. Also consider a periodic
   staging build (or build-before-each-release) so the PyInstaller path
   can't rot unnoticed while daily driving from source.

### Known pitfalls

- Don't break the server.js patch flow: `is_patched()` byte-compares
  `_patched_line()`; changing the dev target automatically restore-first
  re-patches (tested behavior from the 2026-09-22 missing-button bug).
- Repo root from the frozen exe = parent of `dist\` — validate by
  `main.py` + `.venv\Scripts\pythonw.exe` + `vlc\` existing, else dev
  mode is unavailable (that's the end-user case; feature must no-op).
- A restart is still required per iteration (no hot reload) — fine.
- `pythonw` has no stdout: logging already goes to
  `%APPDATA%\MichaelTVPlayer\player.log` (the exe runs windowless the
  same way — proven).

## Verification (minimum)

1. Offline suites still green (`test_wedge_cluster.py`, `test_fixes.py`,
   `test_stremio_panel.py`, `test_always_chase.py`, …).
2. New offline test for the mode switch itself (fake registry for
   `fileassoc` overrides — inject `sys.modules["winreg"]`; temp-dir
   `.lnk` round-trip; `.vbs` content checks). Follow the standalone
   `test_*.py` convention.
3. LIVE (ask the user to close the app first; never kill MichaelTV.exe
   silently — AGENTS.md): flip to dev, relaunch from the user's own
   shortcut → app runs (log shows source run), taskbar icon correct,
   single-instance still enforced (second launch forwards), a Stremio
   "Play in MichaelTV" click starts the source run with no console
   flash and correct `--start-time`, an "Open with" .m3u click works,
   then flip back to dist and repeat the Stremio click.
4. Then `cmd //c build.bat` once so dist matches the source (and to
   prove the exe path still builds).

## Out of scope / open threads (do NOT touch here)

- The RedZone slow-mo/rewind blur is NOT fixed: GOP-anchored seeks ARE
  active (`chase seek … gop=` lines in player.log from the 2026-11-01
  18:13–18:34 session) yet the user reports no improvement — a second
  cause exists (suspect: damaged GOPs written into the live buffer by
  provider cuts, or a slow-motion-specific artifact — the pure
  rate-change pixel test was never completed; see
  `tools/gop_pixel_probe.py` Case D). Another session may already be on
  it (`test_rebase_wedge.py` appeared 2026-11-02) — check the git log
  before starting anything in that area.
