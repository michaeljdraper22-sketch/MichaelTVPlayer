# -*- coding: utf-8 -*-
"""Offscreen probe: Stremio handoff subtitles follow the APP's styling.

User report: "subtitles from stremio streams do not want to follow the
stylings of my app". Root cause (verified against the bundled VLC with
probe_subname.py): the handoff's sub FILE is attached as sub-file, VLC
names the slave generically ("Track 1", never the file name) and
AUTO-SHOWS it in its OWN renderer — freetype/libass styling that ignores
the app's overlay. The app overlay path never engaged for stremio:
_cap_eligible was VOD-only for plain names, _engage_caption_overlay had
no stremio branch, and _caption_tick early-returned for non-chase
non-VOD media.

This probe pins the new path: the file's cues render through the
CaptionOverlay (ONE style), live from subtitle_appearance, with VLC's
own spu forced off and every fallback intact.

No window, no focus, no audio, no network: FakeVLC records calls.
"""
import os
import sys
import tempfile
import time

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


tmpdir = tempfile.mkdtemp(prefix="mtp_probe_ssubs_")


def write(name: str, body: str, enc="utf-8"):
    p = os.path.join(tmpdir, name)
    with open(p, "wb" if enc == "utf-16" else "w",
              **({} if enc == "utf-16" else
                 {"encoding": enc})) as f:
        f.write(body if enc != "utf-16"
                else body.encode("utf-16-le"))
    return p


print("[1] parse_subtitle_cues — every format a handoff carries")
srt_body = ("1\n00:00:10,000 --> 00:00:12,000\nline one\nline two\n\n"
            "2\n00:00:20,000 --> 00:00:22,000\nsecond cue\n\n")
p_srt = write("movie.en.srt", srt_body)
cues = prof_mod.parse_subtitle_cues(prof_mod.read_subtitle_text(p_srt))
check("srt: 2 cues, multi-line preserved",
      len(cues) == 2 and cues[0][:2] == (10.0, 12.0)
      and cues[0][2] == "line one\nline two")
p_vtt = write("movie.vtt",
              "WEBVTT\n\n00:00:20.000 --> 00:00:22.000 position:50%\n"
              "a vtt cue\n\n")
vcues = prof_mod.parse_subtitle_cues(prof_mod.read_subtitle_text(p_vtt))
check("vtt: dot timing + settings tolerated",
      len(vcues) == 1 and vcues[0][:2] == (20.0, 22.0))
ass_body = (
    "[Script Info]\nTitle: x\n\n[V4+ Styles]\n"
    "Format: Name, Fontname\n\n[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
    "Effect, Text\n"
    "Dialogue: 0,0:00:05.00,0:00:08.00,Default,,0,0,0,,"
    "{\\an8}hello {\\i1}world{\\i0}\\Nsecond ass line\n"
    "Comment: 0,0:00:09.00,0:00:10.00,Default,,0,0,0,,comment skipped\n"
    "Dialogue: 0,0:01:02.50,0:01:04.75,Default,,0,0,0,,plain text\n")
p_ass = write("movie.ass", ass_body)
acues = prof_mod.parse_subtitle_cues(prof_mod.read_subtitle_text(p_ass))
check("ass: Dialogue parsed, overrides dropped, \\\\N kept",
      len(acues) == 2 and acues[0][:2] == (5.0, 8.0)
      and acues[0][2] == "hello world\nsecond ass line"
      and "comment" not in acues[1][2].lower(),
      repr(acues))
check("ass: minute-based clock parses (62.5, 64.75)",
      acues[1][:2] == (62.5, 64.75))
check("garbage/empty -> [] (caller falls back to VLC)",
      prof_mod.parse_subtitle_cues("") == []
      and prof_mod.parse_subtitle_cues("not a subtitle file\nat all\n")
      == [])


class FakeVLC:
    def __init__(self):
        self.now_ms = 0
        self.spu = -1
        self.set_spu_calls = []
        self.tracks = []

    def get_time(self):
        return self.now_ms

    def is_playing(self):
        return True

    def spu_tracks(self):
        return list(self.tracks)

    def active_spu(self):
        return self.spu

    def set_spu(self, tid):
        self.spu = int(tid)
        self.set_spu_calls.append(int(tid))

    def __getattr__(self, name):
        def _noop(*_a, **_k):
            return None
        return _noop


stub = FakeVLC()
pv.vlc = stub

