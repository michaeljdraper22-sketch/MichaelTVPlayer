"""Thin wrapper around libvlc with timeshift (pause/rewind/back-to-live) support."""

import logging
import math
import os
import sys
import threading

import vlc

log = logging.getLogger("mtp")

# How long the UI may wait for a busy display player to stop before giving
# up on it and swapping in a fresh one (VLCPlayer.stop, see docstring there).
_STOP_WAIT_S = 2.5

# After unblock_hook fired (kill VLC's local peer sockets), how much longer
# the wedged stop gets to finish before the fresh-player swap really happens.
_UNBLOCK_GRACE_S = 2.0

# User-Agent presented to the provider when VLC opens a stream. Some CDNs
# (e.g. this account's) reject unknown/default agent strings with 520 while
# serving this exact one — and it is what the app's API session already uses.
USER_AGENT = "MichaelTVPlayer/1.0"


# ---- libvlc audio-timeline observability (2026-11-13 field report) -----
# "Audio ~1 s behind the picture on a Stremio VOD; a reload fixes it."
# libvlc KNOWS when its audio output damages the timeline — the aout names
# the damage and the amount: "buffer too late (%lld us): dropped",
# "playback too late (%lld): flushing buffers" / "... up-sampling" — but
# the app never subscribed to its log, so those occurrences left no trace
# and gave us nothing to fix. Capture is free in steady state (measured at
# verbosity 1: 314 messages during an open, ZERO during playback), so it
# is always on; matching lines land in player.log and arm
# VLCPlayer.take_aout_event() for a quiet audio re-anchor.
_AOUT_LATE = (
    "buffer too late",          # a single audio buffer arrived late, dropped
    "playback too late",        # the aout itself was late → flush / resample
    "playback way too late",
)
_AOUT_DEVICE = (
    "cannot start playing buffer",
    "couldn't use hardware sound buffer",
    "cannot lock buffer",
    "buffer overrun",
    "audio output failed",
)


def _log_first_int(args):
    """First integer argument of a libvlc log message, or None.

    libvlc's log callback hands us the raw va_list (an opaque pointer).
    On the Win64 ABI a va_list IS a pointer to the 8-byte argument slots,
    so slot 0 is the first argument — exactly what the aout's timing
    messages lead with ("playback too late (%lld): flushing buffers").
    Non-integer first arguments ("%s" messages) read as a pointer-sized
    value; callers that need a duration range-check it.
    """
    try:
        import ctypes
        if not args:
            return None
        return int(ctypes.cast(args, ctypes.POINTER(ctypes.c_int64))[0])
    except Exception:  # noqa: BLE001 — diagnosis must never break playback
        return None


def _aout_mark(msg: str):
    """Classify a libvlc log line, or None when it is not an aout event.

    "late" = the audio output declared data late (flush / resample / drop),
    "device" = the audio OUTPUT itself failed (buffer allocation, lock,
    overrun) — the other shape that leaves the sound behind the picture.
    """
    low = (msg or "").lower()
    if any(mark in low for mark in _AOUT_LATE):
        return "late"
    if any(mark in low for mark in _AOUT_DEVICE):
        return "device"
    return None


class AoutHealGate:
    """Should a libvlc audio-timeline complaint trigger a quiet re-anchor?

    Pure decision logic (no VLC, no Qt) so it is unit-tested offline. The
    re-anchor is cheap but not free (a brief audio gap), so it stays rare:
    only a LARGE declared loss counts (a small "buffer too late" is normal
    jitter), NEVER while paused (VLC re-prerolls on resume) or while the
    live chase recorder owns the connection.
    """

    MIN_LATE_US = 300_000      # 0.3 s of audio declared lost — not jitter
    COOLDOWN_S = 120.0         # at most one re-anchor per 2 min
    MAX_PER_MEDIA = 3          # and at most 3 for one stream

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        """New media: fresh budget."""
        self._last = None
        self._count = 0

    def should_heal(self, kind: str, us, playing: bool, paused: bool,
                    live: bool, now: float) -> bool:
        if kind != "late":
            return False          # device complaints: nothing to re-anchor
        if not playing or paused:
            return False
        if live:
            return False          # chase/DVR input belongs to the recorder —
                                  # never restart its audio mid-stream
        if self._count >= self.MAX_PER_MEDIA:
            return False
        try:
            if int(us) < self.MIN_LATE_US:
                return False
        except (TypeError, ValueError):
            return False          # unparsed amount: can't judge it
        if self._last is not None and now - self._last < self.COOLDOWN_S:
            return False
        self._last = now
        self._count += 1
        return True


