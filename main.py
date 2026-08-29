"""MichaelTVPlayer - a VLC-powered IPTV player for Xtream (Xtream Codes) accounts."""

import glob
import logging
import os
import shutil
import sys
import tempfile
import time

from PyQt5 import QtCore, QtGui, QtWidgets


APP_NAME = "MichaelTV"   # display name (config/log dirs stay MichaelTVPlayer)

log = logging.getLogger("mtp")


def _is_frozen() -> bool:
    """True when running from the PyInstaller-built MichaelTVPlayer.exe."""
    return bool(getattr(sys, "frozen", False))


def _bundle_dir() -> str:
    """Where the read-only app resources live (script dir, or the PyInstaller
    onefile unpack dir)."""
    if _is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _install_dir() -> str:
    """Where the .exe (or script) lives — writable sidecar files go here."""
    if _is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _setup_windows_identity() -> None:
    """Give the process an explicit AppUserModelID so the taskbar shows the
    app's own icon (not python.exe's) and windows group correctly."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "MichaelTV")
        except Exception:  # noqa: BLE001
            pass


_vlc_dll_dir_cookie = None   # keeps the add_dll_directory() handle alive


def _setup_bundled_vlc() -> None:
    r"""Use the private VLC runtime shipped in a ``vlc`` folder next to the
    exe (a full copy of a VLC install: libvlc.dll, libvlccore.dll, plugins\).
    This keeps MichaelTV completely isolated from the user's installed VLC —
    own DLLs, own plugins, and (with ``--no-config`` on every vlc.Instance)
    no reads or writes of the shared ``%APPDATA%\vlc`` config.
    Without such a folder the installed VLC is used, as before."""
    global _vlc_dll_dir_cookie
    vlc_dir = os.path.join(_install_dir(), "vlc")
    if not os.path.isfile(os.path.join(vlc_dir, "libvlc.dll")):
        return
    # PYTHON_VLC_LIB_PATH is the only knob python-vlc honors for the actual
    # DLL load; VLC_PLUGIN_PATH (the env var libvlc itself reads) pins plugin
    # discovery to the bundled copy.
    os.environ["PYTHON_VLC_LIB_PATH"] = os.path.join(vlc_dir, "libvlc.dll")
    os.environ["VLC_PLUGIN_PATH"] = os.path.join(vlc_dir, "plugins")
    # libvlc.dll's dependencies (libvlccore.dll, ...) live next to it; make
    # Windows resolve them from the bundled folder.
    try:
        _vlc_dll_dir_cookie = os.add_dll_directory(vlc_dir)
    except (OSError, AttributeError):
        pass


def cleanup_stale_temp_files(root: str = None) -> None:
    """Best-effort: delete EVERY leftover mtp_* temp artifact from previous
    runs (a crashed or wedged exit strands multi-GB DVR buffers and VOD
    splitter caches) — mtp_dvr_* / mtp_cap_* buffer DIRECTORIES and
    mtp_split_* relay cache FILES.

    Artifacts belonging to a STILL-RUNNING instance are held open on
    Windows and simply survive this pass; the next launch sweeps again.
    Never raises; everything is logged.
    """
    count = 0
    freed = 0
    try:
        root = root or tempfile.gettempdir()
        for prefix in ("mtp_dvr_", "mtp_cap_", "mtp_split_",
                       "MichaelTV-update-"):
            for path in glob.glob(os.path.join(root, prefix + "*")):
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        try:
                            freed += os.path.getsize(path)
                        except OSError:
                            pass
                        os.remove(path)
                    if os.path.exists(path):
                        log.info("startup cleanup: %s locked (in use?)",
                                 path)
                    else:
                        count += 1
                        log.info("startup cleanup: removed %s", path)
                except Exception as exc:  # noqa: BLE001
                    log.warning("startup cleanup: %s failed: %r", path, exc)
    except Exception:  # noqa: BLE001
        pass
    try:
        if count:
            log.info("startup cleanup: %d artifacts removed, "
                     "%.1f MB freed", count, freed / 1e6)
    except Exception:  # noqa: BLE001
        pass


def vlc_available() -> bool:
    """Return True if the libvlc bindings (and the VLC runtime) can be loaded."""
    try:
        import vlc  # noqa: F401
        return True
    except Exception:
        return False


def main() -> int:
    # Diagnostics first: rotating log + native crash dump, before Qt/VLC start.
    try:
        from src.logging_setup import setup_logging
        setup_logging()
    except Exception:
        pass
    # Dirty-session marker + version-drift record (proves a previous run
    # died without a clean exit; shows when this machine last changed
    # versions — the "stuck on an old build" detector).
    try:
        from src import feedback
        feedback.session_start()
    except Exception:
        pass
    # A crashed previous run can strand GB-sized DVR buffers and VOD
    # splitter caches in %TEMP%.
    cleanup_stale_temp_files()
    _setup_windows_identity()
    _setup_bundled_vlc()
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    # Without this, QIcon treats a DPR-tagged pixmap as its raw device size
    # and smooth-rescales it on every paint (down to logical, back up to
    # device) — that double resample was why the control-bar glyphs read
    # soft/blurry on any Windows display-scaling above 100%.
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("MichaelTV")
    app.setApplicationDisplayName(APP_NAME)
    ico = os.path.join(_bundle_dir(), "assets", "icon.ico")
    if os.path.exists(ico):
        app.setWindowIcon(QtGui.QIcon(ico))

    if not vlc_available():
        QtWidgets.QMessageBox.critical(
            None,
            "VLC is required",
            (
                "MichaelTV needs the VLC media player to be installed.\n\n"
                "Please install VLC from https://www.videolan.org/ and restart.\n\n"
                "Important: the VLC bitness must match your Python bitness\n"
                "(64-bit Python needs 64-bit VLC)."
            ),
        )
        return 1

    from src.config import Config
    from src.ui.login_dialog import LoginDialog
    from src.ui.main_window import MainWindow
    from src.ui.theme import apply_theme

    apply_theme(app)

    config = Config.load()
    try:
        from src.config import APP_VERSION
        if config.data.get("prev_version_seen") != APP_VERSION:
            if config.data.get("prev_version_seen"):
                log.info("version change: %s -> %s "
                         "(in-app update or manual replace)",
                         config.data.get("prev_version_seen"), APP_VERSION)
            config.data["prev_version_seen"] = APP_VERSION
            config.data["prev_version_ts"] = time.time()
            try:
                config.save()
            except Exception:
                pass
    except Exception:
        pass
    try:
        from src import diagnostics
        diagnostics.capture_screen_info()   # main thread only (no Qt off it)
    except Exception:
        pass
    # Opt-in diagnostics ("Help improve MichaelTV", Settings menu): the
    # error/warning trigger + the daily startup heartbeat. Both no-op
    # while the setting is off; both swallow their own errors.
    try:
        from src import diagnostics
        diagnostics.startup_heartbeat(config)
        diagnostics.install_trigger(config)
    except Exception:
        pass
    if not config.has_account():
        if LoginDialog.configure(config).exec_() != QtWidgets.QDialog.Accepted:
            return 0

    win = MainWindow(config)
    win.show()

    # UI freeze watchdog: a Qt timer stamps liveness; the daemon thread in
    # start_ui_watchdog alarms (and can still upload) if the loop stalls.
    try:
        from src import feedback
        _beat = QtCore.QTimer()
        _beat.timeout.connect(feedback.ui_beat)
        _beat.start(5000)
        feedback.start_ui_watchdog()
    except Exception:
        pass

    code = app.exec_()

    # Hard exit once the Qt loop is done: tearing down a libvlc instance at
    # interpreter shutdown can wedge the process (its native threads then
    # keep the dead app — and its frameless on-video overlay — visible on
    # screen). Logs are flushed per record, so nothing is lost here.
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        from src import feedback
        feedback.session_end()   # clean exit — clear the dirty marker
    except Exception:
        pass
    os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
