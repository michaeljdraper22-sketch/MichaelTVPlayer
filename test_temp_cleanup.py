# -*- coding: utf-8 -*-
"""Startup temp sweep: crashed-run artifacts are deleted, in-use ones survive.

Covers main.cleanup_stale_temp_files — the launch-time backstop that keeps
a crashed/wedged exit from filling %TEMP% with GB-sized mtp_dvr_* buffer
dirs and mtp_split_* VOD relay caches. An artifact still OPEN by a running
instance must survive the pass (Windows locks it) and go on the next one.

Run:  .venv\\Scripts\\python.exe test_temp_cleanup.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import cleanup_stale_temp_files  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def write(path, size=1024):
    with open(path, "wb") as f:
        f.write(b"x" * size)


OLD = 3600.0        # an hour old: leftovers from a previous run


def backdate(path, age_s=OLD):
    """Age an artifact (and its children) so the sweep treats it as stale."""
    t = time.time() - age_s
    try:
        if os.path.isdir(path):
            for name in os.listdir(path):
                os.utime(os.path.join(path, name), (t, t))
        os.utime(path, (t, t))
    except OSError:
        pass


def main():
    root = tempfile.mkdtemp(prefix="mtp_sweeptest_")
    try:
        dvr = os.path.join(root, "mtp_dvr_leaked")
        os.makedirs(dvr)
        write(os.path.join(dvr, "buffer.ts"), 4096)
        cap = os.path.join(root, "mtp_cap_leaked")
        os.makedirs(cap)
        write(os.path.join(cap, "tap.srt"), 64)
        split = os.path.join(root, "mtp_split_leaked.mkv")
        write(split, 2048)
        keep = os.path.join(root, "someone_elses_file.bin")
        write(keep, 32)
        locked = os.path.join(root, "mtp_split_inuse.mkv")
        write(locked, 512)
        # a LIVE session's DVR dir: freshly written, must survive the sweep
        # (2026-11-16: a relay launch deleted a running app's 232 MB buffer)
        live = os.path.join(root, "mtp_dvr_runrning")
        os.makedirs(live)
        write(os.path.join(live, "buffer.ts"), 8192)

        for p in (dvr, cap, split):
            backdate(p)

        fh = open(locked, "r+b")        # Windows: holds the file locked
        try:
            cleanup_stale_temp_files(root=root)   # must never raise
        finally:
            fh.close()

        check("dvr buffer dir removed", not os.path.exists(dvr))
        check("caption tap dir removed", not os.path.exists(cap))
        check("splitter cache file removed", not os.path.exists(split))
        check("unrelated files untouched", os.path.exists(keep))
        check("fresh DVR dir (live session) survives",
              os.path.exists(os.path.join(live, "buffer.ts")))
        if sys.platform == "win32":
            check("in-use cache survives the pass (locked)",
                  os.path.exists(locked))
        else:
            print("  (skip locked-file check: not Windows)")

        backdate(locked)
        cleanup_stale_temp_files(root=root)
        check("released cache removed on the next pass",
              not os.path.exists(locked))
        # ...and the still-fresh live buffer is STILL untouched
        check("fresh DVR dir still survives the second pass",
              os.path.exists(os.path.join(live, "buffer.ts")))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
