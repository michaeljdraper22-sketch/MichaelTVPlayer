# -*- coding: utf-8 -*-
"""DVR buffer: record a live stream with VLC so you can rewind reliably.

Uses a **headless libvlc player** (no window) to save the stream to a temp
``buffer.ts`` file. The main player then plays either the live URL (normal) or
that local file (rewind). Because the file is a regular local file, VLC can seek
in it freely \u2014 rewind is just ``set_time``. ``stop()`` deletes the file.
"""

import logging
import os
import shutil
import tempfile
import time

import vlc

log = logging.getLogger("mtp")


class VlcRecorder:
    def __init__(self, max_minutes: int = 30, network_caching: int = 1500,
                 instance=None):
        self.max_minutes = max(1, int(max_minutes))
        self.network_caching = max(0, min(50000, int(network_caching)))
        self.file_path = None
        self.rec_path = None
        self.start_time = None
        self._instance = None
        self._player = None
        self._media = None   # keep the python-vlc Media wrapper alive (GC bug)
        self._dir = None
        self.keep_file = False   # kept for API compatibility
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

        - ``buffer_path`` given: DVR chase buffer (temp file, deleted on final
          stop). If the file already exists it is APPENDED to, so the timeline
          stays continuous when the recorder is restarted mid-session.
        - ``output_path`` additionally given: dual output — the same single
          stream feeds both the DVR buffer and a kept recording file.

        (Auto-captions are no longer forked here: the DISPLAY player forks
        its own caption wav, which keeps the wav in lockstep with what is
        actually being watched — see VLCPlayer.play_at.)
        """
        reuse = bool(buffer_path and os.path.exists(buffer_path))
        self.stop(delete=not reuse)
        if not url:
            return
        if buffer_path:
            self.file_path = buffer_path
            self._dir = os.path.dirname(buffer_path) or None
            self.keep_file = False
        else:
            self._dir = tempfile.mkdtemp(prefix="mtp_dvr_")
            self.file_path = os.path.join(self._dir, "buffer.ts")
            self.keep_file = False
        self.rec_path = output_path or None
        try:
            log.info("dvr.start buffer=%s reuse=%s shared_vlc=%s rec=%s",
                     self.file_path, reuse,
                     self._shared_instance is not None, self.rec_path)
        except Exception:
            pass
        self.start_time = time.time()
        self._size_check_t = 0.0
        self._last_size = 0

        nc = self.network_caching
        if self._shared_instance is not None:
            self._instance = self._shared_instance
        else:
            args = ["--no-video-title-show", "--no-stats",
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
        if reuse:
            # continue an existing buffer instead of truncating it
            self._media.add_option(":sout-file-append")
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

    def elapsed_seconds(self) -> float:
        if not self.start_time:
            return 0.0
        return time.time() - self.start_time

    def stop(self, delete: bool = True):
        """Stop recording. ``delete=False`` keeps the buffer file (used when the
        recorder is about to be restarted onto the same buffer).

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
        self.start_time = None
        # 4) delete the temp dir only now — VLC may still hold the file open
        #    for a moment after stop() on Windows, so retry 3x / 0.5 s apart
        #    and simply log a final failure instead of raising.
        if delete and self._dir and os.path.isdir(self._dir) and not self.keep_file:
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

