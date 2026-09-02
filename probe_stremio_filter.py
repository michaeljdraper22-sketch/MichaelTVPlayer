# -*- coding: utf-8 -*-
"""Offscreen probe: the profanity filter on Stremio handoff streams.

Two gaps closed (both found by inspection of the stremio play path):

  1. _filter_tick's evaluation gate was `chase or _is_vod()` — kind=stremio
     is NEITHER (stremio plays in plain live mode; _is_vod deliberately
     excludes it), so relay cues piled up as windows that were never
     applied. The gate now includes stremio.

  2. Stremio hands the user's subtitles over as an EXTERNAL file
     (--sub-file=...): the VOD relay only peels EMBEDDED tracks, and
     debrid/torrent files usually carry none — no cues at all. The stremio
     branch of _on_media_for_profanity now parses that file directly.

No window, no focus, no audio, no network: FakeVLC records mute calls.
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import DEFAULTS, Config          # noqa: E402
from src import profanity as prof_mod            # noqa: E402
from src.ui.player_view import PlayerView        # noqa: E402

from PyQt5 import QtWidgets                     # noqa: E402

app = QtWidgets.QApplication(sys.argv)
cfg = Config(dict(DEFAULTS), None)
pv = PlayerView(cfg)
pv.resize(1280, 720)
fails = [0]


def check(name, cond, extra=""):
    print(("  ok   " if cond else "FAIL ") + name
          + ("" if cond or not extra else "  [%s]" % extra))
    if not cond:
        fails[0] += 1


print("[1] read_subtitle_text — encodings a handoff can carry")
tmpdir = tempfile.mkdtemp(prefix="mtp_probe_sfilter_")
srt_body = (
    "1\n00:00:10,000 --> 00:00:12,000\nwhat the hell\n\n"
    "2\n00:00:20,000 --> 00:00:22,000\nthis shit is crazy\n\n")
p_utf8 = os.path.join(tmpdir, "a.srt")
p_bom = os.path.join(tmpdir, "b.srt")
p_u16 = os.path.join(tmpdir, "c.srt")
with open(p_utf8, "wb") as f:
    f.write(srt_body.encode("utf-8"))
with open(p_bom, "wb") as f:
    f.write(b"\xef\xbb\xbf" + srt_body.encode("utf-8"))
with open(p_u16, "wb") as f:
    f.write(b"\xff\xfe" + srt_body.encode("utf-16-le"))
for p in (p_utf8, p_bom, p_u16):
    txt = prof_mod.read_subtitle_text(p)
    check("reads %s" % os.path.basename(p),
          bool(txt) and "shit is crazy" in txt)
check("missing file -> None (no raise)",
      prof_mod.read_subtitle_text(os.path.join(tmpdir, "nope.srt")) is None)

print("[2] SrtParser — SRT and VTT-flavored files both yield cues")
parser = prof_mod.SrtParser()
cues = parser.feed(prof_mod.read_subtitle_text(p_utf8)) + parser.flush()
check("srt: two cues parsed",
      len(cues) == 2 and cues[0][0] == 10.0 and cues[1][0] == 20.0)
vtt = ("WEBVTT\n\n"
       "00:00:20.000 --> 00:00:22.000 line:90%\n"
       "this shit is crazy\n\n")
parser2 = prof_mod.SrtParser()
vcues = parser2.feed(vtt) + parser2.flush()
check("vtt: dot timing + cue settings tolerated",
      len(vcues) == 1 and vcues[0][:2] == (20.0, 22.0)
      and "shit" in vcues[0][2])


class FakeVLC:
    def __init__(self):
        self.now_ms = 0
        self.mute_calls = []
        self.plays = []

    def get_time(self):
        return self.now_ms

    def get_length(self):
        return 120000

    def is_playing(self):
        return True

    def set_filter_mute(self, on):
        self.mute_calls.append(bool(on))

    def play(self, url, timeshift=False, start_seconds=0.0,
             start_wait_s=20.0, sub_file=None):
        self.plays.append((url, start_seconds, sub_file))

    def __getattr__(self, name):
        def _noop(*_a, **_k):
            return None
        return _noop


stub = FakeVLC()
real_vlc = pv.vlc
pv.vlc = stub
pv._filter_engine.player = stub

print("[3] the tick gate — stremio windows are now APPLIED (the core bug)")
cfg.data.setdefault("profanity", {})["enabled"] = True
pv.apply_profanity_settings()          # engine config in sync with cfg
pv.current = {"kind": "stremio", "title": "Silo — S01E01",
              "url": "http://x/s", "fav_key": "stremio:t:0"}
pv._mode = "live"                      # stremio never enters chase mode
pv._filter_engine.enabled = True
pv._filter_engine.windows = [(100.0, 101.0)]
pv._filter_engine.muted = False
stub.now_ms = 100500                   # inside the window
pv._filter_tick()
check("mute ON inside a window (stremio, plain live mode)",
      stub.mute_calls[-1:] == [True])
stub.now_ms = 200000                   # far outside
pv._filter_tick()
check("mute OFF outside the window", stub.mute_calls[-1:] == [False])

print("[4] external sub_file -> filter windows (the common handoff case)")
pv._filter_engine.clear()
pv._filter_engine.muted = False
pv.current = {"kind": "stremio", "title": "Silo — S01E01",
              "url": "http://x/s", "fav_key": "stremio:t:0",
              "sub_file": p_utf8}
pv._stop_profanity()                   # play_media's teardown ran before this
pv._on_media_for_profanity("stremio")
wins = pv._filter_engine.windows
check("every filtered word built a window (hell + shit)", len(wins) == 2)
if len(wins) == 2:
    w_hell, w_shit = wins
    check("hell window inside cue 1, shifted by the VOD lead",
          10.0 < w_hell[0] < 11.2 and 10.8 < w_hell[1] <= 12.0,
          repr(w_hell))
    # 'shit' sits mid-cue; word share of (20,22) minus the 0.4 s VOD lead
    check("shit window inside cue 2, shifted by the VOD lead",
          19.0 < w_shit[0] < 20.6 and 20.4 < w_shit[1] <= 22.0,
          repr(w_shit))
check("evaluation loop running", pv._filter_timer.isActive())
stub.mute_calls.clear()
stub.now_ms = int(wins[0][0] * 1000) + 200 if wins else 20500
pv._filter_tick()
check("muted while the bad word plays", stub.mute_calls[-1:] == [True])
stub.now_ms = 15000
pv._filter_tick()
check("unmuted on the clean cue", stub.mute_calls[-1:] == [False])

print("[5] re-engage mid-stream (settings dialog) — no duplicate windows")
n_before = len(pv._filter_engine.windows)
pv._on_media_for_profanity("stremio")
check("windows merged, not duplicated",
      len(pv._filter_engine.windows) == n_before)

print("[6] guards — unreadable file / bare handoff never break playback")
pv._filter_engine.clear()
pv.current = {"kind": "vod", "title": "Movie", "url": "http://x/m.mp4",
              "fav_key": "vod:1", "sub_file": p_utf8}
pv._load_stremio_sub_cues()
check("vod kind: external file honored (fetched-subs generalization)",
      len(pv._filter_engine.windows) == 2)
pv._filter_engine.clear()
pv.current = {"kind": "stremio", "title": "S", "url": "http://x/s",
              "fav_key": "stremio:t:1",
              "sub_file": os.path.join(tmpdir, "gone.srt")}
try:
    pv._load_stremio_sub_cues()
    check("missing sub file: no crash, no windows",
          not pv._filter_engine.windows)
except Exception as exc:
    check("missing sub file: no crash, no windows", False, repr(exc))
pv._filter_engine.clear()
pv.current = {"kind": "stremio", "title": "S", "url": "http://x/s",
              "fav_key": "stremio:t:2"}          # no sub_file at all
pv._load_stremio_sub_cues()
check("handoff without sub file: no-op", not pv._filter_engine.windows)

print("[7] relay-embedded cues (no sub_file) still gate through the tick")
pv._filter_engine.clear()
pv._cap_relay_gen = pv._session        # direct call passes the stale guard
pv._on_vod_cue(30.0, 32.0, "you fuckin kidding me")
check("relay cue built a window", len(pv._filter_engine.windows) == 1)
stub.now_ms = int((pv._filter_engine.windows[0][0]
                   + pv._filter_engine.windows[0][1]) / 2 * 1000)
pv._filter_tick()
check("embedded stremio track mutes too (gate fix)",
      stub.mute_calls[-1:] == [True])

pv.vlc = real_vlc
pv._filter_engine.player = real_vlc
pv.stop()
app.processEvents()
print(f"\n{'ALL PASS' if fails[0] == 0 else str(fails[0]) + ' FAILURES'}")
sys.exit(1 if fails[0] else 0)
