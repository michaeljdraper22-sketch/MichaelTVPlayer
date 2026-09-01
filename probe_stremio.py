# -*- coding: utf-8 -*-
"""Offscreen/live probe for the Stremio handoff feature.

[A] pure parsing (offline): m3u / handoff args / server URLs / S-E
    markers / name cleanup / bencode / stream ranking / vlc-style
    launch args (--start-time / --sub-file)
[B] live catalog + addon chain (Cinemeta, Torrentio): episode lists,
    search, next-episode, stream ranking
[C] live streaming server (127.0.0.1:11470): health, create, identity
    resolution of a real torrent play URL, full next_playable chain
[D] single-instance socket relay (cross-process, like the real exe)
[E] offscreen GUI handoff in an isolated subprocess: vlc-style launch
    args -> MainWindow.handle_handoff -> PlayerView plays (VLC stubbed
    to record, no real playback) -> identity resolution
[F] fileassoc register/unregister round-trip (does NOT touch UserChoice)
[G] streampatch round-trip on a temp copy (the live server.js is never
    touched by the probe) — path redirect + "Play in MichaelTV" title
    relabel + v1->v2 migration + restore
[H] watchfolder: the Downloads auto-play — name gate, baseline (old
    files never replay), new-download pickup, .mtpdone consume, name
    reuse, url-less files ignored

Run:  .venv\\Scripts\\python.exe probe_stremio.py
"""

import os
import re
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# isolated config dir for the GUI leg — set before src.config import
_APPDATA = tempfile.mkdtemp(prefix="mtp_probe_appdata_")
os.environ["APPDATA"] = _APPDATA
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import stremio  # noqa: E402
from src.config import DEFAULTS, Config  # noqa: E402

FAILS = []
REAL_URL = [None]      # leg C stashes a live server play URL for leg E


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" — " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(name)


