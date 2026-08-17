# -*- coding: utf-8 -*-
"""Local auto-generated captions.

The player forks the stream's audio into a 16 kHz mono wav (through VLC's
stream-output, still on the single stream connection) and this module tails
that file and transcribes it with **vosk** — a free, open-source (Apache-2.0)
offline speech-recognition toolkit (https://github.com/alphacephei/vosk).
Nothing leaves the machine.

Everything degrades gracefully: no vosk / no model → the caller is told and
can offer to install/download.
"""

import json
import logging
import os
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from PyQt5 import QtCore

log = logging.getLogger("mtp")

APP_DIR_NAME = "MichaelTVPlayer"
SMALL_MODEL_NAME = "vosk-model-small-en-us-0.15"
LARGE_MODEL_NAME = "vosk-model-en-us-0.22"      # much better with accents
MODEL_URL = ("https://alphacephei.com/vosk/models/"
             + SMALL_MODEL_NAME + ".zip")
LARGE_MODEL_URL = ("https://alphacephei.com/vosk/models/"
                   + LARGE_MODEL_NAME + ".zip")

_CHUNK = 4000        # bytes fed to the recognizer at once (250 ms of PCM)
_POLL_S = 0.15       # wait between file-growth polls
_PCM_BYTES_PER_S = 32000   # 16 kHz mono s16le


def _models_root() -> Path:
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    base = base or os.path.expanduser("~")
    return Path(base) / APP_DIR_NAME / "models"


def model_dir() -> Path:
    """Best available model directory (the large one wins if downloaded)."""
    root = _models_root()
    large = root / LARGE_MODEL_NAME
    if (large / "conf").is_dir():
        return large
    return root / SMALL_MODEL_NAME


def large_model_dir() -> Path:
    return _models_root() / LARGE_MODEL_NAME


def large_model_ready() -> bool:
    d = large_model_dir()
    return (d / "conf").is_dir() and any(d.iterdir())


def model_ready() -> bool:
    d = model_dir()
    return (d / "conf").is_dir() and any(d.iterdir())


def vosk_importable() -> bool:
    try:
        import vosk  # noqa: F401
        return True
    except Exception:
        return False


class ModelDownloader(QtCore.QObject):
    """Downloads + unzips a vosk model in a background thread."""

    progress = QtCore.pyqtSignal(int, int)      # bytes done, bytes total
    finished = QtCore.pyqtSignal(bool, str)     # ok, message

    def start(self, large: bool = False):
        self._large = bool(large)
        threading.Thread(target=self._run, daemon=True,
                         name="mtp-cap-model").start()

    def _run(self):
        name = LARGE_MODEL_NAME if self._large else SMALL_MODEL_NAME
        url = LARGE_MODEL_URL if self._large else MODEL_URL
        try:
            root = _models_root()
            root.mkdir(parents=True, exist_ok=True)
            zip_path = root / (name + ".zip")
            ready = ((large_model_ready() if self._large else model_ready())
                     and (large_model_dir() if self._large
                          else model_dir()).name == name)
            if not ready:
                def hook(n, bs, total):
                    try:
                        self.progress.emit(min(n * bs, total), total or 1)
                    except Exception:
                        pass
                urllib.request.urlretrieve(url, zip_path, reporthook=hook)
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(root)
                try:
                    zip_path.unlink()
                except OSError:
                    pass
            ok = ((large_model_ready() if self._large else model_ready())
                  and (large_model_dir() if self._large
                       else model_dir()).name == name)
            try:
                log.info("caption model download: ok=%s large=%s dir=%s",
                         ok, self._large,
                         large_model_dir() if self._large else model_dir())
            except Exception:
                pass
            self.finished.emit(ok, "" if ok else "Unpacking failed")
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("caption model download failed: %r", exc)
            except Exception:
                pass
            self.finished.emit(False, repr(exc))


