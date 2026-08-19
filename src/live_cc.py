# -*- coding: utf-8 -*-
"""Live closed-caption source: DVR buffer -> ccextractor -> timed cues.

The DVR recorder writes the live stream (with CEA-608/708 captions riding
inside the video) to the local buffer file on the ONE provider connection.
This module tails that file and pipes it into CCExtractor, which emits SRT
cues with stream-PTS timestamps — the SAME content timeline the chase-mode
playhead uses. Zero extra connections, zero audio decoding, ~1 s of CPU
per minute of TV.

Timestamps: ccextractor times are relative to the first packet it sees, so
``content_offset_s`` (the buffer frontier when we joined) is added to land
on the shared content clock.
"""

import logging
import os
import subprocess
import tempfile
import threading
import time

from PyQt5 import QtCore

from .profanity import SrtParser

log = logging.getLogger("mtp")

_TAIL_POLL_S = 0.4      # buffer growth poll
_SRT_POLL_MS = 500      # finished-cue harvest


def find_ccextractor() -> str:
    """Locate CCExtractor (PATH, then the winget/MSI default location)."""
    for name in ("ccextractorwinfull", "ccextractor"):
        try:
            from shutil import which
            exe = which(name)
            if exe:
                return exe
        except Exception:  # noqa: BLE001
            pass
    try:
        import glob
        for pat in (
            r"C:\Program Files\CCExtractor\ccextractorwinfull.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
                               r"\CCExtractor*_*\ccextractorwinfull.exe"),
        ):
            hits = glob.glob(pat)
            if hits:
                return hits[0]
    except Exception:  # noqa: BLE001
        pass
    return ""


class CCSource(QtCore.QObject):
    """Streams captions out of a growing MPEG-TS DVR buffer.

    The TS is piped into CCExtractor's stdin and the SRT read live from
    its stdout (file output is only flushed at exit — useless live).
    """

    cue = QtCore.pyqtSignal(float, float, str)      # start_s, end_s, text
    failed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self.parser = SrtParser()
        self._offset_s = 0.0       # buffer frontier when we joined
        self._ts_pos = 0           # bytes of the buffer already piped
        self._out_chunks = []      # SRT bytes read from stdout, not parsed
        self._out_lock = threading.Lock()
        self._alive = False
        self._timer = None

    # ---- lifecycle ----
    def start(self, ts_path: str, content_offset_s: float = 0.0) -> bool:
        """Begin tailing ``ts_path`` (joined at its current end)."""
        exe = find_ccextractor()
        if not exe:
            self.failed.emit("CCExtractor not found")
            return False
        self.stop()
        self.parser = SrtParser()
        self._offset_s = float(content_offset_s or 0.0)
        try:
            size = os.path.getsize(ts_path)
        except OSError:
            size = 0
        # join at the live end of the buffer — its content time is the
        # frontier passed in as the offset
        self._ts_pos = max(0, size - (size % 188))
        try:
            self.proc = subprocess.Popen(
                [exe, "-in=ts", "-srt", "-utf8", "--stdin", "--stdout"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000)   # CREATE_NO_WINDOW
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"CCExtractor failed to start: {exc}")
            self.proc = None
            return False
        self._alive = True
        threading.Thread(target=self._tail, args=(ts_path,),
                         daemon=True, name="mtp-cc-tail").start()
        threading.Thread(target=self._read_stdout, daemon=True,
                         name="mtp-cc-read").start()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(_SRT_POLL_MS)
        self._timer.timeout.connect(self._harvest)
        self._timer.start()
        try:
            log.info("cc source started: buffer=%s offset=%.1fs join_at=%d",
                     ts_path, self._offset_s, self._ts_pos)
        except Exception:
            pass
        return True

    def stop(self):
        self._alive = False
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._timer = None
        if self.proc is not None:
            for stream in (self.proc.stdin, self.proc.stdout):
                try:
                    if stream:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None

    # ---- piping ----
    def _tail(self, ts_path: str):
        """Thread: pipe new buffer bytes into CCExtractor's stdin."""
        while self._alive:
            try:
                size = os.path.getsize(ts_path)
            except OSError:
                time.sleep(_TAIL_POLL_S)
                continue
            if size > self._ts_pos:
                try:
                    with open(ts_path, "rb") as f:
                        f.seek(self._ts_pos)
                        while self._alive:
                            chunk = f.read(1 << 18)
                            if not chunk:
                                break
                            self.proc.stdin.write(chunk)
                            self._ts_pos += len(chunk)
                except Exception:  # noqa: BLE001
                    # recorder rotated/recreated the buffer — stop cleanly
                    self._alive = False
                    break
            time.sleep(_TAIL_POLL_S)

    def _read_stdout(self):
        """Thread: collect SRT bytes from CCExtractor's stdout."""
        while self._alive and self.proc is not None:
            try:
                chunk = self.proc.stdout.read(4096)
            except Exception:  # noqa: BLE001
                break
            if not chunk:
                break
            with self._out_lock:
                self._out_chunks.append(chunk)

    # ---- harvesting ----
    def _harvest(self):
        """Timer (Qt thread): parse new SRT bytes, emit finished cues."""
        if not self._alive:
            return
        with self._out_lock:
            raw = b"".join(self._out_chunks)
            self._out_chunks = []
        if not raw:
            return
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return
        for start, end, ctxt in self.parser.feed(text):
            self.cue.emit(start + self._offset_s,
                          end + self._offset_s, ctxt)
