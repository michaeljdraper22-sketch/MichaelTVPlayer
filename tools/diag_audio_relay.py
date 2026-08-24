# -*- coding: utf-8 -*-
"""Diagnostic 3: E25 (682804.mkv) played through a REAL VodRelay — the
app's actual VOD path (captions engaged -> localhost relay). Same
VLCPlayer (:audio-language=en,eng at open), muted, no video. If English
fails to be the active track at open here while it works direct, the
relay's serving pattern is what breaks the language preference.

Run:  .venv\\Scripts\\python.exe tools\\diag_audio_relay.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets  # noqa: E402

from src.player import VLCPlayer, USER_AGENT  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402
from src.vod_splitter import VodRelay  # noqa: E402

URL = "http://cf.534842.xyz/series/726352471c/d809266e91/682804.mkv"


def is_en(name):
    return PlayerView._is_english_name(str(name or ""))


def main():
    app = QtWidgets.QApplication(sys.argv)
    relay = VodRelay()
    local = relay.start(URL, USER_AGENT, prefer_language="eng")
    print(f"relay: {URL} -> {local}")
    p = VLCPlayer(timeshift=False, sub_args=["--no-video"])
    p.set_mute(True)
    p.play(local, timeshift=False)
    last = ""
    tracks = []
    t0 = time.time()
    while time.time() - t0 < 30.0:
        app.processEvents()
        time.sleep(0.3)
        tracks = p.audio_tracks()
        active = p.active_audio()
        cur = dict(tracks).get(active, "?")
        line = f"active={cur!r}"
        if line != last:
            print(f"  t={time.time() - t0:5.1f}s {line}")
            last = line
    final = dict(tracks).get(p.active_audio(), "?")
    print(f"FINAL: {final!r}  english={is_en(final)}")
    if not is_en(final):
        print("english tracks present:",
              [n for _, n in tracks if is_en(n)])
    p.stop_and_release()
    relay.stop()
    time.sleep(0.5)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
