# -*- coding: utf-8 -*-
"""Always-chase live TV: every live channel auto-starts the recorder and
enters chase playback; the DVR button is gone; jump-to-live keeps a 5 s
safety (caption) cushion; recorder failure falls back to direct live with
bounded automatic retries (backoff, pill, budget reset on channel change);
while chasing, every transport control stays usable.

Run:  .venv\\Scripts\\python.exe test_always_chase.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def temp_config() -> Config:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    return Config({}, Path(path))


app = QtWidgets.QApplication(sys.argv)


def pump(seconds):
    """Run the event loop for a while so QTimer.singleShot retries fire."""
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.02)

print("[1] config: chase delay default + migration, no DVR leftovers")
import json
import src.config as cfg_mod

cfg = temp_config()
check("default chase delay is 10 s (two provider batches)",
      cfg.chase_delay == 10)
cfg.chase_delay = 2
check("chase delay clamped at a 10 s floor", cfg.chase_delay == 10)

with tempfile.TemporaryDirectory() as td:
    from pathlib import Path as _P
    (_P(td) / "settings.json").write_text(
        json.dumps({"chase_delay": 15}), encoding="utf-8")
    orig_dir = cfg_mod._data_dir
    cfg_mod._data_dir = lambda: _P(td)
    try:
        legacy = Config.load()
        check("legacy 15 s default migrates down, then up to the 10 s floor",
              legacy.chase_delay == 10)
    finally:
        cfg_mod._data_dir = orig_dir
    (_P(td) / "settings.json").write_text(
        json.dumps({"chase_delay": 20, "_chase_delay_migrated": True}),
        encoding="utf-8")
    cfg_mod._data_dir = lambda: _P(td)
    try:
        chosen = Config.load()
        check("an explicit non-default delay is kept", chosen.chase_delay == 20)
    finally:
        cfg_mod._data_dir = orig_dir

check("safety margin is 5 s (caption cushion after jump-to-live)",
      pv_mod._CHASE_SAFETY_S == 5.0)

print("[2] play_media(live) auto-engages the chase pipeline")
REC = []


class StubRecorder:
    def __init__(self, max_minutes=30, network_caching=1500, instance=None):
        self.running = False
        self.file_path = None
        self.rec_path = None
        self._dir = None

    def start(self, url, output_path=None, buffer_path=None):
        REC.append((url, output_path, buffer_path))
        self.running = True
        self.file_path = "X:/buffer.ts"

    def buffer_file(self):
        return self.file_path if self.running else None

    def stop(self, delete=True):
        self.running = False

    def safe_stop(self, delete=True):
        self.running = False


class FakeVLC:
    """Records the calls play_media/_reopen_display make."""

    def __init__(self):
        self.calls = []
        self.spu = -1
        self.tracks = []
        self.instance = object()
        self.t = -1
        self.state = "playing"
        self.last_play_cache = None

    # playback
    def play(self, url, timeshift=None, start_seconds=0.0,
             start_wait_s=20.0, sub_file=None, network_caching_ms=0,
             http_referer=None):
        # start_wait_s/sub_file/network_caching_ms/http_referer: later
        # versions grew the call signature (stremio open wait, external
        # subs, the VOD read-ahead depth, referer-locked CDNs) — the stub
        # accepts and ignores them, except the live read-ahead depth.
        self.calls.append(("play", url, timeshift, start_seconds))
        self.last_play_cache = int(network_caching_ms)

    def play_at(self, url, start_seconds=0.0, record_path=None,
                append=False, timeshift=None, http_referer=None):
        self.calls.append(("play_at", url, start_seconds, timeshift))

    def stop_and_release(self):
        self.calls.append(("stop",))

    def is_playing(self):
        return self.state == "playing"

    def get_time(self):
        return self.t

    def get_length(self):
        return 0

    def state_name(self):
        return self.state

    def set_time(self, ms):
        self.calls.append(("set_time", ms))

    def set_rate(self, r):
        pass

    def set_window(self, wid):
        pass

    # subtitles / audio
    def set_spu(self, tid):
        self.spu = tid
        self.calls.append(("set_spu", tid))

    def active_spu(self):
        return self.spu

    def spu_tracks(self):
        return list(self.tracks)

    def set_spu_delay(self, ms):
        pass

    def set_volume(self, v):
        pass

    def set_mute(self, on):
        pass

    def is_mute(self):
        return False

    def set_filter_mute(self, on):
        pass

    # video
    def set_scale_mode(self, m):
        pass

    def apply_scale(self, w, h):
        pass


pv_mod.VlcRecorder = StubRecorder

cfg = temp_config()
view = PlayerView(cfg)
view._attach_done = True       # skip the startup attach wait (offscreen)
view._attached = True
fake = FakeVLC()
view.vlc = fake
view._filter_engine.player = fake

view.play_media({"kind": "live", "url": "http://x/live.ts", "title": "L"})
check("recorder started on the live URL (one connection)",
      REC and REC[-1][0] == "http://x/live.ts")
check("no direct network playback on the display player",
      not any(c[0] == "play" and c[1] == "http://x/live.ts"
              for c in fake.calls))
check("no buffer-filling pill while waiting (quiet start)",
      view._dvr_status.isHidden()
      or not view._dvr_status.text())

# The live cushion is TWO provider batches (2026-11-22: the panel's .ts
# delivers 5.0 s of MPEG-TS instantly, then 5.0 s of silence). A buffer
# holding a single batch must not start playback — that is exactly the
# shape that froze at every seam.
check("effective live delay floors at 10 s (two batches)",
      pv_mod._LIVE_MIN_CHASE_DELAY_S == 10.0
      and view._live_delay_s() == 10.0)
view._dvr_t0 = time.time() - 3.0
view._dvr_content_s = 6.0                   # one batch
view._dvr_first_data = time.time() - 6.0
fake.calls.clear()
view._wait_and_enter_chase(view._session, tries_left=10)
app.processEvents()
check("no chase start with a one-batch cushion (6 s)",
      view._mode == "live"
      and not [c for c in fake.calls if c[0] in ("play", "play_at")])
view._session += 1          # cancel the scheduled fill retry

# chase entry: age the start clock so the 2.5 s gate passes, and give the
# buffer the full two-batch cushion
view._dvr_t0 = time.time() - 3.0
view._dvr_content_s = 12.0
view._dvr_first_data = time.time() - 12.0
fake.calls.clear()
view._wait_and_enter_chase(view._session)
app.processEvents()
check("chase entered: mode flips", view._mode == "chase")
check("display player watches the buffer file",
      fake.calls and fake.calls[-1][0] == "play_at"
      and fake.calls[-1][1] == "X:/buffer.ts")
tgt = fake.calls[-1][2]
check(f"chase starts 10 s behind the frontier (target {tgt:.1f}s)",
      abs(tgt - 2.0) <= 0.5)

print("[2b] caption reader starts IN PARALLEL with the chase fill wait")
CCStarts = []


class StubCC(QtCore.QObject):
    cue = QtCore.pyqtSignal(float, float, str)

    def __init__(self, parent=None):
        super().__init__()

    def start(self, ts, content_offset_s=0.0, join_bytes=0):
        CCStarts.append((ts, join_bytes))
        return True

    def stop(self):
        pass

    def deleteLater(self):
        pass


pv_mod.CCSource = StubCC
pv_mod.find_ccextractor = lambda: "C:/fake/ccx.exe"
REC.clear()
view._ensure_dvr_stopped()
view._mode = "live"
view._cap_want = True       # captions on: the reader must join EARLY
view.play_media({"kind": "live", "url": "http://x/live2.ts", "title": "L2"})
check("CC reader started during play_media (parallel with the fill)",
      CCStarts and CCStarts[0][0] == "X:/buffer.ts"
      and view._cc_source is not None)
check("young buffer joins from byte 0 (instant catch-up)",
      CCStarts and CCStarts[0][1] == 0)
check("reader up BEFORE chase entry (truly parallel, not sequential)",
      view._mode == "live")
view._session += 1          # kill the pending chase-wait chain of [2b]
view._cap_want = False
view._stop_cc_source()
# [3] below asserts against the [2] media: restore it
view.current = {"kind": "live", "url": "http://x/live.ts", "title": "L"}

print("[3] recorder failure falls back to direct live (retry scheduled)")
REC.clear()
fake.calls.clear()
view._ensure_dvr_stopped()
view._mode = "live"
view.dvr = StubRecorder()
view.dvr.running = True
view.dvr.file_path = None            # never produces a usable buffer
view._dvr_t0 = time.time() - 30.0
orig_delays = pv_mod._CHASE_RETRY_DELAYS
pv_mod._CHASE_RETRY_DELAYS = (9999.0, 9999.0)   # keep it from firing here
view._wait_and_enter_chase(view._session, tries_left=0)
app.processEvents()
check("give-up reverts to plain live mode", view._mode == "live")
check("display player re-dials the live URL",
      any(c[0] == "play" and c[1] == "http://x/live.ts"
          for c in fake.calls))
check("plain live fallback gets a two-batch network cache",
      fake.last_play_cache is not None
      and fake.last_play_cache >= pv_mod._LIVE_READAHEAD_MS
      and pv_mod._LIVE_READAHEAD_MS == 10000)
check("give-up counts against the chase retry budget",
      view._chase_fail_count == 1)
check("pill announces the automatic retry",
      "retry" in view._dvr_status.text().lower())

print("[3b] the fallback re-engages chase automatically (backoff retries)")
pv_mod._CHASE_RETRY_DELAYS = (0.15, 0.15)       # fast enough to test
REC.clear()
fake.calls.clear()
view._ensure_dvr_stopped()
view._mode = "live"
view._chase_fail_count = 0
view._set_dvr_status(None)
view.dvr = StubRecorder()
view.dvr.running = True
view.dvr.file_path = None
view._dvr_t0 = time.time() - 30.0
view._wait_and_enter_chase(view._session, tries_left=0)   # failure #1
check("first failure scheduled the first retry (1/2)",
      view._chase_fail_count == 1)
REC.clear()
fake.calls.clear()
pump(0.6)                       # backoff elapses -> the retry timer fires
check("retry re-engaged the recorder on the live URL",
      REC and REC[-1][0] == "http://x/live.ts")
check("retry stopped the display player for the handoff",
      any(c[0] == "stop" for c in fake.calls))
view._session += 1              # kill the retried engagement's wait chain

# the retried engagement fails too -> one final retry
view.dvr.file_path = None
view._dvr_t0 = time.time() - 30.0
REC.clear()
fake.calls.clear()
view._wait_and_enter_chase(view._session, tries_left=0)   # failure #2
check("second failure schedules the final retry (2/2)",
      view._chase_fail_count == 2)
pump(0.6)
check("second retry re-engaged the recorder",
      REC and REC[-1][0] == "http://x/live.ts")
view._session += 1              # kill this engagement's wait chain too

print("[3c] budget spent: controls stay honest, no more retries")
view.dvr.file_path = None
view._dvr_t0 = time.time() - 30.0
REC.clear()
fake.calls.clear()
view._wait_and_enter_chase(view._session, tries_left=0)   # failure #3
check("third failure exhausts the budget", view._chase_fail_count == 3)
check("stays in plain live mode", view._mode == "live")
check("explanation pill shown (why rewind is disabled)",
      "rewind" in view._dvr_status.text().lower())
pump(0.5)
check("no retry fires after the budget is spent", not REC)
check("rewind honestly disabled without a buffer",
      not view.btn_back10.isEnabled())
check("scrubber hidden in the fallback", view.scrub_row.isHidden())
check("LIVE still works in the fallback", view.btn_live.isEnabled())

# a channel change resets the budget and engages a fresh pipeline
REC.clear()
view.play_media({"kind": "live", "url": "http://x/live2.ts", "title": "L2"})
check("channel change resets the chase retry budget",
      view._chase_fail_count == 0)
check("fresh engagement started on the new channel",
      REC and REC[-1][0] == "http://x/live2.ts")
view._session += 1              # neutralize the fresh engagement's chain
view._ensure_dvr_stopped()
view._set_dvr_status(None)
pv_mod._CHASE_RETRY_DELAYS = orig_delays

print("[4] the DVR button is gone")
check("no btn_dvr on the view", not hasattr(view, "btn_dvr"))

print("[5] jump-to-live keeps the live cushion")
view._mode = "chase"
view.dvr = StubRecorder()
view.dvr.running = True
view.dvr.file_path = "X:/buffer.ts"
view._dvr_base = 0.0
view._reset_dvr_clock()
view._dvr_content_s = 100.0
view._dvr_first_data = time.time() - 100.0
view._vid_s = 50.0
fake.calls.clear()
fake.t = 50_000
fake.state = "ended"                # revive path reopens AT the target
view._chase_started = True
view._jump_live()
app.processEvents()
check("LIVE lands edge - 10 s (two-batch live cushion)",
      fake.calls and fake.calls[-1][0] == "play_at"
      and abs(fake.calls[-1][2] - 90.0) < 0.5)

print("[5b] while chasing, _tick keeps every transport control usable")
view._mode = "chase"
view.dvr = StubRecorder()
view.dvr.running = True
view.dvr.file_path = "X:/buffer.ts"
view._dvr_base = 0.0
view._reset_dvr_clock()
view._dvr_content_s = 100.0
view._dvr_first_data = time.time() - 100.0
view._vid_s = 50.0
view._last_raw = None
fake.calls.clear()
fake.t = 50_000
fake.state = "playing"
view._chase_started = True
transport = (view.btn_back60, view.btn_back10, view.btn_fwd10,
             view.btn_begin, view.btn_speed, view.btn_live)
for b in transport:
    b.setEnabled(False)          # simulate a stale plain-live disable
view._scrub_on = False
view._set_scrub_visible(False)
view._tick()
check("rewind/begin/speed/LIVE all enabled while chasing",
      all(b.isEnabled() for b in transport))
check("scrubber self-heals on while chasing",
      not view.scrub_row.isHidden())
check("scrubber spans the buffer frontier",
      view.slider.maximum() == 100_000)

print("[6] REC tapes the DVR buffer (no recorder restart, no sout branch)")
cfg.data["record_folder"] = tempfile.mkdtemp()
REC.clear()
buf_dir = tempfile.mkdtemp()
buf_path = os.path.join(buf_dir, "buffer.ts")
# a mature buffer (REC pressed an hour into the show) whose current write
# frontier sits exactly on a TS packet boundary
OLD = b"O" * (188 * 20)
with open(buf_path, "wb") as f:
    f.write(OLD)
view._mode = "chase"
view.dvr = StubRecorder()
view.dvr.running = True
view.dvr.file_path = buf_path
view._dvr_first_data = time.time() - 600.0
view._rec_path = None
view._on_rec_toggled(True)
check("REC does not restart/rotate the recorder",
      not REC)
check("REC keeps chase mode", view._mode == "chase")
cp = view._rec_copier
check("REC starts the append-only tape copier", cp is not None)
PAY = bytes(range(256)) * 40
with open(buf_path, "ab") as f:
    f.write(PAY)
deadline = time.time() + 5.0
while time.time() < deadline and cp.written < len(PAY):
    time.sleep(0.02)
rec_path = view._rec_path
try:
    got = open(rec_path, "rb").read()
except OSError:
    got = b""
check("the recording starts at the REC press (no DVR history replayed)",
      got == PAY)
# a recorder rotation (fresh sibling) must APPEND across the seam
buf2 = os.path.join(buf_dir, "buffer_2.ts")
open(buf2, "wb").close()
cp.retarget(buf2, at_end=False)
PAY2 = b"2" * (188 * 5)
with open(buf2, "ab") as f:
    f.write(PAY2)
deadline = time.time() + 5.0
while time.time() < deadline and cp.written < len(PAY) + len(PAY2):
    time.sleep(0.02)
check("a recorder restart appends to the SAME recording (nothing truncated)",
      open(rec_path, "rb").read() == PAY + PAY2)
check("the recording never goes through VLC's sout",
      cp.output_path == rec_path and not cp.failed)
REC.clear()
view._stop_recording()
check("REC off does not restart the recorder either", not REC)
check("REC off keeps the chase pipeline", view._mode == "chase")
check("REC off finalizes the file (all bytes flushed)",
      open(rec_path, "rb").read() == PAY + PAY2)
check("recording bookkeeping cleared", view._rec_copier is None
      and view._rec_path is None)

print("[7] VOD never starts a recorder")
REC.clear()
fake.calls.clear()
view._rec_path = None
view._ensure_dvr_stopped()
view._mode = "live"
view.play_media({"kind": "vod", "url": "http://x/m.mkv", "title": "M"})
check("no recorder for VOD", not REC)
check("VOD plays directly with timeshift off",
      fake.calls and fake.calls[-1][0] == "play"
      and fake.calls[-1][1] == "http://x/m.mkv"
      and fake.calls[-1][2] is False)

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
view.stop()
sys.exit(1 if FAIL else 0)
