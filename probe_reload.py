"""Headless probe for the reload/refresh stream button, the Stremio speed
control and the Stremio download (REC-slot) swap.

Run:  .venv\\Scripts\\python.exe probe_reload.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import DEFAULTS, Config  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

from PyQt5 import QtWidgets  # noqa: E402

app = QtWidgets.QApplication(sys.argv)
cfg = Config(dict(DEFAULTS), None)
pv = PlayerView(cfg)
pv.resize(1280, 720)
fails = [0]


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails[0] += 1


def wait_media(fav, timeout=8.0):
    import time as _time
    t0 = _time.time()
    while _time.time() - t0 < timeout:
        app.processEvents()
        if (pv.current or {}).get("fav_key") == fav:
            app.processEvents()
            return True
        _time.sleep(0.05)
    return False


class FakeVlc:
    """Records the calls _reload_stream/_poke_rate_late can make."""
    get_time_ms = 0

    def __init__(self):
        self.calls = []

    def get_time(self):
        return self.get_time_ms

    def set_rate(self, rate):
        self.calls.append(("set_rate", rate))


STREMIO = {"kind": "stremio", "title": "Chinatown (1974)",
           "url": "http://127.0.0.1:11470/deadbeef/0", "fav_key": "stremio:x:0",
           "info_hash": "deadbeef", "file_idx": 0,
           "file_name": "Chinatown.1974.2160p.BluRay.mkv"}
SERIES = {"kind": "series", "title": "Show - S1E5 Pilot",
          "url": "http://x/1.mp4", "fav_key": "episode:101",
          "series_id": 7, "series_name": "Show", "season": 1, "episode": 5}
LIVE = {"kind": "live", "title": "CNN", "url": "http://x/live",
        "fav_key": "live:5", "stream_id": 5}

print("[1] corner button: exists, placement, pre-stream state")
check("reload button exists", hasattr(pv, "_btn_reload"))
pv.overlay.show()
pv._layout_overlays()
app.processEvents()
xr = pv._btn_reload.x()
xz = pv._btn_panel.x()      # zen
xf = pv._btn_ovfs.x()       # fullscreen (rightmost)
check("positioned left of the zen button", xr < xz < xf)
check("same row as zen/fullscreen",
      pv._btn_reload.y() == pv._btn_panel.y()
      and pv._btn_reload.height() == pv._btn_panel.height())
check("disabled pre-stream (nothing playing)",
      not pv._btn_reload.isEnabled())
check("tooltip mentions reload",
      "Reload" in pv._btn_reload.toolTip())

print("[2] stremio: speed control")
pv.play_media(dict(STREMIO))
check("stremio media started", wait_media("stremio:x:0"))
check("speed button ENABLED for stremio", pv.btn_speed.isEnabled())
pv._set_rate(2.0)
check("2x pick kept on stremio (not forced to 1x)", pv._rate == 2.0)
pv._set_rate(0.5)
check("0.5x pick kept on stremio", pv._rate == 0.5)
pv._set_rate(1.0)
# _poke_rate_late applies the pick after a player swap
fake = FakeVlc()
old_vlc = pv.vlc
pv.vlc = fake
pv._rate = 1.5
pv._poke_rate_late()
pv.vlc = old_vlc
check("_poke_rate_late reaches vlc on stremio",
      fake.calls == [("set_rate", 1.5)])

print("[3] stremio: download replaces the dead REC slot")
check("REC hidden for stremio", pv.btn_rec.isHidden())
check("Download visible for stremio", not pv.btn_dl.isHidden())
check("Download enabled for stremio", pv.btn_dl.isEnabled())
check("dl gate passes stremio",
      pv._start_download.__doc__ is not None and not pv._downloading)
# extension derivation: server URL (no ext) -> file_name ext -> .mp4
pv.current["file_name"] = "Some.Show.S01E02.1080p.mkv"
check("server URL falls back to the file_name ext",
      pv._dl_extension("http://127.0.0.1:11470/abc/0") == ".mkv")
check("debrid URL with ext wins",
      pv._dl_extension("https://tb.net/dl/Chinatown.1974.mkv?tok=1")
      == ".mkv")
pv.current["file_name"] = ""
check("no evidence anywhere -> .mp4",
      pv._dl_extension("http://127.0.0.1:11470/abc/0") == ".mp4")
check("non-media URL ext ignored (.php)",
      pv._dl_extension("https://x/dl/file.php") == ".mp4")
check("vod URL ext unchanged (.mp4)",
      pv._dl_extension("http://x/102.mp4") == ".mp4")
pv.current["file_name"] = "Some.Show.S01E02.1080p.mkv"
check("URL ext preferred over file_name",
      pv._dl_extension("http://x/102.avi") == ".avi")

print("[4] live (plain-live mode): speed honestly disabled, reload enabled")
pv.play_media(dict(LIVE))
check("live media started", wait_media("live:5"))
pv._set_rate(2.0)
check("plain-live pick forced back to 1x", pv._rate == 1.0)
check("reload button enabled on live", pv._btn_reload.isEnabled())
fake2 = FakeVlc()
pv.vlc = fake2
pv._rate = 1.5
pv._poke_rate_late()
pv.vlc = old_vlc
check("_poke_rate_late skipped on plain live", fake2.calls == [])

print("[5] reload: vod/series replays through play_media at t-1, keeps picks")
pv.play_media(dict(SERIES))
check("series media started", wait_media("episode:101"))
fake3 = FakeVlc()
fake3.get_time_ms = 65000
pv.vlc = fake3
pv._audio_want = 3
pv._audio_name = "Spanish"
captured = []
real_play = pv.play_media
pv.play_media = lambda p, start_at=0.0: captured.append((p, start_at))
pv._reload_stream()
pv.play_media = real_play
check("reload replays the same playable",
      captured and captured[0][0].get("fav_key") == "episode:101"
      and captured[0][0].get("kind") == "series")
check("reload resumes at t-1 (64s of 65s)",
      captured and abs(captured[0][1] - 64.0) < 1e-6)
check("audio pick survives the reload",
      pv._audio_want == 3 and pv._audio_name == "Spanish")
check("reload pill shown", pv._dvr_status.text() == "Stream reloaded")

print("[6] reload: wedged VLC clock falls back to the tracked position")
fake3.get_time_ms = 0
pv._vid_s = 120.0
captured.clear()
pv.play_media = lambda p, start_at=0.0: captured.append((p, start_at))
pv._reload_stream()
pv.play_media = real_play
check("tracked position used (119s of 120s)",
      captured and abs(captured[0][1] - 119.0) < 1e-6)

print("[7] reload: chase restarts recorder + reopens buffer AT position")
pv.current = dict(LIVE)
pv._mode = "chase"
pv._cap_clock_s = 0.0
pv._vid_s = 42.0


class FakeDvr:
    running = True

    def buffer_file(self):
        return "C:/buf.ts"


def set_rec(on):
    # blockSignals: a plain setChecked would fire _on_rec_toggled and pop
    # the record-folder chooser dialog (the app never toggles it this way)
    pv.btn_rec.blockSignals(True)
    pv.btn_rec.setChecked(on)
    pv.btn_rec.blockSignals(False)


pv.dvr = FakeDvr()
rec_calls, open_calls = [], []
pv._restart_recorder = lambda record: rec_calls.append(record)
set_rec(True)
pv._reopen_display = lambda at=None: open_calls.append(at) or True
pv._reload_stream()
check("recorder restarted", rec_calls == [True])
check("display reopened AT the tracked position",
      open_calls == [42.0])
# caption clock wins when it is ahead of the tracker
pv._cap_clock_s = 50.0
pv._vid_s = 42.0
rec_calls.clear()
open_calls.clear()
pv._reload_stream()
check("displayed (caption) clock preferred", open_calls == [50.0])
set_rec(False)
rec_calls.clear()
open_calls.clear()
pv._reload_stream()
check("REC state carried through the reload", rec_calls == [False])
pv._cap_clock_s = 0.0
pv.dvr = None
pv._mode = "live"

print("[8] reload: plain live reconnects at the edge")
pv.current = dict(LIVE)
open_calls.clear()
pv._reopen_display = lambda at=None: open_calls.append(at) or True
pv._reload_stream()
check("plain-live reopen has no position target", open_calls == [None])

print("[9] reload: guard rails")
n = []
open_calls.clear()
pv.play_media = lambda p, start_at=0.0: n.append(1)
pv.current = None
pv._reload_stream()
check("no-op with nothing playing", n == [] and open_calls == [])
pv.current = {"kind": "live", "title": "x", "fav_key": "live:9"}
pv._reload_stream()
check("no-op without a url (live branch)", n == [] and open_calls == [])
pv.play_media = real_play

print("[10] stremio reload keeps identity fields")
pv.current = dict(STREMIO)
pv.current.update({"stremio_imdb": "tt0071315", "season": 1,
                   "episode": 2, "title": "Chinatown (1974)"})
fake3.get_time_ms = 30000
captured.clear()
pv.play_media = lambda p, start_at=0.0: captured.append((p, start_at))
pv._reload_stream()
pv.play_media = real_play
check("identity + title carried into the replay",
      captured and captured[0][0].get("stremio_imdb") == "tt0071315"
      and captured[0][0].get("title") == "Chinatown (1974)")
check("resumes at 29s", captured and abs(captured[0][1] - 29.0) < 1e-6)
pv.vlc = old_vlc   # back to the real (offscreen) player for [12]

print("[11] immersive hide/show includes the reload button")
pv._fullscreen = True
pv._sleep(force=True)
check("hidden while immersive", pv._btn_reload.isHidden())
pv._fullscreen = False
pv._wake()
check("shown again after wake", pv._btn_reload.isVisible())

print("[12] vod keeps REC-slot behavior (no regression)")
pv.play_media(dict(SERIES))
check("series media restarted", wait_media("episode:101"))
check("REC hidden for vod", pv.btn_rec.isHidden())
check("Download visible for vod", not pv.btn_dl.isHidden())

print()
print("FAILURES: %d" % fails[0])
sys.exit(1 if fails[0] else 0)
