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
    touched by the probe)

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
n_before = len(plays)
win.handle_handoff(["not-a-file"])
report("junk handoff ignored", len(plays) == n_before)
report("recents got it", any(r.get("fav_key") == cur.get("fav_key")
                             for r in cfg.recents))
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
    # the sample file embeds the module's own _ORIGINAL line, so the
    # fixture can never drift from what the patcher expects
    sample = "x: 1,\n            win32: {\n" + sp._ORIGINAL \
        + "\n            }\n"
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
        check("patch idempotent", sp.patch() and sp.is_patched())
        check("restore round-trips", sp.restore())
        with open(tmp, "r", encoding="utf-8") as f:
            check("restore byte-identical", f.read() == sample)
    finally:
        sp.find_server_js = real_finder


if __name__ == "__main__":
    from PyQt5 import QtWidgets
    app = QtWidgets.QApplication(sys.argv)   # one app for leg D
    leg_a()
    leg_b()
    leg_c()
    leg_d()
    leg_e()
    leg_f()
    leg_g()
    print("\n%s (%d failures)" % ("ALL PASS" if not FAILS else "FAILURES",
                                  len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1 if FAILS else 0)
