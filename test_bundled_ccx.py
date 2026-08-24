# -*- coding: utf-8 -*-
"""Vendored CCExtractor: the no-install fallback for releases.

Discovery order (live_cc.find_ccextractor): an INSTALLED CCExtractor
always wins (PATH / winget / Program Files); the vendored build only
serves machines without one. WP4b replaced the ancient static 0.88 exe
(read stdin to EOF before emitting a byte, rejected every long flag)
with the minimal runtime subset of the official 0.96.6 win portable
build — exe + the DLLs it statically imports (see
vendor/CCEXTRACTOR-VENDORED.txt for the inventory and provenance).

These checks pin that contract, including THE acceptance test: the
bundled binary must emit SRT on stdout while its stdin is still an
OPEN, GROWING stream (the pipe topology CCSource tails a DVR buffer
with), and a zero-install engage (bundled CCX only) must deliver live
cues through CCSource.

Run:  .venv\\Scripts\\python.exe test_bundled_ccx.py
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import live_cc as live_cc_mod  # noqa: E402
from src.live_cc import (bundled_ccextractor, ccx_args,  # noqa: E402
                         find_ccextractor)

ROOT = os.path.dirname(os.path.abspath(__file__))
RECORDING = os.path.join(ROOT, "TV Recordings",
                         "US_NFL_NETWORK_HD_20260820_171631.ts")
# The vendored runtime subset (WP4b): exe + static import closure.
VENDOR_DLLS = [
    "libgpac.dll", "avcodec-60.dll", "avformat-60.dll", "avfilter-9.dll",
    "avdevice-60.dll", "avutil-58.dll", "swscale-7.dll", "swresample-4.dll",
    "postproc-57.dll", "libcryptoMD.dll", "libsslMD.dll",
    "OpenSVCDecoder.dll", "vcruntime140.dll",
]
MODERN_ARGS = ["-in=ts", "-srt", "-utf8", "--stdin", "--stdout",
               "--no-codec", "dvbsub"]

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def stream_probe(exe, args, ts_path, chunk=1 << 20, pause_s=0.05,
                 cap_bytes=24 << 20, grace_s=25.0):
    """Pipe ``ts_path`` into ``exe`` as a GROWING stream (1 MiB appends
    with pauses, stdin never closed) and watch stdout. Returns
    (fed_bytes_at_first_output or None, collected_bytes, proc_was_alive).
    Reader uses read(1) so a partially-buffered stdout still counts —
    blocking read(4096) would hide bytes until EOF and fake a failure.
    """
    proc = subprocess.Popen(
        [exe] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, creationflags=0x08000000)
    out = []

    def reader():
        while True:
            b = proc.stdout.read(1)
            if not b:
                break
            out.append(b)

    threading.Thread(target=reader, daemon=True).start()
    fed = 0
    first_at = None
    with open(ts_path, "rb") as f:
        while fed < cap_bytes:
            c = f.read(chunk)
            if not c:
                break
            try:
                proc.stdin.write(c)
                proc.stdin.flush()
            except OSError:
                break                      # proc died — nothing to wait for
            fed += len(c)
            if out:                        # early exit: it streams
                first_at = fed
                break
            time.sleep(pause_s)
    if first_at is None:                   # grace: stdin stays OPEN
        deadline = time.time() + grace_s
        while not out and time.time() < deadline and proc.poll() is None:
            time.sleep(0.2)
        if out:
            first_at = fed
    alive = proc.poll() is None
    data = b"".join(out)
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass
    return first_at, data, alive


def zero_install_engage(vendored):
    """Simulate a zero-install machine (only the vendored CCX exists):
    CCSource must start on the bundled binary and deliver cues while
    the DVR buffer grows. The 0.88 fail-fast made this exact path
    return False ('bundled CCExtractor 0.88 cannot stream')."""
    from PyQt5 import QtCore
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    del app
    tmp = tempfile.mktemp(suffix=".ts")
    cues, fails = [], []
    orig_find = live_cc_mod.find_ccextractor
    live_cc_mod.find_ccextractor = lambda: vendored
    src = None
    try:
        with open(RECORDING, "rb") as r, open(tmp, "wb") as w:
            w.write(r.read(4 << 20))       # seed: the buffer exists already
        src = live_cc_mod.CCSource()
        src.cue.connect(lambda s, e, t: cues.append((s, e, t)))
        src.failed.connect(fails.append)
        ok = src.start(tmp)
        check("zero-install engage accepted (no 0.88-style fail-fast)",
              ok is True and not fails)
        deadline = time.time() + 60.0
        with open(RECORDING, "rb") as r:
            r.seek(4 << 20)
            while not cues and time.time() < deadline:
                chunk = r.read(1 << 20)    # the DVR buffer keeps growing
                if chunk:
                    with open(tmp, "ab") as w:
                        w.write(chunk)
                time.sleep(0.3)
                try:
                    src._harvest()         # drive the parser without exec()
                except Exception:  # noqa: BLE001
                    pass
        print(f"    (delivered {len(cues)} cue(s), "
              f"{len(fails)} failure signal(s) "
              f"{[f[:60] for f in fails]})")
        check("live cues delivered while the buffer grows", bool(cues))
    finally:
        live_cc_mod.find_ccextractor = orig_find
        if src is not None:
            src.stop()
        try:
            os.remove(tmp)
        except OSError:
            pass


def main():
    vendored = os.path.join(ROOT, "vendor", "ccextractorwinfull.exe")

    check("vendored exe exists in the repo", os.path.isfile(vendored))
    missing = [d for d in VENDOR_DLLS
               if not os.path.isfile(os.path.join(ROOT, "vendor", d))]
    check("vendored DLL set complete (import closure)", not missing)
    if missing:
        print(f"    missing: {missing}")
    check("bundled_ccextractor() finds it (dev layout)",
          os.path.abspath(bundled_ccextractor()) == vendored)

    found = find_ccextractor()
    check("find_ccextractor() returns something", bool(found))
    if found:
        check("installed copy outranks the vendored fallback",
              os.path.abspath(found) != vendored
              or not (
                  os.path.isfile(r"C:\Program Files\CCExtractor"
                                 r"\ccextractorwinfull.exe")
                  or any(__import__("glob").glob(os.path.expandvars(
                      r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
                      r"\CCExtractor*_*\ccextractorwinfull.exe")))))

    # One streaming form for everyone: the modern flags work for the
    # vendored build AND current user installs. --no-codec dvbsub is
    # load-bearing: without it the modern build latches onto the PMT's
    # DVB-subtitle track and OCRs bitmaps (needs tessdata we don't
    # ship, ~1x realtime, 32-ms micro-cues) instead of reading the
    # CEA-608 data from the H.264 SEI.
    bnd = bundled_ccextractor()
    if bnd:
        check("bundled args are the modern streaming form",
              ccx_args(bnd) == MODERN_ARGS)
    if found and (not bnd or os.path.abspath(found)
                  != os.path.abspath(bnd)):
        check("installed build gets the same streaming flags",
              ccx_args(found) == MODERN_ARGS)

    if not os.path.isfile(vendored):
        # graceful failure (e.g. the pre-WP4b tree with the 0.88 exe):
        # every binary-level contract below is unmet, not a crash
        check("vendored binary present for runtime checks", False)
    else:
        check("vendored binary present for runtime checks", True)
        p = subprocess.run([vendored, "--version"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=30)
        out = p.stdout.decode("utf-8", "replace")
        check("vendored exe runs standalone (exit 0)", p.returncode == 0)
        check("reports itself as CCExtractor", "CCExtractor" in out)
        import re
        m = re.search(r"Version:\s*(\d+)\.(\d+)", out)
        if m:
            print(f"    (vendored version {m.group(1)}.{m.group(2)})")
            check("modern build (>= 0.96, not the ancient 0.88)",
                  (int(m.group(1)), int(m.group(2))) >= (0, 96))
        else:
            check("modern build (>= 0.96, not the ancient 0.88)", False)
        probe = subprocess.run([vendored] + MODERN_ARGS, input=b"",
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=30)
        check("modern flags accepted (no 'not understood' error)",
              b"not understood" not in probe.stdout
              and b"not understood" not in (probe.stderr or b""))

        # THE acceptance test: SRT bytes on stdout BEFORE EOF, from an
        # open growing stdin (0.88 provably could not do this with the
        # modern flag form — it rejected --stdin outright, rc=4).
        if os.path.isfile(RECORDING):
            v_args = ccx_args(bnd or vendored)
            first_at, data, alive = stream_probe(vendored, v_args,
                                                 RECORDING)
            print(f"    (first SRT output after {first_at} bytes fed; "
                  f"{len(data)} bytes on stdout; stdin-open "
                  f"alive={alive})")
            check("bundled CCX streams SRT before EOF (growing stdin)",
                  first_at is not None and b"-->" in data and alive)

            # The user-visible contract: a zero-install machine gets
            # live captions through CCSource on the bundled binary.
            zero_install_engage(vendored)
        else:
            check("local test recording present for streaming checks",
                  False)

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
