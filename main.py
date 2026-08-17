"""MichaelTVPlayer - a VLC-powered IPTV player for Xtream (Xtream Codes) accounts."""

import glob
import logging
import os
import shutil
import sys
import tempfile

from PyQt5 import QtCore, QtWidgets


APP_NAME = "MichaelTVPlayer"

log = logging.getLogger("mtp")


def cleanup_stale_dvr_buffers(max_age_days: float = None) -> None:
    """Best-effort: delete EVERY leftover mtp_dvr_* / mtp_cap_* temp dir
    from previous runs (a crashed or wedged exit strands multi-GB buffers).

    Folders belonging to a STILL-RUNNING instance are held open on Windows
    and simply survive this pass. Never raises; everything is logged.
    """
    try:
        root = tempfile.gettempdir()
        for prefix in ("mtp_dvr_", "mtp_cap_"):
            for path in glob.glob(os.path.join(root, prefix + "*")):
                try:
                    if not os.path.isdir(path):
                        continue
                    shutil.rmtree(path, ignore_errors=True)
                    if os.path.exists(path):
                        log.info("startup cleanup: %s locked (in use?)",
                                 path)
                    else:
                        log.info("startup cleanup: removed %s", path)
                except Exception as exc:  # noqa: BLE001
                    log.warning("startup cleanup: %s failed: %r", path, exc)
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
    # A crashed previous run can strand GB-sized DVR buffers in %TEMP%.
    cleanup_stale_dvr_buffers()
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("MichaelTV")
    app.setApplicationDisplayName(APP_NAME)

    if not vlc_available():
        QtWidgets.QMessageBox.critical(
            None,
            "VLC is required",
            (
                "MichaelTVPlayer needs the VLC media player to be installed.\n\n"
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
    if not config.has_account():
        if LoginDialog.configure(config).exec_() != QtWidgets.QDialog.Accepted:
            return 0

    win = MainWindow(config)
    win.show()
    code = app.exec_()

    # Hard exit once the Qt loop is done: tearing down a libvlc instance at
    # interpreter shutdown can wedge the process (its native threads then
    # keep the dead app — and its frameless on-video overlay — visible on
    # screen). Logs are flushed per record, so nothing is lost here.
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001
        pass
    os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
