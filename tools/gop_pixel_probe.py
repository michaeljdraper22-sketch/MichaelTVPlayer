"""Pixel-level probe: how does VLC 3.0.23's picture behave after a mid-GOP
seek on the provider's TS, and after a keyframe-anchored seek / rate change?

Headless (vmem vout, no window, no audio). Compares captured frames against
a clean ffmpeg reference decode at the same content times.

Run:  .venv\\Scripts\\python.exe tools\\gop_pixel_probe.py
"""
import os
import sys
import time
import ctypes
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VLC_DIR = os.path.join(ROOT, "vlc")
os.environ["VLC_PLUGIN_PATH"] = os.path.join(VLC_DIR, "plugins")
os.add_dll_directory(VLC_DIR)

import numpy as np  # noqa: E402

lib = ctypes.CDLL(os.path.join(VLC_DIR, "libvlc.dll"))
REC = os.path.join(ROOT, "TV Recordings",
                   "US_NFL_NETWORK_HD_20260827_191626.ts")
W, H = 320, 180

lib.libvlc_new.restype = ctypes.c_void_p
lib.libvlc_new.argtypes = [ctypes.c_int,
                           ctypes.POINTER(ctypes.c_char_p)]
lib.libvlc_media_new_path.restype = ctypes.c_void_p
lib.libvlc_media_new_path.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lib.libvlc_media_player_new.restype = ctypes.c_void_p
lib.libvlc_media_player_new.argtypes = [ctypes.c_void_p]
lib.libvlc_media_player_set_media.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
lib.libvlc_media_player_play.argtypes = [ctypes.c_void_p]
lib.libvlc_media_player_stop.argtypes = [ctypes.c_void_p]
lib.libvlc_media_player_release.argtypes = [ctypes.c_void_p]
lib.libvlc_media_release.argtypes = [ctypes.c_void_p]
lib.libvlc_media_add_option.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lib.libvlc_media_player_get_time.restype = ctypes.c_int64
lib.libvlc_media_player_get_time.argtypes = [ctypes.c_void_p]
lib.libvlc_media_player_set_time.argtypes = [ctypes.c_void_p, ctypes.c_int64]
lib.libvlc_media_player_set_rate.argtypes = [ctypes.c_void_p, ctypes.c_float]
lib.libvlc_media_player_is_playing.restype = ctypes.c_int
lib.libvlc_media_player_is_playing.argtypes = [ctypes.c_void_p]
lib.libvlc_video_set_format.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                        ctypes.c_uint, ctypes.c_uint,
                                        ctypes.c_uint]
lib.libvlc_video_set_callbacks.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_void_p]

LCKF = ctypes.CFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p))
DSPF = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)


class Grab:
    def __init__(self, argv, media_opts=()):
        args = [a.encode() for a in argv]
        arr = (ctypes.c_char_p * len(args))(*args)
        self.inst = lib.libvlc_new(len(args), arr)
        self.mp = lib.libvlc_media_player_new(self.inst)
        self.media = lib.libvlc_media_new_path(self.inst, REC.encode())
        for o in media_opts:
            lib.libvlc_media_add_option(self.media, o.encode())
        lib.libvlc_media_player_set_media(self.mp, self.media)

        self.frames = []                 # (wall, vlc_time_ms, np copy)
        self._lk = threading.Lock()
        self.buf = ctypes.create_string_buffer(W * H * 4)
        mp_h = ctypes.c_void_p(self.mp)

        def lock_cb(ctx, pixels):
            pixels[0] = ctypes.cast(self.buf, ctypes.c_void_p).value
            return None

        def display_cb(ctx, picture):
            try:
                arr = np.frombuffer(self.buf.raw[:W * H * 4],
                                    dtype=np.uint8) \
                    .reshape(H, W, 4)[:, :, :3].copy()
                t = lib.libvlc_media_player_get_time(mp_h)
                with self._lk:
                    self.frames.append((time.monotonic(), t, arr))
            except Exception:
                pass

        self._cbs = (LCKF(lock_cb), DSPF(display_cb))
        lib.libvlc_video_set_format(self.mp, b"RV32", W, H, W * 4)
        lib.libvlc_video_set_callbacks(
            self.mp,
            ctypes.cast(self._cbs[0], ctypes.c_void_p),
            ctypes.c_void_p(0),          # no unlock callback
            ctypes.cast(self._cbs[1], ctypes.c_void_p),
            None)

    def play(self):
        lib.libvlc_media_player_play(self.mp)

    def close(self):
        try:
            lib.libvlc_media_player_stop(self.mp)
        except Exception:
            pass
        lib.libvlc_media_player_release(self.mp)
        lib.libvlc_media_release(self.media)
        # keep inst; libvlc instances are refcounted at 0 in 3.x


