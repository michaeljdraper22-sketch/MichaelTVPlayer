# -*- coding: utf-8 -*-
"""Tests for the new auto-caption system: paced/synced transcription feed,
multi-line caption widget, and the subtitle settings dialog."""
import os
import struct
import sys
import time
import wave

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.subtitles import (  # noqa: E402
    MAX_LINES, CaptionWidget, SubtitlesSettingsDialog)

app = QtWidgets.QApplication(sys.argv)
PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name, flush=True)


print("[1] CaptionWidget wraps long text into a few lines", flush=True)
cfg = Config.load()
host = QtWidgets.QWidget()
host.resize(1280, 720)
w = CaptionWidget(host, cfg)
w._raw = " ".join(f"word{i}" for i in range(60))   # a fast talker's dump
w._relayout()
check(f"at most {MAX_LINES} lines ({len(w._lines)})", len(w._lines) <= MAX_LINES)
check("box fits the video width", w.width() <= 1280)
check("box fits the video height", w.height() <= 720)
w.set_text("")
check("empty text hides the widget", not w.isVisible())

print("[2] styled rendering switches", flush=True)
st = cfg.subtitle_style
st.update({"outline_enabled": True, "outline_width": 6, "font_size": 30})
cfg.subtitle_style = st
w.apply_style()
w.set_text("hello world")
check("outline mode relayouts", len(w._lines) == 1 and w._font.pointSize() == 30)
pm = QtWidgets.QWidget.grab(w)   # must not crash
check("outline paint doesn't crash", not pm.isNull())
st.update({"outline_enabled": False, "bg_enabled": True})
cfg.subtitle_style = st
w.apply_style()
pm = QtWidgets.QWidget.grab(w)
check("background paint doesn't crash", not pm.isNull())

print("[3] settings dialog + revert", flush=True)
st = cfg.subtitle_style
st.update({"font_size": 24, "text_color": "#ffff00", "bg_opacity": 40})
cfg.subtitle_style = st
cfg.save()
dlg = SubtitlesSettingsDialog(cfg, None, None)
check("dialog built", dlg.size_spin.value() == 24)
dlg._revert()
from src.config import DEFAULTS  # noqa: E402
fresh = Config.load().subtitle_style
want = DEFAULTS["subtitle_style"]
diff = {k: (fresh[k], want[k]) for k in want if fresh[k] != want[k]}
check(f"revert restores defaults (diff={diff or 'none'})",
      not diff)

print("[4] captioner: paced, seekable, pausable feed", flush=True)
import vosk  # noqa: E402

fed_bytes = {"n": 0}


class FakeRec:
    def __init__(self, model, rate):
        pass

    def AcceptWaveform(self, chunk):
        fed_bytes["n"] += len(chunk)
        return False

    def PartialResult(self):
        return '{"text": ""}'

    def Result(self):
        return '{"text": ""}'


vosk.KaldiRecognizer = FakeRec
from src import captions as capmod  # noqa: E402

wav_path = os.path.join(os.environ.get("TEMP", "."), "mtp_test_cap.wav")
sr = 16000
with wave.open(wav_path, "wb") as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(sr)
    f.writeframes(b"\x00\x01" * int(sr * 20))   # 20 s backlog

cap = capmod.AutoCaptioner()
check("starts (model present)", cap.start(wav_path))


def wait_for(pred, timeout_s):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if pred():
            return True
        time.sleep(0.05)
    return False


# the speech model now loads inside the worker thread (loading the 1.3 GB
# high-accuracy model on the GUI thread froze the app for up to a minute).
# In follow mode a STATIC wav correctly feeds nothing (it tails the live
# end), so readiness = model loaded; the seek below then switches to
# paced mode, which feeds from the file.
check("worker loads the model off the GUI thread",
      wait_for(lambda: cap._model is not None, 180))
cap.seek(2.0)          # jump 2 s in, paced sync mode
time.sleep(0.3)
b0 = fed_bytes["n"]
time.sleep(2.0)
b1 = fed_bytes["n"]
paced = (b1 - b0) / (sr * 2)
check(f"feed is PACED at 1x after backlog ({paced:.2f}x)", 0.5 < paced < 2.5)
cap.seek(10.0)
time.sleep(1.0)
cap.set_rate(0.0)      # paused
time.sleep(0.3)
b2 = fed_bytes["n"]
time.sleep(1.2)
b3 = fed_bytes["n"]
check("rate=0 stops feeding", b3 - b2 < 32000)
cap.set_rate(2.0)
time.sleep(1.0)
b4 = fed_bytes["n"]
check("rate=2 feeds faster", (b4 - b3) / (sr * 2) > 1.3)
cap.stop()
os.unlink(wav_path)

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed", flush=True)
for f in FAIL:
    print("  FAILED:", f, flush=True)
os._exit(1 if FAIL else 0)