# ---------------------------------------------------------------- [A] parse
def leg_a():
    print("\n[A] parsing")
    m3u = stremio.parse_m3u(
        "#EXTM3U\n#EXTINF:0\nhttp://127.0.0.1:11470/"
        "8ac2f2df3db05b8f9e7a4b11c8dbf8a1c3d5e7f9/2\n")
    check("parse_m3u url", m3u.endswith("/8ac2f2df3db05b8f9e7a4b11c8dbf8a1"
                                       "c3d5e7f9/2"), m3u)

    path = os.path.join(tempfile.gettempdir(), "mtp_probe_handoff.m3u")
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n#EXTINF:0\nhttps://example.org/stream.mkv\n")
    check("handoff arg (file)", stremio.parse_handoff_arg(path)
          == "https://example.org/stream.mkv")
    check("handoff arg (url)", stremio.parse_handoff_arg(
        "http://x/y") == "http://x/y")
    check("handoff arg (junk)", stremio.parse_handoff_arg("playlist") == "")

    u = stremio.parse_server_url(
        "http://127.0.0.1:11470/8AC2F2DF3DB05B8F9E7A4B11C8DBF8A1C3D5E7F9/2"
        "?external=1&download=1")
    check("server url parse", u == ("8ac2f2df3db05b8f9e7a4b11c8dbf8a1"
                                    "c3d5e7f9", 2), u)
    check("server url no idx",
          stremio.parse_server_url("http://127.0.0.1:11470/"
                                   "8ac2f2df3db05b8f9e7a4b11c8dbf8a1c3d5e7f9")
          == ("8ac2f2df3db05b8f9e7a4b11c8dbf8a1c3d5e7f9", 0))
    check("server url reject plain",
          stremio.parse_server_url("https://cdn.example/abc.mkv") is None)

    # debrid resolve/play links carry the torrent identity (fallback
    # fuel when the debrid link itself stalls)
    torbox = ("https://torrentio.strem.fun/resolve/torbox/"
              "1f7be332-5fea-4aa8-8814-46eabea63736/"
              "30df1b0b3bb8dbfb3bf2b25862b35d7828619721/"
              "Adventure.Time.S02E25.Mortal.Recoil.1080p.AMZN.WEB-DL.mkv"
              "/24/Adventure.Time.S02E25.Mortal.Recoil.1080p.AMZN.WEB-DL"
              ".mkv")
    check("resolve url (torrentio, idx)",
          stremio.parse_resolve_url(torbox)
          == ("30df1b0b3bb8dbfb3bf2b25862b35d7828619721", 24))
    debridio = ("https://addon.debridio.com/play/series/premiumize/"
                "0889be7cbed8417f47e58540afea55c0/tdgs5gcukfk2fcc3/"
                "bf2671f9d1521fa85249b7b5920e7d3294defa80/"
                "Adventure.Time.S02E25.Mortal.Recoil.BluRay.1080p.DD.2.0"
                ".VC-1.REMUX-FraMeSToR.mkv")
    check("resolve url (debridio, no idx)",
          stremio.parse_resolve_url(debridio)
          == ("bf2671f9d1521fa85249b7b5920e7d3294defa80", 0))
    check("resolve url reject plain",
          stremio.parse_resolve_url(
              "https://cdn.example/abc.mkv") is None)
    p = stremio.playable_from_url(torbox)
    check("playable from resolve url",
          p.get("kind") == "stremio"
          and p.get("info_hash")
          == "30df1b0b3bb8dbfb3bf2b25862b35d7828619721"
          and p.get("file_idx") == 24, (p.get("info_hash"),
                                        p.get("file_idx")))

    check("SE marker SxxExx", stremio.parse_se(
        "Game.of.Thrones.S02E05.720p.x264") == (2, 5))
    check("SE marker 2x05", stremio.parse_se("Show 2x05 REPACK") == (2, 5))
    check("SE marker none", stremio.parse_se("A Movie 2023 1080p") is None)

    # find_series single-word floor: a one-word show name ("Silo") used
    # to produce ZERO queries (range(min(4, len-1)) == range(0)) — the
    # live identity dead-end behind the missing episode buttons
    saved_search = stremio.search_series
    calls = []
    stremio.search_series = lambda q: (calls.append(q) or [
        {"id": "ttS", "name": "Silo", "type": "series"}])
    check("find_series single-word name queries + matches",
          stremio.find_series("Silo") == ("ttS", "Silo")
          and calls == ["Silo"], str(calls))
    calls.clear()
    stremio.search_series = lambda q: (calls.append(q) or [])
    stremio.find_series("Bad Parse")
    check("find_series two words: one combined query (unchanged)",
          calls == ["Bad Parse"], str(calls))
    stremio.search_series = saved_search

    cleaned = stremio.clean_show_name(
        "Game.of.Thrones.S02E05.1080p.WEB-DL.x265-QTZ")
    check("clean show name", cleaned == "Game of Thrones", repr(cleaned))

    t = stremio._bdecode(
        b"d4:infod4:name6:Sintel6:lengthi10eee")
    check("bdecode", t == {b"info": {b"name": b"Sintel", b"length": 10}})

    la = stremio.parse_launch_args(
        ["--start-time=30", "--no-video-title-show",
         "http://127.0.0.1:11470/" + "a" * 40 + "/2"])
    check("launch args (vlc-style)", la and la["url"].endswith("/2")
          and la["start_at"] == 30.0 and la["sub_file"] == "", la)
    la = stremio.parse_launch_args(
        ["--sub-file=C:\\x y\\s.srt --no-video-title-show",
         "https://e/z.mkv"])
    check("launch args (sub-file)", la and la["url"] == "https://e/z.mkv"
          and la["sub_file"] == "C:\\x y\\s.srt", la)
    check("launch args (junk -> None)",
          stremio.parse_launch_args(["--nothing", "notafile"]) is None)

    cfg = Config(dict(DEFAULTS), None)
    fake = [{"name": "A", "title": "S01E01 720p \U0001F464 30 \U0001F4BE 2.0 GB",
             "infoHash": "a" * 40, "fileIdx": 1},
            {"name": "B", "title": "S01E01 1080p \U0001F464 5 \U0001F4BE 8.0 GB",
             "infoHash": "b" * 40, "fileIdx": 0},
            {"name": "C", "title": "no hash at all"}]
    best = stremio.best_stream(cfg, fake)
    check("best stream prefers resolution + seeds",
          best and best["infoHash"] == "b" * 40,
          best and best["title"][:20])

    # end-of-media detection: state "ended" OR the tracked-position
    # fallback (VLC resets raw to 0 / stalls the state when a
    # network-relayed VOD finishes — the black-screen-at-credits bug)
    from src.ui.player_view import PlayerView as _PV
    fin = _PV._media_finished
    L = 11 * 60 * 1000
    check("eof: state ended", fin(True, L, 0, 0.0, "ended") is True)
    check("eof: raw at end",
          fin(False, L, L - 900, 600.0, "stopped") is True)
    check("eof: clock reset, tracked pos at end",
          fin(False, L, 0, (L - 1500) / 1000.0, "stopped") is True)
    check("eof: clock reset, stalled state playing",
          fin(False, L, 0, (L - 100) / 1000.0, "playing") is True)
    check("eof: mid-episode not finished",
          fin(False, L, 0, 300.0, "stopped") is False)
    check("eof: playing near end not finished",
          fin(True, L, L - 3000, (L - 3000) / 1000.0, "playing") is False)
    check("eof: nothing loaded",
          fin(False, 0, 0, 0.0, "idle") is False)
    check("eof: seeked back, paused mid",
          fin(False, L, 240000, 240.0, "paused") is False)

    # prev-episode maths (the ⏮ button's backend): synthetic meta
    smeta = {"videos": [
        {"season": 0, "episode": 1},
        {"season": 1, "episode": 1, "name": "Winter Is Coming"},
        {"season": 1, "episode": 2, "name": "The Kingsroad"},
        {"season": 1, "episode": 3, "name": "Lord Snow"},
        {"season": 2, "episode": 1, "name": "The North Remembers"}]}
    check("prev_episode 1x2 -> 1x1",
          stremio.prev_episode(smeta, 1, 2) == (1, 1))
    check("prev_episode season premiere -> finale",
          stremio.prev_episode(smeta, 2, 1) == (1, 3))
    check("prev_episode 1x1 -> None",
          stremio.prev_episode(smeta, 1, 1) is None)
    check("prev_episode skips specials (from s1)",
          stremio.prev_episode(smeta, 1, 1) is None)
    check("next_episode intact through shared core",
          stremio.next_episode(smeta, 1, 2) == (1, 3))

    # prev_playable unit chain: monkeypatched catalog/addon/server so the
    # full next/prev shared core runs without touching the network
    saved = (stremio.series_meta, stremio.addon_streams,
             stremio.StreamingServer)
    full_meta = {"name": "Game of Thrones", "poster": "http://p/poster.jpg",
                 "videos": smeta["videos"]}

    class _FakeServer:
        def __init__(self, base=""):
            self.created = []

        def create(self, info_hash, trackers=()):
            self.created.append(info_hash)
            return True

        @staticmethod
        def play_url(info_hash, file_idx):
            return "http://127.0.0.1:11470/%s/%d" % (info_hash, file_idx)

    fake_server = _FakeServer()
    stremio.series_meta = lambda imdb: dict(full_meta)
    stremio.addon_streams = lambda cfg, imdb, s, e: [
        {"name": "A", "title": "S01E01 1080p \U0001F464 9 \U0001F4BE 2.2 GB",
         "infoHash": "c" * 40, "fileIdx": 3}]
    stremio.StreamingServer = lambda base="": fake_server
    prv = stremio.prev_playable(cfg, {
        "kind": "stremio", "url": "http://x/s", "fav_key": "stremio:zz:0",
        "stremio_imdb": "tt9", "season": 1, "episode": 2,
        "series_name": "Game of Thrones"})
    check("prev_playable -> S01E01 via local server",
          prv and prv.get("season") == 1 and prv.get("episode") == 1
          and prv.get("info_hash") == "c" * 40
          and prv.get("url") == "http://127.0.0.1:11470/%s/3" % ("c" * 40),
          prv and prv.get("title"))
    check("prev_playable title carries episode name",
          prv and "Winter Is Coming" in prv.get("title", ""),
          prv and prv.get("title", ""))
    check("prev_playable carries series identity",
          prv and prv.get("stremio_imdb") == "tt9"
          and prv.get("series_name") == "Game of Thrones")
    check("prev_playable created the torrent on the server",
          fake_server.created == ["c" * 40])
    check("prev_playable E1 -> None",
          stremio.prev_playable(cfg, {
              "kind": "stremio", "url": "http://x/s",
              "stremio_imdb": "tt9", "season": 1, "episode": 1}) is None)
    nxt = stremio.next_playable(cfg, {
        "kind": "stremio", "url": "http://x/s", "stremio_imdb": "tt9",
        "season": 1, "episode": 2, "series_name": "Game of Thrones"})
    check("next_playable intact through the shared core",
          nxt and nxt.get("episode") == 3
          and "Lord Snow" in nxt.get("title", ""),
          nxt and nxt.get("title", ""))
    stremio.series_meta, stremio.addon_streams, stremio.StreamingServer \
        = saved


