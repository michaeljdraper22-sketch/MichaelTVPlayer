# -*- coding: utf-8 -*-
"""Live-TV recovery: buffer rotations, truncation and the dead-open ladder.

This is the 2026-11-16 ESPN field report, pinned. The stream "failed" a
couple of times and stayed dead; switching channels and back fixed it. The
log showed the whole chain:

  * 21:11:14  a second launch (Stremio handoff) ran the startup temp sweep
              and deleted the RUNNING instance's 232 MB DVR buffer
              (``dvr.buffer_file size transition: 232488508 -> 0``).
              Covered by test_temp_cleanup.py.
  * 21:11:18  the display player's file was gone; nothing could grow it, so
              the recorder watchdog waited its full 65 s before re-dialing.
  * 21:12:19  the recorder restarted into a FRESH buffer file — its own
              media timeline starts at ~0 while the app's content clock
              carried 338 s from the previous generation.
  * 21:12:44  the tail-hold resume asked libvlc for start=361.7 s inside a
              ~25 s file: it landed at 0.0, ended immediately, and every
              8 s rescue reopened there again — for 18 minutes, until the
              user switched channels.

What this suite pins:
  [1] a reopen after a rotation is mapped onto the ACTIVE file's timeline
      (content 361.7 in a file that begins at content 338.3 -> play_at 23.4)
  [2] the first generation is unchanged (identity mapping)
  [3] transport seeds keep the generation offset (seeks after a reopen)
  [4] a buffer the writer TRUNCATED is rebased on the spot (no 65 s wait)
  [5] reopens that never produce playback escalate to a full re-dial — the
      user's "switch channel and back", automated — and the re-dial is
      rate-limited
  [6] a dead player's landing cannot clear the unseekable-buffer ladder
  [7] the recorder watchdog restarts on a LOST buffer far sooner than on a
      merely stalled one

Run:  .venv\\Scripts\\python.exe -X utf8 test_live_recovery.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5 import QtWidgets  # noqa: E402

import src.ui.player_view as pv  # noqa: E402
from src.config import Config  # noqa: E402
from src.ui.player_view import (  # noqa: E402
    PlayerView, _REOPEN_DEAD_LIMIT, _HARD_RELOAD_MIN_GAP_S,
    _REC_ENDED_RESTART_S, _REC_GONE_RESTART_S, _REC_STALL_RESTART_S,
    _SEEK_ZERO_STRIKES)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"   [{detail}]" if detail else ""), flush=True)


class FakeVLC:
    """Records transport commands; ``t`` is what get_time reports."""

    def __init__(self):
        self.commanded = []
        self.t = 0.0
        self.state = "playing"
        self.paused = False

    def play_at(self, url, t, record_path=None, timeshift=False,
                **kw):
        self.commanded.append(("play_at", float(t)))
        self.t = float(t)

    def set_time(self, ms):
        self.commanded.append(("set_time", ms / 1000.0))
        self.t = ms / 1000.0

    def get_time(self):
        return int(self.t * 1000)

    def is_playing(self):
        return not self.paused and self.state == "playing"

    def is_busy(self):
        return False

    def state_name(self):
        return self.state

    def set_rate(self, r):
        pass

    def resume(self):
        self.paused = False

    def set_volume(self, v):
        pass

    def set_mute(self, on):
        pass

    def set_spu(self, tid):
        pass

    def stop_and_release(self):
        pass

    def video_size(self):
        return (1280, 720)

    def spu_tracks(self):
        return []

    def active_spu(self):
        return -1

    def is_mute(self):
        return False

    def get_length(self):
        return 0

    def play(self, url, timeshift=False, http_referer=None, **kw):
        self.commanded.append(("play", url))
        self.state = "playing"

    def last(self, kind=None):
        for cmd, val in reversed(self.commanded):
            if kind is None or cmd == kind:
                return val
        return None


class FakeDVR:
    def __init__(self, path):
        self.file_path = path
        self.running = True

    def buffer_file(self):
        try:
            return self.file_path if os.path.getsize(self.file_path) > 50000 \
                else None
        except OSError:
            return None

    def stop(self, delete=False):
        pass

    def safe_stop(self, delete=True):
        pass


def build_view(bufpath):
    cfg = Config({}, Path(build_view.tmp) / "cfg.json")
    view = PlayerView(cfg)
    view.vlc = FakeVLC()
    view.current = {"kind": "live", "url": "http://x/espn.ts",
                    "title": "US: ESPN HD"}
    view._mode = "chase"
    view.dvr = FakeDVR(bufpath)
    # heavy/live machinery the reopen path touches is not under test here
    view._poke_audio = lambda: None
    view._poke_rate = lambda: None
    view._gop_anchor_vlc = lambda vlc_t, guard_s=None: None
    view._head_ahead_s = lambda current: None
    return view


def main():
    app = (QtWidgets.QApplication.instance()
           or QtWidgets.QApplication(sys.argv))
    _ = app
    tmp = tempfile.mkdtemp(prefix="mtp_liverec_")
    build_view.tmp = tmp
    VT = {"t": 1_000_000.0}
    pv.now_s = lambda: VT["t"]          # the sanctioned test seam

    # ---------------------------------------------------------------- [1]
    print("[1] reopen after a recorder rotation lands in the NEW file's "
          "timeline")
    buf = os.path.join(tmp, "buffer.ts")
    Path(buf).write_bytes(b"\x47" * (188 * 400))
    view = build_view(buf)
    view._dvr_base = 338.3          # what earlier generations wrote
    view._dvr_gen_base = 338.3      # the ACTIVE file starts here
    view._dvr_first_data = VT["t"] - 20.0
    view._dvr_content_s = 23.4
    view._vid_s = 361.7
    view._cap_clock_s = 361.7
    r = view._reopen_display(at=361.7)
    got = view.vlc.last("play_at")
    check("reopen issued at all", r is True, f"r={r}")
    check("play_at is file-local (23.4 s), not the content number (361.7 s)",
          got is not None and abs(got - (361.7 - 338.3)) < 1.5,
          f"play_at={got}")
    check("the old bug (start past the end of the new file) is impossible",
          got is not None and got < 30.0, f"play_at={got}")

    print("\n[1b] a target inside a RETIRED buffer is promoted to the "
          "active file's head")
    view.vlc.commanded.clear()
    r = view._reopen_display(at=300.0)        # content below the base
    got = view.vlc.last("play_at")
    check("retired-buffer target reopens at the new file's head, not "
          "negative", r is True and got is not None and 0.0 <= got < 2.0,
          f"play_at={got}")
    check("the tracked position follows the landing",
          abs(view._vid_s - view._dvr_gen_base) < 1e-6,
          f"vid={view._vid_s:.1f} base={view._dvr_gen_base:.1f}")

    # ---------------------------------------------------------------- [2]
    print("\n[2] first generation: identity mapping, unchanged")
    view2 = build_view(buf)
    view2._dvr_base = 0.0
    view2._dvr_gen_base = 0.0
    view2._dvr_first_data = VT["t"] - 300.0
    view2._dvr_content_s = 300.0
    view2._vid_s = 290.0
    view2._reopen_display(at=290.0)
    got = view2.vlc.last("play_at")
    check("no base -> play_at carries the content number",
          got is not None and abs(got - 290.0) < 1.5, f"play_at={got}")

    # ---------------------------------------------------------------- [3]
    print("\n[3] transport seeds keep the generation offset")
    view._cap_seed_transport(361.7)
    mapped = view._cap_vlc_time_for(371.7)
    back = view._cap_content_for_raw(mapped)
    check("after a seed, content+10 s still maps ~10 s ahead in the file",
          abs(mapped - (371.7 - 338.3)) < 1.5, f"mapped={mapped:.2f}")
    check("the inverse map round-trips",
          abs(back - 371.7) < 1e-6, f"back={back:.2f}")
    view._cap_seed_transport(361.7)   # seed again: must not drift to 0
    check("repeated seeds do not lose the base",
          abs(view._cap_vlc_time_for(361.7) - 23.4) < 1.5,
          f"{view._cap_vlc_time_for(361.7):.2f}")

    # ---------------------------------------------------------------- [4]
    print("\n[4] a buffer the writer truncated is rebased on the spot")
    trunc_dir = tempfile.mkdtemp(prefix="mtp_trunc_", dir=tmp)
    tpath = os.path.join(trunc_dir, "buffer.ts")
    with open(tpath, "wb") as f:
        f.write(b"\x47" * 400000)
    v3 = build_view(tpath)
    v3._dvr_base = 0.0
    v3._dvr_gen_base = 0.0
    v3._note_dvr_data()                       # first sample: 400 kB
    VT["t"] += 60.0
    with open(tpath, "ab") as f:              # ordinary growth credits content
        f.write(b"\x47" * 200000)
    v3._note_dvr_data()
    reached = v3._frontier_s()
    with open(tpath, "wb") as f:               # libVLC re-opened with O_TRUNC
        f.write(b"\x47" * 100000)
    v3._note_dvr_data()
    check("truncation detected and base re-frozen at the reached position",
          reached > 5.0 and abs(v3._dvr_base - (reached - 1.0)) < 1e-6,
          f"reached={reached:.1f} base={v3._dvr_base:.1f}")
    check("the active generation now starts at that base",
          abs(v3._dvr_gen_base - v3._dvr_base) < 1e-6,
          f"gen_base={v3._dvr_gen_base:.1f}")
    check("subsequent reopens are file-local again",
          abs(v3._cap_vlc_time_for(v3._dvr_base + 5.0) - 5.0) < 1e-6,
          f"vlc={v3._cap_vlc_time_for(v3._dvr_base + 5.0):.2f}")
    check("no false truncation on ordinary growth",
          v3._dvr_base == v3._dvr_gen_base, "")

    # ---------------------------------------------------------------- [5]
    print("\n[5] reopens that never play escalate to a full re-dial "
          "(the user's fix)")
    v4 = build_view(buf)
    v4._session = 7
    v4._last_reopen = VT["t"] - 8.0
    v4._chase_started = False                 # the last open never played
    reloads = []
    v4._hard_reload = lambda gen, why: reloads.append((gen, why)) or True
    v4._reopen_display = lambda at=None: True
    for i in range(_REOPEN_DEAD_LIMIT - 1):
        VT["t"] += 8.0
        v4._reopen_chase(7)
        check(f"dead open {i + 1}: still just reopening",
              not reloads and v4._reopen_dead == i + 1,
              f"dead={v4._reopen_dead} reloads={len(reloads)}")
    VT["t"] += 8.0
    v4._reopen_chase(7)
    check("the streak hands over to a full re-dial exactly once",
          len(reloads) == 1 and reloads[0][0] == 7,
          f"reloads={reloads}")
    check("the dead-open counter resets for the next session",
          v4._reopen_dead == 0, str(v4._reopen_dead))

    v4._chase_started = True                  # playback alive again
    VT["t"] += 8.0
    v4._reopen_chase(7)
    check("a live rescue is not a dead open",
          v4._reopen_dead == 0 and len(reloads) == 1,
          f"dead={v4._reopen_dead} reloads={len(reloads)}")

    print("\n[5b] the re-dial itself is rate-limited and degrades safely")
    v5 = build_view(buf)
    v5._session = 3
    called = {"engage": 0, "live": 0}
    v5._engage_chase = lambda: called.__setitem__(
        "engage", called["engage"] + 1)
    v5._ensure_dvr_stopped = lambda: setattr(v5, "dvr", None)
    v5._reopen_display = lambda at=None: called.__setitem__(
        "live", called["live"] + 1) or True
    v5._chase_note = lambda *a, **k: None
    v5._fallback_from_chase = lambda waited: (
        called.__setitem__("live", called["live"] + 1),
        setattr(v5, "_mode", "live"))
    ok1 = v5._hard_reload(3, "first")
    check("a reload re-dials the channel through _engage_chase",
          ok1 and called["engage"] == 1, f"{called}")
    ok2 = v5._hard_reload(3, "too soon")
    check("a second reload inside the cooldown is refused",
          ok2 is False and called["engage"] == 1, f"{called}")
    VT["t"] += _HARD_RELOAD_MIN_GAP_S + 1.0
    v5._mode = "live"          # chase gave up between attempts
    ok3 = v5._hard_reload(3, "second")
    VT["t"] += _HARD_RELOAD_MIN_GAP_S + 1.0
    ok4 = v5._hard_reload(3, "third")
    check("past the cap the picture stays on plain live (never dead)",
          ok3 is True and ok4 is True and called["live"] == 1
          and called["engage"] == 2, f"{called}")

    # ---------------------------------------------------------------- [6]
    print("\n[6] a dead player cannot clear the unseekable-buffer ladder")
    v6 = build_view(buf)
    v6._seek_zero_landings = _SEEK_ZERO_STRIKES - 1
    v6._seek_verify = (890.0, 890.0, VT["t"] - 1.0, VT["t"] - 10.0,
                       -1.0, True, True)
    v6._seek_esc_ok_at = 0.0
    v6._last_reopen = 0.0
    v6._verify_seek(VT["t"], 890.0, alive=False)
    check("dead player parked ON the target keeps the strikes",
          v6._seek_zero_landings == _SEEK_ZERO_STRIKES - 1,
          str(v6._seek_zero_landings))
    v6._seek_verify = (890.0, 890.0, VT["t"] - 1.0, VT["t"] - 10.0,
                       -1.0, True, True)
    v6._verify_seek(VT["t"], 890.0, alive=True)
    check("a landing the viewer could see clears them",
          v6._seek_zero_landings == 0, str(v6._seek_zero_landings))

    # ---------------------------------------------------------------- [7]
    print("\n[7] a LOST buffer restarts the recorder far sooner than a stall")
    check("lost-buffer threshold is well inside the stall patience",
          0.0 < _REC_GONE_RESTART_S < _REC_STALL_RESTART_S / 4.0,
          f"gone={_REC_GONE_RESTART_S}s stall={_REC_STALL_RESTART_S}s")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "src", "ui", "player_view.py"),
               encoding="utf-8").read()
    check("the watchdog uses the lost-buffer branch",
          "buffer file lost" in src and "_REC_GONE_RESTART_S" in src)
    check("the stall watchdog still owns the no-growth case",
          "_REC_STALL_RESTART_S" in src)
    check("an ENDED recorder restarts on one batch of patience, not 65 s",
          _REC_ENDED_RESTART_S < _REC_STALL_RESTART_S
          and "_REC_ENDED_RESTART_S" in src
          and 'rec_dead' in src)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