print("[2] eligibility — the handoff slave is overlay fodder on stremio")
pv.current = {"kind": "stremio", "title": "Silo — S01E01",
              "url": "http://x/s", "fav_key": "stremio:t:0",
              "sub_file": p_srt}
check("bare 'Track 1' (the slave's real VLC name) eligible",
      pv._cap_eligible("Track 1"))
check("embedded language name eligible",
      pv._cap_eligible("English (United States) - [English]"))
check("bitmap stays VLC's", not pv._cap_eligible("DVB Subtitle [eng]"))
check("slave-name detector: bare Track N only",
      pv._stremio_ext_slave("Track 1")
      and not pv._stremio_ext_slave("Track 2 - [English]")
      and not pv._stremio_ext_slave("English (United States)"))

print("[3] auto-engage at open — overlay owns the file, VLC's spu OFF")
pv._auto_stremio_subs()
check("overlay engaged", pv._cap_on and pv._cap_timer.isActive())
check("VLC's own rendering forced off (set_spu(-1) recorded)",
      -1 in stub.set_spu_calls)
check("cues in the store", len(pv._cap_cues.cues) == 2)
check("store owned by the external file", pv._cap_store_ext)
check("no cap_fail latch", not pv._cap_fail)
check("sticky pick points at the file (name from the handoff)",
      pv._spu_want == -1 and pv._spu_name == "movie.en")

print("[4] the tick PAINTS (gate fix) on the app clock, delay honored")
stub.now_ms = 11000                     # inside cue 1 (file timeline)
pv._caption_tick()
check("cue 1 lines on the overlay widget",
      pv._cap_wid._lines == ["line one", "line two"],
      repr(pv._cap_wid._lines))
cfg.data["subtitle_appearance"]["delay_ms"] = 10000   # show 10 s later
pv._caption_tick()
check("positive delay holds the cue back (nothing at t=11)",
      pv._cap_wid._lines == [])
stub.now_ms = 21500                     # 21.5 - 10 = 11.5: inside cue 1
pv._caption_tick()
check("delayed cue paints at t-delay",
      pv._cap_wid._lines == ["line one", "line two"])
cfg.data["subtitle_appearance"]["delay_ms"] = 0
stub.now_ms = 25000
pv._caption_tick()
check("past both cues: overlay empty", pv._cap_wid._lines == [])

print("[5] relay cues reach the FILTER but never double-paint")
pv._filter_engine.clear()
pv._cap_relay_gen = pv._session         # direct call passes the stale guard
pv._on_vod_cue(10.5, 11.5, "relay line")
check("relay cue stored NOWHERE while the file owns the store",
      len(pv._cap_cues.cues) == 2)

print("[6] slave id adoption — menu checkmark follows VLC's list")
stub.tracks = [(1, "Track 1"), (2, "English - [English]")]
pv._enforce_spu()
check("adopted the bare-Track-N id+name",
      (pv._spu_want, pv._spu_name) == (1, "Track 1"))

print("[7] re-engage no-dup; disengage -> VLC off, re-pick restores")
n = len(pv._cap_cues.cues)
pv._select_spu(1, "Track 1")
check("explicit re-pick: store stable (dedupe)",
      len(pv._cap_cues.cues) == n and pv._cap_on)
pv._select_spu(-1, "")
check("Off: overlay down, VLC spu forced off",
      not pv._cap_on and not pv._cap_wid._lines and stub.spu == -1)
pv._select_spu(1, "Track 1")
check("re-pick Track 1: overlay back", pv._cap_on
      and len(pv._cap_cues.cues) == n)

print("[8] embedded pick with a live relay — ownership flips cleanly")
class FakeRelay:
    parser_tracks = {1: "S_TEXT/UTF8"}
    parser_selected = 1
    parser_tracks_meta = {1: {"lang": "eng", "name": "English",
                              "codec": "S_TEXT/UTF8"}}
    def set_prefer_language(self, _hint):
        return False
pv._vod_relay = FakeRelay()
pv._select_spu(2, "English (United States) - [English]")
check("relay owns the store now (external cues dropped)",
      not pv._cap_store_ext and len(pv._cap_cues.cues) == 0)
check("overlay engaged for the embedded pick", pv._cap_on)
pv._on_vod_cue(30.0, 32.0, "embedded line")
check("relay cue paints again after the flip",
      len(pv._cap_cues.cues) == 1)
pv._vod_relay = None