# ------------------------------------------------------------- [B] catalog
def leg_b():
    print("\n[B] catalog + addon (live network)")
    meta = stremio.series_meta("tt0944947")
    check("cinemeta meta", bool(meta.get("videos")), meta.get("name"))
    nxt = stremio.next_episode(meta, 1, 1)
    check("next_episode 1x1 -> 1x2", nxt == (1, 2), nxt)
    nxt = stremio.next_episode(meta, 1,
                               max(e for s, e in
                                   [(int(v.get("season") or 0),
                                     int(v.get("episode") or 0))
                                    for v in meta["videos"]] if s == 1))
    check("season finale rolls into S02E01", nxt is None or nxt[0] == 2, nxt)
    check("prev_episode 1x2 -> 1x1",
          stremio.prev_episode(meta, 1, 2) == (1, 1),
          stremio.prev_episode(meta, 1, 2))
    s2e1 = stremio.prev_episode(meta, 2, 1)
    check("prev_episode S02E01 -> S01 finale",
          s2e1 is not None and s2e1[0] == 1, s2e1)
    hit = stremio.find_series("Game of Thrones")
    check("find_series", hit and hit[0] == "tt0944947", hit)
    hit1 = stremio.find_series("Silo")
    check("find_series single-word (live: the user's Silo case)",
          hit1 is not None, hit1)

    cfg = Config(dict(DEFAULTS), None)
    streams = stremio.addon_streams(cfg, "tt0944947", 1, 2)
    check("torrentio streams", len(streams) >= 3, "%d streams" % len(streams))
    best = stremio.best_stream(cfg, streams)
    check("best_stream playable", best is not None and
          (best.get("url") or (best.get("infoHash")
                               and best.get("fileIdx") is not None)),
          best and best.get("title", "")[:60])


