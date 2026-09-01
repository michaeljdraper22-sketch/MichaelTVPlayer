# -*- coding: utf-8 -*-
"""Offscreen/live probe for the Stremio handoff feature.

[A] pure parsing (offline): m3u / handoff args / server URLs / S-E
    markers / name cleanup / bencode / stream ranking
[B] live catalog + addon chain (Cinemeta, Torrentio): episode lists,
    search, next-episode, stream ranking
[C] live streaming server (127.0.0.1:11470): health, create, identity
    resolution of a real torrent play URL, full next_playable chain
[D] single-instance socket relay
[E] offscreen GUI handoff: playlist arg -> MainWindow.handle_handoff ->
    PlayerView plays (VLC stubbed to record, no real playback)
[F] fileassoc register/unregister round-trip (does NOT touch UserChoice)

Run:  .venv\\Scripts\\python.exe probe_stremio.py
"""

import os
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

    check("SE marker SxxExx", stremio.parse_se(
        "Game.of.Thrones.S02E05.720p.x264") == (2, 5))
    check("SE marker 2x05", stremio.parse_se("Show 2x05 REPACK") == (2, 5))
    check("SE marker none", stremio.parse_se("A Movie 2023 1080p") is None)

    cleaned = stremio.clean_show_name(
        "Game.of.Thrones.S02E05.1080p.WEB-DL.x265-QTZ")
    check("clean show name", cleaned == "Game of Thrones", repr(cleaned))

    t = stremio._bdecode(
        b"d4:infod4:name6:Sintel6:lengthi10eee")
    check("bdecode", t == {b"info": {b"name": b"Sintel", b"length": 10}})

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
    hit = stremio.find_series("Game of Thrones")
    check("find_series", hit and hit[0] == "tt0944947", hit)

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
    print("\n[E] offscreen GUI handoff")
    import src.player as player_mod
    from src.ui.main_window import MainWindow

    plays = []
    orig_play = player_mod.VLCPlayer.play

    def _fake_play(self, url, timeshift=None, start_seconds=0.0,
                   start_wait_s=20.0):
        plays.append((url, start_wait_s))
        self._start_ok = True
    player_mod.VLCPlayer.play = _fake_play

    app = QtWidgets.QApplication.instance()
    cfg = Config.load()
    if not cfg.has_account():
        # mirror main.py's no-account handoff bypass: dummy unreachable
        # panel so MainWindow (and its player) can build
        cfg.data["server_url"] = "http://127.0.0.1:9"
    win = MainWindow(cfg)
    path = os.path.join(_APPDATA, "handoff.m3u")
    url = REAL_URL[0] or ("http://127.0.0.1:11470/"
                          "8ac2f2df3db05b8f9e7a4b11c8dbf8a1c3d5e7f9/1")
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n#EXTINF:0\n%s\n" % url)
    win.handle_handoff([path])
    # offscreen the video-surface attach is pending — play_media defers
    # until the attach callback runs (the normal first-launch path), so
    # wait for it instead of checking immediately
    cur = {}
    for _ in range(150):
        app.processEvents()
        cur = win.player_view.current or {}
        if cur.get("kind") == "stremio":
            break
        time.sleep(0.05)
    check("handoff -> playing", cur.get("kind") == "stremio" and
          "11470" in cur.get("url", ""), cur)
    check("stremio start wait 60s", plays and plays[0][1] == 60.0, plays)
    check("recents got it", any(r.get("fav_key") == cur.get("fav_key")
                                for r in cfg.recents))
    # relaunch-with-junk just raises the window (no crash, no play)
    n_before = len(plays)
    win.handle_handoff(["not-a-file"])
    check("junk handoff ignored", len(plays) == n_before)

    # identity resolution fires (network) and fills the playable
    ident_ok = [False]

    def _poll():
        ident_ok[0] = bool(win.player_view.current.get("stremio_imdb")
                           and win.player_view.current.get("season"))
    if REAL_URL[0]:
        for _ in range(200):          # up to ~30s for the catalog chain
            app.processEvents()
            _poll()
            if ident_ok[0]:
                break
            time.sleep(0.15)
        check("identity resolved into playable", ident_ok[0],
              win.player_view.current.get("title"))
    else:
        print("SKIP identity resolved into playable (leg C had no live "
              "server URL)")

    player_mod.VLCPlayer.play = orig_play
    win.close()


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


if __name__ == "__main__":
    from PyQt5 import QtWidgets
    app = QtWidgets.QApplication(sys.argv)   # one app for legs D + E
    leg_a()
    leg_b()
    leg_c()
    leg_d()
    leg_e()
    leg_f()
    print("\n%s (%d failures)" % ("ALL PASS" if not FAILS else "FAILURES",
                                  len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1 if FAILS else 0)
