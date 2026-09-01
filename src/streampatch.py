"""Point Stremio's local "external player" button at MichaelTV.

How "open in VLC" actually works on Windows (found in the streaming
server the Stremio desktop install bundles, server.js): the server
advertises VLC as a cast-style device and, when Stremio web/app "plays"
on it, spawns the FIRST EXISTING of two hardcoded paths with the stream
URL plus VLC-style flags:

    "<vlc.exe>" --start-time=<sec> --no-video-title-show \
        [--sub-file=<stremio-downloaded.srt>] "<stream url>"

No file association is involved — that is why the .m3u route never saw
this click. This module rewrites ONLY the path list inside the local
server.js so that same button launches MichaelTV instead, and relabels
the device so Stremio's player menu honestly says "Play in MichaelTV"
(the device id stays "vlc", so the web app's POST is unchanged). VLC
itself — its executable, folders, registry entries — is never touched,
and a one-time backup lets anything restore the original
byte-for-byte.

Stremio updates replace server.js, so MichaelTV re-applies this at
every startup (idempotent); the patched server only takes effect after
Stremio (stremio-runtime / stremio-service) restarts once.
"""

import logging
import os
import shutil
import sys

log = logging.getLogger("mtp.streampatch")

# the exact vlc player block strings as they appear in server.js (raw
# file bytes)
_ORIGINAL = ("path: [ '\"C:\\\\Program Files (x86)\\\\VideoLAN\\\\VLC"
             "\\\\vlc.exe\"', '\"C:\\\\Program Files\\\\VideoLAN\\\\VLC"
             "\\\\vlc.exe\"' ]")
_TITLE_ORIG = 'title: "VLC"'
_TITLE_NEW = 'title: "MichaelTV" /*mtp*/'
_MARKER = "/*mtp*/"
_BACKUP_SUFFIX = ".mtpbak"


def _exe_path() -> str:
    """The player the Stremio button should launch."""
    if getattr(sys, "frozen", False):
        return sys.executable
    main_py = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "main.py")
    return "%s|%s" % (sys.executable, main_py)   # exe|script form


def _patched_line() -> str:
    """The replacement path list for server.js."""
    exe = _exe_path()
    if "|" in exe:      # dev: spawn "python" "main.py" <url>
        py, script = exe.split("|", 1)
        entries = "'\"%s\" \"%s\"'" % (py.replace("\\", "\\\\"),
                                       script.replace("\\", "\\\\"))
    else:
        entries = "'\"%s\"'" % exe.replace("\\", "\\\\")
    return "path: [ %s ] %s" % (entries, _MARKER)


def _server_js_candidates() -> list:
    out = []
    local = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Stremio\server.js")
    out.append(local)
    out.append(r"C:\Program Files\Stremio\server.js")
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub in (r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Uninstall",
                        r"Software\WOW6432Node\Microsoft\Windows"
                        r"\CurrentVersion\Uninstall"):
                try:
                    key = winreg.OpenKey(root, sub)
                except OSError:
                    continue
                with key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key,
                                                winreg.EnumKey(key, i)) as k:
                                name, _ = winreg.QueryValueEx(k,
                                                              "DisplayName")
                                loc, _ = winreg.QueryValueEx(
                                    k, "InstallLocation")
                        except OSError:
                            continue
                        if "stremio" in str(name).lower() and loc:
                            cand = os.path.join(str(loc), "server.js")
                            if cand not in out:
                                out.append(cand)
    except Exception:  # noqa: BLE001
        pass
    return out


def find_server_js() -> str:
    for cand in _server_js_candidates():
        if os.path.isfile(cand):
            return cand
    return ""


def is_patched(path: str = "") -> bool:
    """True when BOTH the path redirect and the menu-title relabel are
    in place (a v1-era file with only the path patch reads as
    unpatched, so startup re-patching upgrades it)."""
    path = path or find_server_js()
    if not path:
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    return (_MARKER in text and _TITLE_NEW in text
            and _ORIGINAL not in text and _TITLE_ORIG not in text)


def status() -> dict:
    """{server_js, patched, backup, titled, needs_stremio_restart} for
    the UI."""
    path = find_server_js()
    titled = False
    if path:
        try:
            with open(path, "r", encoding="utf-8",
                      errors="replace") as f:
                titled = _TITLE_NEW in f.read()
        except OSError:
            pass
    return {
        "server_js": path,
        "found": bool(path),
        "patched": is_patched(path),
        "titled": titled,
        "backup": bool(path) and os.path.isfile(path + _BACKUP_SUFFIX),
    }


def patch() -> bool:
    """Redirect the Stremio 'VLC' button at MichaelTV and relabel it
    "MichaelTV" in Stremio's player menu. Idempotent; upgrades the
    v1 (path-only) patch in place."""
    path = find_server_js()
    if not path:
        log.info("streampatch: no Stremio server.js found")
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        log.warning("streampatch: cannot read %s: %r", path, exc)
        return False
    if _MARKER in text and _patched_line() not in text:
        # patched for a different MichaelTV location — restore first
        if not _restore_text(text, path):
            return False
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    if _ORIGINAL not in text and _TITLE_ORIG not in text \
            and _MARKER not in text:
        log.info("streampatch: patch target not found in %s (Stremio "
                 "changed it?) — leaving it alone", path)
        return False
    backup = path + _BACKUP_SUFFIX
    if _ORIGINAL in text and not os.path.isfile(backup) \
            and _MARKER not in text:
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            log.warning("streampatch: backup failed: %r", exc)
            return False
    changed = False
    if _ORIGINAL in text:
        text = text.replace(_ORIGINAL, _patched_line())
        changed = True
    if _TITLE_ORIG in text:
        text = text.replace(_TITLE_ORIG, _TITLE_NEW)
        changed = True
    if changed:
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        except OSError as exc:
            log.warning("streampatch: write failed: %r", exc)
            return False
    if _MARKER in text and _TITLE_NEW in text:
        log.info("streampatch: Stremio's player menu now offers "
                 "\"Play in MichaelTV\" and it launches MichaelTV "
                 "(%s; restart Stremio to apply)", path)
        return True
    return False


def _restore_text(text: str, path: str) -> bool:
    backup = path + _BACKUP_SUFFIX
    if not os.path.isfile(backup):
        return False
    try:
        with open(backup, "r", encoding="utf-8", errors="replace") as f:
            original = f.read()
    except OSError:
        return False
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(original)
        return True
    except OSError:
        return False


def restore() -> bool:
    """Put Stremio's original server.js back."""
    path = find_server_js()
    if not path:
        return False
    if not _restore_text("", path):
        return False
    log.info("streampatch: restored original server.js")
    return True


def patch_if_needed() -> None:
    """Startup hook — never raises."""
    try:
        if not is_patched():
            patch()
    except Exception as exc:  # noqa: BLE001
        log.warning("streampatch: %r", exc)
