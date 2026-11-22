# -*- coding: utf-8 -*-
"""DVR recorder: the single provider connection for live TV.

Uses a **headless libvlc player** (no window) to write the stream to a temp
``buffer.ts`` file. The main player watches that local file a few seconds
behind the write frontier (chase mode, see player_view._engage_chase);
because it is a regular local file, VLC can seek in it freely — rewind is
just ``set_time``. ``stop()`` deletes the file.
"""

import logging
import os
import shutil
import tempfile
import threading
import time

import vlc

log = logging.getLogger("mtp")

_COPY_CHUNK = 1024 * 1024     # read/write granularity of the tape copier
_COPY_IDLE_S = 0.25           # poll cadence while the writer sits still
_TS_PACKET = 188              # MPEG-TS packet size (sout mux=ts)


class BufferTapeCopier:
    """Append the recorder's DVR bytes to a kept recording file.

    WHY THIS EXISTS (2026-11-17): the kept recording used to be a second
    ``dst=std{access=file}`` branch of the recorder's sout. Every recorder
    restart re-opened the SAME destination, and libVLC opens a reused sout
    destination with O_TRUNC no matter how append is requested (the very
    limitation that forces the DVR buffer to rotate to a fresh sibling —
    see VlcRecorder.start). A provider cut 2 h 40 m into a Dancing With The
    Stars recording made the stall watchdog restart the recorder twice,
    and each restart truncated the kept file: the 2 h 48 m recording on
    disk was the last 137 seconds.

    The copier is deliberately dumb: it reads the buffer file the recorder
    is writing and appends those bytes to a recording file that IT opened
    once (append mode) and that VLC never touches. Consequences:

      * display-player seeks, pauses, rewinds, rescue reopens and channel
        scrubbing cannot reach the recording at all — they move a read
        position in a different handle;
      * a recorder restart (fresh sibling buffer) only changes WHICH file
        is read; the recording keeps every byte it already had and grows
        across the seam;
      * a provider stall leaves a GAP in the tape (nothing was delivered
        to copy) instead of destroying the file.

    ``retarget()`` is called by the GUI thread on every recorder (re)start;
    a background thread tails the current segment, so the UI never waits
    on disk I/O. ``detach()`` drains and closes the source before the
    recorder's temp dir is deleted.
    """

    def __init__(self, output_path: str):
        self.output_path = os.path.abspath(output_path)
        self.written = 0
        self.segments = 0
        self.failed = False
        self.error = None
        self.last_write_t = None
        self._out = None
        self._src = None
        self._src_path = None
        self._src_pos = 0
        self._pending = None          # (path, offset) for the thread
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._t0 = time.time()

    # ---- control (GUI thread) ----
    def start(self, source: str = None, at_end: bool = True) -> bool:
        """Open the kept file (append) and start tailing ``source``.

        Returns False (with ``self.error`` set) when the destination cannot
        be opened — the caller then refuses the REC press instead of
        silently recording into the void."""
        try:
            self._out = open(self.output_path, "ab", buffering=_COPY_CHUNK)
        except OSError as exc:
            self.failed = True
            self.error = "cannot open recording file: %r" % exc
            return False
        self._t0 = time.time()
        if source:
            self.retarget(source, at_end=at_end)
        self._thread = threading.Thread(target=self._loop,
                                        name="mtp-rec-copy", daemon=True)
        self._thread.start()
        try:
            log.info("recording copier: writing %s%s", self.output_path,
                     "" if not source else " (from %s)"
                     % os.path.basename(source))
        except Exception:
            pass
        return True

    def retarget(self, source: str, at_end: bool = False):
        """Point the copier at the recorder's current buffer segment.

        ``at_end=True`` starts at the current write frontier (REC pressed
        mid-show = record from now); later segments are always read from
        their head (they are brand new files)."""
        if not source:
            return
        off = _aligned_size(source) if at_end else 0
        with self._lock:
            # Capture the tail of the segment being retired FIRST: a
            # recorder restart stops the old writer before calling this,
            # so its last bytes are complete on disk and belong on the
            # tape (losing them would put a hole at the seam).
            try:
                self._drain_locked()
            except Exception as exc:  # noqa: BLE001
                self._note_error(exc)
            self._pending = (str(source), off)

    def drain(self):
        """Copy everything already on disk in the current segment (bounded:
        a regular-file read never blocks)."""
        with self._lock:
            try:
                self._drain_locked()
            except Exception as exc:  # noqa: BLE001
                self._note_error(exc)

    def detach(self):
        """Drain, then close the source handle — required before the
        recorder's temp dir is deleted on Windows."""
        self.drain()
        with self._lock:
            self._close_src_locked()
            self._pending = None

    def stop(self):
        """Finalize the tape: flush and close. Idempotent."""
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3.0)
        with self._lock:
            try:
                self._drain_locked()
            except Exception as exc:  # noqa: BLE001
                self._note_error(exc)
            self._close_src_locked()
            out, self._out = self._out, None
            if out is not None:
                try:
                    out.flush()
                    out.close()
                except Exception as exc:  # noqa: BLE001
                    self._note_error(exc)
        try:
            log.info("recording copier: closed %s (%d bytes, %d segment(s))",
                     self.output_path, self.written, self.segments)
        except Exception:
            pass

    @property
    def running(self) -> bool:
        return self._out is not None and not self.failed

    def silent_s(self):
        """Seconds since the last byte was appended (None before the first
        one) — the recording gap the viewer would see."""
        t = self.last_write_t or self._t0
        return max(0.0, time.time() - t)

    # ---- background thread ----
    def _loop(self):
        while not self._stop.is_set():
            try:
                with self._lock:
                    pend, self._pending = self._pending, None
                    if pend is not None:
                        self._open_src_locked(*pend)
                    if self._pending is None:
                        self._drain_locked()
            except Exception as exc:  # noqa: BLE001
                self._note_error(exc)
                if self.failed:
                    break
            self._stop.wait(_COPY_IDLE_S)

    def _open_src_locked(self, path: str, off: int):
        self._close_src_locked()
        try:
            fh = open(path, "rb")
            size = os.path.getsize(path)
        except OSError:
            # the recorder may not have created the sibling yet — retry
            self._pending = (path, off)
            return
        if off > size:
            off = 0
        fh.seek(off)
        self._src = fh
        self._src_path = path
        self._src_pos = off
        self.segments += 1
        try:
            log.info("recording copier: segment %d <- %s @ %d bytes",
                     self.segments, os.path.basename(path), off)
        except Exception:
            pass

    def _close_src_locked(self):
        fh, self._src = self._src, None
        self._src_path = None
        self._src_pos = 0
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass

    def _drain_locked(self):
        if self._src is None or self._out is None:
            return
        while True:
            try:
                data = self._src.read(_COPY_CHUNK)
            except OSError:
                return    # writer mid-flush / file briefly locked: retry
            if not data:
                break
            try:
                self._out.write(data)
                self._out.flush()
            except OSError as exc:
                # Disk full / folder removed / volume unplugged: this is
                # not retryable. Mark the tape failed so the UI can tell
                # the viewer instead of pretending to record.
                self.failed = True
                self.error = "recording write failed: %r" % exc
                raise
            self.written += len(data)
            self._src_pos += len(data)
            self.last_write_t = time.time()
        # A truncated destination (libVLC re-opening its sout file on a
        # provider cut) restarts the same path at 0 — keep copying the
        # NEW data from its head instead of believing the old position.
        try:
            size = os.path.getsize(self._src_path)
        except OSError:
            return
        if size < self._src_pos:
            try:
                log.warning("recording copier: %s shrank (%d -> %d bytes) "
                            "— continuing from its head",
                            os.path.basename(self._src_path),
                            self._src_pos, size)
            except Exception:
                pass
            try:
                self._src.seek(0)
            except OSError:
                return
            self._src_pos = 0

    def _note_error(self, exc):
        if not self.failed:      # never mask a fatal write error
            self.error = repr(exc)
        try:
            log.warning("recording copier: %r", exc)
        except Exception:
            pass