# ------------------------------------------------------------- [C] server
def leg_c():
    print("\n[C] local streaming server (live)")
    from src import stremio as st
    cfg = Config(dict(DEFAULTS), None)
    server = st.StreamingServer(cfg.data["stremio_server"])
    if not server.health():
        check("server health", False, "no streaming server on 11470 "
              "(Stremio/service not running) — skipping C")
        return None
    check("server health", True)

    # a real episode torrent from the addon, started via our create()
    streams = st.addon_streams(cfg, "tt0944947", 1, 1)
    pick = next((s for s in streams if s.get("infoHash")
                 and s.get("fileIdx") is not None), None)
    check("got a torrent stream to create", pick is not None)
    if not pick:
        return None
    info_hash = str(pick["infoHash"]).lower()
    file_idx = int(pick.get("fileIdx") or 0)
    ok = server.create(info_hash)
    check("server create", ok, info_hash[:12])

    url = server.play_url(info_hash, file_idx)
    REAL_URL[0] = url
    ident = st.resolve_identity(url, server)
    check("resolve_identity", ident is not None and
          ident.get("stremio_imdb") == "tt0944947" and
          ident.get("season") == 1, ident)

    # the play URL must actually serve bytes (bounded fetch)
    import requests
    served = False
    for _ in range(12):
        try:
            r = requests.get(url, headers={"Range": "bytes=0-65535"},
                             timeout=25, stream=True)
            chunk = next(r.iter_content(65536), b"")
            r.close()
            if chunk:
                served = len(chunk) > 1024
                break
        except Exception:
            pass
        time.sleep(5)
    check("play url serves bytes", served)

    # full autoplay chain from the handed-off playable
    cur = st.playable_from_url(url)
    nxt = st.next_playable(cfg, cur)
    check("next_playable chain", nxt is not None and
          nxt.get("stremio_imdb") == "tt0944947" and
          nxt.get("season") == 1 and nxt.get("episode") == 2,
          nxt and (nxt.get("title"), nxt.get("url", "")[:60]))
    # ...and the ⏮ chain back down (live addons + server create)
    prv = st.prev_playable(cfg, dict(nxt))
    check("prev_playable chain (E2 -> E1)", prv is not None and
          prv.get("stremio_imdb") == "tt0944947" and
          prv.get("season") == 1 and prv.get("episode") == 1,
          prv and (prv.get("title"), prv.get("url", "")[:60]))
    return True


