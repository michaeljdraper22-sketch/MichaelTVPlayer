# -*- coding: utf-8 -*-
"""Recorder restarts must never destroy the chase buffer (2026-11-02 ESPN
incident): libVLC 3.0.20+ opens a reused sout destination truncate-mode no
matter how append is requested, so VlcRecorder now rotates to a fresh
sibling file, and a chase-mode stream reload re-enters chase near the NEW
write head instead of reopening at a position only the retired file held
(this wedged the seek verifier in a 23 s rescue loop of black screen).

Part [1] drives the REAL VlcRecorder against stub libvlc bindings;
part [2] drives the REAL PlayerView.reload path with the repo's standard
FakeVLC / stub-recorder harness (test_always_chase pattern).

Run:  .venv\\Scripts\\python.exe test_reload_sibling.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402
import src.dvr as dvr_mod  # noqa: E402

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

print("[1] VlcRecorder: a reused buffer rotates to a fresh sibling "
      "(VLC cannot append)")
MEDIA_OPTIONS = []


class StubMedia:
    def __init__(self, url):
        self.url = url

    def add_option(self, opt):
        MEDIA_OPTIONS.append((self.url, opt))


class StubPlayer:
    def set_media(self, m):
        self.m = m

    def play(self):
        pass

    def stop(self):
        pass


class StubInstance:
    def media_player_new(self):
        return StubPlayer()

    def media_new(self, url):
        return StubMedia(url)


dvr_mod.vlc = SimpleNamespace(Instance=lambda *a, **k: StubInstance())

td = tempfile.mkdtemp(prefix="mtp_sibling_t_")
buf1 = os.path.join(td, "buffer.ts")
MARKER = b"\x47" * 120000                       # a real sized chase chunk
with open(buf1, "wb") as f:
    f.write(MARKER)

rec = dvr_mod.VlcRecorder()
rec.start("http://x/live.ts", buffer_path=buf1)
sibling = os.path.join(td, "buffer_2.ts")
check("restart on an existing buffer writes a fresh sibling",
      rec.file_path == sibling)
with open(buf1, "rb") as f:
    check("the retired buffer is NOT truncated (bytes intact)",
          f.read() == MARKER)
check("no sout-file-append option is issued (it never worked on 3.0.24)",
      not any("sout-file-append" in opt for _, opt in MEDIA_OPTIONS))
check("the sout chain targets the sibling",
      any(opt.startswith(":sout=") and "buffer_2.ts" in opt
          for _, opt in MEDIA_OPTIONS))
check("the recorder heals a cut HTTP stream itself (:http-continuous)",
      any(opt.strip(":") == "http-continuous"
          for _, opt in MEDIA_OPTIONS))
check("the temp dir is preserved for the rotation (delete=False path)",
      rec._dir == td)

with open(sibling, "wb") as f:
    f.write(MARKER)
MEDIA_OPTIONS.clear()
rec2 = dvr_mod.VlcRecorder()
rec2.start("http://x/live.ts", buffer_path=sibling)
check("a second restart rolls to buffer_3.ts",
      rec2.file_path == os.path.join(td, "buffer_3.ts"))

fresh_dir = tempfile.mkdtemp(prefix="mtp_sibling_f_")
fresh = os.path.join(fresh_dir, "buffer.ts")
rec3 = dvr_mod.VlcRecorder()
rec3.start("http://x/live.ts", buffer_path=fresh)
check("a brand-new buffer path is used as-is (no rotation)",
      rec3.file_path == fresh)

rec2.stop(delete=True)
check("final stop deletes the whole temp dir (all siblings)",
      not os.path.exists(td))

print("[2] reload in chase mode: honest re-entry, no stale-position "
      "reopen, CC source re-pointed")

STARTS = []


class RotatingStubRecorder:
    """Mirrors the real VlcRecorder contract: an existing buffer_path
    rotates to a fresh sibling (the file is created on disk so the view's
    os.path checks see reality)."""

    def __init__(self, max_minutes=30, network_caching=1500, instance=None):
        self.running = False
        self.file_path = None
        self.rec_path = None
        self._dir = None

    def start(self, url, output_path=None, buffer_path=None):
        STARTS.append((url, output_path, buffer_path))
        self.running = True
        if buffer_path and os.path.exists(buffer_path):
            d = os.path.dirname(buffer_path)
            n = 2
            sib = os.path.join(d, "buffer_%d.ts" % n)
            while os.path.exists(sib):
                n += 1
                sib = os.path.join(d, "buffer_%d.ts" % n)
            with open(sib, "wb") as f:
                f.write(b"\x47" * 60000)
            self.file_path = sib
            self._dir = d
        elif buffer_path:
            self.file_path = buffer_path
            self._dir = os.path.dirname(buffer_path) or None
        else:
            self._dir = tempfile.mkdtemp(prefix="mtp_dvr_")
            self.file_path = os.path.join(self._dir, "buffer.ts")

    def buffer_file(self):
        return self.file_path if self.running else None

    def stop(self, delete=True):
        self.running = False

    def safe_stop(self, delete=True):
        self.running = False


pv_mod.VlcRecorder = RotatingStubRecorder


class FakeVLC:
    def __init__(self):
        self.calls = []
        self.spu = -1
        self.tracks = []
        self.instance = object()
        self.t = -1
        self.state = "playing"

    def play(self, url, timeshift=None, start_seconds=0.0,
             start_wait_s=20.0, sub_file=None, network_caching_ms=0):
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

    def set_spu(self, tid):
        self.spu = tid

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

    def set_scale_mode(self, m):
        pass

    def apply_scale(self, w, h):
        pass


cfg = temp_config()
view = PlayerView(cfg)
view._attach_done = True
view._attached = True
fake = FakeVLC()
view.vlc = fake
view._filter_engine.player = fake

# CC stub BEFORE any reload: the re-point path must restart the reader on
# the sibling without ever spawning a real CCExtractor
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

# A mid-watch chase state around the 2026-11-02 numbers: content clock
# frozen near 1207 s, viewer near 1226 s on a buffer that holds ~20 min.
td2 = tempfile.mkdtemp(prefix="mtp_sibling_v_")
live_buf = os.path.join(td2, "buffer.ts")
with open(live_buf, "wb") as f:
    f.write(b"\x47" * 60000)
view.play_media({"kind": "live", "url": "http://x/live.ts", "title": "L"})
view._mode = "chase"
view.dvr = RotatingStubRecorder()
view.dvr.running = True
view.dvr.file_path = live_buf
view.dvr._dir = td2
view._dvr_t0 = time.time() - 60.0
view._dvr_first_data = time.time() - 60.0
view._dvr_base = 1207.3
view._dvr_content_s = 15.0
view._dvr_size = 60000
view._vid_s = 1225.8
view._cap_clock_s = 1225.8
view._chase_started = True
view._cap_want = True
view._cc_source = object()          # sentinel: reader "tails" the old file

fake.calls.clear()
STARTS.clear()
CCStarts.clear()
session_before = view._session
view._reload_stream()

sibling2 = os.path.join(td2, "buffer_2.ts")
with open(live_buf, "rb") as f:
    check("reload leaves the retired buffer's bytes intact",
          len(f.read()) == 60000)
check("recorder restarted onto the existing buffer path",
      STARTS and STARTS[-1][2] == live_buf)
check("reload bumped the session (stale deferred callbacks abort)",
      view._session == session_before + 1)
check("the stale-position reopen is gone (no play_at at/beyond the "
      "retired ~1225.8 s spot)",
      not any(c[0] in ("play_at", "play") and len(c) > 2
              and isinstance(c[2], (int, float)) and c[2] > 1222.5
              for c in fake.calls))
check("CC reader re-pointed onto the sibling (not the retired file)",
      bool(CCStarts) and CCStarts[0][0] == sibling2
      and isinstance(view._cc_source, StubCC))

# the chase re-entry waits for the young sibling, then joins behind the
# NEW write head — pump past the 2.5 s gate. The freeze snapshot at reload
# time is frontier-1 = 1221.3 s and the young sibling holds only that
# generation (~3 s), so "behind the new head" is a FILE-LOCAL position
# near 0 s. It used to be handed the content number (~1216 s) instead:
# inside a 3 s file that is past EOF, which is the very wedge the reload
# path exists to avoid (2026-11-02 ESPN, ~23 s of black). The axis
# mapping now converts it (see _axis_div / _dvr_gen_base).
t0 = time.time()
while time.time() - t0 < 3.4:
    app.processEvents()
    time.sleep(0.02)
check("chase re-entered on the sibling buffer",
      any(c[0] == "play_at" and c[1] != live_buf for c in fake.calls))
join = [c for c in fake.calls if c[0] == "play_at" and c[1] != live_buf]
print("   [join play_at = %s]" % (join[-1][2] if join else None))
check("the fresh join is FILE-LOCAL near the new head (not the retired "
      "position, not a past-EOF content number)",
      bool(join) and 0.0 <= join[-1][2] <= 6.0)
check("...and the content-axis number is never passed to VLC",
      bool(join) and join[-1][2] < 1213.0)

print()
print("reload-sibling: %d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