def _slider_to_vlc_volume(slider: int) -> int:
    """Perceptual (dB-linear) volume taper: bar position 0..100 -> VLC
    percent. VLC 3's WASAPI output maps its percent onto the Windows
    session scalar CUBED (mmdevice.c: "vol = vol * vol * vol"), so feeding
    it a straight percent parks half the bar's travel below -18 dB and
    crams every usable listening level into the top sliver — the "quiet
    for 80% of the bar, then everything at once" feel. This maps each 10%
    of bar travel to the same ~6 dB loudness step (-60 dB .. 0 dB):
    vlc = 100 * 10^((bar-100)/100), with bar 0 a true mute."""
    s = max(0, min(100, int(slider)))
    if s <= 0:
        return 0
    return max(1, min(100, round(100.0 * 10.0 ** ((s - 100.0) / 100.0))))


def _vlc_to_slider_volume(vlc_pct: int) -> int:
    """Inverse of _slider_to_vlc_volume: VLC percent -> bar position 0..100
    (for read-back only; rounding makes it approximate at the edges)."""
    v = max(0, min(100, int(vlc_pct)))
    if v <= 0:
        return 0
    return max(0, min(100, round(100.0 + 100.0 * math.log10(v / 100.0))))


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


# One vlc.Instance per PLAYER (= per window): multi-window demands totally
# separate audio trees — volume/mute/opening a stream in one window must
# never disturb the other, and a shared instance does not guarantee that.
# Two instances coexisting in this process is field-proven by the
# sub-style rebuild path (a second instance is constructed mid-playback,
# then the old one goes idle; instances are never released — see
# VLCPlayer.stop_and_release). The dvr.py rule still holds WITHIN a
# window: the recorder always passes instance=<its player's instance>,
# never creating another one while recording.