# --------------------------------------------------- [D] single instance
def leg_d():
    print("\n[D] single-instance relay (cross-process, like the real exe)")
    from src.singleinst import SingleInstance
    app = QtWidgets.QApplication.instance()
    got = []
    a = SingleInstance("mtp-probe-single")
    a.received.connect(lambda args: got.append(args))
    check("first instance owns socket", not a.forward_if_running([]))

    client = (
        "import os,sys\n"
        "os.environ.setdefault('QT_QPA_PLATFORM','offscreen')\n"
        "sys.path.insert(0, r'D:\\Coding\\MichaelTVPlayer')\n"
        "from PyQt5 import QtWidgets\n"
        "app=QtWidgets.QApplication([])\n"
        "from src.singleinst import SingleInstance\n"
        "b=SingleInstance('mtp-probe-single')\n"
        "sys.exit(0 if b.forward_if_running("
        "[r'C:\\some path\\playlist.m3u']) else 1)\n")
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", client],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True)
    # keep the owner's event loop pumping WHILE the client connects —
    # blocking here (subprocess.run) starves the pipe acceptance and the
    # payload is never delivered
    deadline = time.time() + 30
    while time.time() < deadline and (p.poll() is None or not got):
        app.processEvents()
        if p.poll() is not None and not got:
            time.sleep(0.1)
            app.processEvents()
            if got:
                break
        time.sleep(0.02)
    rc = p.poll()
    check("second process relays", rc == 0, "rc=%s" % rc)
    check("owner received args", got == [["C:\\some path\\playlist.m3u"]], got)