print("[9] relay dead-ends — the file takes over instead of going dark")
pv._cap_store_ext = False
pv._cap_cues.clear()
pv._select_spu(2, "English (United States) - [English]")   # re-own file
check("setup: file owns again", pv._cap_store_ext
      and len(pv._cap_cues.cues) == 2)
check("takeover fires on a dead embedded pick",
      pv._stremio_external_takeover() and pv._cap_store_ext
      and pv._cap_on and len(pv._cap_cues.cues) == 2)

print("[10] unparseable file — VLC keeps rendering (freetype fallback)")
pv._cap_store_ext = False
pv._cap_on = False
pv._cap_cues.clear()
pv.current = dict(pv.current, sub_file=write("broken.srt", "\x00\x01 junk"))
pv._auto_stremio_subs()
check("no engage, no crash", not pv._cap_on and not pv._cap_fail)
pv._select_spu(1, "Track 1")
pv._enforce_spu()          # the tick hands the pick back to VLC
check("explicit pick on an unparseable file: cap_fail, VLC renders",
      pv._cap_fail and not pv._cap_on and stub.spu == 1,
      "spu=%r cap_on=%r" % (stub.spu, pv._cap_on))

print("[11] guards — wrong kind / no file / teardown resets")
pv._cap_fail = False
pv.current = {"kind": "vod", "title": "Movie", "url": "http://x/m",
              "fav_key": "vod:1", "sub_file": p_srt}
pv._auto_stremio_subs()
check("non-stremio media: auto no-op", not pv._cap_on)
check("non-stremio media: plain name NOT eligible (VOD relay decides)",
      not pv._cap_eligible("Track 1") or pv._is_vod())
pv.current = {"kind": "stremio", "title": "S", "url": "http://x/s",
              "fav_key": "stremio:t:9"}       # no sub_file at all
pv._auto_stremio_subs()
check("handoff without a file: no-op", not pv._cap_on)
pv.current = {"kind": "stremio", "title": "S2", "url": "http://x/s2",
              "fav_key": "stremio:t:8", "sub_file": p_srt}
pv._auto_stremio_subs()
pv._stremio_sub_path = ""            # simulate a new media's teardown
pv._stremio_sub_cues = []
pv._cap_store_ext = False
cues2 = pv._stremio_handoff_cues()
check("cache re-reads for a fresh media (path reset)",
      len(cues2) == 2)

print("[12] filter still fed from the SAME parse (shared cache)")
pv._filter_engine.clear()
pv.current = {"kind": "stremio", "title": "S3", "url": "http://x/s3",
              "fav_key": "stremio:t:7",
              "sub_file": write("dirty.srt",
                                "1\n00:00:10,000 --> 00:00:12,000\n"
                                "what the fuck\n\n")}
pv._load_stremio_sub_cues()
check("bad word produced a mute window",
      len(pv._filter_engine.windows) == 1)
check("overlay cues parsed too (one read, both consumers)",
      len(pv._stremio_sub_cues) == 1)

print("[13] embedded pick without a relay reopens through it (stremio ok)")
calls = []
orig_play = pv.play_media
pv.play_media = lambda cur, start_at=0.0: calls.append((cur, start_at))
pv._spu_want = 3
pv._spu_name = "English - [English]"
pv._cap_fail = False
pv._restart_through_relay()
for _ in range(10):                    # the singleShot fires after 60 ms —
    app.processEvents()                # pump AND let wall time pass
    time.sleep(0.02)
check("restart scheduled for the embedded pick",
      len(calls) == 1 and calls[0][0].get("kind") == "stremio",
      repr(calls))
pv.play_media = orig_play

print("[14] utf-16 handoff file parses (BOM read path)")
pv._stremio_sub_path = ""
pv.current = {"kind": "stremio", "title": "S4", "url": "http://x/s4",
              "fav_key": "stremio:t:6",
              "sub_file": write("u16.srt", srt_body, enc="utf-16")}
p16 = os.path.join(tmpdir, "u16.srt")
with open(p16, "wb") as f:
    f.write(b"\xff\xfe" + srt_body.encode("utf-16-le"))
pv.current = dict(pv.current, sub_file=p16)
check("utf-16 cues parsed via the cache",
      len(pv._stremio_handoff_cues()) == 2)

print()
if fails[0]:
    print("FAILURES: %d" % fails[0])
    sys.exit(1)
print("ALL PASS")
