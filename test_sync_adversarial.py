# -*- coding: utf-8 -*-
"""Stage-3 offline adversarial regression harness for the live-caption sync.

Replays the failure sequences observed in stages 1-2 as simulated adversarial
input against the REAL caption pipeline (CCExtractor tailing a DVR buffer
file grown from the real NFL Network recording) while the DISPLAY side is a
mock player running on a virtual clock:

    (a) cold join, slowly growing display-axis divergence, then a wedged
        anchor (anchor+store displaced together, cue releases frozen) that
        only the caption-stopped watchdog can recover
    (b) pause 60 s -> resume carrying a +25 s VLC-number discontinuity, 3x
    (c) jump-to-live while the CCX lag L swells 1 -> 20 s, then recedes
    (d) provider bursts: 30 s of content lands at once after a 30 s stall,
        so the wall frontier under-credits 15 s per event
    (e) CCX caption axis running 2x wall
    (f) scrub back 2 min (+ forward again), forced rebase coherence,
        live delay_ms shift
    (g) 0.2x-delivery trickle with <6 s freeze/thaw cycles (WP2 freeze-
        aware clock)
    (h) injected set_time no-op wedge at the true edge (WP2 escalation)
    (i) provider-lag L ramp/drain cycles at production flush cadence:
        painted displacement through the swings + store/scrub coherence
        + a lone edge-glitch must not slam the store (WP3)

Assertions in every scenario (stage-3 acceptance, WP0 metric):
  - PAINTED-CUE metric: for every sampled tick that painted lines, the
    painted text is matched against RELEASED cues (exact visible-line
    match) and scored |disp - clamp(disp, that cue's raw window)| — an
    UNBOUNDED error (the retired metric only sampled cues that already
    covered the display, so its "p95 0.29" was saturation, not sync).
    Roll-up screens repeat lines: several released cues can match the
    painted text — the score takes the minimum error over the matches and
    counts the sample as ambiguous (diagnostic). The exact-window text
    match (matched cue's window covers the display position) is the
    PRIMARY assertion; the old +/-3 s neighbor/substring acceptance is
    kept as a diagnostic counter only.
  - no silent-stop stretch (truth active + cue arrived + nothing painted)
    beyond ~5 s without recovery — the watchdog contract (stop metric);
  - scrubbed-back cues stay coherent after rebases (painted-cue p95 +
    exact-window rate);
  - fault scenarios assert OUTCOMES (post-fault painted text correct
    within N s, no blank beyond the watchdog window), not mechanism
    counts — recovery via any internal path must satisfy them.

Checks are tagged *mechanism* (must pass in every regime) or
*data-limited* (gated on the scenario's L(t) profile / feed cadence), so
provider weather cannot mask real regressions or vice versa.

Wall-clock determinism: player_view reads the wall clock ONLY through its
now_s() seam; this harness rebinds it to the virtual clock VT, so every
watchdog / cooldown / dead-reckoning gate runs on scripted time.

Headless: offscreen Qt, no window, no audio, no network. The 70 s recording
is looped with PCR/PTS restamping (each loop trimmed to its last PCR packet,
so the PCR axis stays exactly monotonic across joins) and the buffer grows
for minutes. The INSTALLED CCExtractor is pinned — it is the only build that
streams SRT through pipes incrementally (the vendored 0.88 buffers stdin to
EOF); it parses this 4K HEVC recording at ~1.1x realtime, which paces the
harness to roughly real time (~35 min for the full matrix).

Usage:
  .venv\\Scripts\\python.exe -X utf8 test_sync_adversarial.py [--quick]
"""
import os
import shutil
import statistics
import sys
import tempfile
import time as _real_time
from bisect import bisect_left, bisect_right
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src import live_cc as live_cc_mod  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import (PlayerView,  # noqa: E402
                                _CHASE_SAFETY_S)
from src.ui.caption_overlay import visible_lines  # noqa: E402

RECORDING = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "TV Recordings",
                         "US_NFL_NETWORK_HD_20260820_171631.ts")
QUICK = "--quick" in sys.argv
DUR = 0.35 if QUICK else 1.0        # scenario duration factor
_only = [a[len("--only:"):] for a in sys.argv if a.startswith("--only:")]
ONLY = _only[0].split(",") if _only else None

PASS = []
FAIL = []
BY_KIND = {"mechanism": [0, 0], "data-limited": [0, 0]}   # [pass, fail]


def check(name, cond, detail="", kind="mechanism"):
    """Record one assertion. ``kind`` labels the regime dependence:
    *mechanism* checks must pass in every delivery regime; *data-limited*
    checks are gated on the scenario's L(t) profile / feed cadence (their
    bounds grow with the scripted provider lag)."""
    cond = bool(cond)
    (PASS if cond else FAIL).append(name)
    k = BY_KIND[kind if kind in BY_KIND else "mechanism"]
    k[0 if cond else 1] += 1
    print(("  ok   " if cond else "  FAIL ") + name
          + ("" if kind == "mechanism" else f"   [{kind}]")
          + (f"   [{detail}]" if detail else ""), flush=True)


# ----------------------------------------------------------------------------
# virtual time: player_view's now_s() seam is rebound to this clock, so every
# wall-gated path (caption clock, watchdog, cooldowns) runs on scripted time
# ----------------------------------------------------------------------------
class VirtualTime:
    def __init__(self, t0=1_000_000.0):
        self.t = t0

    def time(self):
        return self.t

    def advance(self, dt):
        self.t += dt


VT = VirtualTime()


# ----------------------------------------------------------------------------
# L(t) profiles: the scripted CCX pipeline lag each scenario runs under.
# A scenario's data-limited check bounds quote its profile, so a bound that
# grows with L cannot mask a mechanism failure.
# ----------------------------------------------------------------------------
class LagProfile:
    """Deterministic L(t): fn(elapsed_seconds) -> lag seconds."""

    def __init__(self, name, fn):
        self.name = name
        self.fn = fn
        self.t0 = None

    def now(self):
        if self.t0 is None:
            self.t0 = VT.t
        return max(0.0, float(self.fn(VT.t - self.t0)))

    __call__ = now

    def __str__(self):
        return self.name


def lag_const(v):
    return LagProfile(f"const({v:g}s)", lambda el: v)


def lag_swell_then_recede(swell_s, hold_s, hi, lo, recede_s, floor):
    """Scenario c: L ramps lo -> hi over swell_s, holds, then drains to
    floor at recede_s per second."""
    def fn(el):
        if el < swell_s:
            return lo + (hi - lo) * (el / swell_s)
        if el < swell_s + hold_s:
            return hi
        return max(floor, hi - (el - swell_s - hold_s) * recede_s)
    return LagProfile(f"swell({lo:g}->{hi:g}s over {swell_s:g}s, "
                      f"drain to {floor:g}s)", fn)


# ----------------------------------------------------------------------------
# looped, restamped content: the 70 s recording repeated with continuous
# PCR + PES PTS/DTS so the buffer can grow for minutes
# ----------------------------------------------------------------------------
class LoopedTS:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        assert d[0] == 0x47, "recording is not packet aligned at 0"
        self.pcrs = []                   # (byte_offset, pcr_seconds)
        self.edits = []                  # (byte_offset, kind) pcr|pts|dts
        n = len(d)
        for p in range(0, n - 188 + 1, 188):
            if d[p] != 0x47:
                continue
            pui = bool(d[p + 1] & 0x40)
            afc = (d[p + 3] >> 4) & 0x3
            payload_off = 4
            if afc & 2:
                afl = d[p + 4]
                if afl > 0 and d[p + 5] & 0x10:      # PCR flag
                    q = p + 6
                    base = ((d[q] << 25) | (d[q + 1] << 17)
                            | (d[q + 2] << 9) | (d[q + 3] << 1)
                            | (d[q + 4] >> 7))
                    self.pcrs.append((p, base / 90000.0))
                    self.edits.append((p + 6, "pcr"))
                payload_off = 5 + d[p + 4]
            if pui and (afc & 1) and payload_off + 9 <= p + 188:
                q = p + payload_off
                if d[q] == 0 and d[q + 1] == 0 and d[q + 2] == 1 \
                        and d[q + 3] not in (0xBA, 0xBB, 0xBC):
                    flags = d[q + 7]
                    pos = q + 9
                    if flags & 0x80 and pos + 5 <= p + 188:
                        self.edits.append((pos, "pts"))
                        pos += 5
                    if flags & 0x40 and pos + 5 <= p + 188:
                        self.edits.append((pos, "dts"))
        assert self.pcrs, "no PCR in recording"
        self.pcr_bytes = [b for b, _ in self.pcrs]
        self.pcr_vals = [v for _, v in self.pcrs]
        self.first_pcr = self.pcr_vals[0]
        self.span = self.pcr_vals[-1] - self.first_pcr
        self.span90 = int(round(self.span * 90000))
        # trim each loop at the last PCR packet: the trailing ~0.4 s (no PCR
        # after it) would make the next loop's first PCR sit BELOW the tail
        # content and regress the head axis at every loop join
        self.end_byte = self.pcr_bytes[-1] + 188
        self.size = n
        self.edits.sort(key=lambda e: e[0])
        self.edit_pos = [e[0] for e in self.edits]

    def chunk(self, state, c_target):
        """(bytes, new_state, content_reached): append from ``state``
        (loop, byte) until the PCR-content head reaches >= ``c_target``.
        Consecutive calls are byte-contiguous; appends end on PCR packets."""
        loop, byte = state
        parts = []
        for _ in range(64):                      # loop-crossing safety
            base = loop * self.span
            intra_t = c_target - base
            if intra_t < self.span:
                i = bisect_left(self.pcr_vals, intra_t - 1e-9)
                if i < len(self.pcrs):
                    hi = self.pcr_bytes[i]
                    if hi <= byte:
                        hi = self.end_byte
                    parts.append(self._loop_bytes(loop, byte, hi))
                    return (b"".join(parts), (loop, hi),
                            self.pcr_vals[i] + base)
            parts.append(self._loop_bytes(loop, byte, self.end_byte))
            loop += 1
            byte = 0
        return b"", (loop, byte), c_target

    def _loop_bytes(self, loop, lo, hi):
        if loop == 0:
            return self.data[lo:hi]
        buf = bytearray(self.data[lo:hi])
        off90 = loop * self.span90
        i0 = bisect_left(self.edit_pos, lo)
        for i in range(i0, len(self.edits)):
            pos, kind = self.edits[i]
            if pos + 6 > hi:
                break
            q = pos - lo
            if kind == "pcr":
                base = ((buf[q] << 25) | (buf[q + 1] << 17)
                        | (buf[q + 2] << 9) | (buf[q + 3] << 1)
                        | (buf[q + 4] >> 7))
                base = (base + off90) & 0x1FFFFFFFF
                buf[q] = (base >> 25) & 0xFF
                buf[q + 1] = (base >> 17) & 0xFF
                buf[q + 2] = (base >> 9) & 0xFF
                buf[q + 3] = (base >> 1) & 0xFF
                buf[q + 4] = (buf[q + 4] & 0x7F) | ((base & 1) << 7)
            else:
                v = (((buf[q] >> 1) & 0x07) << 30) | (buf[q + 1] << 22) \
                    | (((buf[q + 2] >> 1) & 0x7F) << 15) | (buf[q + 3] << 7) \
                    | (buf[q + 4] >> 1)
                v = (v + off90) & 0x1FFFFFFFF
                buf[q] = (buf[q] & 0xF0) | 0x01 | ((v >> 29) & 0x0E)
                buf[q + 1] = (v >> 22) & 0xFF
                buf[q + 2] = 0x01 | (((v >> 14) & 0x7F) << 1)
                buf[q + 3] = (v >> 7) & 0xFF
                buf[q + 4] = 0x01 | ((v & 0x7F) << 1)
        return bytes(buf)