def _aligned_size(path: str) -> int:
    """Current write frontier aligned down to a whole TS packet (the sout
    muxer writes complete 188-byte packets, so this is normally exact)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    return size - (size % _TS_PACKET)


class VlcRecorder:
    def __init__(self, network_caching: int = 1500,
                 instance=None):
        self.network_caching = max(0, min(50000, int(network_caching)))
        self.file_path = None
        self.rec_path = None
        self._instance = None
        self._player = None
        self._media = None   # keep the python-vlc Media wrapper alive (GC bug)
        self._dir = None
        self._size_check_t = 0.0
        self._last_size = 0
        self._shared_instance = instance
        # IMPORTANT: a second vlc.Instance() in one process is slow and can
        # deadlock with the first on Windows — always pass the player's
        # instance when recording inside the app.

    @property
    def running(self) -> bool:
        return self._player is not None

    def start(self, url: str, output_path: str = None,
              buffer_path: str = None) -> None:
        """Record ``url`` over ONE connection.

        - ``buffer_path`` given: DVR chase buffer (temp file, deleted on
          final stop). If that file already exists a FRESH SIBLING is
          started next to it instead: libVLC 3.0.20+ opens a reused
          destination truncate-mode no matter how append is requested
          (:sout-file-append as a media option or instance flag,
          file{append} chain syntax — offline-verified 2026-11-02), which
          destroyed a 770 MB chase buffer under the display player on
          every recorder restart (manual reload + the stall watchdog).
          The retired file stays readable until the temp dir is deleted;
          timeline continuity across the restart comes from the content
          clock freeze plus the stream's own PTS, not from file append.
        - ``output_path`` additionally given: dual output — the same single
          stream feeds both the DVR buffer and a kept recording file.
          NOTE: live REC no longer rides this branch. A recorder restart
          re-opens the same destination and libVLC truncates it (the exact
          reason the buffer rotates), so a provider cut mid-show used to
          wipe the kept file. Kept recordings are made by BufferTapeCopier
          from the buffer bytes instead. This path remains for the
          direct-live safety net only.
        """
        reuse = bool(buffer_path and os.path.exists(buffer_path))
        self.stop(delete=not reuse)
        if not url:
            return
        if buffer_path:
            self.file_path = buffer_path
            self._dir = os.path.dirname(buffer_path) or None
            if reuse and self._dir:
                # VLC cannot append (see docstring) — write a fresh
                # sibling so the run the display may still be watching
                # survives the restart.
                n = 2
                sibling = os.path.join(self._dir, "buffer_%d.ts" % n)
                while os.path.exists(sibling):
                    n += 1
                    sibling = os.path.join(self._dir, "buffer_%d.ts" % n)
                self.file_path = sibling
                try:
                    log.warning("dvr buffer rotation: %s exists — writing "
                                "%s instead (VLC cannot append)",
                                os.path.basename(buffer_path),
                                os.path.basename(sibling))
                except Exception:
                    pass
        else:
            self._dir = tempfile.mkdtemp(prefix="mtp_dvr_")
            self.file_path = os.path.join(self._dir, "buffer.ts")
        self.rec_path = output_path or None
        try:
            log.info("dvr.start buffer=%s reuse=%s shared_vlc=%s rec=%s",
                     self.file_path, reuse,
                     self._shared_instance is not None, self.rec_path)
        except Exception:
            pass
        self._size_check_t = 0.0
        self._last_size = 0

        nc = self.network_caching
        if self._shared_instance is not None:
            self._instance = self._shared_instance
        else:
            args = ["--ignore-config", "--no-video-title-show", "--no-stats",
                    f"--network-caching={nc}", f"--live-caching={nc}"]
            self._instance = vlc.Instance(args) or vlc.Instance()
        self._player = self._instance.media_player_new()
        # Keep the Media wrapper referenced on self: a local variable could be
        # garbage-collected mid-recording, releasing the underlying
        # libvlc_media and silently killing the recording.
        self._media = self._instance.media_new(url)
        try:
            # Same CDN rule as the display player (see player.USER_AGENT):
            # some providers 520 on VLC's default agent string.
            from .player import USER_AGENT
            self._media.add_option(f"http-user-agent={USER_AGENT}")
        except Exception:
            pass
        try:
            # A provider/CDN session cap or blip closes the HTTP socket
            # silently: VLC "plays" the dead input, the buffer stops
            # growing and the watchdog has to restart the recorder (a
            # 60 s hole and a buffer rotation). 2026-11-17 22:45, 2 h 40 m
            # into a recording, that is exactly what happened. Continuous
            # mode makes the HTTP access re-request the live URL itself,
            # so the ONE connection heals in place — no rotation, no
            # timeline reset, no gap. Verified against libVLC 3.0.24 with
            # a local server that cuts the socket every 8 s: without the
            # option the sout file froze at 3 MB, with it the recorder
            # kept writing (72 MB, 21 transparent reconnects).
            self._media.add_option(":http-continuous")
        except Exception:
            pass
        # VLC prefers forward slashes in sout destinations on Windows.
        buf = self.file_path.replace("\\", "/")
        branches = [f"dst=std{{access=file,mux=ts,dst='{buf}'}}"]
        if self.rec_path:
            rec = self.rec_path.replace("\\", "/")
            branches.append(f"dst=std{{access=file,mux=ts,dst='{rec}'}}")
        if len(branches) == 1:
            # The leading '#' is REQUIRED: libvlc 3.0.23 stopped accepting
            # a bare chain as :sout — without it the value is treated as a
            # plain destination URL, VLC "auto-detects" nothing ("no mux
            # specified or found by extension") and the input dies instantly
            # with a ZERO-byte buffer. (This is what killed DVR entirely.)
            sout = "#" + branches[0][len("dst="):]
        else:
            sout = "#duplicate{{{}}}".format(",".join(branches))
        self._media.add_option(f":sout={sout}")
        self._player.set_media(self._media)
        self._player.play()

    def buffer_file(self):
        """Return the buffer file path if it has enough data to rewind into.

        The size check is cached briefly — this is polled by the UI timer and
        hitting the disk every 400 ms is wasteful.
        """
        if not self.file_path:
            return None
        now = time.time()
        if now - self._size_check_t > 0.5:
            self._size_check_t = now
            try:
                new_size = os.path.getsize(self.file_path)
            except OSError:
                new_size = 0
            try:
                # log transitions only (first data / usable >=50KB / gone)
                if ((new_size > 0) != (self._last_size > 0)
                        or (new_size >= 50000) != (self._last_size >= 50000)):
                    log.info("dvr.buffer_file size transition: %d -> %d bytes",
                             self._last_size, new_size)
            except Exception:
                pass
            self._last_size = new_size
        return self.file_path if self._last_size > 50000 else None

    def state_name(self) -> str:
        """libVLC state of the headless recorder, for the stall log.

        A provider cut leaves VLC 'playing' a dead socket (that is why the
        watchdog exists at all); logging the state next to "no growth"
        tells a future incident apart from a wedged instance."""
        try:
            st = self._player.get_state() if self._player is not None else None
            return getattr(st, "name", str(st))
        except Exception:  # noqa: BLE001
            return "?"

    def stop(self, delete: bool = True):
        """Stop recording. ``delete=False`` keeps the buffer temp dir (used
        when the recorder is about to be restarted onto a sibling buffer).

        Strict order (Windows crash notes): stop the headless player → detach
        its media → drop ALL python-vlc references → only then delete the temp
        dir (with retries), because Windows cannot delete a file VLC still has
        open. Safe to call twice or when never started; each step is guarded.
        """
        try:
            log.info("dvr.stop delete=%s was_running=%s buffer=%s",
                     delete, self._player is not None, self.file_path)
        except Exception:
            pass
        # 1) stop the headless recorder player
        if self._player is not None:
            try:
                self._player.stop()
            except Exception as exc:
                try:
                    log.warning("dvr.stop: player.stop() failed: %r", exc)
                except Exception:
                    pass
            # 2) detach the media so libvlc closes its file handles
            try:
                self._player.set_media(None)
            except Exception as exc:
                try:
                    log.warning("dvr.stop: set_media(None) failed: %r", exc)
                except Exception:
                    pass
            # 3) drop references BEFORE touching the temp dir
            self._player = None
        self._media = None
        self._instance = None
        # 4) delete the temp dir only now — VLC may still hold the file open
        #    for a moment after stop() on Windows, so retry 3x / 0.5 s apart
        #    and simply log a final failure instead of raising.
        if delete and self._dir and os.path.isdir(self._dir):
            d = self._dir
            for attempt in (1, 2, 3):
                try:
                    shutil.rmtree(d)
                    break
                except Exception as exc:
                    try:
                        log.warning("dvr.stop: rmtree attempt %d/3 failed: %r",
                                    attempt, exc)
                    except Exception:
                        pass
                    if attempt < 3:
                        time.sleep(0.5)
            else:
                try:
                    log.warning("dvr.stop: could not delete temp dir %s "
                                "(left on disk)", d)
                except Exception:
                    pass
        if delete:
            self._dir = None
            self.file_path = None
            self.rec_path = None

    def safe_stop(self, delete: bool = True):
        """Never-raising stop(): safe to call twice, from anywhere (timers,
        signal handlers, shutdown paths). All exceptions are swallowed and
        logged so a DVR teardown can never take the UI down with it."""
        try:
            self.stop(delete=delete)
        except Exception as exc:
            try:
                log.warning("dvr.safe_stop: swallowed error: %r", exc)
            except Exception:
                pass

