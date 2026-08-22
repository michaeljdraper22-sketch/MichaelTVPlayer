# -*- coding: utf-8 -*-
"""CueStore regression suite (WP1-1b): eviction/dedup coherence on rewind
and the text_at early-break vs still-active long cues.

Run:  .venv\\Scripts\\python.exe -X utf8 test_cuestore.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.ui.caption_overlay import (CueStore, _MAX_CUES,  # noqa: E402
                                    _CUE_GRACE_S)

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


app = QtWidgets.QApplication(sys.argv)

print("[1] CueStore contract: windows, grace, newest-wins, dedupe, shift,"
      " clear")
cs = CueStore()
cs.add(10.0, 12.0, "first")
cs.add(12.0, 14.0, "second")
check("no cue before its window", cs.text_at(5.0) == [])
check("cue active inside its window", cs.text_at(11.0) == ["first"])
check("newest covering cue wins in an overlap",
      cs.text_at(12.05) == ["second"])
check("cue held through the anti-flicker grace",
      cs.text_at(14.0 + _CUE_GRACE_S - 0.05) == ["second"])
check("cue clears past the grace",
      cs.text_at(14.0 + _CUE_GRACE_S + 0.1) == [])
cs.add(14.0, 16.0, "l1\nl2\nl3\nl4")
check("roll-up window keeps the last 3 lines",
      cs.text_at(15.0) == ["l2", "l3", "l4"])
cs.add(14.0, 16.0, "l1\nl2\nl3\nl4")      # exact duplicate, still stored
check("re-received STORED cue still dedupes",
      len([c for c in cs.cues if c[2] == "l1\nl2\nl3\nl4"]) == 1)
cs.add(20.0, 22.0, "\u202b\xa0\u202c")    # bidi/NBSP padding only
check("invisible-only cue never enters the store",
      cs.text_at(21.0) == [] and not [c for c in cs.cues if c[0] == 20.0])

# shift: a rebase slides every window; the relay's re-parse after the
# rebase re-emits the SAME cues at their NEW positions and must dedupe
cs2 = CueStore()
cs2.add(10.0, 12.0, "line")
cs2.shift(2.0)
check("shift moves the window with it",
      cs2.text_at(13.0) == ["line"] and cs2.text_at(11.0) == [])
cs2.add(12.0, 14.0, "line")
check("re-parse of a shifted cue dedupes (rebase coherence)",
      len(cs2.cues) == 1)
cs2.clear()
check("clear empties the store", cs2.cues == [] and cs2.text_at(13.0) == [])
cs2.add(12.0, 14.0, "line")
check("re-add after clear re-enters (dedupe memory reset)",
      len(cs2.cues) == 1)

print("[2] eviction keeps the rewind path alive (bug: eviction orphaned"
      " _seen)")
cs3 = CueStore()
N = _MAX_CUES + 300
for i in range(N):
    cs3.add(float(i), float(i) + 2.0, f"c{i}")
check(f"store bounded ({N} adds -> exactly {_MAX_CUES})",
      len(cs3.cues) == _MAX_CUES)
check("oldest evicted, newest kept",
      cs3.cues[-1][2] == f"c{N - 1}" and cs3.cues[0][0] > 100.0)
# the VOD relay re-parses on seek-back: a cue EVICTED long ago (its start
# below the oldest kept one) must re-enter the store and be queryable
# again -- with the old code its key stayed in _seen forever, so every
# re-add was dropped as a duplicate and rewound regions painted nothing
cs3.add(150.0, 152.0, "c150")            # evicted ~5100 adds ago
check("deep rewind: evicted cue re-enters the store",
      any(c[2] == "c150" for c in cs3.cues))
check("deep rewind: evicted cue queryable again via text_at",
      cs3.text_at(150.5) == ["c150"])
cs3.add(299.0, 301.0, "c299")            # evicted moments ago
check("shallow rewind: just-evicted cue queryable again",
      cs3.text_at(299.5) == ["c299"])
check("_seen matches exactly what is stored (pruned in step with"
      " eviction)",
      cs3._seen == {(round(s, 3), txt) for s, _e, txt in cs3.cues})
before = len(cs3.cues)
cs3.add(float(N - 1), float(N - 1) + 2.0, f"c{N - 1}")   # still stored
check("rewind fix does not weaken in-store dedupe",
      len(cs3.cues) == before)

print("[3] text_at must not abandon still-active long cues (early-break"
      " bug)")
cs4 = CueStore()
cs4.add(0.0, 90.0, "long window")        # spans way past the 60 s horizon
check("control: lone long cue paints deep inside its window",
      cs4.text_at(70.0) == ["long window"])
cs4.add(0.5, 5.0, "short early")         # newer start, long dead by t=70
check("long cue still paints under a dead newer cue (regression)",
      cs4.text_at(70.0) == ["long window"])
check("long cue paints at start + 70 and right up to its end + grace",
      cs4.text_at(70.0) == ["long window"]
      and cs4.text_at(89.9) == ["long window"]
      and cs4.text_at(90.0 + _CUE_GRACE_S) == ["long window"])
check("long cue clears past its grace", cs4.text_at(90.4) == [])
check("the short cue still shows inside its own window",
      cs4.text_at(2.0) == ["short early"])
# movie shape: one song/description block under a pile of newer cues
cs5 = CueStore()
cs5.add(100.0, 400.0, "song lyrics block")
for i in range(20):
    cs5.add(110.0 + i * 5.0, 113.0 + i * 5.0, f"dialogue {i}")
check("deep long cue found under many newer cues",
      cs5.text_at(350.0) == ["song lyrics block"])
check("newer cues still win while they cover t",
      cs5.text_at(112.0) == ["dialogue 0"])

print("[4] overlap policy (pinned)")
cs6 = CueStore()
cs6.add(10.0, 20.0, "older start")
cs6.add(12.0, 22.0, "newer start")
check("overlap: the cue with the greater start wins",
      cs6.text_at(15.0) == ["newer start"])
cs6.add(12.0, 22.0, "same start, later arrival")
check("tie on start: the later-arrived cue wins",
      cs6.text_at(15.0) == ["same start, later arrival"])

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