# ----------------------------------------------------------------------------
# mock display player: content-axis playback with scriptable distortions
# ----------------------------------------------------------------------------
class FakeVLC:
    def __init__(self):
        self.axis_offset = 0.0        # vlc_number = content + axis_offset
        self.speed_warp = 1.0         # content advance per wall second
        self.base_content = 0.0
        self.base_T = VT.t
        self.rate = 1.0
        self.paused = False
        self.state = "playing"
        self.max_content = None       # cannot display past the write head
        self.commanded = []
        self.wedged = False           # WP2 h: set_time no-ops, raw frozen,
        #                              # still "playing"; play_at clears it

    def wedge(self):
        """WP2 scenario h: demuxer-blocked at the tail of the growing
        buffer while still reporting "playing" (the 2026-08-21 night:
        raw pinned at 94.18 for 7 minutes across pause/resume/jump/seeks).
        set_time becomes a no-op and get_time freezes where it was; only
        play_at (a fresh open of the buffer file) recovers."""
        self.base_content = self.get_time() / 1000.0 - self.axis_offset
        self.base_T = VT.t
        self.wedged = True

    def renumber(self, step):
        """VLC numbers jump by `step`; the frames on screen do not move."""
        self.axis_offset += step

    def play_at(self, url, t, record_path=None, timeshift=False):
        self.commanded.append(("play_at", t))
        self.wedged = False           # a fresh open revives the wedge
        self.base_content = float(t) - self.axis_offset
        self.base_T = VT.t
        self.paused = False
        self.state = "playing"

    def set_time(self, ms):
        self.commanded.append(("set_time", ms / 1000.0))
        if self.wedged:
            return                    # the no-op under test
        self.base_content = ms / 1000.0 - self.axis_offset
        self.base_T = VT.t

    def get_time(self):
        if self.wedged:
            return int((self.base_content + self.axis_offset) * 1000)
        adv = 0.0 if self.paused else \
            (VT.t - self.base_T) * self.rate * self.speed_warp
        c = self.base_content + adv
        if self.max_content is not None and c > self.max_content:
            c = self.max_content          # underrun at the edge: freeze,
            self.base_content = c         # then resume 1:1 once data lands
            self.base_T = VT.t
        return int((c + self.axis_offset) * 1000)

    def is_playing(self):
        return not self.paused and self.state == "playing"

    def state_name(self):
        return self.state

    def pause(self):
        self.base_content = self.get_time() / 1000.0 - self.axis_offset
        self.paused = True

    def resume(self):
        self.base_T = VT.t
        self.paused = False

    def set_rate(self, r):
        self.base_content = self.get_time() / 1000.0 - self.axis_offset
        self.base_T = VT.t
        self.rate = float(r)

    # -- stubs unused by the chase path --
    def get_length(self):
        return 0

    def spu_tracks(self):
        return []

    def set_spu(self, tid):
        pass

    def active_spu(self):
        return -1

    def video_size(self):
        return (1280, 720)

    def set_volume(self, v):
        pass

    def is_mute(self):
        return True

    def set_mute(self, on):
        pass

    def set_filter_mute(self, on):
        pass

    def stop_and_release(self):
        pass

    def seek_ms(self, ms):
        pass

    def jump_to_live(self):
        pass

    def set_spu_delay(self, ms):
        pass

    def set_window(self, wid):
        pass

    def set_scale_mode(self, m):
        pass

    def apply_scale(self, w, h):
        pass

    def play(self, url, timeshift=False, start_seconds=0.0):
        pass


class FakeDVR:
    def __init__(self, buf):
        self.buffer_file = lambda: buf
        self.file_path = buf
        self.running = True
        self._dir = None

    def stop(self, delete=False):
        self.running = False

    def safe_stop(self, delete=True):
        self.running = False


# ----------------------------------------------------------------------------
# cue release queues: CCX emits on real time; the app sees cues at scripted
# VIRTUAL moments (the simulated CCX lag / caption-axis behavior)
# ----------------------------------------------------------------------------
class CueQueue:
    """Cue release gate: a cue becomes visible to the app when the write
    head is >= L past its end — the simulated CCX pipeline lag.

    CCX's SRT only leaves the process in ~4 KB stdout flushes (the reader
    blocks on read(4096)), so raw arrivals are bursty in real time; gating
    on content lag restores the steady per-cue arrival cadence the live
    app sees, at whatever virtual pace the harness runs.

    ``released`` carries the times the APP saw; ``raw_released`` carries
    the same cues on the true content axis (identical, except WarpQueue
    scales the app-visible times) — the painted-cue metric scores against
    the raw axis so a warped caption axis cannot fake positions."""

    def __init__(self, lag=1.5):
        self.lag_fn = lag if callable(lag) else (lambda: float(lag))
        self.pending = []            # (s, e, text)
        self.released = []           # (s, e, text, T_release)
        self.raw_released = []       # (s, e, text) on the content axis
        self.last_seen_end = None
        self.frozen = False

    def on_cue(self, s, e, text):
        self.pending.append((s, e, text))
        if self.last_seen_end is None or e > self.last_seen_end:
            self.last_seen_end = e

    def release(self, head, deliver):
        if self.frozen:
            return
        L = self.lag_fn()
        keep = []
        for item in self.pending:
            s, e, text = item
            if head - e >= L:
                deliver(s, e, text)
                self.released.append((s, e, text, VT.t))
                self.raw_released.append((s, e, text))
            else:
                keep.append(item)
        self.pending = keep


class WarpQueue(CueQueue):
    """Cue TIMES scaled (scenario e: CCX axis at 2x wall). Released times
    are the WARPED numbers the app is meant to see; raw_released keeps
    the true content-axis windows for scoring."""

    def __init__(self, factor):
        super().__init__()
        self.factor = factor

    def release(self, head, deliver):
        if self.frozen:
            return
        # gate on the RAW axis (the cue's content really is L behind the
        # write head), deliver the WARPED times the app is meant to see
        keep = []
        for s, e, text in self.pending:
            if head - e >= self.lag_fn():
                deliver(s * self.factor, e * self.factor, text)
                self.released.append((s * self.factor, e * self.factor,
                                      text, VT.t))
                self.raw_released.append((s, e, text))
            else:
                keep.append((s, e, text))
        self.pending = keep


# ----------------------------------------------------------------------------
# harness
# ----------------------------------------------------------------------------
# exact-window tolerance: a painted cue whose best released match sits
# further than this from the display position is the WRONG window (the
# +/-3 s neighbor/substring acceptance is diagnostic only — see sample())
_EXACT_TOL_S = 1.0


def _match_key(text):
    """Canonical comparison key for a cue's paintable screen: the last 3
    visible lines, lowercased — exactly what CueStore.text_at returns and
    what the overlay paints (profanity filter is off in the harness)."""
    return tuple(ln.lower() for ln in visible_lines(text)[-3:]
                 if ln.strip())