class AutoCaptioner(QtCore.QObject):
    """Transcribes a growing 16 kHz mono wav file with vosk — in sync with
    what the video is actually SHOWING.

    The wav is written by VLC as a sequential LOG of the audio that is being
    displayed (seeks, pauses and speed changes all land in it), so staying
    in sync mostly means tailing that log: ``follow_live()`` skips to the
    newest audio, ``set_rate()`` paces the feed at the playback speed (0 =
    paused) and ``set_backoff()`` holds the feed a fixed distance behind
    the tail (the user's manual sync adjustment).

    Threading: each ``start()`` spawns a worker with its OWN stop event and
    a generation number; ``stop()`` bumps the generation, so a worker that
    outlives its 2 s join timeout (the big model can make vosk slower than
    that) can never emit again — the old shared-flag design un-killed such
    zombies and produced overlapping, unrelated caption streams.

    Emits Qt signals (thread-safe: they are queued to the GUI thread):
      - ``partial(str)``  current in-progress line ("" = clear the display)
      - ``final(str)``    a finished sentence
      - ``status(str)``   "running" / "novosk" / "nomodel" / "error:..."
    """

    partial = QtCore.pyqtSignal(str)
    final = QtCore.pyqtSignal(str)
    status = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = None
        self._thread = None
        self._run_stop = None     # per-worker stop event (never shared)
        self._gen = 0             # worker generation; emissions must match
        self._wake_evt = threading.Event()
        self._lock = threading.Lock()
        self._rate = 1.0        # feed speed multiplier; 0.0 = paused
        self._seek_s = None     # pending jump target (seconds into the wav)
        self._follow = True     # True: track the live tail; False: paced sync
        self._fed_s = 0.0       # mirror of the worker's fed position
        self._backoff_s = 0.0   # manual sync: stay this far behind the tail
        self._retail = False    # re-position the feed to tail - backoff

    # ---- lifecycle ----
    def start(self, wav_path: str) -> bool:
        self.stop()
        if not vosk_importable():
            self.status.emit("novosk")
            return False
        if not model_ready():
            self.status.emit("nomodel")
            return False
        # NOTE: the model itself is loaded inside the worker thread — the
        # high-accuracy model is ~1.3 GB and loading it on the GUI thread
        # froze the whole app for several seconds on every enable.
        stop_evt = threading.Event()
        gen = self._gen + 1
        self._run_stop = stop_evt
        self._gen = gen
        with self._lock:
            self._rate = 1.0
            self._seek_s = None
            self._follow = True
            self._fed_s = 0.0
            self._retail = False
        self._thread = threading.Thread(
            target=self._run, args=(wav_path, stop_evt, gen), daemon=True,
            name="mtp-captions")
        self._thread.start()
        self.status.emit("running")
        return True

    def stop(self):
        # Bump the generation FIRST: even if the worker survives the join
        # timeout, its emissions become no-ops instead of ghost captions.
        self._gen += 1
        evt = self._run_stop
        if evt is not None:
            evt.set()
        self._wake_evt.set()
        t = self._thread
        if t is not None and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=2.0)
        self._thread = None

    # ---- sync control (call from the GUI thread) ----
    def offset(self) -> float:
        """Seconds into the wav the recognizer was last fed (for drift
        correction — compare with what the video is showing)."""
        with self._lock:
            return self._fed_s

    def seek(self, offset_s: float):
        """Jump transcription to ``offset_s`` seconds into the wav and pace
        from there — used after rewinds, jumps to live and slider drags."""
        with self._lock:
            self._seek_s = max(0.0, float(offset_s))
            self._follow = False
            self._fed_s = max(0.0, float(offset_s))
        self._wake_evt.set()

    def follow_live(self):
        """Skip the backlog and tail the newest audio — the wav logs what is
        being displayed, so this is "transcribe from what's on screen now"."""
        with self._lock:
            self._follow = True
            self._seek_s = None
        self._wake_evt.set()

    def set_rate(self, rate: float):
        """Feed speed multiplier (1.0 = normal). 0 pauses transcription."""
        with self._lock:
            self._rate = max(0.0, min(6.0, float(rate)))
        self._wake_evt.set()

    def backoff(self) -> float:
        """Current manual sync offset (seconds behind the audio tail)."""
        with self._lock:
            return self._backoff_s

    def set_backoff(self, seconds: float):
        """Manual sync: hold the feed ``seconds`` behind the wav tail and
        re-position it there immediately (positive = captions appear later)."""
        with self._lock:
            self._backoff_s = max(0.0, min(60.0, float(seconds)))
            self._retail = True
        self._wake_evt.set()

    # ---- worker ----
    def _data_offset(self, f) -> int:
        """Walk the RIFF chunks to the start of the data payload."""
        try:
            f.seek(0)
            hdr = f.read(12)
            if len(hdr) < 12 or hdr[:4] != b"RIFF":
                return 44
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    return 44
                size = int.from_bytes(ch[4:8], "little")
                if ch[:4] == b"data":
                    return f.tell()
                f.seek(size, 1)
        except Exception:
            return 44

    def _run(self, wav_path: str, stop_evt: threading.Event, gen: int):
        import vosk
        if self._model is None:
            # loaded here (worker thread): the 1.3 GB high-accuracy model
            # takes seconds to load — never block the GUI thread with it
            try:
                self._model = vosk.Model(str(model_dir()))
            except Exception as exc:  # noqa: BLE001
                self.status.emit(f"error:model {exc!r}")
                return
        f = None
        rec = None
        data_start = 44
        fed_s = 0.0          # seconds of audio already fed
        last_t = time.time()

        def alive() -> bool:
            return gen == self._gen and not stop_evt.is_set()

        def feed(chunk):
            nonlocal rec
            if rec is None or not alive():
                return
            try:
                if rec.AcceptWaveform(chunk):
                    txt = json.loads(rec.Result()).get("text", "")
                    if txt and alive():
                        self.final.emit(txt)
                elif alive():
                    self.partial.emit(
                        json.loads(rec.PartialResult()).get("text", ""))
            except Exception:  # noqa: BLE001
                pass

        while not stop_evt.is_set():
            try:
                # 1) pick up pending seek / rate / follow / retail changes
                seek_s = None
                retail = False
                with self._lock:
                    if self._seek_s is not None:
                        seek_s = self._seek_s
                        self._seek_s = None
                    if self._retail:
                        self._retail = False
                        retail = True
                    follow = self._follow
                    rate = self._rate
                    backoff = self._backoff_s
                if f is not None and seek_s is not None:
                    try:
                        f.seek(data_start
                               + int(seek_s * _PCM_BYTES_PER_S))
                        fed_s = seek_s
                        rec = vosk.KaldiRecognizer(self._model, 16000)
                        if alive():
                            self.partial.emit("")   # clear stale text
                    except OSError:
                        f.close()
                        f = None
                # 2) (re)open the wav
                if f is None:
                    try:
                        f = open(wav_path, "rb")
                    except OSError:
                        time.sleep(0.3)
                        continue
                    data_start = self._data_offset(f)
                    try:
                        size0 = os.path.getsize(wav_path)
                    except OSError:
                        size0 = data_start
                    if follow:
                        # skip any backlog: transcribe from "now" on
                        fed_s = max(
                            0.0, (size0 - data_start) / _PCM_BYTES_PER_S
                            - backoff)
                    else:
                        fed_s = 0.0   # the pending seek positions us
                    rec = vosk.KaldiRecognizer(self._model, 16000)
                    last_t = time.time()
                # 3) detect a recreated (shrunk) wav — VLC restarts rewrite
                #    it from scratch; drop the decoder and pick up at "now".
                try:
                    size = os.path.getsize(wav_path)
                except OSError:
                    size = 0
                if size < data_start + int(fed_s * _PCM_BYTES_PER_S) - 4096:
                    try:
                        log.info("captions: wav recreated — resetting")
                    except Exception:
                        pass
                    try:
                        f.close()
                    except Exception:
                        pass
                    f = None
                    continue
                # 4) paced feeding
                avail_s = max(0.0, (size - data_start)) / _PCM_BYTES_PER_S
                now = time.time()
                dt = max(0.0, now - last_t)
                last_t = now
                if retail and f is not None:
                    # manual sync changed: jump the feed to tail - backoff
                    fed_s = max(0.0, avail_s - backoff)
                    try:
                        f.seek(data_start + int(fed_s * _PCM_BYTES_PER_S))
                        rec = vosk.KaldiRecognizer(self._model, 16000)
                        if alive():
                            self.partial.emit("")
                    except OSError:
                        f.close()
                        f = None
                        continue
                if follow:
                    # tail the log: hold the manual-sync backoff behind the
                    # newest audio, catch up fast after gaps, then feed at
                    # the playback rate so text stays with the speech
                    target_s = max(0.0, avail_s - backoff)
                    lag = target_s - fed_s
                    eff = 0.0 if rate <= 0.0 else (
                        6.0 if lag > 1.0 else rate)
                else:
                    target_s = avail_s
                    eff = rate          # strict sync with the video
                allowed_s = dt * eff
                if allowed_s <= 0.0:
                    self._wake_evt.wait(_POLL_S)
                    self._wake_evt.clear()
                    continue
                want_s = min(target_s, fed_s + allowed_s)
                while (not stop_evt.is_set()
                        and fed_s < want_s - 0.005):
                    take = min(_CHUNK,
                               max(1, int(round(
                                   (want_s - fed_s) * _PCM_BYTES_PER_S))))
                    chunk = f.read(take)
                    if not chunk:
                        break
                    fed_s += len(chunk) / _PCM_BYTES_PER_S
                    with self._lock:
                        self._fed_s = fed_s
                    feed(chunk)
                if fed_s >= want_s - 0.005:
                    self._wake_evt.wait(_POLL_S)
                    self._wake_evt.clear()
            except OSError:
                if f is not None:
                    try:
                        f.close()
                    except Exception:
                        pass
                f = None
                time.sleep(0.25)
            except Exception as exc:  # noqa: BLE001
                try:
                    log.warning("captions worker: %r", exc)
                except Exception:
                    pass
                time.sleep(0.5)
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
