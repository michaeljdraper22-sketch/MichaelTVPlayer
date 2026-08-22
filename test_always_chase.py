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

from src.config import Config, BUTTON_KEYS  # noqa: E402
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
check("default chase delay is 5 s", cfg.chase_delay == 5)
cfg.chase_delay = 2
check("chase delay clamped at a 5 s floor", cfg.chase_delay == 5)

with tempfile.TemporaryDirectory() as td:
    from pathlib import Path as _P
    (_P(td) / "settings.json").write_text(
        json.dumps({"chase_delay": 15}), encoding="utf-8")
    orig_dir = cfg_mod._data_dir
    cfg_mod._data_dir = lambda: _P(td)
    try:
        legacy = Config.load()
        check("legacy 15 s default migrates down to 5 s once",
              legacy.chase_delay == 5)
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
check("no 'dvr' key in control buttons", "dvr" not in BUTTON_KEYS)

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

    # playback
    def play(self, url, timeshift=None, start_seconds=0.0):
        self.calls.append(("play", url, timeshift, start_seconds))

    def play_at(self, url, start_seconds=0.0, record_path=None,
                append=False, timeshift=None):
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

# chase entry: age the start clock so the 2.5 s gate passes
view._dvr_t0 = time.time() - 3.0
view._dvr_content_s = 6.0
view._dvr_first_data = time.time() - 6.0
view._wait_and_enter_chase(view._session)
app.processEvents()
check("chase entered: mode flips", view._mode == "chase")
check("display player watches the buffer file",
      fake.calls and fake.calls[-1][0] == "play_at"
      and fake.calls[-1][1] == "X:/buffer.ts")
tgt = fake.calls[-1][2]
check(f"chase starts behind the frontier (target {tgt:.1f}s)", 0.0 <= tgt <= 6.0)

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
check("no 'dvr' entry in the control-button set",
      "dvr" not in cfg.control_buttons)

print("[5] jump-to-live keeps the caption cushion")
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
check("LIVE lands edge - 5 s (frontier fallback: no transport seeded yet)",
      fake.calls and fake.calls[-1][0] == "play_at"
      and abs(fake.calls[-1][2] - 95.0) < 0.5)

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

print("[6] REC rides the always-on chase pipeline")
cfg.data["record_folder"] = tempfile.mkdtemp()
REC.clear()
view._mode = "chase"
view.dvr = StubRecorder()
view.dvr.running = True
view.dvr.file_path = "X:/buffer.ts"
view._on_rec_toggled(True)
check("REC restarts the recorder with the recording output",
      REC and REC[-1][1] is not None)
check("REC keeps chase mode", view._mode == "chase")
REC.clear()
view._stop_recording()
check("REC off keeps the chase pipeline (recorder restarts alone)",
      view._mode == "chase" and REC and REC[-1][1] is None)

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
