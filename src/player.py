"""Thin wrapper around libvlc with timeshift (pause/rewind/back-to-live) support."""

import logging
import os
import sys
import threading

import vlc

log = logging.getLogger("mtp")

# How long the UI may wait for a busy display player to stop before giving
# up on it and swapping in a fresh one (VLCPlayer.stop, see docstring there).
_STOP_WAIT_S = 2.5

# User-Agent presented to the provider when VLC opens a stream. Some CDNs
# (e.g. this account's) reject unknown/default agent strings with 520 while
# serving this exact one — and it is what the app's API session already uses.
USER_AGENT = "MichaelTVPlayer/1.0"


class VLCPlayer:
    """Wraps a libvlc media player.

    Timeshift is enabled per-media via the input's ``input-timeshift`` option,
    which makes VLC continuously buffer the live input to a temporary file.
    This enables pause (picture freezes while buffering continues) and seeking
    within the buffered region. "Jump to live" seeks to the live edge.
    """

    def __init__(self, timeshift: bool = True, volume: int = 100,
                 network_caching: int = 1500):
        nc = max(0, min(50000, int(network_caching)))
        args = [
            "--no-video-title-show",
            "--no-stats",
            f"--network-caching={nc}",
            f"--live-caching={nc}",
            "--file-caching=1000",
            "--disc-caching=1000",
            "--avcodec-skiploopfilter=1",
        ]
        self.timeshift = timeshift
        # A few caching/display options. Some VLC builds reject unknown options
        # and return None, so fall back to a plain instance if that happens.
        self.instance = (
            vlc.Instance(args) or vlc.Instance()
        )
        self.player = self.instance.media_player_new()
        self._volume = max(0, min(100, int(volume)))
        self._mute = False            # desired mute state (re-applied per player)
        self._scale_mode = "fit"      # "fit" | "stretch" | "crop"
        self._scale_wh = None         # last (w, h) the scale was computed for
        self._scale_last = None       # (mode, w, h) last sent to a player
        self._ems = []                # keep event-manager refs alive (GC segfault)
        self._window_id = None   # last HWND/XID handed to set_window()
        self._setup_player(self.player)
        self.media = None

    def _setup_player(self, player) -> None:
        """Bring a (new) media player object into the known state: volume,
        mute, window binding, video scale — and hook the Playing event so
        audio state is re-applied once VLC's audio output actually exists
        (Windows: setting volume before playback starts gets lost when the
        audio device is opened, which silenced every channel switch)."""
        # a NEW player starts with VLC defaults — force the video scale to
        # be re-applied even if (mode, w, h) is unchanged
        self._scale_last = None
        self._apply_volume(player)
        try:
            player.audio_set_mute(1 if self._mute else 0)
        except Exception:
            pass
        if self._scale_wh:
            self._apply_scale_to(player, *self._scale_wh)
        try:
            em = player.event_manager()
            em.attach(vlc.EventType.MediaPlayerPlaying, self._on_playing_event)
            self._ems.append(em)
            if len(self._ems) > 8:
                self._ems = self._ems[-4:]
        except Exception as exc:  # noqa: BLE001
            try:
                log.debug("event attach failed: %r", exc)
            except Exception:
                pass

    def _on_playing_event(self, _event) -> None:
        """VLC thread context: only thread-safe libvlc audio calls, no Qt."""
        try:
            self._apply_volume(self.player)
            self.player.audio_set_mute(1 if self._mute else 0)
        except Exception:
            pass

    # ---- window attachment ----
    def set_window(self, window_id: int) -> None:
        self._window_id = int(window_id)
        self._bind_window(self.player, window_id)

    def _bind_window(self, player, window_id=None) -> None:
        """Bind ``player`` to the native window; no-op when there is none."""
        wid = self._window_id if window_id is None else window_id
        if wid is None:
            return
        try:
            if sys.platform == "win32":
                player.set_hwnd(wid)
            elif sys.platform.startswith("linux"):
                player.set_xwindow(wid)
            elif sys.platform == "darwin":
                player.set_nsobject(wid)
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("_bind_window failed: %r", exc)
            except Exception:
                pass

    # ---- playback ----
    def play_at(self, url: str, start_seconds: float = 0.0,
                record_path: str = None, cap_path: str = None,
                append: bool = False) -> None:
        """Play ``url``, optionally starting at ``start_seconds``.

        ``:start-time=`` makes VLC open directly at the target position — no
        flash of the beginning followed by a seek (that was the rewind jank).

        ``record_path`` / ``cap_path`` attach extra outputs to the SAME
        single connection through VLC stream-output duplication:
        - ``record_path``: a kept MPEG-TS recording file,
        - ``cap_path``: a 16 kHz mono wav fed to the local caption engine.
          Because the fork taps the input stream, the wav is a sequential
          LOG of the displayed audio — seeks, pauses and rate changes land
          in it, which is what keeps auto-captions in sync.
        """
        try:
            kind = "live" if url.startswith(("http://", "https://")) else "file"
            log.info("display open kind=%s start=%.1fs rec=%s cap=%s "
                     "append=%s (prev_state=%s busy=%s)",
                     kind, float(start_seconds), bool(record_path),
                     bool(cap_path), bool(append), self.state_name(),
                     self.is_busy())
        except Exception:
            pass
        # Full teardown of the previous media first (stop + detach): one
        # provider connection at a time, and local buffer files must be closed
        # before the DVR code deletes them.
        self.stop_and_release()
        self.media = self.instance.media_new(url)
        try:
            # Some provider CDNs 520 on default agent strings — pin ours.
            self.media.add_option(f"http-user-agent={USER_AGENT}")
        except Exception:
            pass
        if start_seconds > 0:
            try:
                self.media.add_option(f":start-time={float(start_seconds):.3f}")
            except Exception as exc:
                try:
                    log.warning("play_at: add start-time failed: %r", exc)
                except Exception:
                    pass
        branches = []
        if record_path or cap_path:
            # display first, then the extra outputs (see play_outputs note
            # about the double-quoted transcode chain)
            branches.append("dst=display")
            if record_path:
                rec = record_path.replace("\\", "/")
                branches.append(f"dst=std{{access=file,mux=ts,dst='{rec}'}}")
            if cap_path:
                cap = cap_path.replace("\\", "/")
                branches.append(
                    'dst="transcode{acodec=s16l,samplerate=16000,channels=1}'
                    f":std{{access=file,mux=wav,dst='{cap}'}}{{select=audio}}\"")
            sout = "#duplicate{{{}}}".format(",".join(branches))
            self.media.add_option(f":sout={sout}")
            if append:
                self.media.add_option(":sout-file-append")
        elif self.timeshift and url.startswith(("http://", "https://")):
            # Timeshift for live inputs only (never for local buffer files),
            # and only without a sout chain (timeshift + sout conflict).
            try:
                self.media.add_option("input-timeshift=1")
                self.media.add_option("timeshift-granularity=50")
            except Exception as exc:
                try:
                    log.warning("play_at: add timeshift options failed: %r",
                                exc)
                except Exception:
                    pass
        # Insurance against a video window detaching (a fresh player after
        # a hung-stop swap, or a race in the swap path): without the hwnd
        # bound, VLC spawns its OWN top-level video window.
        if self._window_id is not None:
            self._bind_window(self.player)
        self.player.set_media(self.media)
        self.player.play()

    def play(self, url: str) -> None:
        self.play_at(url, 0.0)

    def play_and_record(self, url: str, path: str, append: bool = False) -> None:
        """Backwards-compatible wrapper around play_at()."""
        self.play_at(url, 0.0, record_path=path, append=append)

    def play_outputs(self, url: str, record_path: str = None,
                     append: bool = False, cap_path: str = None) -> None:
        """Watch ``url`` on the display while forking extra outputs from the
        SAME single connection (kept for API compatibility — everything
        funnels into play_at now)."""
        self.play_at(url, 0.0, record_path=record_path, cap_path=cap_path,
                     append=append)

    def stop(self) -> None:
        """Stop playback. Never raises, never deadlocks the UI thread.

        The Qt main thread may NOT call player.stop() on a player that is
        actively rendering: libvlc 3's stop() joins the input thread, and on
        Windows the video-output teardown needs the video window's message
        pump — the very main thread sitting inside stop() → permanent hang
        (seen with a live 4K stream + attached HWND). Therefore a BUSY
        player is stopped on a daemon thread and awaited at most
        ``_STOP_WAIT_S`` seconds:

        - normal case: the stop finishes quickly; the same player keeps
          being used (mute + volume are restored),
        - hung case: a FRESH player is swapped in (volume + window binding
          reapplied) so the app keeps working; the wedged one is released
          by the daemon thread whenever libvlc unwedges. Worst case (VLC
          never returns) that one player — and its provider connection —
          leaks until process exit; that is strictly better than a frozen
          app holding the same connection forever.
        """
        try:
            log.info("display stop (state=%s busy=%s)",
                     self.state_name(), self.is_busy())
        except Exception:
            pass
        playing = False
        try:
            playing = bool(self.player.is_playing())
        except Exception:
            pass
        if self.media is None and not playing:
            try:
                self.player.stop()
            except Exception as exc:  # noqa: BLE001
                try:
                    log.warning("display stop: player.stop() failed: %r", exc)
                except Exception:
                    pass
            return
        # Busy: mute instantly first (non-blocking) so a hung stop can never
        # leave the old stream audible, then stop off the UI thread.
        try:
            self.player.audio_set_volume(0)
        except Exception:
            pass
        old = self.player
        done = threading.Event()
        swapped = threading.Event()

        def _teardown():
            try:
                old.stop()
            except Exception as exc:  # noqa: BLE001
                try:
                    log.warning("display stop: player.stop() failed: %r", exc)
                except Exception:
                    pass
            try:
                old.set_media(None)
            except Exception as exc:  # noqa: BLE001
                try:
                    log.warning("display stop: set_media(None) failed: %r",
                                exc)
                except Exception:
                    pass
            done.set()
            if swapped.is_set():
                # Main thread moved on with a fresh player — release ours.
                try:
                    old.release()
                except Exception:
                    pass
                try:
                    log.info("display stop: wedged player finally stopped; "
                             "released")
                except Exception:
                    pass

        threading.Thread(target=_teardown, name="mtp-display-stop",
                         daemon=True).start()
        if done.wait(_STOP_WAIT_S):
            self._apply_volume(self.player)   # undo the pre-stop mute
            return
        swapped.set()
        try:
            log.error("display stop: still hung after %.1fs — swapping in a "
                      "fresh player (old one leaks until it unwedges)",
                      _STOP_WAIT_S)
        except Exception:
            pass
        try:
            self.player = self.instance.media_player_new()
            self._setup_player(self.player)
            self._bind_window(self.player)
        except Exception as exc:  # noqa: BLE001
            try:
                log.error("display stop: fresh player swap failed: %r", exc)
            except Exception:
                pass

    def stop_and_release(self) -> None:
        """Full teardown: stop() → detach media → drop the Media reference.

        Every teardown path must use this so libvlc actually closes the input
        (provider stream or local DVR buffer file) before anything else —
        e.g. the DVR temp dir — is touched. Never raises. stop() bounds
        itself: a busy player is stopped off-thread and, if libvlc wedges,
        replaced by a fresh player (see VLCPlayer.stop).
        """
        self.stop()
        try:
            self.player.set_media(None)
        except Exception as exc:
            try:
                log.warning("stop_and_release: set_media(None) failed: %r", exc)
            except Exception:
                pass
        self.media = None
        try:
            log.info("display released (state=%s)", self.state_name())
        except Exception:
            pass

    def is_busy(self) -> bool:
        """True while a media is attached or playback is running."""
        try:
            return self.media is not None or bool(self.player.is_playing())
        except Exception:
            return False

    def state_name(self) -> str:
        """Human-readable libvlc state for the log (open/stop transitions)."""
        try:
            st = self.player.get_state()
            names = {
                vlc.State.NothingSpecial: "idle",
                vlc.State.Opening: "opening",
                vlc.State.Buffering: "buffering",
                vlc.State.Playing: "playing",
                vlc.State.Paused: "paused",
                vlc.State.Stopped: "stopped",
                vlc.State.Ended: "ended",
                vlc.State.Error: "error",
            }
            return names.get(st, str(st))
        except Exception:
            return "unknown"

    def pause(self) -> None:
        self.player.set_pause(1)

    def resume(self) -> None:
        self.player.set_pause(0)

    def toggle_pause(self) -> None:
        self.player.pause()

    def is_playing(self) -> bool:
        try:
            return bool(self.player.is_playing())
        except Exception as exc:
            try:
                log.debug("is_playing failed: %r", exc)
            except Exception:
                pass
            return False

    def is_seekable(self) -> bool:
        try:
            return self.player.is_seekable() != 0
        except Exception as exc:
            try:
                log.debug("is_seekable failed: %r", exc)
            except Exception:
                pass
            return False

    # ---- time / position (all milliseconds) ----
    def get_time(self) -> int:
        try:
            return self.player.get_time()
        except Exception as exc:
            try:
                log.debug("get_time failed: %r", exc)
            except Exception:
                pass
            return -1

    def get_length(self) -> int:
        try:
            return self.player.get_length()
        except Exception as exc:
            try:
                log.debug("get_length failed: %r", exc)
            except Exception:
                pass
            return 0

    def get_position(self) -> float:
        try:
            return self.player.get_position()
        except Exception as exc:
            try:
                log.debug("get_position failed: %r", exc)
            except Exception:
                pass
            return 0.0

    def seek_ms(self, delta_ms: int) -> None:
        length = self.get_length()
        current = self.get_time()
        if current < 0:
            current = 0
        target = max(0, current + delta_ms)
        if length > 0:
            target = min(target, length)
        self.player.set_time(int(target))
    def set_time(self, ms: int) -> None:
        self.player.set_time(int(ms))

    def jump_to_live(self) -> None:
        """Seek to the live edge of the timeshift buffer."""
        length = self.get_length()
        if length > 0:
            self.player.set_time(int(length))
        else:
            try:
                self.player.set_position(0.999)
            except Exception as exc:
                try:
                    log.warning("jump_to_live: set_position failed: %r", exc)
                except Exception:
                    pass

    # ---- audio ----
    def _apply_volume(self, player) -> None:
        """Best-effort volume on one specific player object (never raises)."""
        try:
            player.audio_set_volume(self._volume)
        except Exception:
            pass

    def set_volume(self, value: int) -> None:
        """Store + apply. Safe at any moment — always targets the CURRENT
        player: during a DVR handoff, while the previous player stops on a
        daemon thread, and after a hung-stop player swap."""
        self._volume = max(0, min(100, int(value)))
        self._apply_volume(self.player)

    def get_volume(self) -> int:
        try:
            return self.player.audio_get_volume()
        except Exception:
            return self._volume

    def toggle_mute(self) -> None:
        self._mute = not self._mute
        try:
            self.player.audio_toggle_mute()
        except Exception:
            pass

    def set_mute(self, on: bool) -> None:
        self._mute = bool(on)
        try:
            self.player.audio_set_mute(1 if on else 0)
        except Exception as exc:
            try:
                log.warning("set_mute failed: %r", exc)
            except Exception:
                pass

    def is_mute(self) -> bool:
        try:
            return self.player.audio_get_mute() != 0
        except Exception as exc:
            try:
                log.debug("is_mute failed: %r", exc)
            except Exception:
                pass
            return self._mute

    # ---- playback rate ----
    def set_rate(self, rate: float) -> None:
        """Playback speed (0.125 .. 5). Audio stays time-stretched."""
        try:
            self.player.set_rate(max(0.125, min(5.0, float(rate))))
        except Exception as exc:
            try:
                log.warning("set_rate failed: %r", exc)
            except Exception:
                pass

    def get_rate(self) -> float:
        try:
            return float(self.player.get_rate())
        except Exception:
            return 1.0

    # ---- subtitle tracks (embedded in the stream) ----
    def spu_tracks(self):
        """[(id, name), ...] of embedded subtitle tracks, decoded."""
        try:
            raw = self.player.video_get_spu_description()
            out = []
            for item in raw or []:
                tid = item[0]
                name = item[1]
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "replace")
                out.append((int(tid), str(name)))
            return out
        except Exception:
            return []

    def set_spu(self, spu_id: int) -> None:
        """Select an embedded subtitle track (-1 = subtitles off)."""
        try:
            self.player.video_set_spu_integer(int(spu_id))
        except Exception as exc:
            try:
                log.debug("set_spu failed: %r", exc)
            except Exception:
                pass


    # ---- video ----
    def set_scale_mode(self, mode: str) -> None:
        """'fit' (letterbox), 'stretch' (distort to fill) or 'crop' (zoom)."""
        self._scale_mode = mode if mode in ("fit", "stretch", "crop") else "fit"

    def apply_scale(self, w: int, h: int) -> None:
        """(Re)apply the scale mode for a widget of ``w x h`` pixels. Called
        on mode changes, resizes and after player swaps."""
        self._scale_wh = (int(w), int(h))
        self._apply_scale_to(self.player, w, h)

    def _apply_scale_to(self, player, w: int, h: int) -> None:
        mode = self._scale_mode
        if w <= 0 or h <= 0:
            return
        # Skip the (blocking) vout round-trips when the exact same mode and
        # size were already applied to the current player — resizes used to
        # re-send "None/None" to VLC dozens of times per drag for no reason.
        key = (mode, int(w), int(h))
        if key == self._scale_last:
            return
        self._scale_last = key
        try:
            # reset both, then force the one the mode needs
            try:
                player.video_set_aspect_ratio(None)
            except Exception:
                pass
            try:
                # python-vlc 3 has no video_set_crop_ratio; reset the classic way
                player.video_set_crop_geometry(None)
            except Exception:
                pass
            from math import gcd
            g = gcd(int(w), int(h)) or 1
            ratio = f"{int(w) // g}:{int(h) // g}"
            if mode == "stretch":
                player.video_set_aspect_ratio(ratio)
            elif mode == "crop":
                try:
                    player.video_set_crop_ratio(int(w) // g, int(h) // g)
                except Exception:
                    player.video_set_crop_geometry(ratio)
        except Exception as exc:
            try:
                log.debug("apply_scale failed: %r", exc)
            except Exception:
                pass

    def set_fullscreen(self, fullscreen: bool) -> None:
        try:
            self.player.set_fullscreen(int(bool(fullscreen)))
        except Exception as exc:
            try:
                log.warning("set_fullscreen failed: %r", exc)
            except Exception:
                pass

    def event_manager(self):
        return self.player.event_manager()
