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


def _rgb_int(hex_color: str, fallback: int) -> int:
    """'#RRGGBB' -> int for VLC's add_rgb options (they are 0xRRGGBB)."""
    try:
        h = str(hex_color).lstrip("#")
        if len(h) == 6:
            return int(h, 16)
    except Exception:
        pass
    return fallback


def subtitle_instance_args(appearance: dict) -> list:
    """Map the saved subtitle appearance onto libvlc instance arguments.

    VLC 3 has NO runtime API for text-renderer styling — these options are
    read once when the freetype module loads, i.e. at vlc.Instance() creation
    — so visual changes only take effect when the instance is rebuilt
    (PlayerView._reapply_sub_style does that without an app restart; the
    delay is the exception: it has a live API, see set_spu_delay). A size
    of 0 with defaults otherwise returns [] so the player launches with
    VLC's exact auto sizing.
    """
    ap = appearance or {}
    args = []
    font = str(ap.get("font", "") or "").strip()
    if font:
        args.append(f"--freetype-font={font}")
    size = int(ap.get("size", 0) or 0)
    if size > 0:
        args.append(f"--freetype-fontsize={size}")
    pos = int(ap.get("pos_pct", 0) or 0)
    if pos:
        # Mirror the overlay's raise above the picture bottom (see
        # CaptionOverlay.paintEvent): 4 % of the height + half the
        # position percentage, expressed at VLC's 1080p pixel reference
        # (VLC scales --sub-margin with the video height), so VLC-rendered
        # fallback tracks (bitmap DVB/PGS) anchor roughly where the app
        # overlay would put them. VLC's own default bottom padding stands
        # in for the overlay's control-bar inset.
        args.append(f"--sub-margin={round(0.04 * 1080 + pos * 5.4)}")
    text = _rgb_int(ap.get("text_color", ""), 0xFFFFFF)
    if text != 0xFFFFFF:
        args.append(f"--freetype-color={text}")
    if ap.get("bg_enabled"):
        args.append(f"--freetype-background-color="
                    f"{_rgb_int(ap.get('bg_color', ''), 0)}")
        op = max(0, min(100, int(ap.get("bg_opacity", 50) or 0)))
        args.append(f"--freetype-background-opacity={round(op * 255 / 100)}")
    if ap.get("outline_enabled", True):
        oc = _rgb_int(ap.get("outline_color", ""), 0)
        if oc != 0:      # black is VLC's default outline color
            args.append(f"--freetype-outline-color={oc}")
        th = max(0, min(50, int(ap.get("outline_thickness", 4) or 4)))
        if th != 4:
            args.append(f"--freetype-outline-thickness={th}")
    else:
        # the default look HAS an outline — turning it off must be explicit
        args.append("--freetype-outline-opacity=0")
    return args


