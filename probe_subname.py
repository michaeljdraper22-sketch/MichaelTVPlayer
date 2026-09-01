# -*- coding: utf-8 -*-
"""One-shot diagnostic: what does VLC NAME the external sub-file track?

The stremio overlay routing keys off the spu track's name (match against
the file's basename). This settles the naming with the real bundled VLC:
plays the local probe mkv with a sub-file attached, dummy vout, volume 0
(invisible + silent, per the testing rules), and dumps spu_tracks().
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(BASE, "probe_jump_media.mkv")
SUB = os.path.join(BASE, "_probe_subname.srt")

with open(SUB, "w", encoding="utf-8") as f:
    f.write("1\n00:00:01,000 --> 00:00:04,000\nhello there\n\n"
            "2\n00:00:08,000 --> 00:00:11,000\nsecond line\n\n")

from src.player import VLCPlayer  # noqa: E402

w = VLCPlayer(volume=0, sub_args=["--vout=dummy"])
try:
    w.play(MEDIA, timeshift=False, start_wait_s=15.0, sub_file=SUB)
    for _ in range(30):
        time.sleep(0.5)
        tracks = w.spu_tracks()
        if tracks:
            break
    print("spu_tracks:", tracks)
    print("active:", w.active_spu())
    # also with spaces + unicode in the name (Stremio downloads look like
    # "S01E02-English- [opensubtitles.com].srt")
    sub2 = os.path.join(BASE, "_probe sub name é.srt")
    with open(sub2, "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:04,000\nhi\n\n")
    w2 = VLCPlayer(volume=0, sub_args=["--vout=dummy"])
    try:
        w2.play(MEDIA, timeshift=False, start_wait_s=15.0, sub_file=sub2)
        for _ in range(30):
            time.sleep(0.5)
            t2 = w2.spu_tracks()
            if t2:
                break
        print("spu_tracks2:", t2)
    finally:
        w2.stop_and_release()
finally:
    w.stop_and_release()
    for p in (SUB, os.path.join(BASE, "_probe sub name é.srt")):
        try:
            os.remove(p)
        except OSError:
            pass
