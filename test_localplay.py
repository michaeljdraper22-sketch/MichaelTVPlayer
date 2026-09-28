# -*- coding: utf-8 -*-
"""Tests for local video files: the playable/sidecar helpers, the handoff
parse, the setup-skip flag, and LocalSubTap's end-to-end cue extraction
(small REAL containers built with ffmpeg, same approach as
test_vod_splitter.py).

Run:  .venv\\Scripts\\python.exe test_localplay.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.localplay import (LocalSubTap, find_sidecar_sub,  # noqa: E402
                           is_video_path, make_file_playable)
from src.profanity import find_ffmpeg  # noqa: E402
from src import stremio  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


FF = find_ffmpeg()
MKV = os.path.abspath("build/local_test.mkv").replace("\\", "/")
MP4 = os.path.abspath("build/local_test.mp4").replace("\\", "/")
MP4_FS = os.path.abspath("build/local_test_fs.mp4").replace("\\", "/")
SRT_IN = os.path.abspath("build/local_test_in.srt").replace("\\", "/")

SRT_TEXT = ("1\n00:00:01,000 --> 00:00:03,000\n"
            "what the hell is this\n\n"
            "2\n00:00:05,000 --> 00:00:07,000\n"
            "clean as snow\n\n"
            "3\n00:00:09,000 --> 00:00:11,000\n"
            "damn dogs everywhere\n")


def build_samples():
    os.makedirs("build", exist_ok=True)
    with open(SRT_IN, "w", encoding="utf-8") as f:
        f.write(SRT_TEXT)
    subprocess.run(
        [FF, "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=steelblue:s=256x144:d=12:r=10",
         "-i", SRT_IN,
         "-map", "0:v", "-map", "1:s", "-c:v", "libx264",
         "-preset", "ultrafast", "-c:s", "srt", MKV],
        check=True, timeout=120, creationflags=0x08000000)
    for dst, extra in ((MP4, []), (MP4_FS, ["-movflags", "+faststart"])):
        subprocess.run(
            [FF, "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=seagreen:s=256x144:d=12:r=10",
             "-i", SRT_IN,
             "-map", "0:v", "-map", "1:s", "-c:v", "libx264",
             "-preset", "ultrafast", "-c:s", "mov_text",
             "-metadata:s:s:0", "language=eng"] + extra + [dst],
            check=True, timeout=120, creationflags=0x08000000)


class TapCollector:
    """Plain-Python signal sink: records cues/failures delivered while a
    Qt event loop spins (queued cross-thread deliveries need one)."""

    def __init__(self, app):
        self.app = app
        self.cues = []
        self.fails = []

    def drain(self, seconds=8.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def on_cue(self, start, end, text):
        self.cues.append((start, end, text))

    def on_fail(self, why):
        self.fails.append(why)


def run_tap(app, path, prefer="eng"):
    tap = LocalSubTap()
    out = TapCollector(app)
    tap.cue.connect(out.on_cue)
    tap.failed.connect(out.on_fail)
    ok = tap.start(path, prefer_language=prefer)
    if ok:
        out.drain(8.0)
        tap.stop()
    return ok, out


def main():
    app = QtWidgets.QApplication.instance() \
        or QtWidgets.QApplication(sys.argv)

    print("[1] path / playable / sidecar helpers")
    check("video ext recognized (mkv/mp4/avi/MKV)",
          is_video_path("a/b/c.MKV") and is_video_path("x.mp4")
          and is_video_path("y.avi"))
    check("non-video rejected (txt/srt/m3u)",
          not is_video_path("notes.txt") and not is_video_path("sub.srt")
          and not is_video_path("list.m3u"))

    tmp = tempfile.mkdtemp()
    try:
        vid = os.path.join(tmp, "Some Movie (2023).mkv")
        open(vid, "wb").close()
        pl = make_file_playable(vid)
        check("playable shape", pl["kind"] == "file"
              and pl["title"] == "Some Movie (2023)"
              and pl["url"] == os.path.abspath(vid)
              and pl["fav_key"].startswith("file:"))
        pl2 = make_file_playable(vid[:-3] + "MKV")   # same file, upper ext
        check("fav_key stable across casing", pl2["fav_key"] == pl["fav_key"])
        check("no sidecar -> no sub_file", "sub_file" not in pl)

        side = os.path.join(tmp, "Some Movie (2023).srt")
        with open(side, "w", encoding="utf-8") as f:
            f.write(SRT_TEXT)
        check("exact-basename sidecar found",
              find_sidecar_sub(vid) == side)
        pl3 = make_file_playable(vid)
        check("playable picks up sidecar", pl3.get("sub_file") == side)
        os.remove(side)

        with open(os.path.join(tmp, "Some Movie (2023).en.srt"), "w",
                  encoding="utf-8") as f:
            f.write(SRT_TEXT)
        check("lang-suffixed sidecar found",
              find_sidecar_sub(vid).endswith(".en.srt"))

        print("[2] handoff parse (local file launch)")
        launch = stremio.parse_launch_args([vid])
        check("video file arg -> playable launch",
              launch and launch["url"] == vid
              and launch["start_at"] == 0.0)
        en_side = os.path.join(tmp, "Some Movie (2023).en.srt")
        launch2 = stremio.parse_launch_args(
            ["--start-time=12.5", "--sub-file=" + en_side, vid])
        check("flags + file parse",
              launch2 and launch2["url"] == vid
              and abs(launch2["start_at"] - 12.5) < 0.01)
        junk = os.path.join(tmp, "readme.txt")
        with open(junk, "w", encoding="utf-8") as f:
            f.write("no urls here\n")
        check("non-video file rejected by handoff",
              stremio.parse_launch_args([junk]) is None)

        print("[3] setup-skip flag")
        fd, cfg_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        cfg = Config({}, Path(cfg_path))
        check("setup_skipped defaults off", cfg.setup_skipped is False)
        cfg.setup_skipped = True
        cfg.save()
        loaded = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        check("setup_skipped persists to disk",
              loaded.get("setup_skipped") is True)
        check("setup_skipped reads back",
              Config(loaded, Path(cfg_path)).setup_skipped is True)
        try:
            os.remove(cfg_path)
        except OSError:
            pass

        print("[4] LocalSubTap container detection")
        garbage = os.path.join(tmp, "garbage.bin")
        with open(garbage, "wb") as f:
            f.write(os.urandom(4096))
        ok, out = run_tap(app, garbage)
        check("garbage -> start refused + failed", ok is False
              and out.fails)

        if not FF:
            print("  (ffmpeg not found — skipping container cue tests)")
        else:
            print("[5] LocalSubTap end-to-end (real containers)")
            build_samples()
            for name, path in (("MKV", MKV), ("MP4 (moov at end)", MP4),
                               ("MP4 faststart", MP4_FS)):
                ok, out = run_tap(app, path)
                texts = [t for _, _, t in out.cues]
                check("%s: cues extracted (%d)" % (name, len(out.cues)),
                      ok and len(out.cues) >= 3
                      and any("hell" in t for t in texts)
                      and any("snow" in t for t in texts)
                      and not out.fails)
            if os.path.isfile(MKV):
                os.remove(MKV)
            if os.path.isfile(MP4):
                os.remove(MP4)
            if os.path.isfile(MP4_FS):
                os.remove(MP4_FS)

        print()
        if FAIL:
            print("FAILED %d/%d:" % (len(FAIL), len(PASS) + len(FAIL)))
            for name in FAIL:
                print("  - " + name)
            sys.exit(1)
        print("all %d checks passed" % len(PASS))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
