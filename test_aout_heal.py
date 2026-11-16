# -*- coding: utf-8 -*-
"""Offline suite for the audio-timeline re-anchor (2026-11-13 field report).

"Audio ~1 s behind the picture on a Stremio VOD; a reload fixes it."
libvlc's aout declares that damage itself ("buffer too late ... dropped",
"playback too late ... flushing buffers") and the amount, but the app had
never subscribed to its log. This suite pins the whole new path without a
real stream or a real libvlc:

  [1] log-line classification (_aout_mark)
  [2] va_list first-argument decode (_log_first_int)
  [3] the heal gate's decision matrix (amount / paused / live / budget)
  [4] VLCPlayer log callback records the event for the UI tick
  [5] VLCPlayer.heal_audio_sync's libvlc call sequence
  [6] PlayerView end-to-end: one heal, budget + kind + pause guards

Run:  .venv\\Scripts\\python.exe test_aout_heal.py
"""
import copy
import ctypes
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config, DEFAULTS  # noqa: E402
from src.player import (VLCPlayer, AoutHealGate, _aout_mark,  # noqa: E402
                        _log_first_int)
from src.ui.player_view import PlayerView  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          ("   [" + detail + "]" if detail else ""))


# --------------------------------------------------------------------------
print("[1] log-line classification")
for msg, want in (
        ("buffer too late (250000 us): dropped", "late"),
        ("playback too late (1000000): flushing buffers", "late"),
        ("playback way too late (3000000): flushing buffers", "late"),
        ("playback too late (5000): up-sampling", "late"),
        ("cannot start playing buffer: (hr=0x8007000e)", "device"),
        ("cannot lock buffer", "device"),
        ("audio output failed", "device"),
        ("directsound: Buffer overrun", "device"),
        ("using %s module \"%s\"", None),
        ("selected audio language[%d] %s", None),
):
    got = _aout_mark(msg)
    check(f"classify {msg[:38]!r} -> {want}", got == want, f"got {got}")

# --------------------------------------------------------------------------
print("\n[2] va_list first-argument decode")
slots = (ctypes.c_int64 * 3)(1_500_000, 42, 99)
check("reads slot 0 as the declared amount",
      _log_first_int(ctypes.cast(slots, ctypes.c_void_p).value) == 1_500_000,
      str(_log_first_int(ctypes.cast(slots, ctypes.c_void_p).value)))
check("None args -> None", _log_first_int(None) is None)
check("0 args -> None", _log_first_int(0) is None)

# --------------------------------------------------------------------------
print("\n[3] AoutHealGate decision matrix")
g = AoutHealGate()
check("large late event heals",
      g.should_heal("late", 900_000, True, False, False, 100.0) is True)
check("cooldown blocks the next one",
      g.should_heal("late", 900_000, True, False, False, 160.0) is False)
check("after the cooldown it heals again",
      g.should_heal("late", 900_000, True, False, False, 250.0) is True)
check("per-media budget caps at 3",
      g.should_heal("late", 900_000, True, False, False, 400.0) is True
      and g.should_heal("late", 900_000, True, False, False, 600.0) is False)
g.reset()
check("reset() restores the budget",
      g.should_heal("late", 900_000, True, False, False, 10.0) is True)
check("small jitter is not a desync",
      AoutHealGate().should_heal("late", 50_000, True, False, False, 1.0)
      is False)
check("unparsed amount is never acted on",
      AoutHealGate().should_heal("late", None, True, False, False, 1.0)
      is False)
check("paused is never healed",
      AoutHealGate().should_heal("late", 900_000, True, True, False, 1.0)
      is False)
check("not playing is never healed",
      AoutHealGate().should_heal("late", 900_000, False, False, False, 1.0)
      is False)
check("live chase is never healed",
      AoutHealGate().should_heal("late", 900_000, True, False, True, 1.0)
      is False)
check("device complaints do not re-anchor",
      AoutHealGate().should_heal("device", 900_000, True, False, False, 1.0)
      is False)

# --------------------------------------------------------------------------
print("\n[4] VLCPlayer._on_vlc_log records the event")


