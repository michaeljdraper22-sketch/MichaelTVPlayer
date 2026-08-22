# -*- coding: utf-8 -*-
"""Live closed-caption source: DVR buffer -> ccextractor -> timed cues.

The DVR recorder writes the live stream (with CEA-608/708 captions riding
inside the video) to the local buffer file on the ONE provider connection.
This module tails that file and pipes it into CCExtractor, which emits SRT
cues — zero extra connections, zero audio decoding, ~1 s of CPU per minute
of TV.

Timestamps: CCExtractor's SRT times are on its OWN PTS-derived axis, and
that axis does NOT match VLC's playback clock on every provider — a probe
against a burst-y 4K channel measured CCX running 12-32 s AHEAD of
get_time() with continuous drift (VLC's demuxer absorbs PTS
discontinuities its own way; CCX accumulates raw deltas). So cue display
times are NOT taken from CCX at all: the UI anchors each arriving cue
against its own frontier clock (PlayerView._on_cc_cue), which re-syncs the
two axes at every fresh cue and makes the exact join byte irrelevant —
which in turn lets a mid-session engage skip the back of the buffer
(join_bytes) instead of replaying minutes of content nobody will display.
"""

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

from PyQt5 import QtCore

from .profanity import SrtParser

log = logging.getLogger("mtp")

_TAIL_POLL_S = 0.25     # buffer growth poll
_SRT_POLL_MS = 250      # finished-cue harvest
_PCR_PROBE_BYTES = 262144   # tail window scanned for the newest PCR


def _align_ts(data: bytes) -> int:
    """Offset of the first 188-byte packet grid inside ``data`` (-1 if none).

    The DVR buffer is packet-aligned, but a tail WINDOW read starts at an
    arbitrary byte — three consecutive 0x47s one packet apart confirm the
    grid before anything is parsed."""
    n = len(data)
    if n < 3 * 188:
        return -1
    for off in range(0, 188):
        if data[off] == 0x47 and data[off + 188] == 0x47 \
                and data[off + 376] == 0x47:
            return off
    return -1


def _packet_pcr(data: bytes, p: int):
    """(pid, pcr_seconds) of the TS packet at ``p`` (None when it carries
    no PCR). Needs ≥ 7 adaptation-field bytes: flags + the 6-byte PCR."""
    if data[p] != 0x47:
        return None
    if (data[p + 3] >> 4) & 0x3 < 2:          # no adaptation field
        return None
    afl = data[p + 4]
    if afl < 7 or afl > 183:
        return None
    if not data[p + 5] & 0x10:                # PCR flag
        return None
    q = p + 6
    base = ((data[q] << 25) | (data[q + 1] << 17) | (data[q + 2] << 9)
            | (data[q + 3] << 1) | (data[q + 4] >> 7))
    ext = ((data[q + 4] & 0x01) << 8) | data[q + 5]
    pid = ((data[p + 1] & 0x1F) << 8) | data[p + 2]
    return pid, base / 90000.0 + ext / 27000000.0


def probe_tail_pcr(path: str, nbytes: int = _PCR_PROBE_BYTES):
    """Newest PCR in the last ``nbytes`` of a growing TS file ->
    (pid, pcr_seconds), or (None, None). This is the content time at the
    WRITE HEAD of the buffer — same 90 kHz family CCX's cue times come
    from, so (head PCR - join PCR) - cue_end measures CCX's true
    processing lag."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, None
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return None, None
    off = _align_ts(data)
    if off < 0:
        return None, None
    found = None
    for p in range(off, len(data) - 188 + 1, 188):
        r = _packet_pcr(data, p)
        if r is not None:
            found = r          # keep going — the LAST PCR is the newest
    return found if found else (None, None)


def probe_first_pcr_at(path: str, offset: int = 0,
                       nbytes: int = _PCR_PROBE_BYTES):
    """First PCR at/after byte ``offset`` -> (pid, pcr_seconds), or
    (None, None). Used to pin the PCR at CCExtractor's JOIN byte: the
    content position where CCX's own (zero-based) cue axis begins."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, None
    start = max(0, int(offset))
    if start >= size:
        return None, None
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(nbytes)
    except OSError:
        return None, None
    off = _align_ts(data)
    if off < 0:
        return None, None
    for p in range(off, len(data) - 188 + 1, 188):
        r = _packet_pcr(data, p)
        if r is not None:
            return r
    return None, None


def find_ccextractor() -> str:
    """Locate CCExtractor: an INSTALLED copy first (PATH, then the winget/
    MSI default location), then the static build vendored in vendor/ as
    the zero-install fallback (bundled into the PyInstaller release)."""
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
    return bundled_ccextractor()


