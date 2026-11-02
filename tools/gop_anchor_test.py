"""Case B verification: is a keyframe-anchored set_time both FAST (no 2 s
blackout) and CLEAN (no smear)? Writes plain text to stdout; run per case.

Run:  .venv\\Scripts\\python.exe tools\\gop_anchor_test.py <seek_seconds>
"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

src = open(os.path.join(HERE, "gop_pixel_probe.py")).read()
src = src.replace('if __name__ == "__main__":', 'if False:')
gpp = type(sys)("gpp")
gpp.__file__ = os.path.abspath(__file__)
exec(compile(src, "gop_pixel_probe", "exec"), gpp.__dict__)

SEEK = float(sys.argv[1]) if len(sys.argv) > 1 else 30.08


def prog(msg):
    print("[prog] %s" % msg, flush=True)


CACHE = os.path.join(ROOT, "gop_refs.npz")
if os.path.exists(CACHE):
    z = np.load(CACHE, allow_pickle=True)
    refs = {float(k): v for k, v in z.items()}
    prog("refs from cache: %d" % len(refs))
else:
    refs = gpp.ref_frames(29.0, 8.0)
    np.savez(CACHE, **{("%f" % k): v for k, v in refs.items()})
    prog("refs built: %d" % len(refs))
rkeys = sorted(refs)

prog("building player")
g = gpp.Grab(["--ignore-config", "--no-audio", "--vout=vmem",
              "--file-caching=1000"])
prog("play")
g.play()
time.sleep(4.0)
n0 = len(g.frames)
prog("baseline frames: %d" % n0)
t0 = time.monotonic()
gpp.lib.libvlc_media_player_set_time(g.mp, int(SEEK * 1000))
prog("seek issued")
while time.monotonic() - t0 < 4.0:
    time.sleep(0.05)
with g._lk:
    got = list(g.frames[n0:])
wall0 = t0
prog("captured %d; closing" % len(got))
g.close()
prog("closed")

print("### anchored set_time(%.2f): %d frames" % (SEEK, len(got)), flush=True)
for w, vt, arr in got:
    ct = vt / 1000.0
    near = [r for r in rkeys if abs(r - ct) <= 0.12]
    if not near:
        continue
    _bt, bd = min(((r, gpp.mad(arr, refs[r])) for r in near),
                  key=lambda p: p[1])
    print("  +%.2fs ct=%6.2f bestMAD=%6.2f%s"
          % (w - wall0, ct, bd, "  <== BAD" if bd > 25 else ""), flush=True)
