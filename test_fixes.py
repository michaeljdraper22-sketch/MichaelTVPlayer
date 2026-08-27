# -*- coding: utf-8 -*-
"""Offscreen smoke test for the playback-overlay / scrubber / DVR fixes.

Run:  .venv\\Scripts\\python.exe test_fixes.py   (sets QT_QPA_PLATFORM itself)
"""
import os
import sys
import time

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


def main():
    app = QtWidgets.QApplication(sys.argv)
    cfg = Config.load()
    cfg.data["control_buttons"] = dict(cfg.control_buttons)  # timebar True
    view = PlayerView(cfg)
    view.resize(1280, 720)
    view.show()
    app.processEvents()

    print("[1] QSS / black plates removed")
    qss = pv_mod._OVERLAY_QSS
    qss_nc = __import__("re").sub(r"/\*.*?\*/", "", qss, flags=__import__("re").S)
    check("no rgba(0,0,0,1) plates left in QSS", "rgba(0,0,0,1)" not in qss_nc)
    check("buttons carry invisible alpha-2 gray hit plates",
          "#ctlOverlay QToolButton { background-color: rgba(63,63,63,2);"
          in qss)
    check("corner buttons carry alpha-2 gray hit plates too",
          "#ovButton { background-color: rgba(63,63,63,2);" in qss)
    check("no visible white plates left (tiles on dark video)",
          "rgba(255,255,255,3)" not in qss_nc)
    check("#ovStatus pill style present", "#ovStatus" in qss)
    try:
        app.setStyleSheet(app.styleSheet())  # must not raise
        check("stylesheet re-applies cleanly", True)
    except Exception as exc:  # noqa: BLE001
        check(f"stylesheet re-applies cleanly ({exc!r})", False)

    print("[2] time scrubber appears in chase mode, above the buttons")
    view._mode = "chase"
    view._update_control_state()
    view._wake()
    view.ctl.adjustSize()
    view._layout_overlays()
    app.processEvents()
    check("_scrub_on is True", view._scrub_on is True)
    check("scrub_row is not hidden", not view.scrub_row.isHidden())
    y_scrub = view.scrub_row.mapTo(view.ctl, QtCore.QPoint(0, 0)).y()
    y_btn = view.btn_play.mapTo(view.ctl, QtCore.QPoint(0, 0)).y()
    h_scrub = view.scrub_row.height()
    check(f"scrub_row sits above the button row "
          f"(y={y_scrub}+{h_scrub} <= btn y={y_btn})",
          h_scrub > 0 and y_scrub + h_scrub <= y_btn + 1)
    check("slider has a live range", view.slider.maximum() >= 0)

    print("[3] scrubber self-heal inside _tick")
    view._scrub_on = False
    view.scrub_row.hide()
    view.dvr = type("FakeDVR", (), {
        "running": True, "file_path": None, "buffer_file": lambda self: None})()
    view._tick()          # chase branch must re-show the row
    app.processEvents()
    check("_tick re-shows the scrubber while chasing",
          view._scrub_on and not view.scrub_row.isHidden())
    view.dvr = None

    print("[4] DVR status pill")
    view._set_dvr_status("DVR 5s / 20s buffered\u2026")
    app.processEvents()
    check("pill visible + overlay kept alive",
          view._dvr_status.isVisible() and view.overlay.isVisible())
    check("pill text set", view._dvr_status.text().startswith("DVR 5s"))
    view._set_dvr_status(None)
    check("pill hides again", not view._dvr_status.isVisible())

    print("[5] seek clamping uses the safety margin")
    view._dvr_base = 0.0
    view._reset_dvr_clock()
    view._dvr_content_s = 100.0      # 100 s of confirmed content
    view._dvr_first_data = time.time() - 100.0
    frontier = view._frontier_s()
    tgt = view._safe_seek_target(9999.0)
    check(f"target clamped to frontier-3 ({frontier:.0f} -> {tgt:.0f})",
          abs(tgt - (frontier - pv_mod._CHASE_SAFETY_S)) < 0.5)

    print("[6] content clock freezes when the buffer stops growing")
    import tempfile as _tf
    tmp = _tf.NamedTemporaryFile(suffix=".ts", delete=False)
    tmp.write(b"x" * 1000)
    tmp.close()
    fake_dvr = type("FakeDVR", (), {
        "running": True, "file_path": tmp.name, "rec_path": None,
        "_dir": None, "start_time": time.time(),
        "buffer_file": lambda self: tmp.name,
        "safe_stop": lambda self, delete=True: None,
        "stop": lambda self, delete=True: None,
        "elapsed_seconds": lambda self: 0.0,
    })()
    view.dvr = fake_dvr
    view._dvr_base = 0.0
    view._reset_dvr_clock()
    view._note_dvr_data()                    # clock starts
    time.sleep(0.3)
    view._note_dvr_data()                    # no growth -> must NOT advance
    f1 = view._frontier_s()
    check(f"clock frozen while file idle (frontier={f1:.2f}s)", f1 < 0.05)
    with open(tmp.name, "ab") as fh:
        fh.write(b"y" * 5000)                # now it grows
    time.sleep(0.3)
    view._note_dvr_data()
    f2 = view._frontier_s()
    check(f"clock advances only across growth ({f2:.2f}s > 0.3s-ish)",
          0.25 <= f2 <= 2.5)
    os.unlink(tmp.name)
    view.dvr = None

    print("[7] revive path: buttons work on a dead (ended) player")
    calls = []

    class FakeVlc:
        def __init__(self):
            self.state = "playing"
            self.t = 50_000
        def is_playing(self):
            return self.state == "playing"
        def state_name(self):
            return self.state
        def get_time(self):
            return self.t if self.state != "idle" else -1
        def get_length(self):
            return 0
        def set_time(self, ms):
            calls.append(("set_time", ms))
        def play_at(self, url, start, record_path=None):
            calls.append(("play_at", round(start, 1)))
            self.state = "playing"
            self.t = int(start * 1000)
        def resume(self):
            calls.append(("resume",))
        def pause(self):
            calls.append(("pause",))
        def toggle_pause(self):
            calls.append(("toggle_pause",))
        def seek_ms(self, ms):
            calls.append(("seek_ms", ms))
        def set_rate(self, r):
            calls.append(("set_rate", r))
        def set_volume(self, v):
            pass
        def set_mute(self, m):
            pass

    view._mode = "chase"
    view.dvr = fake_dvr = type("FakeDVR2", (), {
        "running": True, "file_path": "buf.ts",
        "buffer_file": lambda self: "buf.ts"})()
    view._reset_dvr_clock()
    view._dvr_content_s = 200.0
    view.vlc, real_vlc = FakeVlc(), view.vlc

    def saw(kind, val=None):
        for c in reversed(calls[-5:]):
            if c[0] == kind and (val is None or c[1] == val):
                return True
        return False

    # healthy player: rewind is a plain set_time
    view._seek_ms(-60000)
    check("healthy player: rewind -> set_time(0)",
          saw("set_time", 0))
    # dead player: same press revives via play_at instead of a no-op
    real = view.vlc
    real.state = "ended"
    real.t = 50_000
    view._vid_s = 50.0        # tracked position (VLC timestamps untrusted)
    view._chase_started = True
    view._seek_ms(-60000)
    check("dead player: rewind revives via play_at(0)",
          saw("play_at", 0.0))
    real.state = "ended"          # fake's play_at revived it; kill again
    real.t = 50_000
    view._vid_s = 50.0
    view._seek_ms(60000)
    check("dead player: modest FF lands exactly (110s, no clamp needed)",
          saw("play_at", 110.0))
    real.state = "ended"
    real.t = 50_000
    view._vid_s = 50.0
    view._seek_ms(300000)         # 50s + 300s > frontier: must clamp
    check("dead player: big FF revives clamped to frontier-safety",
          saw("play_at", 195.0))
    view._chase_paused = True
    real.state = "ended"
    view._jump_live()
    check("LIVE from paused+dead resumes at frontier-safety",
          saw("play_at", 195.0) and not view._chase_paused)
    view._chase_paused = False
    real.state = "playing"
    view.current = {"kind": "live", "title": "t", "url": "u"}  # media loaded
    view._toggle_pause()
    check("pause while playing -> vlc.pause + flag",
          view._chase_paused and saw("pause"))
    real.state = "playing"
    view._toggle_pause()
    check("resume while healthy -> vlc.resume",
          not view._chase_paused and saw("resume"))
    # no media loaded: play/pause does nothing (pre-stream Space key)
    view.current = None
    del calls[:]
    view._toggle_pause()
    check("pre-stream toggle_pause is a no-op",
          not calls and not view._chase_paused)
    view.vlc = real_vlc
    view.dvr = None
    view._mode = "live"

    print("[8] speed-aware catch-up + tracked-position reopen")
    import inspect
    src = inspect.getsource(pv_mod.PlayerView._tick)
    check("catch-up threshold scales with rate", "self._rate * 0.5" in src)
    check("no 0.75x edge damping left", "0.75" not in src)
    rsrc = inspect.getsource(pv_mod.PlayerView._reopen_chase)
    check("watchdog reopen keeps the tracked position (no jump to live)",
          "_safe_seek_target(at)" in rsrc
          and "max(self._cap_clock_s, self._vid_s)" in rsrc)
    check("watchdog reopen has a same-anchor loop breaker",
          "_reopen_repeats" in rsrc)
    check("speeds capped at 4x (audio mute limit)",
          max(pv_mod._SPEEDS) <= 4.0)
    check("jump-to-beginning button exists + wired",
          hasattr(view, "btn_begin")
          and view.btn_live.x() > view.btn_begin.x() >= view.sep1.x())

    print("[9] chase entry is fast (no runway wait)")
    sig = inspect.signature(view._wait_and_enter_chase)
    check("tries default is 'compute' (-1)", sig.parameters["tries_left"].default == -1)
    view._session += 1
    view._wait_and_enter_chase(view._session - 5)   # stale -> silent no-op
    check("stale wait call is a silent no-op", True)
    src = inspect.getsource(pv_mod.PlayerView._wait_and_enter_chase)
    check("entry needs only buffer-ready + 2.5s",
          "waited >= 2.5" in src)
    body = src.split('"""', 2)[-1]      # drop the docstring
    check("no countdown or buffering pill text", "s buffered" not in body
          and "Buffering" not in body)

    print("[10] watchdog gated on playback start")
    check("_chase_started defaults False", view._chase_started is False)
    view._mode = "chase"
    view._chase_started = False
    view._tick()   # idle player: must NOT count stalls / reopen
    check("no stall counted before playback starts", view._stall_ticks == 0)
    view._mode = "live"

    print("[11] JumpSlider: a click completes (seek runs, _seeking clears)")
    from PyQt5 import QtGui  # noqa: E402
    sl = pv_mod.JumpSlider(QtCore.Qt.Horizontal)
    sl.setRange(0, 1000)
    sl.resize(400, 22)
    sl.show()
    app.processEvents()
    moved, released = [], []
    sl.sliderMoved.connect(lambda v: moved.append(v))
    sl.sliderReleased.connect(lambda: released.append(True))
    x = int(sl.width() * 0.9)
    sl.mousePressEvent(QtGui.QMouseEvent(
        QtCore.QEvent.MouseButtonPress, QtCore.QPointF(x, 11),
        QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier))
    app.processEvents()
    sl.mouseReleaseEvent(QtGui.QMouseEvent(
        QtCore.QEvent.MouseButtonRelease, QtCore.QPointF(x, 11),
        QtCore.Qt.LeftButton, QtCore.Qt.NoButton, QtCore.Qt.NoModifier))
    app.processEvents()
    check(f"click jumps to the clicked point (value={sl.value()})",
          sl.value() >= 700)
    check("click emits sliderMoved", bool(moved))
    check("click emits sliderReleased (the seek actually runs)",
          bool(released))

    print("[12] VOD: Record swaps to Download; speed/seek without live TV")
    view.current = {"kind": "vod", "title": "Movie", "url": "http://x/m.mp4"}
    view._mode = "live"
    view._update_control_state()
    app.processEvents()
    check("Record button hidden for VOD", view.btn_rec.isHidden())
    check("Download button shown + enabled for VOD",
          not view.btn_dl.isHidden() and view.btn_dl.isEnabled())
    check("speed button enabled for VOD", view.btn_speed.isEnabled())
    check("seek buttons enabled for VOD", view.btn_back10.isEnabled())
    view._set_rate(2.0)
    check("VOD speed applies without live TV", abs(view._rate - 2.0) < 1e-6)
    view.current = {"kind": "live", "title": "Ch", "url": "http://x/s.ts"}
    view._update_control_state()
    app.processEvents()
    check("Record button back for live", not view.btn_rec.isHidden())
    check("Download hidden for live", view.btn_dl.isHidden())
    check("rate resets to 1x leaving VOD", abs(view._rate - 1.0) < 1e-6)
    check("LIVE enabled in plain live mode", view.btn_live.isEnabled())
    view.current = None
    view._update_control_state()

    print("[13] volume: clicking the bar jumps the volume there")
    v0 = view.vol_slider.value()
    vx = int(view.vol_slider.width() * 0.9)
    view.vol_slider.mousePressEvent(QtGui.QMouseEvent(
        QtCore.QEvent.MouseButtonPress, QtCore.QPointF(vx, 5),
        QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier))
    app.processEvents()
    view.vol_slider.mouseReleaseEvent(QtGui.QMouseEvent(
        QtCore.QEvent.MouseButtonRelease, QtCore.QPointF(vx, 5),
        QtCore.Qt.LeftButton, QtCore.Qt.NoButton, QtCore.Qt.NoModifier))
    check(f"volume click sets near-max (was {v0}, now "
          f"{view.vol_slider.value()})", view.vol_slider.value() >= 70)
    view.vol_slider.setValue(v0)
    view._vol_save_timer.stop()

    print("[14] control row compacts on narrow (half/quarter) windows")
    view._fit_ctl(300)
    check(f"row compacts (level={view._compact_level})",
          view._compact_level >= 1)
    if view._compact_level >= 2:
        check("separators hidden when tight",
              not view.sep1.isVisibleTo(view.ctl))
    else:
        check("separators hidden when tight (skipped: level < 2)", True)
    view._fit_ctl(1600)
    check("wide window restores the full layout",
          view._compact_level == 0)

    print("[15] frozen VLC clock no longer freezes the timestamps")
    frozen = type("FrozenVlc", (), {
        "is_playing": lambda self: True,
        "get_time": lambda self: 50_000,
        "get_length": lambda self: 0,
        "is_mute": lambda self: False,
        "state_name": lambda self: "playing",
        "set_rate": lambda self, r: None,
        "set_volume": lambda self, v: None,
        "set_mute": lambda self, m: None,
    })()
    view.vlc, real_vlc = frozen, view.vlc
    view._mode = "chase"
    view.dvr = type("FakeDVR3", (), {
        "running": True, "file_path": None,
        "buffer_file": lambda self: None})()
    view._dvr_base = 0.0
    view._reset_dvr_clock()
    view._dvr_content_s = 100.0
    view._dvr_first_data = time.time() - 100.0
    view._chase_started = True
    view._chase_paused = False
    view._vid_s = 50.0
    view._last_raw = None
    view._tick()
    time.sleep(0.5)
    view._tick()
    check(f"chase clock advances past frozen VLC time "
          f"({view._vid_s:.2f}s > 50.15s)", view._vid_s > 50.15)
    # VOD variant: same guard against a stuck clock
    view._mode = "live"
    view.current = {"kind": "vod", "title": "m", "url": "x"}
    frozen.get_length = lambda: 120_000
    view._vid_s = 0.0
    view._last_raw = None
    view._tick_t = None
    view._tick()
    time.sleep(0.5)
    view._tick()
    check(f"VOD clock advances with a frozen VLC time "
          f"({view._vid_s:.2f}s > 0.7s)", view._vid_s > 0.7)
    view.current = None
    view.vlc = real_vlc
    view.dvr = None
    view._mode = "live"

    view.stop()
    app.processEvents()
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