def bundled_ccextractor() -> str:
    """Path of the vendored static CCExtractor (0.88 win build — a
    self-contained exe, unlike the modern MSI build whose ffmpeg DLLs
    it must not ship without). Checked LAST on purpose: whatever the
    user installed wins; this only serves machines with nothing, so
    releases get captions without any install step."""
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cands = [os.path.join(root, "vendor", "ccextractorwin.exe")]
        mep = getattr(sys, "_MEIPASS", None)   # PyInstaller onefile data
        if mep:
            cands.append(os.path.join(mep, "vendor", "ccextractorwin.exe"))
        if getattr(sys, "frozen", False):      # dropped next to the exe
            cands.append(os.path.join(os.path.dirname(sys.executable),
                                      "vendor", "ccextractorwin.exe"))
        for c in cands:
            if os.path.isfile(c):
                return c
    except Exception:  # noqa: BLE001
        pass
    return ""


def ccx_args(exe: str) -> list:
    """CLI for streaming captions through pipes. The vendored 0.88 build
    predates the long flags — it wants the old single-dash form (and '-'
    as the positional input for stdin)."""
    if exe and bundled_ccextractor() and \
            os.path.abspath(exe) == os.path.abspath(bundled_ccextractor()):
        return ["-in=ts", "-srt", "-utf8", "-", "-stdout"]
    return ["-in=ts", "-srt", "-utf8", "--stdin", "--stdout"]


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
        self._offset_s = 0.0       # always 0: cues are emitted on the
        #                            # buffer file's own (VLC) timeline
        self._ts_pos = 0           # bytes of the buffer already piped
        self._out_chunks = []      # SRT bytes read from stdout, not parsed
        self._out_lock = threading.Lock()
        self._alive = False
        self._timer = None

    # ---- lifecycle ----
    def start(self, ts_path: str, content_offset_s: float = 0.0,
              join_bytes: int = 0) -> bool:
        """Begin tailing ``ts_path`` — from byte 0, or from ``join_bytes``
        when engaging mid-session on a long-running buffer.

        ``content_offset_s`` is accepted for API compatibility and IGNORED
        (see below). ``join_bytes`` skips the back of the buffer so
        CCExtractor does not spend ~1 s of CPU per buffered minute
        replaying content nobody will display — the emitted cue times are
        then relative to the join, but the UI anchors live cues by ARRIVAL
        against its own clock (see PlayerView._on_cc_cue), so the exact
        join point does not need to be precise. Bytes past EOF are
        clamped; a bad guess only shifts which cues exist, never their
        display times.
        """
        exe = find_ccextractor()
        if not exe:
            self.failed.emit("CCExtractor not found")
            return False
        if exe and bundled_ccextractor() and \
                os.path.abspath(exe) == os.path.abspath(bundled_ccextractor()):
            # The vendored 0.88 build reads stdin to EOF before emitting a
            # single SRT byte (measured: 30 MB piped, 0 B out until close) —
            # it cannot tail a growing buffer. Fail fast so the owner falls
            # back to VLC's caption rendering instead of a pipeline that
            # never produces a cue.
            self.failed.emit("bundled CCExtractor 0.88 cannot stream")
            return False
        self.stop()
        self.parser = SrtParser(keep_lines=True)   # overlay renders the
        #                                            # roll-up line structure
        self._offset_s = 0.0
        self._ts_pos = max(0, int(join_bytes))     # skip the back of the
        #                                            # buffer (arrival-
        #                                            # anchored display)
        self._ts_pos -= self._ts_pos % 188          # land on a packet
        try:
            size = os.path.getsize(ts_path)
            if self._ts_pos >= size:
                self._ts_pos = 0 if size == 0 else \
                    max(0, (size - 1) // 188 * 188)
        except OSError:
            self._ts_pos = 0
        try:
            self.proc = subprocess.Popen(
                [exe] + ccx_args(exe),
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
            log.info("cc source started: buffer=%s join_at_byte=%d "
                     "(arrival-anchored display)", ts_path, self._ts_pos)
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
                    if self._alive:
                        # recorder rotated/recreated the buffer under us —
                        # the caption pipeline is done (stop() clears
                        # _alive first, so a normal teardown stays silent)
                        self._alive = False
                        try:
                            self.failed.emit("buffer stream error")
                        except Exception:  # noqa: BLE001
                            pass
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
                if self._alive:
                    # CCExtractor exited on its own (crash / bad stream) —
                    # let the owner fall back to VLC's caption rendering
                    self._alive = False
                    try:
                        self.failed.emit("CCExtractor exited")
                    except Exception:  # noqa: BLE001
                        pass
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
