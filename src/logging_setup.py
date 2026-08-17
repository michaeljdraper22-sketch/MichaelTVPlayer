# -*- coding: utf-8 -*-
"""Crash/freeze diagnostics for MichaelTVPlayer (observability only).

- Rotating log:   %APPDATA%\\MichaelTVPlayer\\player.log  (2 MB, 2 backups)
- Native crashes: %APPDATA%\\MichaelTVPlayer\\crash.dump  (faulthandler --
  catches segfaults/aborts inside libvlc and dumps all thread stacks; the
  previous run's dump is kept as crash.dump.prev)
- Python crashes: uncaught exceptions are logged via sys.excepthook

Every log call elsewhere in the app is wrapped in try/except so logging can
never break playback. The hot polling paths (is_playing/get_time/...) log at
DEBUG on purpose so a wedged libvlc cannot flood the file; drop the handler
level to DEBUG below to see them.
"""

import faulthandler
import logging
import logging.handlers
import os
import sys

_LOG_DIR = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "MichaelTVPlayer"
)
LOG_PATH = os.path.join(_LOG_DIR, "player.log")
DUMP_PATH = os.path.join(_LOG_DIR, "crash.dump")
DUMP_PREV_PATH = os.path.join(_LOG_DIR, "crash.dump.prev")

_configured = False
_dump_file = None  # module-level ref: must stay open for faulthandler


def _open_dump_file() -> None:
    """Preserve the previous run's dump, then open a fresh one."""
    global _dump_file
    try:
        if os.path.exists(DUMP_PATH) and os.path.getsize(DUMP_PATH) > 0:
            try:
                os.replace(DUMP_PATH, DUMP_PREV_PATH)
            except OSError:
                pass
        _dump_file = open(DUMP_PATH, "wb")
    except OSError:
        _dump_file = None


def _install_excepthook() -> None:
    prev_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        try:
            logging.getLogger("mtp").error(
                "Uncaught exception", exc_info=(exc_type, exc_value, exc_tb)
            )
        except Exception:
            pass
        try:
            prev_hook(exc_type, exc_value, exc_tb)  # keep default stderr output
        except Exception:
            pass

    sys.excepthook = _hook


def setup_logging() -> None:
    """Install rotating log + faulthandler + excepthook (idempotent, safe)."""
    global _configured
    if _configured:
        return
    _configured = True

    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
    except OSError:
        pass

    try:
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        logging.getLogger("mtp").setLevel(logging.DEBUG)
    except OSError:
        pass  # best effort -- the app must run even without a log file

    try:
        logging.getLogger("mtp").info(
            "=== MichaelTVPlayer starting (pid=%s, python=%s, log=%s, dump=%s) ===",
            os.getpid(), sys.version.split()[0], LOG_PATH, DUMP_PATH,
        )
    except Exception:
        pass

    try:
        _open_dump_file()
        if _dump_file is not None:
            faulthandler.enable(file=_dump_file, all_threads=True)
        else:
            faulthandler.enable()  # fallback: stderr
    except Exception:
        try:
            faulthandler.enable()
        except Exception:
            pass

    try:
        _install_excepthook()
    except Exception:
        pass