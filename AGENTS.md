# MichaelTVPlayer — agent rules

## How this machine launches MichaelTV — CHECK BEFORE "SHIPPING"

Since 2026-11-02 this machine daily-drives the DEV/SOURCE launch: the
shortcuts run `.venv\Scripts\pythonw.exe "main.py"`, so code changes take
effect on the next app relaunch. The mode is flipped (reversibly, no
admin) by:

    powershell -NoProfile -ExecutionPolicy Bypass -File tools\launch_mode.ps1            # status
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\launch_mode.ps1 -Mode dev
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\launch_mode.ps1 -Mode dist

Run the status line first if unsure which mode is active. What "ship it"
means depends on the mode:

### Dev mode (this machine's default)
- After ANY change (`src/`, `main.py`, spec datas): NO build. Tell the
  user to close and relaunch MichaelTV — the relaunch runs the new code.
- Still run the relevant offline `test_*.py` suites as usual.
- Never leave a console `python main.py` session as the player — the
  shortcuts/handoffs all use windowless pythonw for a reason (console
  python attaches a console window for the whole session).

### Dist mode / EXE + APK releases
- Build ONLY when the user asks for it (a release, or a switch back to
  dist mode): run `cmd //c build.bat` (takes ~3 min; PyInstaller +
  uninstaller + VLC copy + release zip), then tell the user to
  relaunch. NO periodic/preventive rebuilds — the user decided
  2026-11-02 that a 3-minute build on demand beats keeping the exe
  around (dist\MichaelTV.exe was deliberately deleted so taskbar search
  can never launch it by accident; its absence is EXPECTED).
- EXE and APK releases ship to GitHub ONLY when the user asks.
- Because builds are now rare, the first requested build after a long
  dev stretch may surface PyInstaller rot — fixing that is part of
  that build, not a reason to schedule builds.

### Always
- If PyInstaller fails with `PermissionError ... dist\MichaelTV.exe`,
  the dist player is running — ask the user before killing it, they are
  often watching TV. Never kill it silently. Same rule for a running
  pythonw/python instance of the app.
- After a mode flip, Stremio's "Play in MichaelTV" button and the .m3u
  "Open with" entry re-point automatically on the next app start, but a
  RUNNING Stremio only picks the change up after it restarts once.