class _FakePlayer:
    def __init__(self):
        self.calls = []

    def audio_set_track(self, tid):
        self.calls.append(int(tid))
        return 0

    def audio_get_track(self):
        return 2


class _FakeSelf(VLCPlayer):
    """VLCPlayer-shaped, no libvlc behind it."""

    def __init__(self):
        import threading
        self.player = _FakePlayer()
        self._aout_evt = None
        self._aout_lock = threading.Lock()
        self.on_aout_event = None
        self._tracks = [(1, b"English"), (2, b"Spanish")]
        self._active = 2

    # active_audio() is the real method; give it the minimal surface
    def _audio_get_track_description(self):
        return self._tracks

    def state_name(self):
        return "playing"


fake = _FakeSelf()
slots850 = (ctypes.c_int64 * 2)(850_000, 0)
fmts = [b"playback too late (%lld): flushing buffers"]
fake._on_vlc_log(None, 2, None, fmts[0],
                 ctypes.cast(slots850, ctypes.c_void_p).value)
evt = fake.take_aout_event()
check("late line becomes ('late', 850000)", evt == ("late", 850_000),
      str(evt))
check("event is consumed once", fake.take_aout_event() is None)
fake._on_vlc_log(None, 2, None, b"using %s module \"%s\"", 12345)
check("unrelated line records nothing", fake.take_aout_event() is None)

# --------------------------------------------------------------------------
print("\n[5] heal_audio_sync call sequence")
check("re-anchor issued", fake.heal_audio_sync() is True)
check("disable then re-enable the ACTIVE track",
      fake.player.calls == [-1, 2], str(fake.player.calls))


class _NoTracks(_FakeSelf):
    def __init__(self):
        super().__init__()
        self.player.audio_get_track = lambda: -1   # VLC reports no track


check("no active track -> no call, False",
      _NoTracks().heal_audio_sync() is False)

# --------------------------------------------------------------------------
print("\n[6] PlayerView end-to-end")


class _StubVlc:
    def __init__(self):
        self.events = []
        self.heals = 0

    def take_aout_event(self):
        return self.events.pop(0) if self.events else None

    def heal_audio_sync(self):
        self.heals += 1
        return True

    def state_name(self):
        return "playing"


def main():
    app = QtWidgets.QApplication(sys.argv)
    cfg = Config(copy.deepcopy(DEFAULTS),
                 Path(tempfile.gettempdir()) / "mtp_aout_heal_settings.json")
    view = PlayerView(cfg)
    view.resize(1280, 720)
    view.show()
    app.processEvents()
    stub = _StubVlc()
    view.vlc = stub
    view.current = {"kind": "stremio", "title": "Spirited (2022)"}
    view._aout_gate.reset()

    # a big late report heals once...
    stub.events.append(("late", 1_000_000))
    view._maybe_heal_audio_sync(True)
    check("stremio: large late report re-anchors", stub.heals == 1,
          f"heals={stub.heals}")

    # ...and the gate's cooldown swallows an immediate second one
    stub.events.append(("late", 1_000_000))
    view._maybe_heal_audio_sync(True)
    check("stremio: cooldown prevents a second immediate heal",
          stub.heals == 1, f"heals={stub.heals}")

    # a small report never heals
    view._aout_gate.reset()
    stub.events.append(("late", 20_000))
    view._maybe_heal_audio_sync(True)
    check("stremio: small jitter ignored", stub.heals == 1,
          f"heals={stub.heals}")

    # live chase is excluded
    view._mode = "chase"
    view._aout_gate.reset()
    stub.events.append(("late", 1_000_000))
    view._maybe_heal_audio_sync(True)
    check("live chase: never re-anchored", stub.heals == 1,
          f"heals={stub.heals}")
    view._mode = "live"

    # paused is excluded
    view._aout_gate.reset()
    stub.events.append(("late", 1_000_000))
    stub.state_name = lambda: "paused"
    view._maybe_heal_audio_sync(True)
    check("paused: never re-anchored", stub.heals == 1, f"heals={stub.heals}")
    stub.state_name = lambda: "playing"

    # the event is consumed even when the guard refuses it
    check("consumed events never queue up", stub.take_aout_event() is None)

    view.close()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
