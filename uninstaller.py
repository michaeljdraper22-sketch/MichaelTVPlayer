"""MichaelTV uninstaller (shipped as UninstallMichaelTV.exe next to the app).

Built with PyInstaller (see build.bat): a tiny windowless exe whose only
job is to remove the app cleanly:

  1. asks before doing anything,
  2. stops a running MichaelTV,
  3. removes Start Menu / Desktop shortcuts,
  4. optionally removes the user's settings + Xtream login
     (%APPDATA%\\MichaelTVPlayer) — NO by default, so logins/settings
     survive an uninstall-then-reinstall,
  5. deletes the install folder (including itself, via a delayed cmd).

No Qt dependency: the two questions are native MessageBoxW dialogs, so the
exe stays small and starts instantly.
"""

import ctypes
import os
import subprocess
import sys
import tempfile

MB_YESNO = 0x04
MB_ICONQUESTION = 0x20
MB_DEFBUTTON2 = 0x100
IDYES = 6


def ask(hwnd, text, caption, style=MB_YESNO | MB_ICONQUESTION):
    return ctypes.windll.user32.MessageBoxW(
        hwnd, text, caption, style) == IDYES


def main():
    hwnd = None
    if not ask(hwnd,
               "Uninstall MichaelTV?\n\n"
               "This closes the app if it is running and removes it from "
               "this computer.", "Uninstall MichaelTV"):
        return 0

    keep_data = not ask(
        hwnd,
        "Also delete your personal data?\n\n"
        "Settings, Xtream login, favorites and recent items "
        "(%APPDATA%\\MichaelTVPlayer).\n\n"
        "Choose No to keep them for a future reinstall.",
        "Uninstall MichaelTV",
        MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2)

    # stop a running instance (silently — a live libvlc would keep files
    # locked and the folder delete would fail halfway)
    try:
        subprocess.run(
            ["taskkill", "/IM", "MichaelTV.exe", "/F"],
            capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        pass

    # shortcuts (Start Menu + both Desktop locations)
    appdata = os.environ.get("APPDATA", "")
    for lnk in (
        os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                     "Programs", "MichaelTV.lnk"),
        os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                     "Programs", "MichaelTVPlayer", "MichaelTV.lnk"),
        os.path.join(os.path.expanduser("~"), "Desktop", "MichaelTV.lnk"),
        os.path.join(os.environ.get("PUBLIC", "C:\\Users\\Public"),
                     "Desktop", "MichaelTV.lnk"),
    ):
        try:
            if os.path.isfile(lnk):
                os.remove(lnk)
        except OSError:
            pass

    # restore Stremio's original server.js if we redirected its
    # "open in VLC" button at MichaelTV (streampatch keeps a .mtpbak)
    try:
        sj = os.path.expandvars(
            r"%LOCALAPPDATA%\Programs\Stremio\server.js")
        bak = sj + ".mtpbak"
        if os.path.isfile(bak) and os.path.isfile(sj):
            import shutil
            shutil.copy2(bak, sj)
            os.remove(bak)
    except Exception:  # noqa: BLE001
        pass

    # registry: the Stremio-handoff .m3u registration (self-contained —
    # the uninstaller exe does not bundle the src package). If we are the
    # current default, clear the UserChoice too so nothing dangles.
    try:
        import winreg

        def _del(root, path, value=None):
            try:
                if value is None:
                    winreg.DeleteKey(root, path)
                else:
                    with winreg.OpenKey(root, path, 0,
                                        winreg.KEY_SET_VALUE) as k:
                        winreg.DeleteValue(k, value)
            except OSError:
                pass

        progid = "MichaelTVPlayer.Playlist"
        uc = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
              r"\FileExts\.m3u\UserChoice")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uc) as k:
                cur, _ = winreg.QueryValueEx(k, "ProgId")
            if cur == progid:
                _del(winreg.HKEY_CURRENT_USER, uc)
        except OSError:
            pass
        _del(winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\Explorer"
             r"\FileExts\.m3u\OpenWithProgids", progid)
        for sub in (r"\shell\open\command", r"\shell\open", r"\shell",
                    r"\DefaultIcon", ""):
            _del(winreg.HKEY_CURRENT_USER,
                 r"Software\Classes\%s%s" % (progid, sub))
        _del(winreg.HKEY_CURRENT_USER,
             r"Software\MichaelTVPlayer\Capabilities\FileAssociations")
        _del(winreg.HKEY_CURRENT_USER,
             r"Software\MichaelTVPlayer\Capabilities")
        _del(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications",
             "MichaelTV")
    except Exception:  # noqa: BLE001
        pass

    if not keep_data:
        cfg = os.path.join(appdata, "MichaelTVPlayer")
        try:
            import shutil
            shutil.rmtree(cfg, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    exe = os.path.abspath(sys.argv[0]) if getattr(sys, "frozen", False) \
        else sys.executable
    install_dir = os.path.dirname(exe)
    # delete the install folder AFTER this exe has exited — a detached
    # cmd waits, then removes everything including UninstallMichaelTV.exe
    try:
        subprocess.Popen(
            ["cmd", "/c",
             "timeout /t 3 /nobreak >nul & rd /s /q "
             f'"{install_dir}"'],
            close_fds=True, creationflags=0x00000008)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