# ------------------------------------------------------- [E] GUI handoff
def leg_e():
    """The GUI leg runs in its own process: building MainWindow (real
    libVLC) on top of everything legs A-D leave behind proved flaky
    offscreen."""
    print("\n[E] offscreen GUI handoff (isolated subprocess)")
    import subprocess
    inner = os.path.join(_APPDATA, "leg_e_inner.py")
    with open(inner, "w", encoding="utf-8") as f:
        f.write(LEG_E_SRC)
    env = dict(os.environ)
    env["MTP_PROBE_URL"] = REAL_URL[0] or ""
    try:
        r = subprocess.run([sys.executable, "-u", inner], env=env,
                           capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        check("leg E subprocess finished", False, "timeout after 240s")
        return
    out = (r.stdout or "") + (r.stderr or "")
    saw = 0
    for line in out.splitlines():
        if line.startswith("LEG_E "):
            body = line[6:]
            m = re.search(r" (OK|FAIL)\|", body)
            if m:
                check(body[:m.start()], m.group(1) == "OK", body[m.end():])
                saw += 1
    check("leg E produced checks", saw >= 5, "%d checks" % saw)
    check("leg E subprocess clean exit", r.returncode == 0,
          out[-300:] if r.returncode else "")


LEG_E_SRC = r'''
import os, sys, time, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="mtp_legE_")
sys.path.insert(0, r"D:\Coding\MichaelTVPlayer")

def report(name, ok, detail=""):
    print("LEG_E %s %s|%s" % (name, "OK" if ok else "FAIL", detail),
          flush=True)

from PyQt5 import QtWidgets
app = QtWidgets.QApplication(sys.argv)
import src.player as player_mod
plays = []

def _fake_play(self, url, timeshift=None, start_seconds=0.0,
               start_wait_s=20.0, sub_file=None):
    plays.append((url, start_wait_s, sub_file, start_seconds))
    self._start_ok = True

player_mod.VLCPlayer.play = _fake_play
from src.config import Config
cfg = Config.load()
if not cfg.has_account():
    cfg.data["server_url"] = "http://127.0.0.1:9"
from src.ui.main_window import MainWindow
win = MainWindow(cfg)
url = os.environ.get("MTP_PROBE_URL") or (
    "http://127.0.0.1:11470/8ac2f2df3db05b8f9e7a4b11c8dbf8a1c3d5e7f9/1")
m3u = os.path.join(os.environ["APPDATA"], "handoff.m3u")
with open(m3u, "w", encoding="utf-8") as f:
    f.write("#EXTM3U\n#EXTINF:0\n%s\n" % url)
# exactly what the patched Stremio server sends: vlc flags + the source
win.handle_handoff(["--start-time=12", "--no-video-title-show",
                    "--sub-file=does-not-exist.srt", m3u])
cur = {}
for _ in range(150):
    app.processEvents()
    cur = win.player_view.current or {}
    if cur.get("kind") == "stremio":
        break
    time.sleep(0.05)
report("handoff -> playing", cur.get("kind") == "stremio"
       and "11470" in cur.get("url", ""), str(cur.get("url", ""))[:60])
report("stremio start wait 60s", bool(plays) and plays[0][1] == 60.0)
report("start-time carried", bool(plays) and plays[0][3] == 12.0)
_pv = win.player_view
# identity-failure regression: a (fav_key, None) result used to crash
# _on_stremio_identity (cur.update(None)) instead of showing the honest
# "could not identify" message
try:
    _pv._on_stremio_identity(("ok", ("no-such-fav", None)))
    report("identity None result: no crash", True)
except Exception as exc:  # noqa: BLE001
    report("identity None result: no crash", False, repr(exc))
# episode buttons stay hidden until identity resolves a season/episode
# (a Stremio movie never grows them)
pre_ident_hidden = (_pv.btn_prev.isHidden() and _pv.btn_next.isHidden()
                    and _pv.btn_auto.isHidden())
n_before = len(plays)
win.handle_handoff(["not-a-file"])
report("junk handoff ignored", len(plays) == n_before)
report("recents got it", any(r.get("fav_key") == cur.get("fav_key")
                             for r in cfg.recents))
# the Downloads auto-play path: a Stremio playlist download lands in a
# (temp) Downloads folder -> watcher -> handle_handoff -> playing
import tempfile as _tf
dl = _tf.mkdtemp(prefix="mtp_legE_dl_")
from src.watchfolder import DownloadsWatcher
w = DownloadsWatcher(directory=dl, parent=win)
w.handoff.connect(win.handle_handoff)
report("watcher started", w.start())
with open(os.path.join(dl, "playlist.m3u"), "w") as f:
    f.write("#EXTM3U\n#EXTINF:0\n%s\n" % url)
n2 = len(plays)
played2 = False
for _ in range(200):
    app.processEvents()
    if len(plays) > n2:
        played2 = True
        break
    time.sleep(0.05)
report("watchfolder handoff -> playing", played2)
c2 = win.player_view.current or {}
report("watchfolder played the right url", c2.get("url") == url)
report("watchfile consumed",
       os.path.isfile(os.path.join(dl, "playlist.m3u.mtpdone")))
w.stop()
if os.environ.get("MTP_PROBE_URL"):
    ident = False
    for _ in range(200):
        app.processEvents()
        c = win.player_view.current or {}
        ident = bool(c.get("stremio_imdb") and c.get("season"))
        if ident:
            break
        time.sleep(0.15)
    report("identity resolved into playable", ident,
           str(win.player_view.current.get("title", "")))
    # episode NAME in the identity title (Cinemeta videos carry it in
    # "name", not "title" — the bare "Show — S01E01" bug)
    t = str(win.player_view.current.get("title", ""))
    report("identity title has episode name",
           ident and len(t.split(" — ", 1)[-1].split(" ", 2)) >= 3, t)
    # the ⏮/⏭/autoplay buttons surface the moment identity lands
    report("episode buttons hidden before identity", pre_ident_hidden)
    report("prev btn visible + enabled once identified",
           not _pv.btn_prev.isHidden() and _pv.btn_prev.isEnabled())
    report("next btn visible for stremio episode",
           not _pv.btn_next.isHidden())
    report("autoplay btn visible for stremio episode",
           not _pv.btn_auto.isHidden())
    # lookahead: the next episode is prefetched while this one plays
    look = None
    for _ in range(300):
        app.processEvents()
        look = win.player_view._stremio_lookahead
        if look and look[0] == (win.player_view.current or {}).get(
                "fav_key") and look[1]:
            break
        time.sleep(0.2)
    nxt = look[1] if (look and look[1]) else None
    report("lookahead prefetched next episode", bool(nxt),
           str((nxt or {}).get("title", ""))[:60])
    report("lookahead title has episode name", bool(nxt) and len(
        str(nxt.get("title", "")).split(" — ", 1)[-1].split(" ", 2)) >= 3,
        str((nxt or {}).get("title", ""))[:60])
win.close()
'''


# ---------------------------------------------------------- [F] fileassoc
def leg_f():
    print("\n[F] fileassoc round-trip (UserChoice untouched)")
    from src import fileassoc
    import winreg
    fileassoc.register()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Classes\%s\shell\open\command"
                            % fileassoc.PROGID) as k:
            cmd, _ = winreg.QueryValueEx(k, None)
        check("progid command registered", "%1" in cmd, cmd)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion"
                            r"\Explorer\FileExts\.m3u\OpenWithProgids") as k:
            v, _ = winreg.QueryValueEx(k, fileassoc.PROGID)
        check("openwith entry present", True)
    except OSError as exc:
        check("progid command registered", False, exc)
    finally:
        fileassoc.unregister()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Classes\%s" % fileassoc.PROGID):
            check("unregister removed progid", False)
    except OSError:
        check("unregister removed progid", True)


