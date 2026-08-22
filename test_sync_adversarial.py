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

Assertions in every scenario (stage-3 acceptance):
  - cue windows land within +/-1.5 s of where the mock clock displays that
    content (p95 of sampled display error, gated on truth being active and
    the cue having arrived);
  - no silent-stop stretch (truth active + cue arrived + nothing painted)
    beyond ~5 s without recovery — the watchdog contract;
  - scrubbed-back cues stay coherent after rebases (error + text match).

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
from src.ui.player_view import PlayerView  # noqa: E402
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


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"   [{detail}]" if detail else ""), flush=True)


# ----------------------------------------------------------------------------
# virtual time: every wall-clock read inside player_view runs on this clock
# ----------------------------------------------------------------------------
class VirtualTime:
    def __init__(self, t0=1_000_000.0):
        self.t = t0

    def time(self):
        return self.t

    def advance(self, dt):
        self.t += dt


VT = VirtualTime()


class _TimeProxy:
    """Module-level stand-in for `import time` inside player_view."""
    def time(self):
        return VT.t

    def __getattr__(self, name):
        return getattr(_real_time, name)


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

    def renumber(self, step):
        """VLC numbers jump by `step`; the frames on screen do not move."""
        self.axis_offset += step

    def play_at(self, url, t, record_path=None, timeshift=False):
        self.commanded.append(("play_at", t))
        self.base_content = float(t) - self.axis_offset
        self.base_T = VT.t
        self.paused = False
        self.state = "playing"

    def set_time(self, ms):
        self.commanded.append(("set_time", ms / 1000.0))
        self.base_content = ms / 1000.0 - self.axis_offset
        self.base_T = VT.t

    def get_time(self):
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
    app sees, at whatever virtual pace the harness runs."""

    def __init__(self, lag=1.5):
        self.lag_fn = lag if callable(lag) else (lambda: float(lag))
        self.pending = []            # (s, e, text)
        self.released = []           # (s, e, text, T_release)
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
            else:
                keep.append(item)
        self.pending = keep


class WarpQueue(CueQueue):
    """Cue TIMES scaled (scenario e: CCX axis at 2x wall)."""

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
            else:
                keep.append((s, e, text))
        self.pending = keep


# ----------------------------------------------------------------------------
# harness
# ----------------------------------------------------------------------------
class Harness:
    def __init__(self, stream: LoopedTS, tmproot: str):
        self.stream = stream
        self.tmpdir = tempfile.mkdtemp(prefix="mtp_adv_", dir=tmproot)
        self.exceptions = []
        self.m0 = None
        self.rebase_count = 0
        self.frontier_gap_samples = []
        self.samples = []
        self.stop_run = 0.0
        self.max_stop = 0.0
        self.text_ok = 0
        self.text_tot = 0
        self.paint_events = 0
        self.text_neighbor_s = 3.0   # roll-up adjacency for text matching
        self.diag = False            # per-second state dump (debugging)

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
        """The newest RELEASED cue whose raw CCX window covers disp.
        Scans the WHOLE release history (a scrubbed-back viewer is far
        behind the arrival head; the early break keeps it cheap)."""
        best = None
        for s, e, text, _ in reversed(self.queue.released):
            if s - 0.3 <= disp_ccx <= e + 0.55:
                if best is None or e > best[1]:
                    best = (s, e, text)
            elif e < disp_ccx - 60.0:
                break
        return best

    def sample(self, t):
        clock = self.view._cap_clock_s
        painted = list(self.view._cap_wid._lines)
        m0 = self.m0 if self.m0 is not None else 0.0
        disp = clock - m0
        truth = self.truth_cue(disp)
        if painted:
            self.paint_events += 1
        if truth is None:
            self.stop_run = 0
            return
        s, e, text = truth
        if painted:
            err = disp - min(max(disp, s), e)
            self.samples.append((t, err))
            self.text_tot += 1
            # roll-up adjacency: the painted screen may be the truth cue's
            # near neighbor (sub-second m0 offset, CCX's partial-line
            # intermediate screens, and under a warped axis up to a window
            # width ahead). Candidates are collected AROUND the truth cue
            # (not the newest — pauses/scrubs leave the display far behind
            # the arrival head), and a line-containment match accepts both
            # partial and completed versions of the same screen.
            cands = [visible_lines(text)]
            for ns, ne, ntext, _ in reversed(self.queue.released):
                if ns < e - self.text_neighbor_s:
                    break
                if ns <= e + self.text_neighbor_s:
                    cands.append(visible_lines(ntext))
            pl = [ln.lower() for ln in painted if ln.strip()]
            hit = any(p in c or c in p
                      for cl in cands for c in
                      (ln.lower() for ln in cl[-3:] if ln.strip())
                      for p in pl)
            if hit:
                self.text_ok += 1
            self.stop_run = 0
        else:
            self.stop_run += 0.1
            self.max_stop = max(self.max_stop, self.stop_run)

    def calibrate(self, seconds=45.0):
        """Settle, then measure m0 = clock-to-raw-CCX-axis display offset."""
        self.run(max(5.0, seconds - 15.0), sample=False)
        ds = []
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
            if self.view._cap_wid._lines:
                for s, e, text, _ in reversed(self.queue.released[-200:]):
                    d = min(max(clock, s), e)
                    if abs(clock - d) < 4.0:
                        ds.append(clock - d)
                        break
                    if e < clock - 60.0:
                        break
            self.starve_guard()
            _real_time.sleep(0.1 / 22.0)
        self.m0 = statistics.median(ds) if ds else 0.0
        return self.m0, len(ds)

    # -- reporting --
    def stats(self):
        errs = sorted(abs(e) for _, e in self.samples)
        if not errs:
            return None
        n = len(errs)
        return {"n": n, "p50": errs[n // 2],
                "p95": errs[min(n - 1, int(n * 0.95))], "max": errs[-1]}

    def report(self, label):
        st = self.stats()
        if st:
            print(f"    {label}: painted={self.paint_events} n={st['n']} "
                  f"|err| p50={st['p50']:.2f} p95={st['p95']:.2f} "
                  f"max={st['max']:.2f} maxstop={self.max_stop:.1f}s "
                  f"rebases={self.rebase_count} "
                  f"text={self.text_ok}/{self.text_tot}", flush=True)
        else:
            print(f"    {label}: NO PAINTED SAMPLES "
                  f"(maxstop={self.max_stop:.1f}s "
                  f"rebases={self.rebase_count})", flush=True)
        return st


# ----------------------------------------------------------------------------
# scenarios
# ----------------------------------------------------------------------------
STREAM = None
M0 = [None]
TMPROOT = None


def fresh_harness(queue, backlog=8.0):
    h = Harness(STREAM, TMPROOT)
    h.setup(queue, start_backlog=backlog)
    h.m0 = M0[0]
    return h


def scenario_a():
    print("\n== scenario a: cold join, growing divergence, anchor wedge ==",
          flush=True)
    h = fresh_harness(CueQueue())
    t0 = VT.t
    wedge_at = 170 * DUR
    wedged = [False]

    def script(t):
        el = t - t0
        # display axis runs progressively slow: ~7 s of divergence before
        # the wedge at full duration (the stage-1 "captions would stop"
        # condition — under stage-2 timing it must stay benign)
        h.fake.speed_warp = 1.0 - 0.0005 * el
        if not wedged[0] and el >= wedge_at:
            wedged[0] = True
            # the exact failure the watchdog exists for: anchor AND store
            # displaced together (a rebase with a wrong target), cue
            # deliveries frozen so no fresh cue can snap-rebase first
            h.view._cc_off = (h.view._cc_off or 0.0) + 5.0
            h.view._cap_cues.shift(+5.0)
            h.view._filter_engine.shift_windows(+5.0)
            h.queue.frozen = True

    m0, n = h.calibrate(40 * DUR + 12)
    M0[0] = m0
    h.m0 = m0
    print(f"    calibration m0={m0:+.2f}s ({n} samples)", flush=True)
    h.run(wedge_at + 6, script=script)
    st = h.report("drift")
    check("a: drift keeps cues within 1.5 s (p95)",
          st is not None and st["p95"] <= 1.5,
          f"p95={(st['p95'] if st else -1):.2f}")
    check("a: no silent-stop over 6 s during drift",
          h.max_stop <= 6.0, f"max={h.max_stop:.1f}s")
    check("a: wedge applied", wedged[0])
    stop_at_wedge = h.max_stop
    rebases_at_wedge = h.rebase_count
    h.queue.frozen = False       # data flows again; the watchdog fired?
    h.run(15, script=lambda t: None)
    st = h.report("post-wedge")
    check("a: watchdog fired during the freeze",
          h.rebase_count > rebases_at_wedge,
          f"rebases {rebases_at_wedge}->{h.rebase_count}")
    check("a: wedge stop recovered within ~6 s",
          h.max_stop - stop_at_wedge <= 6.5,
          f"stretch={h.max_stop - stop_at_wedge:.1f}s")
    check("a: post-wedge cues within 1.5 s (p95)",
          st is not None and st["p95"] <= 1.5, f"p95={(st['p95'] if st else -1):.2f}")
    check("a: no pipeline exceptions", not h.exceptions,
          h.exceptions[:2] and str(h.exceptions[:2]) or "")
    h.teardown()


def scenario_b():
    print("\n== scenario b: pause 60 s -> resume +25 s step (3 cycles) ==",
          flush=True)
    h = fresh_harness(CueQueue())
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
    check("b: cues within 1.5 s through pause/resume/step (p95)",
          st is not None and st["p95"] <= 1.5,
          f"p95={(st['p95'] if st else -1):.2f}")
    check("b: no silent-stop beyond watchdog window (~5+3 s)",
          h.max_stop <= 8.0, f"max={h.max_stop:.1f}s")
    check("b: 3 pause cycles executed", state["cycle"] == 3)
    check("b: no pipeline exceptions", not h.exceptions)
    h.teardown()


def scenario_c():
    print("\n== scenario c: jump-to-live, L swells 1 -> 20 s ==", flush=True)
    LNOW = [1.0]
    h = fresh_harness(CueQueue(lambda: LNOW[0]), backlog=10.0)
    jumped = {"done": False, "edge": 0.0, "clock": 0.0, "true_head": 0.0}
    t0 = VT.t
    swell_end = 60 * DUR

    def script(t):
        el = t - t0
        if el < swell_end:
            LNOW[0] = 1.0 + (20.0 - 1.0) * (el / swell_end)
        elif not jumped["done"]:
            jumped["done"] = True
            jumped["edge"] = h.view._cap_edge_s()
            jumped["true_head"] = h.head
            h.view._jump_live()               # real jump path
            jumped["clock"] = h.view._cap_clock_s
        elif el > 90 * DUR + swell_end:
            LNOW[0] = max(1.5, LNOW[0] - 0.06)
    h.run(swell_end + 160 * DUR, script=script)
    st = h.report("c")
    check("c: jump landed ~5 s behind the true edge",
          jumped["done"]
          and abs(jumped["clock"] - (jumped["true_head"] - 5.0)) < 3.0
          and abs(jumped["clock"] - (jumped["edge"] - 5.0)) < 2.0,
          f"clock@jump={jumped['clock']:.1f} edge@jump="
          f"{jumped['edge']:.1f} true_head={jumped['true_head']:.1f}")
    check("c: cues within 1.5 s after lag recovery (p95)",
          st is not None and st["p95"] <= 1.5, f"p95={(st['p95'] if st else -1):.2f}")
    check("c: blank stretches stay data-limited (<= L swell + margin)",
          h.max_stop <= 26.0, f"max={h.max_stop:.1f}s")
    check("c: no pipeline exceptions", not h.exceptions)
    h.teardown()


def scenario_d():
    print("\n== scenario d: provider bursts, frontier under-credits ==",
          flush=True)
    h = fresh_harness(CueQueue())
    t0 = VT.t

    def growth(t):
        el = (t - t0) % 60.0
        if 25.0 <= el < 55.0:
            return                          # 30 s stall: nothing appended
        h.grow_to(t - h._t0c)               # 1:1 (catches up the burst)

    h.run(200 * DUR + 10, growth=growth)
    st = h.report("d")
    gaps = h.frontier_gap_samples
    check("d: frontier really under-credited (>=10 s at some point)",
          bool(gaps) and max(gaps) >= 10.0,
          f"max edge-frontier gap={max(gaps or [0]):.1f}s")
    check("d: cues within 1.5 s through bursts (p95)",
          st is not None and st["p95"] <= 1.5, f"p95={(st['p95'] if st else -1):.2f}")
    check("d: no silent-stop beyond watchdog window (~5+4 s)",
          h.max_stop <= 9.0, f"max={h.max_stop:.1f}s")
    check("d: no pipeline exceptions", not h.exceptions)
    h.teardown()


def scenario_e():
    print("\n== scenario e: CCX caption axis runs 2x wall ==", flush=True)
    h = fresh_harness(WarpQueue(2.0))
    h.m0 = None
    h.text_neighbor_s = 12.0         # warped axis: display runs up to
    #                              # (backlog - L) ahead of the viewer
    m0e, n = h.calibrate(45 * DUR + 8)
    h.m0 = m0e
    print(f"    warp-axis calibration m0={m0e:+.2f}s ({n} samples)",
          flush=True)
    h.run(150 * DUR + 5)
    st = h.report("e")
    check("e: 2x caption axis stays displayable (p95 <= 1.5)",
          st is not None and st["p95"] <= 1.5, f"p95={(st['p95'] if st else -1):.2f}")
    check("e: no silent-stop beyond watchdog window (~5+3 s)",
          h.max_stop <= 8.0, f"max={h.max_stop:.1f}s")
    check("e: painted text stays local to the speech (>=80%)",
          h.text_tot >= 10 and h.text_ok / max(1, h.text_tot) >= 0.80,
          f"{h.text_ok}/{h.text_tot} (warped axis: newest-arrived screen "
          f"is shown up to backlog-L ahead — position gate is the p95)")
    check("e: no pipeline exceptions", not h.exceptions)
    h.teardown()


def scenario_f():
    print("\n== scenario f: scrub back 2 min, rebase coherence, delay_ms ==",
          flush=True)
    h = fresh_harness(CueQueue())
    h.run(205 * DUR)
    h.report("f: steady")
    c0 = h.view._cap_clock_s
    h.view._seek_ms(-120000)                # scrub back 2 min (real path)
    h.run(1, sample=False)
    landed = h.view._cap_clock_s
    want = max(0.0, c0 - 119.0)   # clamped at the buffer start
    check("f: scrub -120 s lands on target",
          abs(landed - want) < 2.5,
          f"landed={landed:.1f} want~{want:.1f}")
    h.samples.clear()
    h.text_ok = h.text_tot = 0
    h.paint_events = 0
    h.max_stop = 0.0
    h.stop_run = 0.0
    h.run(45 * DUR + 5)
    st1 = h.report("f: scrubbed-back region")
    check("f: scrubbed-back cues coherent (p95 <= 1.5)",
          st1 is not None and st1["p95"] <= 1.5,
          f"p95={st1 and st1['p95']:.2f}")
    check("f: scrubbed-back text matches (>=85%)",
          h.text_tot >= 10 and h.text_ok / max(1, h.text_tot) >= 0.85,
          f"{h.text_ok}/{h.text_tot}")
    # force a rebase while scrubbed back: the next fresh cue snaps the
    # anchor back — the store (and the region behind us) must stay coherent
    reb0 = h.rebase_count
    h.diag = bool(os.environ.get("MTP_ADV_DIAG"))
    h.view._cc_rebase((h.view._cc_off or 0.0) - 6.0, "harness-force")
    h.view._seek_ms(-60000)
    h.run(1, sample=False)
    h.samples.clear()
    h.run(30 * DUR)
    h.diag = False
    st2 = h.report("f: post-rebase stability")
    check("f: rebase round-tripped (forced + snap-back)",
          h.rebase_count >= reb0 + 2,
          f"rebases {reb0}->{h.rebase_count}")
    check("f: rebased store stays coherent behind the head",
          st2 is not None and st2["p95"] <= 1.5,
          f"p95={(st2['p95'] if st2 else -1):.2f}")
    h.view._seek_ms(120000)                 # forward again, near the edge
    h.run(20, sample=False)
    h.view._seek_ms(-30000)                 # somewhere with dense cues for
    h.run(2, sample=False)                  # the delay probe
    # live delay_ms shift: pure arithmetic — one tick repaints at t+delay
    h.cfg.subtitle_appearance = dict(h.cfg.subtitle_appearance, delay_ms=0)
    h.safe(h.view._caption_tick)
    clock = h.view._cap_clock_s
    base_lines = list(h.view._cap_wid._lines)
    h.cfg.subtitle_appearance = dict(h.cfg.subtitle_appearance, delay_ms=1500)
    h.safe(h.view._caption_tick)
    shifted_lines = list(h.view._cap_wid._lines)
    exp_base = h.view._cap_cues.text_at(clock)
    exp_shifted = h.view._cap_cues.text_at(clock + 1.5)
    check("f: delay_ms shifts the painted cue live (+1.5 s)",
          shifted_lines == exp_shifted and base_lines == exp_base
          and (bool(exp_base) or bool(exp_shifted)),
          f"base={len(exp_base)}ln shifted={len(exp_shifted)}ln")
    check("f: no pipeline exceptions", not h.exceptions)
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

    pv_mod.time = _TimeProxy()      # virtual clock for player_view

    TMPROOT = tempfile.mkdtemp(prefix="mtp_adv_root_")
    try:
        for name, fn in (("a", scenario_a), ("b", scenario_b),
                         ("c", scenario_c), ("d", scenario_d),
                         ("e", scenario_e), ("f", scenario_f)):
            if ONLY and name not in ONLY:
                continue
            fn()
    finally:
        shutil.rmtree(TMPROOT, ignore_errors=True)
        pv_mod.time = _real_time

    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed", flush=True)
    for f in FAIL:
        print("  FAILED:", f, flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
