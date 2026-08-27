# MichaelTVPlayer — agent rules

## Ship it means SHIP IT IN THE EXE

The user runs `dist\MichaelTV.exe` (Start menu + desktop shortcut both point
there — verify with the shortcut target if unsure). They do NOT run from
source. A fix that is only committed to `src/` is, from the user's point of
view, NOT DONE. They have been repeatedly annoyed by having to point this out.

Therefore, after ANY change to files bundled into the app (`src/`, `main.py`,
spec datas):

1. Run `cmd //c build.bat` (takes ~3 min; PyInstaller + uninstaller + VLC copy
   + release zip).
2. If PyInstaller fails with `PermissionError ... dist\MichaelTV.exe`, the
   player is running — ask the user before killing it, they are often watching
   TV. Never kill it silently.
3. Tell the user to relaunch the app afterward.

This step is NOT optional, even for "tiny" UI tweaks.