def case(title, seek_t=None, rate=None, hw="any", hold=5.0, collect=5.0):
    g = Grab(["--ignore-config", "--no-audio", "--vout=vmem",
              "--avcodec-hw=" + hw, "--file-caching=1000"])
    g.play()
    time.sleep(hold)
    n0 = len(g.frames)
    t0 = time.monotonic()
    if seek_t is not None:
        lib.libvlc_media_player_set_time(g.mp, int(seek_t * 1000))
    if rate is not None:
        lib.libvlc_media_player_set_rate(g.mp, ctypes.c_float(rate))
    while time.monotonic() - t0 < collect:
        time.sleep(0.05)
    with g._lk:
        got = list(g.frames[n0:])
    wall0 = got[0][0] if got else t0
    g.close()
    print("\n### %s (seek=%s rate=%s hw=%s): %d frames"
          % (title, seek_t, rate, hw, len(got)), flush=True)
    return got, wall0


def ref_frames(t_start, dur):
    """clean ffmpeg decode of [t_start, t_start+dur) -> {content_s: array}"""
    import subprocess
    import tempfile
    import glob
    import shutil
    from PyQt5.QtGui import QImage
    ff = (r"C:\Users\micha\AppData\Local\Microsoft\WinGet\Packages"
          r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
          r"\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe")
    out = tempfile.mkdtemp(prefix="gopref_")
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-ss",
                    "%.3f" % t_start, "-i", REC, "-t", "%.3f" % dur,
                    "-vf", "fps=30,scale=%d:%d" % (W, H),
                    os.path.join(out, "f%03d.png")], check=True)
    frames = {}
    for pth in sorted(glob.glob(os.path.join(out, "f*.png"))):
        img = QImage(pth).convertToFormat(QImage.Format_RGB32)
        ptr = img.constBits()
        ptr.setsize(img.byteCount())
        arr = np.frombuffer(ptr, dtype=np.uint8) \
            .reshape(img.height(), img.bytesPerLine())[:, :W * 4] \
            .reshape(img.height(), W, 4)
        idx = int(os.path.basename(pth)[1:4]) - 1
        frames[t_start + idx / 30.0] = arr[:, :, :3].copy()
    shutil.rmtree(out, ignore_errors=True)
    return frames


def mad(a, b):
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def damage_report(got, wall0, refs, save=None):
    """MAD against the BEST-matching ref within +-2 frames (kills motion
    misalignment noise); optionally save PNGs of vlc frame + best ref."""
    rkeys = sorted(refs)
    saved = 0
    for w, vt, arr in got:
        ct = vt / 1000.0
        near = [r for r in rkeys if abs(r - ct) <= 0.10]
        if not near:
            continue
        best_t, best_d = min(
            ((r, mad(arr, refs[r])) for r in near), key=lambda p: p[1])
        print("  +%.2fs ct=%6.2f  bestMAD=%6.2f (ref %6.2f) %s"
              % (w - wall0, ct, best_d, best_t,
                 "  <== DAMAGED" if best_d > 25 else ""), flush=True)
        if save and saved < 12 and best_d > 25:
            from PyQt5.QtGui import QImage
            qi = QImage(arr.data, W, H, W * 4, QImage.Format_RGB32)
            qi.save(os.path.join(save, "dmg_vlc_%02d_ct%05.2f.png"
                                 % (saved, ct)))
            qi2 = QImage(refs[best_t].data, W, H, W * 4, QImage.Format_RGB32)
            qi2.save(os.path.join(save, "dmg_ref_%02d_ct%05.2f.png"
                                  % (saved, ct)))
            saved += 1


if __name__ == "__main__":
    import faulthandler
    faulthandler.enable()
    IDR = 30.463          # keyframe before the 31.0 probe point
    refs = ref_frames(29.0, 8.0)
    print("refs: %d frames" % len(refs), flush=True)
    SAVE = os.path.join(ROOT, "gop_probe_png")
    os.makedirs(SAVE, exist_ok=True)

    print("== CASE A: set_time(31.0) mid-GOP ==", flush=True)
    got, w0 = case("A", seek_t=31.0)
    damage_report(got, w0, refs, save=SAVE)

    print("\n== CASE B: set_time(30.1) keyframe-anchored ==", flush=True)
    got, w0 = case("B", seek_t=IDR - 0.35)
    damage_report(got, w0, refs, save=SAVE)

    print("\n== CASE D: rate 0.25x from clean play, NO seek ==", flush=True)
    got, w0 = case("D", rate=0.25, collect=6.0)
    prev = None
    for w, vt, arr in got[:45]:
        sig = float(arr.mean())
        ds = "" if prev is None else "%+7.2f" % (sig - prev)
        prev = sig
        print("  +%.2fs ct=%6.2f mean=%7.2f %s"
              % (w - w0, vt / 1000.0, sig, ds), flush=True)
    print("\nDONE", flush=True)
