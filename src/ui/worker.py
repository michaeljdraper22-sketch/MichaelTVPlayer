"""Tiny helpers to run blocking work on a background thread and signal the UI."""

import os
import threading
import time
import urllib.request

from PyQt5 import QtCore


class AsyncRunner(QtCore.QObject):
    """Runs a callable on a daemon thread, emitting ``finished(("ok", result))``
    or ``finished(("err", message))`` on the thread the runner lives on
    (i.e. the GUI thread, where it was created)."""

    finished = QtCore.pyqtSignal(object)

    def run(self, fn, *args, **kwargs):
        def _task():
            try:
                result = fn(*args, **kwargs)
                try:
                    self.finished.emit(("ok", result))
                except RuntimeError:
                    pass    # owner deleted mid-flight (app closing) — drop
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                try:
                    self.finished.emit(("err", str(exc)))
                except RuntimeError:
                    pass

        threading.Thread(target=_task, daemon=True).start()


class FileDownloader(QtCore.QObject):
    """Downloads a URL straight to a file on a daemon thread (the original
    bytes, not a re-mux — used by the VOD "download" and catch-up window
    buttons). Writes to ``<path>.part`` and moves it into place at the end,
    so a failed/interrupted download never leaves a broken media file.

    The provider's timeshift backend KILLS sibling connections mid-body
    (measured 2026-08-25: a concurrent playback stream + download = one of
    the two dies every ~15-30 s while they overlap — and with the app's
    relay re-dialing too, kills can arrive much faster). An EOF short of
    the promised Content-Length is therefore NOT success: the downloader
    re-dials with a byte-exact Range header and appends. Retries are
    PROGRESS-driven — they continue as long as each attempt delivers
    bytes and stop after ``max_resume`` consecutive zero-byte attempts —
    and a server that will not resume (200 instead of 206) fails loudly:
    a silent short file used to be reported as a finished download.
    """

    progress = QtCore.pyqtSignal(int, int)      # bytes done, total (0=unknown)
    finished = QtCore.pyqtSignal(bool, str)     # ok, final path or error

    _BACKOFF_S = 0.75

    def start(self, url: str, path: str, max_resume: int = 12):
        threading.Thread(target=self._run, args=(url, path, max_resume),
                         daemon=True, name="mtp-download").start()

    def _run(self, url, path, max_resume):
        part = path + ".part"
        done = 0
        total = 0
        try:
            stalls = 0
            while True:
                before = done
                headers = {"User-Agent": "MichaelTVPlayer/1.0"}
                if done:
                    headers["Range"] = f"bytes={done}-"
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as r, \
                        open(part, "ab" if done else "wb") as f:
                    if done and r.status != 206:
                        # server restarted from byte 0 — appending would
                        # corrupt the file; refuse rather than guess
                        raise OSError("server ignored the resume request "
                                      f"(got HTTP {r.status})")
                    if not done:
                        total = int(r.headers.get("Content-Length") or 0)
                    while True:
                        try:
                            chunk = r.read(1 << 20)
                        except Exception:  # noqa: BLE001
                            # killed mid-body (IncompleteRead / reset) —
                            # same treatment as an early EOF: resume
                            break
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        self.progress.emit(done, total)
                if not total or done >= total:
                    break               # complete (or unlengthened stream)
                stalls = stalls + 1 if done == before else 0
                if stalls > max_resume:
                    raise OSError(
                        f"connection kept dying — got {done // 1048576} of "
                        f"{total // 1048576} MB after {stalls} dead tries")
                time.sleep(min(3.0, self._BACKOFF_S * (1 + stalls)))
            os.replace(part, path)
            self.finished.emit(True, path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            try:
                os.remove(part)
            except OSError:
                pass
            self.finished.emit(False, str(exc))