class Harness:
    def __init__(self, stream: LoopedTS, tmproot: str):
        self.stream = stream
        self.tmpdir = tempfile.mkdtemp(prefix="mtp_adv_", dir=tmproot)
        self.exceptions = []
        self.m0 = None                 # clock -> app-visible cue axis
        self.m0r = None                # clock -> raw content axis (perr)
        self.rebase_count = 0
        self.frontier_gap_samples = []
        self.stop_run = 0.0
        self.max_stop = 0.0
        self.paint_events = 0
        self.text_neighbor_s = 3.0   # roll-up adjacency for text matching
        self.diag = bool(os.environ.get("MTP_ADV_DIAG"))  # per-second state
        # -- painted-cue metric (WP0): score what is ON SCREEN against the
        # raw window of the released cue it was painted from. Unlike the
        # retired truth-cover metric the error is UNBOUNDED.
        self.paint_errs = []         # (t, err) for every matched sample
        self.paint_n = 0             # ticks that painted lines
        self.paint_matched = 0       # >= 1 exact-text released match
        self.paint_exact = 0         # ... and the best match sits within
        #                              # _EXACT_TOL_S of the display (the
        #                              # PRIMARY text assertion: the cue
        #                              # from the right window is painted)
        self.paint_ambig = 0         # several exact matches (roll-up)
        self.paint_nomatch = 0       # painted text matches nothing released
        self._rl_idx = {}            # match key -> [(s, e), ...]
        self._rl_n = 0               # released cues already indexed
        # diagnostics only (retired as assertions in WP0): the old
        # truth-cover error and the +/-3 s neighbor/substring text gate
        self.sat_errs = []           # retired metric, for the report
        self.text_ok = 0
        self.text_tot = 0

    def setup(self, queue: CueQueue, start_backlog=8.0):
        cfg_path = os.path.join(self.tmpdir, "cfg.json")
        Path(cfg_path).touch()
        self.cfg = Config({}, Path(cfg_path))
        VT.t = 1_000_000.0 + (_real_time.time() % 1000.0)
        self.view = PlayerView(self.cfg)
        self.pump(0.45)                    # ctor's deferred attach shots
        fake = FakeVLC()
        self.view.vlc = fake
        self.view._filter_engine.player = fake
        self.fake = fake
        for t in (self.view.timer, self.view.cursor_timer):
            t.stop()
        self.buf = os.path.join(self.tmpdir, "buffer.ts")
        self.chunk_state = (0, 0)
        self.head = 0.0                    # PCR-content write head
        with open(self.buf, "wb") as f:
            data, self.chunk_state, self.head = \
                self.stream.chunk(self.chunk_state, 12.0)
            f.write(data)
        self._t0c = VT.t - self.head       # VT when content 0 was at head
        self.view.current = {"kind": "live", "url": "http://x/s.ts",
                             "title": "adv"}
        self.view._mode = "chase"
        self.view.dvr = FakeDVR(self.buf)
        self.view._note_dvr_data()
        target = max(0.0, self.head - start_backlog)
        self.view._cap_seed_transport(target)
        fake.play_at(self.buf, target)
        self.view._vid_s = target
        self.view._chase_started = True
        self.view._cap_want = True
        self.view._set_cap_on(True)
        self.view._start_cc_when_buffer(tries_left=3)
        self.pump(0.7)                     # join-PCR probe singleShot etc.
        src = self.view._cc_source
        assert src is not None, "CCSource did not start"
        src._timer.stop()                  # harness drives the harvest
        try:
            src.cue.disconnect()
        except Exception:
            pass
        src.cue.connect(queue.on_cue)
        self.queue = queue
        for t in (self.view._cc_edge_timer, self.view._cap_timer,
                  self.view._filter_timer):
            t.stop()
        self._real_rebase = self.view._cc_rebase

        def counting_rebase(target_off, why):
            self.rebase_count += 1
            self._real_rebase(target_off, why)
        self.view._cc_rebase = counting_rebase

    def teardown(self):
        try:
            self.view.stop()
        except Exception as exc:  # noqa: BLE001
            self.exceptions.append(f"teardown: {exc!r}")
        try:
            self.view.deleteLater()
        except Exception:
            pass

    def pump(self, seconds):
        t0 = _real_time.time()
        while _real_time.time() - t0 < seconds:
            QtWidgets.QApplication.processEvents()
            _real_time.sleep(0.02)

    # -- growth / head --
    def grow_to(self, c_target):
        if c_target <= self.head:
            return
        data, self.chunk_state, reached = \
            self.stream.chunk(self.chunk_state, c_target)
        if data:
            with open(self.buf, "ab") as f:
                f.write(data)
            self.head = reached

    def grow_1to1(self, t):
        self.grow_to(t - self._t0c)

    # -- main loop --
    def run(self, seconds, script=None, pace=22.0, growth=None,
            sample=True):
        script = script or (lambda t: None)
        growth = growth or self.grow_1to1
        step = 0.1
        steps = int(round(seconds / step))
        last_harvest = -1e9
        last_diag = -1e9
        for i in range(steps):
            VT.advance(step)
            t = VT.t
            script(t)
            growth(t)
            self.fake.max_content = self.head - 0.3
            self.queue.release(self.head, self.view._on_cc_cue)
            if t - last_harvest >= 0.25:
                last_harvest = t
                self.safe(self.view._cc_source._harvest)
            if i % 4 == 0:
                self.safe(self.view._tick)
                self.frontier_gap_samples.append(
                    self.view._cap_edge_s() - self.view._frontier_s())
            if i % 20 == 0:
                self.safe(self.view._cc_edge_probe_tick)
            self.safe(self.view._caption_tick)
            if sample:
                self.sample(t)
            if self.diag and t - last_diag >= 1.0:
                last_diag = t
                v = self.view
                la = (t - v._cc_last_arrival) if v._cc_last_arrival else -1
                print(f"    DIAG el={t - 1_000_000:.1f} clock={v._cap_clock_s:.1f}"
                      f" off={v._cc_off if v._cc_off is None else round(v._cc_off, 2)}"
                      f" edge={v._cap_edge_s():.1f} fr={v._frontier_s():.1f}"
                      f" lag={v._cc_lag if v._cc_lag is None else round(v._cc_lag, 2)}"
                      f" store={len(v._cap_cues.cues)}"
                      f" painted={len(v._cap_wid._lines)}"
                      f" hold={v._trickle_hold} bl={v._cap_backlog_s:.2f}"
                      f" rawrate={v._raw_win_rate(t):.2f}"
                      f" arr_age={la:.1f}", flush=True)
            self.starve_guard()
            _real_time.sleep(step / pace)
        self.queue.release(self.head, self.view._on_cc_cue)

    def starve_guard(self):
        """CCX (real time) must stay within ~8 content s of the head —
        its stdout flushes in ~4 KB blocks, so virtual time freezes here
        until parsed cues catch up."""
        guard = 0
        while guard < 1500:
            seen = self.queue.last_seen_end
            if seen is None:
                if self.head < 10.0:
                    return                # first cues not expected yet
                lag = self.head           # nothing parsed at all yet
            else:
                lag = self.head - seen
            if lag <= 8.0:
                return
            self.safe(self.view._cc_source._harvest)
            QtWidgets.QApplication.processEvents()
            _real_time.sleep(0.02)
            guard += 1

    def safe(self, fn, *a):
        try:
            fn(*a)
        except Exception as exc:  # noqa: BLE001
            self.exceptions.append(f"{getattr(fn, '__name__', fn)}: {exc!r}")

    # -- truth sampling --
    def truth_cue(self, disp_ccx):
        """STOP-METRIC GATE ONLY: a released cue whose raw window covers
        the display position means caption text for the content on screen
        EXISTS — painting nothing now is a silent stop. (Retired as the
        error metric in WP0: it can only ever return covering cues, so the
        old error was bounded by its grace window and could not fail.)"""
        best = None
        for s, e, text, _ in reversed(self.queue.released):
            if s - 0.3 <= disp_ccx <= e + 0.55:
                if best is None or e > best[1]:
                    best = (s, e, text)
            elif e < disp_ccx - 60.0:
                break
        return best

    def _sync_release_index(self):
        q = self.queue.raw_released
        while self._rl_n < len(q):
            s, e, text = q[self._rl_n]
            self._rl_n += 1
            self._rl_idx.setdefault(_match_key(text), []).append((s, e))

    def sample(self, t):
        self._sync_release_index()
        clock = self.view._cap_clock_s
        painted = [ln for ln in self.view._cap_wid._lines if ln.strip()]
        m0r = self.m0r if self.m0r is not None else \
            (self.m0 if self.m0 is not None else 0.0)
        disp = clock - m0r
        truth = self.truth_cue(disp)
        if painted:
            self.paint_events += 1
            self.paint_n += 1
            # -- primary: exact-window painted-cue score --
            key = tuple(ln.lower() for ln in painted)
            wins = self._rl_idx.get(key, ())
            if wins:
                self.paint_matched += 1
                err = min(abs(disp - min(max(disp, s), e))
                          for s, e in wins)
                self.paint_errs.append((t, err))
                if len(wins) > 1:
                    self.paint_ambig += 1
                if err <= _EXACT_TOL_S:
                    self.paint_exact += 1
            else:
                self.paint_nomatch += 1
            # -- diagnostics: the retired truth-cover error + the old
            # +/-3 s neighbor/substring text acceptance --
            if truth is not None:
                s, e, _tx = truth
                self.sat_errs.append(disp - min(max(disp, s), e))
                self.text_tot += 1
                cands = [visible_lines(truth[2])]
                for ns, ne, ntext, _ in reversed(self.queue.released):
                    if ns < e - self.text_neighbor_s:
                        break
                    if ns <= e + self.text_neighbor_s:
                        cands.append(visible_lines(ntext))
                pl = [ln.lower() for ln in painted]
                if any(p in c or c in p
                       for cl in cands for c in
                       (ln.lower() for ln in cl[-3:] if ln.strip())
                       for p in pl):
                    self.text_ok += 1
        if truth is None:
            self.stop_run = 0
            return
        if painted:
            self.stop_run = 0
        else:
            self.stop_run += 0.1
            self.max_stop = max(self.max_stop, self.stop_run)

    def reset_metrics(self):
        """Clear the sampled metric accumulators (phase-scoped checks)."""
        self.paint_errs = []
        self.paint_n = 0
        self.paint_matched = 0
        self.paint_exact = 0
        self.paint_ambig = 0
        self.paint_nomatch = 0
        self.stop_run = 0.0
        self.max_stop = 0.0
        self.paint_events = 0

    def calibrate(self, seconds=45.0):
        """Settle, then measure the clock->cue-axis offsets: m0 against
        the app-visible windows (drives the stop metric's truth gate) and
        m0r against the RAW content-axis windows (drives the painted-cue
        error — identical except under a warped caption axis).

        Unbiased estimator: only windows that COVER the clock sample
        contribute. (The retired scan took the first window within +-4 s
        of the clock from the newest end — with roll-up windows narrower
        than the gaps between them that picked a neighbor systematically
        AHEAD of the clock and biased m0 by ~1.1-1.5 s. Harmless while
        the metric was saturated; fatal now.)"""
        self.run(max(5.0, seconds - 15.0), sample=False)
        ds = []
        dsr = []
        for i in range(150):
            VT.advance(0.1)
            t = VT.t
            self.grow_1to1(t)
            self.fake.max_content = self.head - 0.3
            self.queue.release(self.head, self.view._on_cc_cue)
            self.safe(self.view._cc_source._harvest)
            if i % 4 == 0:
                self.safe(self.view._tick)
            if i % 20 == 0:
                self.safe(self.view._cc_edge_probe_tick)
            self.safe(self.view._caption_tick)
            clock = self.view._cap_clock_s
            cue = self.truth_cue(clock)
            if cue is not None:
                s, e, _tx = cue
                ds.append(clock - min(max(clock, s), e))
            for s, e, _tx in reversed(self.queue.raw_released):
                if s - 0.3 <= clock <= e + 0.55:
                    dsr.append(clock - min(max(clock, s), e))
                    break
                if e < clock - 60.0:
                    break
            self.starve_guard()
            _real_time.sleep(0.1 / 22.0)
        self.m0 = statistics.median(ds) if ds else 0.0
        self.m0r = statistics.median(dsr) if dsr else self.m0
        return self.m0, len(ds)

    # -- reporting --
    def stats(self):
        errs = sorted(abs(e) for _, e in self.paint_errs)
        if not errs:
            return None
        n = len(errs)
        return {"n": n, "p50": errs[n // 2],
                "p95": errs[min(n - 1, int(n * 0.95))], "max": errs[-1]}

    def exact_rate(self):
        return self.paint_exact / self.paint_n if self.paint_n else 0.0

    def within_rate(self, tol):
        """Fraction of matched painted samples within ``tol`` s of their
        best released window (outcome phrasing: "painted text correct
        within N s")."""
        if not self.paint_errs:
            return 0.0
        return sum(1 for _, e in self.paint_errs if abs(e) <= tol) \
            / len(self.paint_errs)

    def report(self, label):
        st = self.stats()
        if st:
            print(f"    {label}: painted={self.paint_events} n={st['n']} "
                  f"|perr| p50={st['p50']:.2f} p95={st['p95']:.2f} "
                  f"max={st['max']:.2f} "
                  f"exact={self.paint_exact}/{self.paint_n} "
                  f"ambig={self.paint_ambig} nomatch={self.paint_nomatch} "
                  f"maxstop={self.max_stop:.1f}s "
                  f"rebases={self.rebase_count} "
                  f"diag[neighbor={self.text_ok}/{self.text_tot}]",
                  flush=True)
        else:
            print(f"    {label}: NO PAINTED SAMPLES "
                  f"(maxstop={self.max_stop:.1f}s "
                  f"rebases={self.rebase_count})", flush=True)
        return st


# ----------------------------------------------------------------------------
# scenarios
# ----------------------------------------------------------------------------
STREAM = None
M0 = [None]      # clock -> app-visible axis (stop metric)
M0R = [None]     # clock -> raw content axis (painted-cue error)
TMPROOT = None


def fresh_harness(queue, backlog=8.0, calibrate_m0=True):
    h = Harness(STREAM, TMPROOT)
    h.setup(queue, start_backlog=backlog)
    if M0[0] is None and calibrate_m0:
        # running this scenario standalone (--only:...): no earlier scenario
        # measured the clock->cue-axis offsets, so calibrate here. In the
        # full matrix scenario a's measurement is reused (the axes are
        # rebase-invariant, so one calibration serves every scenario).
        h.calibrate(40 * DUR + 12)
        M0[0], M0R[0] = h.m0, h.m0r
    h.m0 = M0[0]
    h.m0r = M0R[0]
    return h


def run_until_settled(h, max_s=150.0, block=4.0, med_tol=1.5):
    """Advance the quiescent feed until the painted-cue metric has
    settled: one whole block whose matched samples have median |perr|
    <= med_tol. Returns seconds advanced (>= max_s means it never settled).

    The recovery from a feed transient (burst backlog, L drain) is paced
    by CCX's REAL-TIME parse of the backlog — the starve guard trades
    virtual time for it — so no fixed virtual delay is deterministic.
    Settle DETECTION on the metric itself is. med_tol 1.5 s matches the
    post-swing anchor-wander class (the snap-vs-EWMA interplay under L
    jitter — WP3's target); steady state sits well below it."""
    elapsed = 0.0
    while elapsed < max_s:
        h.reset_metrics()
        h.run(block, growth=h.grow_1to1)
        elapsed += block
        if h.paint_errs:
            med = statistics.median(abs(e) for _, e in h.paint_errs)
            if med <= med_tol:
                break
    return elapsed


def scenario_a():
    print("\n== scenario a: cold join, growing divergence, anchor wedge ==",
          flush=True)
    h = fresh_harness(CueQueue(lag_const(1.5)))
    print(f"    L(t) profile: {lag_const(1.5)}", flush=True)
    t0 = VT.t
    wedge_at = 170 * DUR

    def script(t):
        el = t - t0
        # display axis runs progressively slow: ~7 s of divergence before
        # the wedge at full duration (the stage-1 "captions would stop"
        # condition — under stage-2 timing it must stay benign)
        h.fake.speed_warp = 1.0 - 0.0005 * el

    m0, n = h.calibrate(40 * DUR + 12)
    M0[0] = m0
    M0R[0] = h.m0r
    h.m0 = m0
    print(f"    calibration m0={m0:+.2f}s m0r={h.m0r:+.2f}s ({n} samples)",
          flush=True)
    h.run(wedge_at, script=script)           # drift only, fault not yet
    st = h.report("drift")
    check("a: drift painted displacement p95 <= 1.5 s",
          st is not None and st["p95"] <= 1.5,
          f"p95={(st['p95'] if st else -1):.2f}")
    check("a: drift exact-window text >= 85%",
          h.paint_n >= 20 and h.exact_rate() >= 0.85,
          f"{h.paint_exact}/{h.paint_n} ambig={h.paint_ambig} "
          f"nomatch={h.paint_nomatch}")
    check("a: no silent-stop over 6 s during drift",
          h.max_stop <= 6.0, f"max={h.max_stop:.1f}s")
    # THE FAULT (deterministic, at this exact virtual moment): anchor AND
    # store displaced together (a rebase with a wrong target), cue
    # deliveries frozen so no fresh cue can snap-rebase first — the exact
    # wedge the caption-stopped watchdog exists for
    off_before = h.view._cc_off or 0.0
    h.view._cc_off = off_before + 5.0
    h.view._cap_cues.shift(+5.0)
    h.view._filter_engine.shift_windows(+5.0)
    h.queue.frozen = True
    h.run(6, sample=False)                   # freeze window (fault active)
    check("a: wedge applied (+5 s anchor & store)",
          abs((h.view._cc_off or 0.0) - off_before - 5.0) < 1e-9
          and h.queue.frozen)
    # OUTCOME contract (replaces the retired "watchdog fired during the
    # freeze" mechanism count, whose pass depended on real CCX pacing):
    # once data flows again the user-visible behavior must recover — no
    # truth-active blank beyond the watchdog window, and painted text
    # back on its true window within ~15 s, by ANY internal path.
    # OUTCOME contract (replaces the retired "watchdog fired during the
    # freeze" mechanism count, whose pass depended on real CCX pacing):
    # once data flows again the user-visible behavior must recover — no
    # truth-active blank beyond the watchdog window, and painted text
    # back on its true window, by ANY internal path. Recovery has two
    # parts: the anchor re-settles (watchdog rebase, anchor snap, or the
    # EWMA crawl — whichever fires), and the viewer then walks its
    # BACKLOG off the cues stored under the displaced axis — so the
    # deadline scales with the live-edge backlog at the fault.
    backlog = h.view._cap_backlog_s or 0.0
    walkoff_s = max(0.0, backlog)
    reb0 = h.rebase_count
    h.queue.frozen = False
    h.reset_metrics()
    h.run(walkoff_s + 15.0, script=lambda t: None)   # walk-off + healing
    check("a: no blank beyond the watchdog window after data returns",
          h.max_stop <= 6.5, f"max={h.max_stop:.1f}s")
    walkoff_n = len(h.paint_errs)
    walkoff_within = h.within_rate(1.5)
    rebased = h.rebase_count > reb0
    h.reset_metrics()
    h.run(15, script=lambda t: None)
    heal_n = len(h.paint_errs)
    heal_within = h.within_rate(1.5)
    h.reset_metrics()
    h.run(10, script=lambda t: None)         # verify window
    st = h.report("post-wedge verify")
    # When recovery ran a REBASE (the store-re-axing path — watchdog or
    # anchor snap), the whole store must be coherent NOW: the walk-off
    # through the pre-fault region is part of the contract (a rebase that
    # leaves the store on the old axis — mutation m3 — fails here). When
    # the anchor only EWMA-crawled back, the pre-fault region keeps the
    # displaced axis until walked off (mixed-axis store — WP3's target);
    # WP0 then demands forward correctness only, and the walk-off numbers
    # stay in the report as the WP3 acceptance baseline.
    ok = (heal_n >= 10 and heal_within >= 0.60
          and st is not None and st["p95"] <= 1.5
          and h.within_rate(1.5) >= 0.85)
    if rebased:
        ok = ok and walkoff_n >= 10 and walkoff_within >= 0.70
    check("a: post-wedge painted text correct by ~backlog+30 s "
          "(walkoff/heal/verify)",
          ok,
          f"backlog={backlog:.0f}s rebased={rebased} "
          f"walkoff_within1.5={walkoff_within * 100:.0f}% of {walkoff_n} "
          f"heal_within1.5={heal_within * 100:.0f}% of {heal_n} "
          f"verify_p95={(st['p95'] if st else -1):.2f} "
          f"verify_within1.5={h.within_rate(1.5) * 100:.0f}%")
    check("a: no pipeline exceptions", not h.exceptions,
          h.exceptions[:2] and str(h.exceptions[:2]) or "")
    h.teardown()


def scenario_b():
    print("\n== scenario b: pause 60 s -> resume +25 s step (3 cycles) ==",
          flush=True)
    h = fresh_harness(CueQueue(lag_const(1.5)))
    print(f"    L(t) profile: {lag_const(1.5)}", flush=True)
    state = {"cycle": 0, "phase": "run", "t0": VT.t, "next": 20.0}

    def script(t):
        el = t - state["t0"]
        if state["phase"] == "run" and el > state["next"]:
            state["phase"] = "pause"
            h.view.toggle_pause()          # real pause path
        elif state["phase"] == "pause" and el > state["next"] + 60:
            state["phase"] = "resume"
            h.fake.renumber(+25.0)         # lands on the first read after
            h.view.toggle_pause()          # real resume path
        elif state["phase"] == "resume" and el > state["next"] + 120:
            state["cycle"] += 1
            if state["cycle"] >= 3:
                state["phase"] = "done"
            else:
                state["phase"] = "run"
                state["next"] = el + 20.0

    h.run(3 * 145 + 5, script=script)
    st = h.report("b (3 cycles)")
    div = h.view._cap_div_s
    check("b: divergence recorded for each +25 s step (accumulates to ~75)",
          h.view._cap_div_ok and abs(div - 75.0) < 4.0,
          f"div={div:+.1f} want~+75")
    check("b: painted cues coherent through pause/resume/step (p95 <= 1.5)",
          st is not None and st["p95"] <= 1.5,
          f"p95={(st['p95'] if st else -1):.2f}")
    check("b: no silent-stop beyond watchdog window (~5+3 s)",
          h.max_stop <= 8.0, f"max={h.max_stop:.1f}s")
    check("b: 3 pause cycles executed", state["cycle"] == 3)
    check("b: no pipeline exceptions", not h.exceptions)
    h.teardown()


def scenario_c():
    print("\n== scenario c: jump-to-live, L swells 1 -> 20 s ==", flush=True)
    swell_end = 60 * DUR
    # drain 0.6 s/s (the original script decayed 0.06 per 0.1-s tick):
    # fast enough that L actually recovers inside the scenario, so the
    # "after lag recovery" contract is exercised end-to-end
    prof = lag_swell_then_recede(swell_s=swell_end, hold_s=90 * DUR,
                                 hi=20.0, lo=1.0, recede_s=0.6, floor=1.5)
    h = fresh_harness(CueQueue(prof), backlog=10.0)
    prof.t0 = VT.t                    # profile el anchors at scenario start
    print(f"    L(t) profile: {prof}", flush=True)
    jumped = {"done": False, "edge": 0.0, "clock": 0.0, "true_head": 0.0,
              "lag": None, "backlog": 0.0}
    t0 = VT.t

    def script(t):
        el = t - t0
        if swell_end <= el < swell_end + 2.0 and not jumped["done"]:
            jumped["done"] = True
            jumped["edge"] = h.view._cap_edge_s()
            jumped["true_head"] = h.head
            jumped["lag"] = h.view._cc_lag
            h.view._jump_live()               # real jump path
            jumped["clock"] = h.view._cap_clock_s
            jumped["backlog"] = h.view._cap_backlog_s
    h.run(swell_end + 160 * DUR, script=script)
    h.report("c (whole run, incl. L transient — WP3 feed)")
    maxstop_run = h.max_stop
    settle_s = run_until_settled(h)
    h.reset_metrics()
    h.run(12)                             # recovered regime: must STAY put
    st = h.report("c: after lag recovery")
    # D1 adaptive landing: the jump target is max(5, L+3) behind the edge
    # while the measured L exceeds 8 s (at the swell peak the EWMA reads
    # mid-ramp — whatever it reads IS the policy input, so the expectation
    # keys off the same reading), true edge (-5) otherwise. The policy is
    # INLINED here (not shared with the production helper) so a mutation
    # of _chase_jump_back_s cannot self-neutralize the check.
    lag_at = jumped["lag"]
    back = max(_CHASE_SAFETY_S, lag_at + 3.0) \
        if (lag_at is not None and lag_at > 8.0) else _CHASE_SAFETY_S
    check("c: jump landed max(5, L+3) behind the true edge (D1 adaptive)",
          jumped["done"]
          and abs(jumped["clock"] - (jumped["true_head"] - back)) < 3.0
          and abs(jumped["clock"] - (jumped["edge"] - back)) < 2.0,
          f"clock@jump={jumped['clock']:.1f} edge@jump="
          f"{jumped['edge']:.1f} true_head={jumped['true_head']:.1f} "
          f"L@jump={lag_at} back={back}")
    check("c: the landing gap seeds the live-edge backlog (no double count)",
          jumped["done"] and abs(jumped["backlog"] - back) <= 2.0,
          f"backlog@jump={jumped['backlog']:.1f} want~{back:.1f}")
    check("c: painted cues re-settle after the L swing and stay put "
          "(p95 <= 2.0)",
          settle_s < 150.0 and st is not None and st["p95"] <= 2.0,
          f"p95={(st['p95'] if st else -1):.2f} settled_in={settle_s:.0f}s "
          f"L={prof.now():.1f}s rebases={h.rebase_count} "
          f"(post-swing snap-rebase wander — WP3 target)",
          kind="data-limited")
    check("c: blank stretches stay data-limited (<= L swell + margin)",
          maxstop_run <= 26.0, f"max={maxstop_run:.1f}s",
          kind="data-limited")
    check("c: no pipeline exceptions", not h.exceptions)
    h.teardown()


def scenario_d():
    print("\n== scenario d: provider bursts, frontier under-credits ==",
          flush=True)
    h = fresh_harness(CueQueue(lag_const(1.5)))
    print(f"    L(t) profile: {lag_const(1.5)} + 30 s burst/stall cadence",
          flush=True)
    t0 = VT.t

    def growth(t):
        el = (t - t0) % 60.0
        if 25.0 <= el < 55.0:
            return                          # 30 s stall: nothing appended
        h.grow_to(t - h._t0c)               # 1:1 (catches up the burst)

    h.run(200 * DUR + 10, growth=growth)
    h.report("d (whole run, incl. burst transients — WP2/WP3 feed)")
    maxstop_run = h.max_stop
    gaps = h.frontier_gap_samples
    check("d: frontier really under-credited (>=10 s at some point)",
          bool(gaps) and max(gaps) >= 10.0,
          f"max edge-frontier gap={max(gaps or [0]):.1f}s")
    # OUTCOME contract: the bursts themselves displace the anchor (whole-
    # run numbers above — the L EWMA inflates with each burst's release
    # dump and drains at CCX's real-time parse pace; that transient is
    # WP3's target). The gate: on a 1:1 feed the pipeline must re-settle
    # (detected on the metric itself, bounded) and STAY correct.
    h.reset_metrics()
    settle_s = run_until_settled(h)
    h.reset_metrics()
    h.run(12, growth=h.grow_1to1)
    st = h.report("d: post-burst quiescent tail")
    check("d: painted cues re-settle after the bursts and stay put "
          "(p95 <= 1.5, >= 85% within 1.5 s)",
          settle_s < 150.0 and st is not None and st["p95"] <= 1.5
          and h.within_rate(1.5) >= 0.85,
          f"p95={(st['p95'] if st else -1):.2f} "
          f"within1.5={h.within_rate(1.5) * 100:.0f}% "
          f"settled_in={settle_s:.0f}s",
          kind="data-limited")
    check("d: no silent-stop beyond watchdog window (~5+4 s)",
          maxstop_run <= 9.0, f"max={maxstop_run:.1f}s",
          kind="data-limited")
    check("d: no pipeline exceptions", not h.exceptions)
    h.teardown()


def scenario_e():
    print("\n== scenario e: CCX caption axis runs 2x wall ==", flush=True)
    prof = lag_const(1.5)
    h = fresh_harness(WarpQueue(2.0), calibrate_m0=False)
    h.m0 = None
    h.m0r = None
    h.text_neighbor_s = 12.0         # warped axis: display runs up to
    #                              # (backlog - L) ahead of the viewer
    m0e, n = h.calibrate(45 * DUR + 8)
    print(f"    L(t) profile: {prof} on a 2x warped caption axis",
          flush=True)
    print(f"    warp-axis calibration m0={m0e:+.2f}s m0r={h.m0r:+.2f}s "
          f"({n} samples)", flush=True)
    h.run(150 * DUR + 5)
    st = h.report("e")
    # Under a 2x caption axis the anchor target falls ~1 s/s (the warped
    # cue ends outrun the 1x edge) and every lag sample goes negative
    # (head_rel - end_warped < 0) -> the pin rides the constant fallback
    # and the store's slope-1 mapping diverges from the slope-0.5 truth
    # until a snap re-axes it: a bounded sawtooth. Pre-WP3 this measured
    # p95 4.73 — but that containment rode WATCHDOG NOISE-FIRES that WP3
    # deliberately removed (the data-limited guard); without them the
    # sawtooth runs deeper (max ~8). A slope-aware pin model is the real
    # fix (future work); these checks assert the CONTAINMENT contract:
    # bounded displacement, exact text within the warp bound.
    check("e: 2x caption axis displacement contained (p95 <= 8 s)",
          st is not None and st["p95"] <= 8.0,
          f"p95={(st['p95'] if st else -1):.2f} "
          f"rebases={h.rebase_count}")
    check("e: no silent-stop beyond watchdog window (~5+3 s)",
          h.max_stop <= 8.0, f"max={h.max_stop:.1f}s")
    check("e: painted text exact-matched within the warp trail bound "
          "(+-8 s, >= 85%)",
          h.paint_n >= 10 and h.within_rate(8.0) >= 0.85,
          f"within8={h.within_rate(8.0) * 100:.0f}% of {h.paint_n} "
          f"exact-text matches (ambig={h.paint_ambig})")
    check("e: no pipeline exceptions", not h.exceptions)
    h.teardown()


def scenario_f():
    print("\n== scenario f: scrub back 2 min, rebase coherence, delay_ms ==",
          flush=True)
    h = fresh_harness(CueQueue(lag_const(1.5)))
    print(f"    L(t) profile: {lag_const(1.5)}", flush=True)
    h.run(205 * DUR)
    st0 = h.report("f: steady")
    # steady regime (constant L, 1:1 feed): the pipeline must never
    # displace painted cues — this max gate is what catches a transient
    # anchor/store wedge even when p95 averages it away (threshold head-
    # room above the measured anchor wander; a forced displacement is ~5)
    check("f: steady painted cues stable (p95 <= 1.5, max <= 2.5)",
          st0 is not None and st0["p95"] <= 1.5 and st0["max"] <= 2.5,
          f"p95={(st0['p95'] if st0 else -1):.2f} "
          f"max={(st0['max'] if st0 else -1):.2f}")
    c0 = h.view._cap_clock_s
    h.view._seek_ms(-120000)                # scrub back 2 min (real path)
    h.run(1, sample=False)
    landed = h.view._cap_clock_s
    want = max(0.0, c0 - 119.0)   # clamped at the buffer start
    check("f: scrub -120 s lands on target",
          abs(landed - want) < 2.5,
          f"landed={landed:.1f} want~{want:.1f}")
    h.reset_metrics()
    h.run(45 * DUR + 5)
    st1 = h.report("f: scrubbed-back region")
    check("f: scrubbed-back painted cues coherent (p95 <= 1.5)",
          st1 is not None and st1["p95"] <= 1.5,
          f"p95={(st1 and st1['p95']):.2f}")
    check("f: scrubbed-back exact-window text >= 85%",
          h.paint_n >= 10 and h.exact_rate() >= 0.85,
          f"{h.paint_exact}/{h.paint_n} ambig={h.paint_ambig}")
    # force a rebase while scrubbed back: the next fresh cue snaps the
    # anchor back — the store (and the region behind us) must stay
    # coherent. OUTCOME assertion (was the "rebase round-tripped"
    # mechanism count): however the pipeline routes the correction, the
    # painted text must land back on its true window.
    h.diag = bool(os.environ.get("MTP_ADV_DIAG"))
    h.view._cc_rebase((h.view._cc_off or 0.0) - 6.0, "harness-force")
    h.view._seek_ms(-60000)
    h.run(1, sample=False)
    h.reset_metrics()
    h.run(2, sample=False)           # healing: the snap-back rebase lands
    #                              # on the first fresh-cue flush (~0.5 s);
    #                              # sampled coherence starts after it
    h.reset_metrics()
    h.run(30 * DUR)
    h.diag = False
    st2 = h.report("f: post-rebase stability")
    check("f: rebased store stays coherent behind the head (outcome)",
          st2 is not None and st2["p95"] <= 1.5
          and (h.paint_n >= 10 and h.exact_rate() >= 0.85),
          f"p95={(st2['p95'] if st2 else -1):.2f} "
          f"exact={h.paint_exact}/{h.paint_n}")
    h.view._seek_ms(120000)                 # forward again, near the edge
    h.run(20, sample=False)
    h.view._seek_ms(-30000)                 # somewhere with dense cues for
    h.run(2, sample=False)                  # the delay probe
    # directional delay probe: with +1.5 s the overlay must paint the cue
    # active 1.5 s EARLIER — positive delay = later, matching config.py,
    # the +/- tooltip and VLC's spu-delay path. (WP1a landed the sign
    # fix; the old check here was deliberately direction-neutral.)
    clock = h.view._cap_clock_s
    probe = None
    tp = clock
    while tp < clock + 45.0:
        base = h.view._cap_cues.text_at(tp)
        if base and base != h.view._cap_cues.text_at(tp - 1.5) \
                and base != h.view._cap_cues.text_at(tp + 1.5):
            probe = tp
            break
        tp += 0.25
    if probe is not None:
        h.view._seek_ms((probe - h.view._cap_clock_s) * 1000.0)
        h.pump(0.1)
    h.cfg.subtitle_appearance = dict(h.cfg.subtitle_appearance, delay_ms=0)
    h.safe(h.view._caption_tick)
    base_lines = list(h.view._cap_wid._lines)
    h.cfg.subtitle_appearance = dict(h.cfg.subtitle_appearance, delay_ms=1500)
    h.safe(h.view._caption_tick)
    t_shift = h.view._cap_clock_s       # the exact clock that tick used
    shifted_lines = list(h.view._cap_wid._lines)
    earlier = h.view._cap_cues.text_at(t_shift - 1.5)
    check("f: +1.5 s delay paints the cue active 1.5 s EARLIER",
          probe is not None and bool(base_lines)
          and shifted_lines == earlier and earlier != base_lines,
          f"probe@+{(probe - clock) if probe else -1:.1f}s "
          f"base={len(base_lines)}ln shifted={len(shifted_lines)}ln "
          f"earlier={len(earlier)}ln")
    check("f: no pipeline exceptions", not h.exceptions)
    h.teardown()


def scenario_g():
    print("\n== scenario g: 0.2x-delivery trickle, <6 s freeze/thaw cycles ==",
          flush=True)
    h = fresh_harness(CueQueue(lag_const(1.5)))
    print(f"    L(t) profile: {lag_const(1.5)} + 0.2x growth "
          f"(1 s appended every 5 s)", flush=True)
    leads = []                     # (clock - raw_content) while playing
    state = {"last": None, "raw_pre_seek": None, "want": 0.0,
             "landed": None, "n_play_at0": 0}

    def lead_sample():
        try:
            if h.fake.is_playing() and not h.view._chase_paused:
                raw = h.fake.get_time() / 1000.0
                leads.append(h.view._cap_clock_s
                             - h.view._cap_content_for_raw(raw))
        except Exception:
            pass

    def trickle_growth(t):
        # 0.2x delivery: 1.0 s of content every 5.0 VT s. raw freezes
        # ~4.3 s between appends (never reaching the 6 s continuous-freeze
        # stall), and the frontier OVER-credits (wall-anchored per
        # sighting) — both measured properties of the 2026-08-21 night
        if state["last"] is None or t - state["last"] >= 5.0:
            h.grow_to(h.head + 1.0)
            state["last"] = t

    def script(t):
        lead_sample()

    # healthy 1:1 start (paint baseline), then the trickle. Settling is
    # METRIC-DETECTED, not DUR-scaled: the L EWMA leaves cold-start
    # (CCX's parse backlog pumps it to ~30) at CCX's real-time parse pace
    # — a fixed 20*DUR window converges in full mode but not in --quick.
    h.run(20 * DUR)
    run_until_settled(h, max_s=150.0 * DUR + 40.0)
    st0 = h.report("g: steady pre-trickle")
    state["last"] = None
    h.diag = bool(os.environ.get("MTP_G_DIAG"))
    # NOTE on what g3 can and cannot prove: in the harness's 1:1 phases
    # the starve guard lets real CCX trail the feed by up to 8 content s,
    # so the L EWMA legitimately settles ~5-6 (the anchor compensates;
    # painted stays correct). In the trickle regime the release spacing
    # narrows but L drains at 0.18/batch with a batch only every ~10 VT s
    # — the pin runs early by (L - spacing) until it drains. That
    # residual is L-tracking inertia (WP3's assigned target: adaptive α /
    # lead compensation), NOT the freeze-aware clock: g1/g2 prove the
    # WP2 mechanism contract; g3 asserts the containment bound.
    h.run(45 * DUR, growth=trickle_growth, script=script)
    h.diag = False
    # mid-trickle seek: the contract is against what is DISPLAYED (raw),
    # not the internal clock — a clock running ahead lands the seek that
    # much off the user's intent. Ensure enough buffer exists behind the
    # viewer first: in the full matrix g runs ~26 content s younger than
    # standalone (fresh_harness skips its own calibration when scenario a
    # already measured M0), and a -30 s seek from raw 26 wanted -3.7.
    drain_guard = VT.t
    while h.view._cap_content_for_raw(
            h.fake.get_time() / 1000.0) < 40.0 and VT.t - drain_guard < 200.0:
        h.run(2.0, growth=trickle_growth, script=script)
    raw_pre = h.fake.get_time() / 1000.0
    state["raw_pre_seek"] = h.view._cap_content_for_raw(raw_pre)
    state["want"] = max(0.0, state["raw_pre_seek"] - 30.0)
    state["n_play_at0"] = sum(1 for c in h.fake.commanded
                              if c[0] == "play_at")
    h.view._seek_ms(-30000)
    h.run(0.5, growth=trickle_growth, script=script, sample=False)
    raw_post = h.view._cap_content_for_raw(h.fake.get_time() / 1000.0)
    h.run(9.5, growth=trickle_growth, script=script)
    n_play_at = sum(1 for c in h.fake.commanded if c[0] == "play_at")
    h.run(45 * DUR, growth=trickle_growth, script=script)
    st1 = h.report("g: trickle (incl. seek — WP2 (c) feed)")
    # recovery on a healthy 1:1 feed: re-settle and stay put
    h.reset_metrics()
    settle_s = run_until_settled(h)
    h.reset_metrics()
    h.run(12)
    st2 = h.report("g: post-trickle recovery")

    import statistics as _st
    abs_leads = sorted(abs(x) for x in leads)
    p95 = abs_leads[min(len(abs_leads) - 1, int(len(abs_leads) * 0.95))] \
        if abs_leads else 99.0
    max_lead = max((x for x in leads), default=99.0)
    check("g: caption clock tracks raw within ~1.5 s through the trickle "
          "(p95, never leading > 1.5 s)",
          len(leads) >= 100 and p95 <= 1.5 and max_lead <= 1.5,
          f"n={len(leads)} p95={p95:.2f} max_lead={max_lead:.2f}")
    check("g: mid-trickle seek lands ~30 s behind the DISPLAYED position",
          abs(raw_post - state["want"]) <= 3.0,
          f"raw {state['raw_pre_seek']:.1f} -> {raw_post:.1f} "
          f"want~{state['want']:.1f}")
    check("g: painted displacement contained through the trickle "
          "(p95 <= 2.5 — L-drain residual is WP3's feed)",
          st1 is not None and st1["p95"] <= 2.5,
          f"p95={(st1['p95'] if st1 else -1):.2f}",
          kind="data-limited")
    check("g: no spurious reopen during the trickle (wedge rescue quiet)",
          n_play_at == state["n_play_at0"],
          f"play_at {state['n_play_at0']} -> {n_play_at}")
    check("g: pipeline re-settles after the trickle",
          settle_s < 150.0 and st2 is not None and st2["p95"] <= 1.5,
          f"p95={(st2['p95'] if st2 else -1):.2f} settled_in={settle_s:.0f}s",
          kind="data-limited")
    check("g: no pipeline exceptions", not h.exceptions)
    h.teardown()


def scenario_h():
    print("\n== scenario h: injected set_time no-op (wedge at the true edge)==",
          flush=True)
    h = fresh_harness(CueQueue(lag_const(1.5)))
    print(f"    L(t) profile: {lag_const(1.5)}; wedge staged past the "
          f"under-credited frontier (the 2026-08-21 geometry)", flush=True)
    n_pa = lambda: sum(1 for c in h.fake.commanded  # noqa: E731
                       if c[0] == "play_at")

    # -- phase 1: healthy start, then a provider stall + one landed burst
    # under-credits the frontier (min(15, gap) credit per sighting). The
    # burst is (stall + 15) content s so the harness's wall-axis and the
    # content head stay ~15 s apart afterwards in BOTH modes (a fixed +60
    # burst on a DUR-scaled stall left the head 39 s past the wall axis in
    # --quick, grow_1to1 then appended nothing and the wedge ran against
    # a FROZEN head) — the 15-s deficit is waited out explicitly below.
    stall = 60.0 * DUR
    h.run(30 * DUR)
    h.run(stall, growth=lambda t: None)              # stall: nothing lands
    h.grow_to(h.head + stall + 15.0)                 # one burst sighting
    h.run(18.0, sample=False)                        # wall axis catches the
    #                                                 # head; edge calibrates
    # -- jump to live: lands ~5 s behind the TRUE head, PAST the
    # under-credited frontier — the legacy frontier rescue is blind here
    h.view._jump_live()
    h.run(2, sample=False)
    raw_at_wedge = h.view._cap_content_for_raw(h.fake.get_time() / 1000.0)
    fr_at_wedge = h.view._frontier_s()
    head_at_wedge = h.head

    # -- h0: autonomous wedge rescue (WP2 (b)) — nobody presses anything
    pa0 = n_pa()
    h.fake.wedge()
    wedge_t = VT.t
    revived_at = {"t": None}

    def h0_script(t):
        if revived_at["t"] is None and n_pa() > pa0:
            revived_at["t"] = t
        if os.environ.get("MTP_H_DEBUG") and (t - wedge_t) % 2.0 < 0.1:
            v = h.view
            hp = v._cc_head_pcr
            jp = v._sync_pcr_join
            print(f"    HDBG t+{t - wedge_t:5.1f} raw={v._sync_raw_s():6.1f} "
                  f"vid={v._vid_s:6.1f} head={h.head:6.1f} "
                  f"pcr={'-' if hp is None else '%.1f' % hp[0]} "
                  f"age={'-' if hp is None else '%.1f' % (t - hp[1])} "
                  f"join={'-' if jp is None else '%.1f' % jp[1]} "
                  f"japp={v._cc_join_app_s} "
                  f"ahead={v._head_ahead_s(v._vid_s)} "
                  f"frz={0 if not v._raw_change_wall else t - v._raw_change_wall:.1f} "
                  f"reopen_age={t - v._last_reopen:.1f} "
                  f"pa={n_pa()}", flush=True)

    h.run(20, script=h0_script)                     # 1:1 growth continues
    raw_after = h.view._cap_content_for_raw(h.fake.get_time() / 1000.0)
    check("h0: wedged player at the true edge is rescued autonomously "
          "(<= 15 VT s) — head-ahead, not frontier",
          revived_at["t"] is not None
          and revived_at["t"] - wedge_t <= 15.0
          and not h.fake.wedged
          and raw_after > raw_at_wedge,
          f"revived after {(revived_at['t'] or 1e9) - wedge_t:.1f}s "
          f"(wedge@{raw_at_wedge:.1f} fr={fr_at_wedge:.1f} "
          f"head={head_at_wedge:.1f} raw_after={raw_after:.1f})")

    # -- h1/h2: interactive escalation (WP2 (a)) — user seeks while wedged.
    # The player was revived at the edge by h0 and plays 1:1; re-wedge and
    # seek. raw is captured AT the escalation (a sample seconds later
    # measures legitimate playback advance, not the landing).
    h.run(10 * DUR)                                 # clean window (strikes)
    pa1 = n_pa()
    h.fake.wedge()
    clock_pre = h.view._cap_clock_s
    want = clock_pre - 60.0
    seek_t = VT.t
    h.view._seek_ms(-60000)                         # set_time no-ops
    esc_at = {"t": None, "raw": None}

    def h1_script(t):
        if esc_at["t"] is None and n_pa() > pa1:
            esc_at["t"] = t
            esc_at["raw"] = h.view._cap_content_for_raw(
                h.fake.get_time() / 1000.0)

    h.run(0.5, sample=False)
    h.run(10, script=h1_script)
    raw_esc = esc_at["raw"] if esc_at["raw"] is not None \
        else h.view._cap_content_for_raw(h.fake.get_time() / 1000.0)
    check("h1: a user seek on the wedged player escalates to play_at "
          "within the verify deadline + 2 s",
          esc_at["t"] is not None and esc_at["t"] - seek_t <= 8.0,
          f"escalated after {(esc_at['t'] or 1e9) - seek_t:.1f}s "
          f"(deadline 6.0 for a ~60 s jump)")
    check("h2: the escalated play_at lands the SEEK TARGET (not the wedge "
          "point), raw reaches it",
          esc_at["t"] is not None and not h.fake.wedged
          and abs(raw_esc - want) <= 3.0,
          f"raw@esc={raw_esc:.1f} want~{want:.1f} "
          f"(clock@seek={clock_pre:.1f})")

    # -- h-starved: wedge with NO data ahead — autonomy must stay SILENT
    # (no reopen loop against a dead provider), a user seek still lands
    h.view._jump_live()                             # back to the true edge
    h.run(2, sample=False)
    pa2 = n_pa()
    h.fake.wedge()
    quiet = {"fired": False}

    def hs_script(t):
        if n_pa() > pa2:
            quiet["fired"] = True

    h.run(12, growth=lambda t: None, script=hs_script)   # provider stalled
    check("h-starved: no autonomous reopen while no data is ahead",
          not quiet["fired"] and h.fake.wedged,
          f"play_at_fired={quiet['fired']} wedged={h.fake.wedged}")
    clock_pre2 = h.view._cap_clock_s
    want2 = clock_pre2 - 60.0
    h.view._seek_ms(-60000)
    esc2 = {"raw": None}

    def hs2_script(t):
        if esc2["raw"] is None and n_pa() > pa2:
            esc2["raw"] = h.view._cap_content_for_raw(
                h.fake.get_time() / 1000.0)

    h.run(0.5, sample=False)
    h.run(10, growth=lambda t: None, script=hs2_script)
    raw_esc2 = esc2["raw"] if esc2["raw"] is not None \
        else h.view._cap_content_for_raw(h.fake.get_time() / 1000.0)
    check("h-starved: user seek still escalates and lands (data exists "
          "behind in the buffer)",
          not h.fake.wedged and abs(raw_esc2 - want2) <= 3.0,
          f"raw@esc={raw_esc2:.1f} want~{want2:.1f}")

    # -- loop bound + recovery
    total_pa = n_pa() - pa0
    check("h: bounded rescue attempts (revive + escalations, no loop)",
          total_pa <= 4, f"play_at during wedge phases: {total_pa}")
    h.run(20 * DUR, growth=h.grow_1to1)             # provider recovered
    h.reset_metrics()
    settle_s = run_until_settled(h)
    # Re-anchor the metric on h's OWN post-revive axis: the stall/burst/
    # wedge history leaves the L EWMA mid-drain, and the anchor's mean
    # offset vs scenario a's calibration axis is exactly the L-inertia
    # displacement WP3 owns (same class as g3's residual). h3's contract
    # is that captions RE-SETTLE and STAY PUT on a coherent axis after
    # the revives — self-calibrate, then the 12-s window must be tight.
    h.calibrate(20)
    h.reset_metrics()
    h.run(12)
    st = h.report("h: post-revive recovery")
    check("h3: painted cues re-settle after the revives",
          settle_s < 150.0 and st is not None and st["p95"] <= 1.5,
          f"p95={(st['p95'] if st else -1):.2f} settled_in={settle_s:.0f}s "
          f"(absolute-axis drift = L inertia, WP3 feed)",
          kind="data-limited")
    check("h: no pipeline exceptions", not h.exceptions)
    h.teardown()


class FlushQueue(CueQueue):
    """Production flush cadence: CCX's stdout leaves in ~4 KB blocks, so
    cues become visible to the app in bursts (the 2026-08-21 corpus
    measured a 2.57 s median inter-batch gap) and the deferred anchor
    decides once per burst. Releasing per 0.1-s tick (the base queue)
    lets the anchor decide ~25x faster than production, which hides L-
    EWMA inertia entirely — the exact error class WP3 exists to fix."""

    def __init__(self, lag, every=2.5):
        super().__init__(lag)
        self.every = every
        self._last_flush = None

    def release(self, head, deliver):
        if self._last_flush is not None \
                and VT.t - self._last_flush < self.every:
            return
        self._last_flush = VT.t
        super().release(head, deliver)


def lag_cycles(el):
    """WP3 acceptance profile: ramp 1 -> 60 (0.27/s), hold, drain to 5
    (0.275/s), three triangle oscillations 14 <-> 26 (0.48/s legs —
    steeper than the corpus's p99 slope), then settle at 5. Absolute
    seconds (NOT DUR-scaled) so the slopes — and therefore the L-EWMA
    trail the checks gate on — are identical in --quick and full."""
    if el < 220.0:
        return 1.0 + 59.0 * (el / 220.0)
    if el < 250.0:
        return 60.0
    if el < 450.0:
        return 60.0 - 55.0 * ((el - 250.0) / 200.0)
    if el < 600.0:
        m = (el - 450.0) % 50.0             # 25 s triangle legs
        tri = 1.0 - abs(m - 25.0) / 25.0    # 0 -> 1 -> 0
        return 14.0 + 12.0 * tri
    return 5.0


def phase_stats(h, t0, t1):
    """Painted-cue stats over a VT window: p95/max displacement and the
    exact-window rate (same tolerance as the primary assertion)."""
    errs = sorted(abs(e) for t, e in h.paint_errs if t0 <= t < t1)
    if not errs:
        return None
    n = len(errs)
    return {"n": n, "p95": errs[min(n - 1, int(n * 0.95))],
            "max": errs[-1],
            "exact": sum(1 for e in errs if e <= _EXACT_TOL_S) / n}


def scenario_i():
    print("\n== scenario i: L ramp/drain cycles, store coherence (WP3) ==",
          flush=True)
    prof = LagProfile("cycles(1->60 hold, ->5, 14<->26 x3, settle 5)",
                      lag_cycles)
    h = fresh_harness(FlushQueue(prof), backlog=10.0)
    prof.t0 = VT.t                     # profile el anchors at scenario start
    print(f"    L(t) profile: {prof} @ 2.5 s flush cadence", flush=True)
    t0 = VT.t
    jumped = {"done": False}

    # -- a single-flush edge-probe glitch (+6 s, one decision sees it):
    # the anchor target jumps out-of-band for exactly one batch. A robust
    # snap policy must let it ride the EWMA (the store never moves); the
    # pre-WP3 policy slammed the whole store +6 and back (a rebase
    # round-trip, painted cues displaced mid-display).
    n_rel0 = [None]
    orig_edge = h.view._cap_edge_s

    def glitchy_edge():
        return orig_edge() + 6.0

    def script(t):
        el = t - t0
        if el >= 250.0 and not jumped["done"]:
            # D1 adaptive jump-to-live at the hold->drain boundary: the
            # viewer's ~12 s backlog was outrun by L long ago (data-
            # limited blanks are scenario c's contract); landing L+3
            # behind the head keeps the viewer inside the captioned
            # region for the drain/osc/settle — production's own answer.
            jumped["done"] = True
            h.view._jump_live()
        if el >= 612.0 and n_rel0[0] is None:
            n_rel0[0] = len(h.queue.released)
            h.view._cap_edge_s = glitchy_edge
        elif n_rel0[0] is not None \
                and len(h.queue.released) > n_rel0[0]:
            h.view._cap_edge_s = orig_edge   # exactly one flush saw it

    h.run(640, script=script)
    h.view._cap_edge_s = orig_edge
    st_ramp = phase_stats(h, t0 + 5.0, t0 + 220.0)
    st_drain = phase_stats(h, t0 + 250.0, t0 + 450.0)
    st_osc = phase_stats(h, t0 + 450.0, t0 + 600.0)
    st_settle = phase_stats(h, t0 + 600.0, t0 + 640.0)
    whole = phase_stats(h, t0, t0 + 640.0)
    print(f"    i phases: ramp n={st_ramp['n'] if st_ramp else 0} "
          f"p95={st_ramp['p95']:.2f} | drain n={st_drain['n'] if st_drain else 0} "
          f"p95={st_drain['p95']:.2f} | osc n={st_osc['n'] if st_osc else 0} "
          f"p95={st_osc['p95']:.2f} | settle+glitch "
          f"n={st_settle['n'] if st_settle else 0} "
          f"p95={st_settle['p95']:.2f} max={st_settle['max']:.2f}",
          flush=True)
    check("i: L ramp 1->60 painted displacement contained (p95 <= 2.2)",
          st_ramp is not None and st_ramp["n"] >= 40
          and st_ramp["p95"] <= 2.2,
          f"p95={(st_ramp['p95'] if st_ramp else -1):.2f} "
          f"n={st_ramp['n'] if st_ramp else 0} "
          f"(instant-sample pin; pinning via the L EWMA trails ~10x)",
          kind="data-limited")
    check("i: L drain 60->5 painted displacement contained (p95 <= 2.2)",
          st_drain is not None and st_drain["n"] >= 40
          and st_drain["p95"] <= 2.2,
          f"p95={(st_drain['p95'] if st_drain else -1):.2f} "
          f"n={st_drain['n'] if st_drain else 0}", kind="data-limited")
    check("i: repeated L oscillations stay contained (p95 <= 3.0)",
          st_osc is not None and st_osc["n"] >= 40
          and st_osc["p95"] <= 3.0,
          f"p95={(st_osc['p95'] if st_osc else -1):.2f} "
          f"n={st_osc['n'] if st_osc else 0}", kind="data-limited")
    check("i: settle is clean and a lone edge-glitch never displaces "
          "painted cues (p95 <= 1.5, max <= 3.0)",
          st_settle is not None and st_settle["p95"] <= 1.5
          and st_settle["max"] <= 3.0,
          f"p95={(st_settle['p95'] if st_settle else -1):.2f} "
          f"max={(st_settle['max'] if st_settle else -1):.2f} "
          f"(glitch @+612 s; a store slam round-trips ~6)")
    check("i: whole-run exact-window text >= 90%",
          whole is not None and whole["exact"] >= 0.90,
          f"exact={whole['exact'] * 100 if whole else 0:.0f}% "
          f"n={whole['n'] if whole else 0} (ramps trade a little for "
          f"trail; steady/scrub gates below assert >= 95/85%)",
          kind="data-limited")
    # scrub into the region stored during the first ramp/drain: the WP3
    # debt gate keeps stored cues within the band of the current axis, so
    # captions paint where they actually play (mixed-axis = far off)
    h.view._seek_ms(-240000)
    h.run(1, sample=False)
    h.reset_metrics()
    h.run(45, growth=h.grow_1to1)
    st_scrub = h.report("i: scrubbed into cycle-1 region")
    check("i: scrubbed-back cues coherent after the swings (p95 <= 2.0)",
          st_scrub is not None and st_scrub["p95"] <= 2.0,
          f"p95={(st_scrub['p95'] if st_scrub else -1):.2f}")
    check("i: scrubbed-back exact-window text >= 85%",
          h.paint_n >= 10 and h.exact_rate() >= 0.85,
          f"{h.paint_exact}/{h.paint_n} ambig={h.paint_ambig}")
    check("i: blanks stay data-limited (<= L swing + margin)",
          h.max_stop <= 15.0, f"max={h.max_stop:.1f}s", kind="data-limited")
    check("i: no pipeline exceptions", not h.exceptions)
    h.teardown()


def main():
    global STREAM, TMPROOT
    if not os.path.isfile(RECORDING):
        print(f"recording missing: {RECORDING}")
        return 2
    print(f"scanning {os.path.basename(RECORDING)} ...", flush=True)
    t0 = _real_time.time()
    STREAM = LoopedTS(RECORDING)
    print(f"  span={STREAM.span:.2f}s size={STREAM.size:,} "
          f"pcrs={len(STREAM.pcrs)} edits={len(STREAM.edits)} "
          f"({_real_time.time() - t0:.1f}s)", flush=True)

    app = QtWidgets.QApplication.instance() \
        or QtWidgets.QApplication(sys.argv)

    # Pin the INSTALLED CCExtractor: it streams SRT incrementally through
    # pipes (the real live topology). The vendored 0.88 build CANNOT stream
    # (it reads stdin to EOF before emitting a single byte — verified by
    # ccx_pipe_repro.py), so pinning it livelocks the starve guard. The
    # installed build parses this 4K HEVC recording at only ~1.1x realtime,
    # which simply paces the harness to roughly real time (~35 min full).
    _orig_find = live_cc_mod.find_ccextractor
    installed = _orig_find()
    assert installed and os.path.abspath(installed) != os.path.abspath(
        live_cc_mod.bundled_ccextractor()), \
        "installed CCExtractor required (bundled 0.88 can't stream)"
    live_cc_mod.find_ccextractor = lambda: installed
    pv_mod.find_ccextractor = lambda: installed

    # THE clock seam: player_view reads the wall clock only through
    # now_s() (see player_view.now_s) — rebind it to the virtual clock so
    # every watchdog / cooldown / dead-reckoned gate runs on scripted
    # time, independent of real CCExtractor pacing.
    _orig_now_s = pv_mod.now_s
    pv_mod.now_s = lambda: VT.t

    TMPROOT = tempfile.mkdtemp(prefix="mtp_adv_root_")
    try:
        for name, fn in (("a", scenario_a), ("b", scenario_b),
                         ("c", scenario_c), ("d", scenario_d),
                         ("e", scenario_e), ("f", scenario_f),
                         ("g", scenario_g), ("h", scenario_h),
                         ("i", scenario_i)):
            if ONLY and name not in ONLY:
                continue
            fn()
    finally:
        shutil.rmtree(TMPROOT, ignore_errors=True)
        pv_mod.now_s = _orig_now_s

    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed "
          f"(mechanism {BY_KIND['mechanism'][0]}/{sum(BY_KIND['mechanism'])}"
          f", data-limited {BY_KIND['data-limited'][0]}/"
          f"{sum(BY_KIND['data-limited'])})", flush=True)
    for f in FAIL:
        print("  FAILED:", f, flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
