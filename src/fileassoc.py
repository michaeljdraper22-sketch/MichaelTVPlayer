"""Windows file-association glue for the Stremio handoff.

Stremio's only desktop external-player option ("M3U Playlist") downloads
a playlist.m3u and lets Windows open it with the .m3u handler. This
module puts MichaelTV into that path:

  * a ProgId + "Open with" entry for .m3u (always safe, per-user),
  * a Default-Programs "Capabilities" registration so the app is allowed
    to be the default,
  * a best-effort IApplicationAssociationRegistration::SetAppAsDefault
    call (the documented, hash-valid way installers set per-user
    defaults). If Windows still refuses, the UI falls back to showing
    the classic "Open with > Always" instructions.

VLC is never touched: we add ourselves, we never edit VLC's ProgIds,
associations or executables. All writes are under HKCU (no admin).
"""

import logging
import os
import sys

log = logging.getLogger("mtp.fileassoc")

PROGID = "MichaelTVPlayer.Playlist"
_EXT = ".m3u"
_APP_REG_NAME = "MichaelTV"
_AT_FILEEXTENSION = 0        # APPLICATION_ASSOCIATION_TYPE


def _launch_command() -> str:
    """What Windows should run for a playlist: the built exe with the
    file path as its argument (dev runs: python main.py <file>)."""
    if getattr(sys, "frozen", False):
        return '"%s" "%%1"' % sys.executable
    main_py = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "main.py")
    return '"%s" "%s" "%%1"' % (sys.executable, main_py)


def register() -> None:
    """Idempotent: (re)write our keys only where they differ."""
    if sys.platform != "win32":
        return
    try:
        import winreg
    except ImportError:
        return
    cmd = _launch_command()
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\Classes\%s" % PROGID) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ,
                              "MichaelTV Playlist")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\Classes\%s\DefaultIcon"
                                % PROGID) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ,
                              sys.executable + ",0")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\Classes\%s\shell\open\command"
                                % PROGID) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, cmd)
        with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\FileExts\%s\OpenWithProgids" % _EXT) as k:
            try:
                existing, _ = winreg.QueryValueEx(k, PROGID)
            except OSError:
                existing = None
            if existing is None:
                winreg.SetValueEx(k, PROGID, 0, winreg.REG_SZ, "")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\MichaelTVPlayer\Capabilities") as k:
            winreg.SetValueEx(k, "ApplicationName", 0, winreg.REG_SZ,
                              "MichaelTV")
            winreg.SetValueEx(k, "ApplicationDescription", 0, winreg.REG_SZ,
                              "VLC-powered IPTV / stream player")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\MichaelTVPlayer\Capabilities"
                                r"\FileAssociations") as k:
            winreg.SetValueEx(k, _EXT, 0, winreg.REG_SZ, PROGID)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\RegisteredApplications") as k:
            winreg.SetValueEx(k, _APP_REG_NAME, 0, winreg.REG_SZ,
                              r"Software\MichaelTVPlayer\Capabilities")
        log.info("fileassoc: registered ProgId + OpenWith for %s", _EXT)
    except OSError as exc:
        log.warning("fileassoc: registration failed: %r", exc)


def _user_choice_progid() -> str:
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\FileExts\%s\UserChoice" % _EXT) as k:
            value, _ = winreg.QueryValueEx(k, "ProgId")
            return str(value)
    except OSError:
        return ""


def is_default() -> bool:
    return _user_choice_progid() == PROGID


def try_set_default() -> bool:
    """Ask Windows (documented installer API) to make MichaelTV the
    per-user default for .m3u. Returns True on success."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import ctypes.wintypes as wt

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD),
                        ("Data3", wt.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        def guid(spec):
            d1, d2, d3, d4 = spec
            g = _GUID()
            g.Data1, g.Data2, g.Data3 = int(d1, 16), int(d2, 16), \
                int(d3, 16)
            for i, byte in enumerate(bytes.fromhex(d4)):
                g.Data4[i] = byte
            return g

        clsid = guid(("591209C7", "767B", "42B2",
                      "9FBA44EE4615F2C7"))       # ApplicationAssociationRegistration
        iid = guid(("4E530B0A", "E611", "4C77",
                    "A3AC9031D022F1E4"))          # IApplicationAssociationRegistration

        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 0x2)           # APARTMENTTHREADED
        ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 0x1,
                                    ctypes.byref(iid), ctypes.byref(ptr))
        if hr != 0 or not ptr:
            log.info("fileassoc: SetAppAsDefault unavailable (hr=0x%x)",
                     hr & 0xFFFFFFFF)
            return False
        try:
            iface = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))
            vtbl = ctypes.cast(iface[0], ctypes.POINTER(ctypes.c_void_p))
            # vtable slot 3 (after IUnknown's 3) = SetAppAsDefault
            fn = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p, wt.LPCWSTR, wt.LPCWSTR,
                ctypes.c_int)(vtbl[3])
            hr = fn(ptr, _APP_REG_NAME, _EXT, _AT_FILEEXTENSION)
            ok = hr == 0
            log.info("fileassoc: SetAppAsDefault hr=0x%x ok=%s",
                     hr & 0xFFFFFFFF, ok)
            return ok
        finally:
            try:
                release = ctypes.WINFUNCTYPE(
                    ctypes.HRESULT, ctypes.c_void_p, ctypes.c_ulong)(vtbl[2])
                release(ptr)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning("fileassoc: SetAppAsDefault failed: %r", exc)
        return False


def unregister() -> None:
    """Remove everything register() wrote (uninstaller / settings)."""
    if sys.platform != "win32":
        return
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

        _del(winreg.HKEY_CURRENT_USER, r"Software\Classes\%s\shell\open"
                                       r"\command" % PROGID)
        _del(winreg.HKEY_CURRENT_USER, r"Software\Classes\%s\shell\open"
                                       % PROGID)
        _del(winreg.HKEY_CURRENT_USER, r"Software\Classes\%s\shell" % PROGID)
        _del(winreg.HKEY_CURRENT_USER, r"Software\Classes\%s\DefaultIcon"
                                       % PROGID)
        _del(winreg.HKEY_CURRENT_USER, r"Software\Classes\%s" % PROGID)
        _del(winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\Explorer"
             r"\FileExts\%s\OpenWithProgids" % _EXT, PROGID)
        _del(winreg.HKEY_CURRENT_USER,
             r"Software\MichaelTVPlayer\Capabilities\FileAssociations")
        _del(winreg.HKEY_CURRENT_USER,
             r"Software\MichaelTVPlayer\Capabilities")
        _del(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications",
             _APP_REG_NAME)
        log.info("fileassoc: unregistered")
    except Exception as exc:  # noqa: BLE001
        log.warning("fileassoc: unregister failed: %r", exc)
