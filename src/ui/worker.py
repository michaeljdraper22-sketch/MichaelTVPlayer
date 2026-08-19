"""Tiny helpers to run blocking work on a background thread and signal the UI."""

import os
import threading
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
    bytes, not a re-mux — used by the VOD "download" button). Writes to
    ``<path>.part`` and moves it into place at the end, so a failed/interrupted
    download never leaves a broken media file behind."""

    progress = QtCore.pyqtSignal(int, int)      # bytes done, total (0=unknown)
    finished = QtCore.pyqtSignal(bool, str)     # ok, final path or error

    def start(self, url: str, path: str):
        threading.Thread(target=self._run, args=(url, path), daemon=True,
                         name="mtp-download").start()

    def _run(self, url, path):
        part = path + ".part"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "MichaelTVPlayer/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r, \
                    open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    self.progress.emit(done, total)
            os.replace(part, path)
            self.finished.emit(True, path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            try:
                os.remove(part)
            except OSError:
                pass
            self.finished.emit(False, str(exc))
