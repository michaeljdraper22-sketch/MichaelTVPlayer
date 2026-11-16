# -*- coding: utf-8 -*-
"""Offscreen test for the volume pin (PlayerView._enforce_volume).

Field report 2026-11-11: "the volume slider is going down no matter how
you adjust it". Root cause (reproduced against real libvlc 3.0.24):
--aout=directsound keeps ONE volume for the whole process — the last
audio_set_volume from ANY player wins, and a player's FIRST audio output
adopts that value instead of its own. With two windows (or a rebuilt
player) at different bars, a window could PLAY at the other one's level
while its slider said something else. The fixed 0 s / 0.7 s pokes often
fire before a slow stream's output exists, so the alien level stuck.

The pin compares the output's ACTUAL percent with the slider's taper and
re-applies on a real mismatch, at most once per 1.5 s for a stubborn one.

Run:  .venv\\Scripts\\python.exe test_volume_pin.py   (sets offscreen itself)
"""
import copy
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config, DEFAULTS  # noqa: E402
from src.player import VLCPlayer, _slider_to_vlc_volume  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


class StubVlc:
    """Just enough VLCPlayer surface for _enforce_volume (+ _tick)."""

    def __init__(self, out=100, playing=True):
        self.out = out
        self.playing = playing
        self.calls = []          # bar values passed to set_volume
        self.t = 0

    def get_vlc_volume(self):
        return self.out

    def set_volume(self, bar):
        self.calls.append(int(bar))
        self.out = _slider_to_vlc_volume(int(bar))

    # ---- _tick surface ----
    def is_playing(self):
        return self.playing

    def state_name(self):
        return "playing" if self.playing else "idle"

    def get_time(self):
        return self.t

    def get_length(self):
        return 0

    def is_mute(self):
        return False

    def audio_tracks(self):
        return []

    def active_audio(self):
        return -1

    def spu_tracks(self):
        return []

    def active_spu(self):
        return -1

    def set_rate(self, _r):
        pass

    def set_mute(self, _m):
        pass


def main():
    app = QtWidgets.QApplication(sys.argv)
    # A private config on a temp path: the volume test drags the slider, and
    # the debounced save must never touch the user's real settings.json.
    cfg = Config(copy.deepcopy(DEFAULTS),
                 Path(tempfile.gettempdir()) / "mtp_volume_pin_settings.json")
    view = PlayerView(cfg)
    view.resize(1280, 720)
    view.show()
    app.processEvents()
    real = view.vlc

    print("[1] raw read-back helper (VLCPlayer.get_vlc_volume)")
    class FakePlayer:
        def __init__(self, v):
            self.v = v

        def audio_get_volume(self):
            if isinstance(self.v, Exception):
                raise self.v
            return self.v

    class FakeSelf(VLCPlayer):
        """A VLCPlayer-shaped object without a real libvlc behind it."""

        def __init__(self, v):
            self.player = FakePlayer(v)
            self._volume = 70

    check("79% -> 79", VLCPlayer.get_vlc_volume(FakeSelf(79)) == 79)
    check("-1 (no output yet) -> None",
          VLCPlayer.get_vlc_volume(FakeSelf(-1)) is None)
    check("raising player -> None",
          VLCPlayer.get_vlc_volume(FakeSelf(RuntimeError("x"))) is None)
    f = FakeSelf(79)
    check("get_volume maps 79% back to bar 90", VLCPlayer.get_volume(f) == 90)
    check("get_volume falls back to stored bar without an output",
          VLCPlayer.get_volume(FakeSelf(-1)) == 70)

    print("[2] alien output level is re-pinned to the bar")
    view.vol_slider.setValue(100)
    app.processEvents()
    view.vlc = stub = StubVlc(out=20)      # e.g. the other window's 20%
    view._enforce_volume(True)
    check("output 20% at bar 100 -> set_volume(100)",
          stub.calls == [100])

    print("[3] no storm: a mismatch that will not stick retries slowly")
    stub.out = 20                          # pretend the write didn't take
    for _ in range(3):
        view._enforce_volume(True)
    check("three ticks inside 1.5 s -> no extra writes", stub.calls == [100])
    view._vol_pin_t -= 2.0                  # let the retry window pass
    view._enforce_volume(True)
    check("after 2 s -> one retry", stub.calls == [100, 100])

    print("[4] matching output is left alone")
    stub.calls.clear()
    stub.out = _slider_to_vlc_volume(100)
    view._enforce_volume(True)
    check("match -> no set_volume", stub.calls == [])
    stub.out = _slider_to_vlc_volume(100) + 2
    view._enforce_volume(True)
    check("+2% rounding noise -> no set_volume", stub.calls == [])
    stub.out = _slider_to_vlc_volume(100) + 3
    view._enforce_volume(True)
    check("+3% real mismatch -> set_volume", stub.calls == [100])

    print("[5] follows the LIVE slider (a drag is never fought)")
    stub.calls.clear()
    view.vol_slider.setValue(50)           # set_volume is also called by
    stub.calls.clear()                     # _on_volume; isolate the pin
    stub.out = _slider_to_vlc_volume(50) + 20
    view._enforce_volume(True)
    check("bar 50 with a 70%-ish output -> set_volume(50)",
          stub.calls == [50])
    stub.calls.clear()
    view.vol_slider.setValue(0)
    stub.calls.clear()
    stub.out = 20
    view._enforce_volume(True)
    check("bar 0 (slider mute) with output 20% -> set_volume(0)",
          stub.calls == [0])

    print("[6] guarded against missing/odd players")
    stub.calls.clear()
    view._enforce_volume(False)
    check("not playing -> no writes", stub.calls == [])
    view.vlc = StubVlc(out=None)
    view._enforce_volume(True)
    check("-1/None output (device not up) -> no crash, no write", True)

    class OldStub:
        """Duck-typed view VLC from older tests: no get_vlc_volume."""

        def set_volume(self, _v):
            raise AssertionError("must not be called")

    view.vlc = OldStub()
    view._enforce_volume(True)
    check("player without get_vlc_volume -> no crash", True)

    print("[7] _tick runs the pin (integration)")
    view.vlc = StubVlc(out=100)
    view.vol_slider.setValue(100)          # settle the bar with a benign stub
    app.processEvents()
    stub = StubVlc(out=20)
    view.vlc = stub
    view._was_playing = True               # skip the play-start _poke_audio
    view._tick()
    check("_tick re-pinned the alien 20% to bar 100",
          stub.calls and stub.calls[-1] == 100)

    view.vlc = real
    view.close()


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed "
          f"({time.time() - t0:.1f}s)")
    for name in FAIL:
        print("  FAILED:", name)
    sys.exit(1 if FAIL else 0)