class VLCPlayer:
    """Wraps a libvlc media player.

    Timeshift is enabled per-media via the input's ``input-timeshift`` option,
    which makes VLC continuously buffer the live input to a temporary file.
    This enables pause (picture freezes while buffering continues) and seeking
    within the buffered region. "Jump to live" seeks to the live edge.
    """

    def __init__(self, timeshift: bool = True, volume: int = 100,
                 network_caching: int = 1500, sub_args: list = None,
                 spu_delay_ms: int = 0, extra_args: list = None,
                 instance=None):
        nc = max(0, min(50000, int(network_caching)))
        args = [
            # never read/write the user's shared %APPDATA%\vlc config — the
            # app passes everything it needs explicitly (see main.py's
            # bundled-VLC isolation). NOTE: libvlc knows this option as
            # --ignore-config; a wrong name would make vlc.Instance(args)
            # return None and silently fall back to a no-args instance.
            "--ignore-config",
            "--no-video-title-show",
            # input stats ON: the VOD stall watchdog reads cumulative
            # demux bytes via libvlc_media_get_stats (with --no-stats the
            # counters read zero and the starvation trigger can never
            # fire); the cost is a few atomic adds per packet.
            "--stats",
            f"--network-caching={nc}",
            f"--live-caching={nc}",
            "--file-caching=1000",
            "--disc-caching=1000",
            # Multi-window audio divorce (2026-11-07): VLC's default
            # Windows output (mmdevice) applies volume/mute to the PROCESS
            # audio session scalar (see _slider_to_vlc_volume) — every
            # player in the process shares it, so two windows' sliders
            # dragged each other's audio around. DirectSound applies
            # volume per audio BUFFER (per player), giving each window a
            # truly independent volume/mute. Must be on EVERY instance:
            # one mmdevice player would drag the shared session for all.
            "--aout=directsound",
            # NO --avcodec-skiploopfilter here, ever: skipping the H.264
            # deblocking filter smears fast motion (sports) into visible
            # macroblock blur — field-reported as "color blurring" on
            # 2026-10-19. VLC's default (full deblocking) is what we want;
            # the CPU cost is fine on anything that decodes 1080i at all.
        ]
        # subtitle appearance (freetype options are instance-level only)
        args.extend([str(a) for a in (sub_args or [])])
        # test-only escape hatch (headless runs pass ["--vout=dummy"] so a
        # real-VLC test never pops a visible window / steals focus); the
        # app itself never sets it. A bad option can only make the args
        # Instance None, and the plain-instance fallback below covers it.
        args.extend([str(a) for a in (extra_args or [])])
        self.timeshift = timeshift
        # A caller may hand us an explicit instance (the dvr.py recorder
        # pattern); otherwise this player gets its OWN instance — one per
        # window, full audio separation (see the note above). Some VLC
        # builds reject unknown options and return None, so fall back to a
        # plain instance.
        self.instance = instance or (vlc.Instance(args) or vlc.Instance())
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
        # Source stream URL when the player is watching a LOCAL DVR buffer
        # file: the never-started watchdog's probe must dial the PROVIDER,
        # not requests-get a C:\ path (InvalidSchema noise, issue #12).
        self.source_url_hint = ""
        # Set by the UI (player_view): force-kills whatever local source the
        # wedged player's input thread is blocked reading from (relay client
        # sockets), so a hung stop can finish instead of forcing a swap.
        self.unblock_hook = None
        # last libvlc audio-timeline complaint (kind, us) — see _AOUT_LATE
        self._aout_evt = None
        self._aout_lock = threading.Lock()
        self._aout_logged = 0       # aout log lines written for this media
        self.on_aout_event = None   # optional hook; set by the UI
        self._setup_player(self.player)
        self.media = None
        self._install_vlc_log()

    def _install_vlc_log(self) -> None:
        """Subscribe to libvlc's own log, filtered to audio-timeline events.

        Verbosity 1 (warnings) costs nothing during playback — measured 0
        messages in 10 s of steady play, 314 during the open — so this is
        on for every player. A failed subscription is silent: diagnosis
        must never break playback."""
        try:
            cb = vlc.cb.LogCb(self._on_vlc_log)
            self.instance.set_log_verbosity(1)
            self.instance.log_set(cb, None)
            self._log_cb = cb     # CFUNCTYPE must outlive libvlc's slot
        except Exception as exc:  # noqa: BLE001
            try:
                log.debug("vlc log capture unavailable: %r", exc)
            except Exception:
                pass

    def _on_vlc_log(self, _data, _level, _ctx, fmt, args) -> None:
        """VLC thread context: record + log only, never touch Qt or stop."""
        try:
            try:
                msg = (fmt or b"").decode("utf-8", "replace")
            except Exception:
                return
            kind = _aout_mark(msg)
            if kind is None:
                return
            us = _log_first_int(args)
            # a badly damaged stream can repeat these; keep the log bounded
            self._aout_logged = getattr(self, "_aout_logged", 0) + 1
            try:
                if self._aout_logged <= 20:
                    log.warning("vlc aout: %s (amount=%s us) state=%s",
                                msg, us, self.state_name())
                else:
                    log.debug("vlc aout: %s (amount=%s us)", msg, us)
            except Exception:
                pass
            with self._aout_lock:
                self._aout_evt = (kind, us)
            hook = self.on_aout_event
            if hook is not None:
                try:
                    hook(kind, us)
                except Exception:
                    pass
        except Exception:  # noqa: BLE001
            pass

    def take_aout_event(self):
        """Pop the last audio-timeline complaint (kind, us), or None."""
        try:
            with self._aout_lock:
                evt, self._aout_evt = self._aout_evt, None
            return evt
        except Exception:  # noqa: BLE001
            return None

    def heal_audio_sync(self) -> bool:
        """Re-anchor the audio output at the current position.

        This is what the user's manual reload does for a ~1 s audio lag,
        minus the re-buffer: the active track is re-selected, which tears
        VLC's aout down and rebuilds it against the RUNNING picture clock,
        so the sound re-anchors to where the video actually is. The
        same-id call alone is a no-op in libvlc's ES layer, hence the
        disable/re-enable pair. Picture, position and subtitles untouched.
        """
        try:
            tid = self.active_audio()
            if tid is None or int(tid) < 1:
                return False
            self.player.audio_set_track(-1)
            self.player.audio_set_track(int(tid))
            # the rebuilt output adopts VLC's defaults for the session
            # scalar — re-assert OUR volume + mute (including the profanity
            # filter's, which only re-applies on a change) so a re-anchor
            # can never un-mute a muted stream or change the level.
            self._apply_volume(self.player)
            self._apply_effective_mute(self.player)
            return True
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("heal_audio_sync failed: %r", exc)
            except Exception:
                pass
            return False


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
        """VLC thread context: only thread-safe libvlc calls, no Qt."""
        try:
            self._start_ok = True   # playback-start watchdog clears here
        except Exception:
            pass
        try:
            # The vout is created around NOW. If it came up detached (the
            # binding raced a wedge-swap player replacement, or the wedged
            # co-player's late teardown invalidated this vout), libVLC is
            # already painting into its OWN top-level window — the stray
            # "pop-out video with no controls" the user cannot close.
            # Re-asserting the HWND here re-parents it onto our surface;
            # set_hwnd is thread-safe and a no-op when already parented.
            self._bind_window(self.player)
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
                      self.state_name(), feedback.redact_url(url))
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
                start_wait_s: float = 20.0,
                sub_file: str = None,
                network_caching_ms: int = 0,
                http_referer: str = None) -> None:
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
        self._aout_logged = 0        # fresh aout-log budget for this media
        self.stop_and_release()
        self.media = self.instance.media_new(url)
        try:
            # Some provider CDNs 520 on default agent strings — pin ours.
            self.media.add_option(f"http-user-agent={USER_AGENT}")
        except Exception:
            pass
        if http_referer:
            try:
                # Referer-locked CDNs (e.g. Bunny Stream embeds) 403 every
                # request without the embedding page's referer — VLC 3's
                # http-referrer sends it on the m3u8 AND its segments.
                self.media.add_option(f"http-referrer={http_referer}")
            except Exception:
                pass
        if network_caching_ms > 0:
            try:
                # Per-media read-ahead depth, overriding the instance-wide
                # --network-caching for this one input (the same mechanism
                # as per-entry :network-caching in .m3u playlists). VOD
                # gets a deep buffer so a CDN dip is absorbed invisibly;
                # plain live (timeshift) gets the two-batch floor from
                # player_view, because batched delivery drains a 1.5 s
                # cache at every seam. This is a fill-behind target, not a
                # gate: playback starts on the first decoded frames while
                # the cache fills during playback.
                self.media.add_option(
                    f":network-caching={int(network_caching_ms)}")
                if timeshift:
                    # --live-caching is the live-specific knob VLC reads;
                    # the per-media override must set BOTH or the instance
                    # default (1.5 s) can still win for a live input.
                    self.media.add_option(
                        f":live-caching={int(network_caching_ms)}")
            except Exception as exc:  # noqa: BLE001
                try:
                    log.warning("play_at: add network-caching failed: %r",
                                exc)
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
        if sub_file:
            # external subtitle file (e.g. the srt Stremio downloads for
            # its external players) — attach it as the media's sub-file
            try:
                self.media.add_option("sub-file=%s" % sub_file)
            except Exception as exc:  # noqa: BLE001
                try:
                    log.warning("play_at: add sub-file failed: %r", exc)
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
        # The vout can also appear detached a beat AFTER play() (wedge-swap
        # races): the Playing-event rebind covers the usual case, this short
        # ladder covers a vout that materializes late. Bound (4 calls) so it
        # can never flicker a healthy, already-parented vout.
        if self._window_id is not None:
            try:
                for delay in (0.5, 1.5, 3.5):

                    def _reassert_after_play(pl=self.player):
                        if pl is self.player:
                            try:
                                self._bind_window(pl)
                            except Exception:  # noqa: BLE001
                                pass

                    threading.Timer(delay, _reassert_after_play).start()
            except Exception:  # noqa: BLE001
                pass
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
                if self.state_name() == "ended":
                    # played AND finished inside the wait window — not
                    # the never-started black window this watches for
                    return
            except Exception:
                return
            from . import feedback
            feedback.stat("plays_never_started")
            log.error("playback never started within %.0fs (state=%s "
                      "url=%s) — probing stream", wait_s,
                      self.state_name(), feedback.redact_url(url))

            def _probe():
                try:
                    # probe the PROVIDER when the player watches the local
                    # DVR buffer — requests cannot fetch a C:\ path
                    # (issue #12)
                    target = url
                    if not target.startswith(("http://", "https://")) \
                            and self.source_url_hint:
                        target = self.source_url_hint
                    res = feedback.probe_url(target)
                    log.error("stream probe: %s — url=%s", res,
                              feedback.redact_url(target))
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
             start_seconds: float = 0.0, start_wait_s: float = 20.0,
             sub_file: str = None, network_caching_ms: int = 0,
             http_referer: str = None) -> None:
        self.play_at(url, start_seconds, timeshift=timeshift,
                     start_wait_s=start_wait_s, sub_file=sub_file,
                     network_caching_ms=network_caching_ms,
                     http_referer=http_referer)

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
                # Both players shared the video HWND while this one sat
                # wedged; the teardown above can invalidate the CURRENT
                # player's running video output, and libVLC then re-creates
                # it detached — its OWN small top-level window the app
                # cannot control (live-seen: switching from a hung Stremio
                # stream to live TV). Re-asserting the binding re-parents
                # the running vout back onto our surface. The recreation is
                # ASYNCHRONOUS, so one immediate re-parent can land before
                # it: re-assert a few times over the next seconds.
                try:
                    self._bind_window(self.player)
                except Exception:
                    pass
                try:
                    rebind = self.player
                    for delay in (0.4, 1.2, 2.5, 5.0, 9.0):

                        def _reassert(pl=rebind):
                            if pl is self.player:
                                try:
                                    self._bind_window(pl)
                                except Exception:  # noqa: BLE001
                                    pass

                        threading.Timer(delay, _reassert).start()
                except Exception:  # noqa: BLE001
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
        # Still hung. Before reaching for the player swap (two vouts on one
        # HWND — the frozen-picture / floating-window incident), let the UI
        # kill the wedged input's local peer (relay client sockets) and give
        # libvlc one short grace: with the peer gone the stop usually lands
        # in well under a second and the SAME player keeps serving.
        try:
            hook = self.unblock_hook
            if hook is not None:
                log.info("display stop: hung after %.1fs — kicking local "
                         "media sources before any player swap",
                         _STOP_WAIT_S)
                hook()
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("display stop: unblock hook failed: %r", exc)
            except Exception:
                pass
        if done.wait(_UNBLOCK_GRACE_S):
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

    def demuxed_bytes(self) -> int:
        """Cumulative demuxed bytes for the current media (VLC input
        stats). The VOD stall watchdog's starvation signal: a provider
        cut leaves VLC 'playing' with its CLOCK still advancing while
        nothing is demuxed — the frozen-clock test alone can't see it.
        -1 when stats are unavailable (the caller treats that as no
        signal, never as frozen). NB: this vlc.py binding's get_stats
        takes a CALLER-ALLOCATED struct — calling it bare raises
        TypeError."""
        try:
            m = self.player.get_media()
            if m is None:
                return -1
            stats = vlc.MediaStats()
            if not m.get_stats(stats):
                return -1
            return stats.demux_read_bytes or 0
        except Exception:
            return -1

    def displayed_pictures(self) -> int:
        """Cumulative displayed video pictures for the current media
        (VLC input stats; -1 when unavailable — the caller treats that
        as no signal, never as frozen). The VOD watchdog's third
        signal: a starved-but-trickling input keeps VLC 'playing' with
        its CLOCK and demux counters creeping while the video output
        holds the last picture — 'video stuck, time bar rolling'."""
        try:
            m = self.player.get_media()
            if m is None:
                return -1
            stats = vlc.MediaStats()
            if not m.get_stats(stats):
                return -1
            return stats.displayed_pictures
        except Exception:
            return -1

    def seek_ms(self, delta_ms: int) -> int:
        """Relative seek. Returns the clamped target ms (the caller's
        position tracker rebases on it — VLC's own clock only catches up
        after the seek lands)."""
        length = self.get_length()
        current = self.get_time()
        if current < 0:
            current = 0
        target = max(0, current + delta_ms)
        if length > 0:
            target = min(target, length)
        self.player.set_time(int(target))
        return int(target)
    def set_time(self, ms: int) -> None:
        self.player.set_time(int(ms))

    def set_position(self, frac: float) -> None:
        """Byte-fraction seek — the reliable axis for raw MPEG-TS inputs
        (no duration index, so time-based set_time lands imprecisely)."""
        self.player.set_position(max(0.0, min(1.0, float(frac))))

    def jump_to_live(self) -> None:
        """Seek to the live edge of the timeshift buffer — or the END of
        the file for VOD. The target lands 1.5 s SHORT of the length:
        real VLC clamps a set_time at exactly the length back to the last
        cue and then crawls for tens of seconds WITHOUT ever raising
        end-of-media (measured 16.8 s on a 20 s http mkv; the 2026-09-01
        18:13 incident — LIVE pressed twice on a Stremio episode, zero
        effect, autoplay never fired). 1.5 s short, VLC plays the final
        instants and reaches genuine "ended" in ~2 s, which is what the
        park-at-end scrubber and autoplay-next both key on."""
        length = self.get_length()
        if length > 0:
            self.player.set_time(max(0, int(length) - 1500))
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
        """Best-effort volume on one specific player object (never raises).
        The stored value is the 0..100 BAR position — the perceptual taper
        below converts it to VLC percent at the libvlc boundary."""
        try:
            player.audio_set_volume(_slider_to_vlc_volume(self._volume))
        except Exception:
            pass

    def set_volume(self, value: int) -> None:
        """Store + apply the 0..100 BAR position. Safe at any moment —
        always targets the CURRENT player: during a DVR handoff, while the
        previous player stops on a daemon thread, and after a hung-stop
        player swap."""
        self._volume = max(0, min(100, int(value)))
        self._apply_volume(self.player)

    def get_vlc_volume(self):
        """Raw libvlc percent of the CURRENT player, or None when there is
        no readable audio output (reads < 0 until the device is up, e.g.
        mid-open). The UI's volume pin compares this against the slider's
        taper — see PlayerView._enforce_volume."""
        try:
            vlc_pct = self.player.audio_get_volume()
        except Exception:
            return None
        if vlc_pct is None or int(vlc_pct) < 0:
            return None
        return int(vlc_pct)

    def get_volume(self) -> int:
        """The current BAR position (inverse-mapped from VLC's percent)."""
        vlc_pct = self.get_vlc_volume()
        if vlc_pct is None:
            return self._volume
        return _vlc_to_slider_volume(vlc_pct)

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