# ------------------------------------------------ [G] streampatch
def leg_g():
    print("\n[G] streampatch round-trip (temp copy - live file untouched)")
    from src import streampatch as sp
    # the sample file embeds the module's own _ORIGINAL/_TITLE_ORIG
    # lines, so the fixture can never drift from what the patcher expects
    sample = ("x: 1,\n            vlc: {\n                "
              + sp._TITLE_ORIG + ",\n                args: [ "
              "\"--no-video-title-show\" ],\n                win32: {\n"
              + sp._ORIGINAL + "\n            }\n")
    tmp = os.path.join(tempfile.mkdtemp(prefix="mtp_sp_"), "server.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(sample)
    real_finder = sp.find_server_js
    sp.find_server_js = lambda: tmp
    try:
        check("not patched initially", not sp.is_patched())
        check("patch applies", sp.patch())
        with open(tmp, "r", encoding="utf-8") as f:
            patched = f.read()
        check("patch redirects vlc paths",
              "VideoLAN" not in patched and "MichaelTV" in patched
              and sp._MARKER in patched, patched[:120])
        check("patch relabels menu title",
              sp._TITLE_NEW in patched and sp._TITLE_ORIG not in patched)
        check("patch idempotent", sp.patch() and sp.is_patched())
        # v1->v2 migration: a file patched by the previous MichaelTV
        # (path swapped, title still "VLC") must be upgraded in place
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(sample.replace(sp._ORIGINAL, sp._patched_line()))
        check("v1 file reads unpatched", not sp.is_patched())
        check("v1 file upgraded", sp.patch() and sp.is_patched())
        with open(tmp, "r", encoding="utf-8") as f:
            up = f.read()
        check("v1 upgrade keeps redirect",
              sp._patched_line() in up and sp._TITLE_NEW in up)
        check("restore round-trips", sp.restore())
        with open(tmp, "r", encoding="utf-8") as f:
            check("restore byte-identical", f.read() == sample)
    finally:
        sp.find_server_js = real_finder


# ------------------------------------------------ [H] watchfolder
def leg_h():
    print("\n[H] watchfolder (Downloads auto-play)")
    from src.watchfolder import DownloadsWatcher, _NAME_RE
    from PyQt5 import QtWidgets  # noqa: F401  (instance() in wait_for)
    for name, ok in (("playlist.m3u", True), ("playlist (1).m3u", True),
                     ("playlist-2.m3u8", True), ("Playlist.M3U", True),
                     ("mylist.m3u", False), ("playlist.m3u.txt", False),
                     ("playlist.m3u.mtpdone", False),
                     ("playlistx.m3u", False)):
        check("name gate %r" % name, bool(_NAME_RE.match(name)) == ok)

    tmpdir = tempfile.mkdtemp(prefix="mtp_watch_")

    def m3u(url):
        return "#EXTM3U\n#EXTINF:0\n%s\n" % url

    def wait_for(cond, seconds=8.0):
        qapp = QtWidgets.QApplication.instance()
        end = time.time() + seconds
        while time.time() < end:
            qapp.processEvents()
            if cond():
                return True
            time.sleep(0.05)
        return False

    # pre-existing downloads are baselined, never replayed at startup
    with open(os.path.join(tmpdir, "playlist.m3u"), "w") as f:
        f.write(m3u("http://OLD/should-not-play"))
    w = DownloadsWatcher(directory=tmpdir)
    got = []
    w.handoff.connect(lambda args: got.append(list(args)))
    check("start ok", w.start())
    QtWidgets.QApplication.instance().processEvents()

    # a new Stremio-shaped download plays through handoff
    new = os.path.join(tmpdir, "playlist (1).m3u")
    url1 = "http://127.0.0.1:11470/" + "a" * 40 + "/2"
    with open(new, "w") as f:
        f.write(m3u(url1))
    check("new download picked up",
          wait_for(lambda: len(got) >= 1), got)
    check("handoff payload is the stream url",
          got and got[0] == [url1], got)
    check("consumed file renamed", os.path.isfile(new + ".mtpdone"))

    # a non-matching name is ignored even with a valid playlist inside
    got.clear()
    with open(os.path.join(tmpdir, "iptv-list.m3u"), "w") as f:
        f.write(m3u("http://IGNORED/x"))
    check("unrelated playlist ignored", not wait_for(
        lambda: got, seconds=3.0), got)

    # the same filename can hand off again (rename freed the name)
    with open(new, "w") as f:
        f.write(m3u("http://SECOND/handoff"))
    check("name reuse hands off again",
          wait_for(lambda: len(got) >= 1), got)
    check("second handoff url", got and got[0] == ["http://SECOND/handoff"])

    # a playlist with no URL never fires and eventually settles
    got.clear()
    with open(os.path.join(tmpdir, "playlist (2).m3u"), "w") as f:
        f.write("#EXTM3U\n")
    check("url-less file ignored", not wait_for(
        lambda: got, seconds=4.0), got)
    w.stop()

    # purge_consumed: week-old .mtpdone files go, fresh ones and
    # unconsumed playlists stay
    from src.watchfolder import purge_consumed
    import src.watchfolder as _wf
    old = os.path.join(tmpdir, "playlist.m3u.mtpdone")
    new = os.path.join(tmpdir, "playlist (9).m3u.mtpdone")
    keep = os.path.join(tmpdir, "real.m3u")
    for p in (old, new, keep):
        with open(p, "w") as f:
            f.write("x")
    os.utime(old, (time.time() - 30 * 86400,) * 2)
    _orig_dir = _wf.downloads_dir
    _wf.downloads_dir = lambda: tmpdir
    try:
        purge_consumed()
    finally:
        _wf.downloads_dir = _orig_dir
    check("purge removes old consumed handoffs",
          not os.path.exists(old))
    check("purge keeps fresh consumed handoffs", os.path.exists(new))
    check("purge never touches unconsumed playlists",
          os.path.exists(keep))


if __name__ == "__main__":
    from PyQt5 import QtWidgets
    app = QtWidgets.QApplication(sys.argv)   # one app for legs D/H
    leg_a()
    leg_b()
    leg_c()
    leg_d()
    leg_e()
    leg_f()
    leg_g()
    leg_h()
    print("\n%s (%d failures)" % ("ALL PASS" if not FAILS else "FAILURES",
                                  len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1 if FAILS else 0)