class VLCPlayer:
    """Wraps a libvlc media player.

    Timeshift is enabled per-media via the input's ``input-timeshift`` option,
    which makes VLC continuously buffer the live input to a temporary file.
    This enables pause (picture freezes while buffering continues) and seeking
    within the buffered region. "Jump to live" seeks to the live edge.
    """

    def __init__(self, timeshift: bool = True, volume: int = 100,
                 network_caching: int = 1500, sub_args: list = None,
                 spu_delay_ms: int = 0):
        nc = max(0, min(50000, int(network_caching)))
        args = [
            # never read/write the user's shared %APPDATA%\vlc config — the
            # app passes everything it needs explicitly (see main.py's
            # bundled-VLC isolation). NOTE: libvlc knows this option as
            # --ignore-config; a wrong name would make vlc.Instance(args)
            # return None and silently fall back to a no-args instance.
            "--ignore-config",
            "--no-video-title-show",
            "--no-stats",
            f"--network-caching={nc}",
            f"--live-caching={nc}",
            "--file-caching=1000",
            "--disc-caching=1000",
            "--avcodec-skiploopfilter=1",
        ]
        # subtitle appearance (freetype options are instance-level only)
        args.extend([str(a) for a in (sub_args or [])])
        self.timeshift = timeshift
        # A few caching/display options. Some VLC builds reject unknown options
        # and return None, so fall back to a plain instance if that happens.
        self.instance = (
            vlc.Instance(args) or vlc.Instance()
        )
        self.player = self.instance.media_player_new()
        self._volume = max(0, min(100, int(volume)))
        self._spu_delay_ms = int(spu_delay_ms)   # desired sub delay (ms)
        self._mute = False            # desired mute state (re-applied per player)
        self._filter_mute = False     # profanity filter's mute (layered on top)
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
        self._apply_spu_delay(player)
        self._apply_effective_mute(player)
        if self._scale_wh:
            self._apply_scale_to(player, *self._scale_wh)
        try:
            em = player.event_manager()
            em.attach(vlc.EventType.MediaPlayerPlaying, self._on_playing_event)
            try:
                em.attach(vlc.EventType.MediaPlayerEncounteredError,
                          self._on_vlc_error_event)
                em.attach(vlc.EventType.MediaPlayerBuffering,
                          self._on_buffering_event)
            except Exception:
                pass   # older binding without these events — log only
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
            self._start_ok = True   # playback-start watchdog clears here
        except Exception:
            pass
        try:
            self._apply_volume(self.player)
            self._apply_effective_mute(self.player)
            self._unmute_late(self.player)
            self._apply_spu_delay(self.player)
        except Exception:
            pass

    def _on_vlc_error_event(self, _event) -> None:
        """VLC thread context: libvlc gives no error detail, so record the
        event + state and let the auto-diagnostics report carry it."""
        try:
            from . import feedback
            feedback.stat("vlc_errors")
            url = getattr(self, "_current_url", "")
            log.error("VLC playback error event (state=%s url=%s)",
                      self.state_name(), url)
        except Exception:
            pass

    def _on_buffering_event(self, event) -> None:
        """VLC thread context: count REBUFFER events (a drop back below
        99%% cache after playback was established), not every cache tick."""
        try:
            cache = getattr(event, "u", None)
            cache = getattr(cache, "new_cache", -1.0) if cache else -1.0
            was = getattr(self, "_last_cache", 100.0)
            self._last_cache = cache
            if was >= 99.0 and 0.0 <= cache < 99.0 \
                    and getattr(self, "_start_ok", False):
                from . import feedback
                feedback.stat("rebuffer_events")
        except Exception:
            pass

    def _unmute_late(self, player) -> None:
        """Re-assert volume + mute ~1.2 s after they were first applied.

        On Windows the audio device opens asynchronously around the
        Playing event; a volume/mute value set before the device exists
        can be silently discarded — one retry closes that window without
        touching Qt (thread-safe libvlc audio calls only)."""
        pl = player
        try:
            tid = self._unmute_tid = (self._unmute_tid or 0) + 1
        except AttributeError:
            tid = self._unmute_tid = 1

        def _retry():
            if tid != self._unmute_tid or pl is not self.player:
                return
            try:
                self._apply_volume(pl)
                self._apply_effective_mute(pl)
            except Exception:
                pass
        threading.Timer(1.2, _retry).start()

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
                record_path: str = None,
                append: bool = False, timeshift: bool = None,
                start_wait_s: float = 20.0) -> None:
        """Play ``url``, optionally starting at ``start_seconds``.

        ``:start-time=`` makes VLC open directly at the target position — no
        flash of the beginning followed by a seek (that was the rewind jank).

        ``record_path`` attaches a kept MPEG-TS recording file to the SAME
        single connection through VLC stream-output duplication.

        ``timeshift`` (None = the instance default) must be False for
        seekable VOD: VLC's input-timeshift runs the whole input through
        its pause/rewind cache, which drifts A/V sync over a movie and
        fights the local relay's single connection. Live needs it for DVR.
        """
        try:
            kind = "live" if url.startswith(("http://", "https://")) else "file"
            log.info("display open kind=%s start=%.1fs rec=%s "
                     "append=%s (prev_state=%s busy=%s)",
                     kind, float(start_seconds), bool(record_path),
                     bool(append), self.state_name(),
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
        try:
            # Prefer English audio at VLC's own ES selection (streams with
            # several dubs otherwise open on the mux order, often not
            # English). The UI's tick enforcement re-asserts a user pick
            # or the English default once the track list is known.
            self.media.add_option(":audio-language=en,eng")
        except Exception:
            pass
        branches = []
        if record_path:
            branches.append("dst=display")
            rec = record_path.replace("\\", "/")
            branches.append(f"dst=std{{access=file,mux=ts,dst='{rec}'}}")
            sout = "#duplicate{{{}}}".format(",".join(branches))
            self.media.add_option(f":sout={sout}")
            if append:
                self.media.add_option(":sout-file-append")
        elif (self.timeshift if timeshift is None else timeshift) \
                and url.startswith(("http://", "https://")):
            # Timeshift for live inputs only (never for local buffer files
            # and never for seekable VOD), and only without a sout chain
            # (timeshift + sout conflict).
            try:
                self.media.add_option("input-timeshift=1")
                self.media.add_option("timeshift-granularity=50")
            except Exception as exc:  # noqa: BLE001
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
        self._watch_playback_start(url, wait_s=start_wait_s)

    def _watch_playback_start(self, url: str, wait_s: float = 20.0) -> None:
        """Daemon timer: if playback never reaches Playing within ~20 s,
        that is the classic 'black window, no error' failure — log it at
        ERROR (auto-report) and probe the stream URL so the report shows
        whether the provider's stream itself is dead."""
        self._current_url = url
        self._start_ok = False
        self._last_cache = 100.0
        try:
            self._start_token = getattr(self, "_start_token", 0) + 1
        except Exception:
            self._start_token = 1
        token = self._start_token

        def _check():
            if token != getattr(self, "_start_token", 0):
                return   # a newer play_at superseded this one
            try:
                if self._start_ok or self.player.is_playing():
                    from . import feedback
                    feedback.stat("plays_started")
                    return
            except Exception:
                return
            from . import feedback
            feedback.stat("plays_never_started")
            log.error("playback never started within %.0fs (state=%s "
                      "url=%s) — probing stream", wait_s,
                      self.state_name(), url)

            def _probe():
                try:
                    res = feedback.probe_url(url)
                    log.error("stream probe: %s — url=%s", res, url)
                except Exception:
                    pass
            threading.Thread(target=_probe, name="mtp-probe",
                             daemon=True).start()

        try:
            t = threading.Timer(wait_s, _check)
            t.daemon = True
            t.start()
        except Exception:
            pass

    def play(self, url: str, timeshift: bool = None,
             start_seconds: float = 0.0, start_wait_s: float = 20.0) -> None:
        self.play_at(url, start_seconds, timeshift=timeshift,
                     start_wait_s=start_wait_s)

    def play_and_record(self, url: str, path: str, append: bool = False) -> None:
        """Backwards-compatible wrapper around play_at()."""
        self.play_at(url, 0.0, record_path=path, append=append)

    def play_outputs(self, url: str, record_path: str = None,
                     append: bool = False) -> None:
        """Watch ``url`` on the display while forking extra outputs from the
        SAME single connection (kept for API compatibility — everything
        funnels into play_at now)."""
        self.play_at(url, 0.0, record_path=record_path, append=append)

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
        # Busy: pause instantly first (non-blocking) so a hung stop can
        # never leave the old stream audible, then stop off the UI thread.
        # (Pause, NOT mute/volume=0: those write audio-session state that
        # outlives the player on Windows — a lost restore after a swap
        # meant silence no click could undo.)
        try:
            self.player.set_pause(1)
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
            self._unmute_late(self.player)
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

    def set_position(self, frac: float) -> None:
        """Byte-fraction seek — the reliable axis for raw MPEG-TS inputs
        (no duration index, so time-based set_time lands imprecisely)."""
        self.player.set_position(max(0.0, min(1.0, float(frac))))

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
        """Absolute toggle off the DESIRED state — never off what VLC
        happens to report mid-swap (a polled flip during a player swap
        desynced the two and re-muted behind the user's back)."""
        self.set_mute(not self._mute)

    def set_mute(self, on: bool) -> None:
        self._mute = bool(on)
        try:
            self._apply_effective_mute(self.player)
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("set_mute failed: %r", exc)
            except Exception:
                pass

    def set_filter_mute(self, on: bool) -> None:
        """Profanity-filter mute — LAYERED on top of the user's mute, never
        touching it: the mute button keeps showing the user's own choice,
        and clearing either one re-applies the other. Thread-safe (called
        from the filter engine's timer on the Qt thread)."""
        on = bool(on)
        if on == self._filter_mute:
            return
        self._filter_mute = on
        try:
            self._apply_effective_mute(self.player)
        except Exception as exc:  # noqa: BLE001
            try:
                log.debug("set_filter_mute failed: %r", exc)
            except Exception:
                pass

    def _apply_effective_mute(self, player) -> None:
        """The ONE place audio mute is written: user OR filter."""
        try:
            player.audio_set_mute(1 if (self._mute or self._filter_mute)
                                  else 0)
        except Exception:
            pass

    def is_mute(self) -> bool:
        """The user's DESIRED mute state — the single source of truth.

        Querying the player instead made every stop/swap race observable:
        the UI poll re-checked the mute button off a stale/true reading
        and the next poke re-applied mute over the user's click."""
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

    # ---- subtitles (embedded stream tracks) ----
    def set_spu_delay(self, ms: int) -> None:
        """Shift subtitle timing (positive = later, negative = earlier).

        Unlike the visual styling this has a REAL runtime API — it applies
        immediately, mid-playback, audio untouched. The desired value is
        stored here and re-applied on fresh players (hung-stop swaps start
        at 0) and on every Playing event, mirroring the volume handling."""
        self._spu_delay_ms = int(ms)
        self._apply_spu_delay(self.player)

    def get_spu_delay(self) -> int:
        """Current subtitle delay in ms (the stored DESIRED value)."""
        return self._spu_delay_ms

    def _apply_spu_delay(self, player) -> None:
        try:
            player.video_set_spu_delay(int(self._spu_delay_ms) * 1000)
        except Exception as exc:  # noqa: BLE001
            try:
                log.debug("_apply_spu_delay failed: %r", exc)
            except Exception:
                pass

    def spu_tracks(self) -> list:
        """[(id, name), ...] subtitle tracks of the current media.

        Empty until VLC has parsed the elementary streams — a remote MKV can
        take a couple of seconds after Playing before the SRT tracks show up,
        so callers should poll rather than trust one early read. python-vlc
        hands the names back as bytes; they are decoded here, and VLC's
        leading "Disable" pseudo-track (id 0) is dropped — the UI has its
        own Off entry."""
        try:
            desc = self.player.video_get_spu_description() or []
            out = []
            for track in desc:
                try:
                    name = track[1]
                    if isinstance(name, bytes):
                        name = name.decode("utf-8", "replace")
                    if not name or name.strip().lower() == "disable":
                        continue
                    out.append((int(track[0]), name))
                except Exception:
                    continue
            return out
        except Exception:
            return []

    def active_spu(self) -> int:
        """Currently selected subtitle track id, -1 when none (or unknown)."""
        try:
            return int(self.player.video_get_spu())
        except Exception:
            return -1

    def set_spu(self, spu_id: int) -> None:
        """Select a subtitle track by id (``-1`` disables subtitles).

        Thin call only — the desired state is owned and re-asserted by the
        UI (VLC re-selects a stream's own default track on media opens and
        ES updates, and a fresh player after a hung-stop swap loses the
        selection entirely, so a one-shot call is never enough)."""
        try:
            self.player.video_set_spu(int(spu_id))
        except Exception as exc:  # noqa: BLE001
            try:
                log.debug("set_spu(%r) failed: %r", spu_id, exc)
            except Exception:
                pass

    # ---- audio tracks (embedded stream tracks) ----
    def audio_tracks(self) -> list:
        """[(id, name), ...] audio tracks of the current media.

        Same contract as spu_tracks(): empty until VLC has parsed the
        elementary streams (poll, don't trust one early read), bytes names
        decoded here, and VLC's leading "Disable" pseudo-track dropped —
        for audio it carries id -1 (audio ids count from 1)."""
        try:
            desc = self.player.audio_get_track_description() or []
            out = []
            for track in desc:
                try:
                    tid = int(track[0])
                    name = track[1]
                    if isinstance(name, bytes):
                        name = name.decode("utf-8", "replace")
                    if tid < 1 or not name \
                            or name.strip().lower() == "disable":
                        continue
                    out.append((tid, name))
                except Exception:
                    continue
            return out
        except Exception:
            return []

    def active_audio(self) -> int:
        """Currently selected audio track id, -1 when none (or unknown)."""
        try:
            return int(self.player.audio_get_track())
        except Exception:
            return -1

    def set_audio(self, track_id: int) -> None:
        """Select an audio track by id. Thin call only — the desired state
        is owned and re-asserted by the UI, exactly like set_spu (VLC
        re-selects a stream's default track on media opens and ES updates,
        and fresh players after hung-stop swaps lose the selection)."""
        try:
            self.player.audio_set_track(int(track_id))
        except Exception as exc:  # noqa: BLE001
            try:
                log.debug("set_audio(%r) failed: %r", track_id, exc)
            except Exception:
                pass

    # ---- video ----
    def video_size(self) -> tuple:
        """Decoded video size (w, h) of the main vout, (0, 0) while there
        is none (before playback starts / after stop — video_get_size
        raises then, and python-vlc hands 0s back in other transient
        states). Callers treat (0, 0) as "unknown, keep the fallback"."""
        try:
            w, h = self.player.video_get_size(0)
            return int(w or 0), int(h or 0)
        except Exception:  # noqa: BLE001
            return (0, 0)

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
