r"""Verify MichaelTV's VLC isolation (run with the project venv python).

Checks, in order:
1. main._setup_bundled_vlc() points python-vlc at the private vlc\ runtime.
2. libvlc.dll actually loads FROM the private copy (real module path).
3. A vlc.Instance can be created with the app's args (incl. --no-config).
4. %APPDATA%\\vlc\\vlcrc is byte-identical before/after (no reads->writes).
"""

import ctypes
import filecmp
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402  (safe: app code is guarded by __main__)

VLCRC = os.path.join(os.environ["APPDATA"], "vlc", "vlcrc")

main._setup_bundled_vlc()
import vlc  # noqa: E402

# --- 1+2: which libvlc.dll is actually mapped into this process? ---
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
k32.GetModuleHandleW.restype = ctypes.c_void_p
k32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
k32.GetModuleFileNameW.restype = ctypes.c_uint32
handle = k32.GetModuleHandleW("libvlc.dll")
if not handle:
    print("FAIL: libvlc.dll is not loaded")
    sys.exit(1)
buf = ctypes.create_unicode_buffer(1024)
k32.GetModuleFileNameW(handle, buf, 1024)
loaded = os.path.normcase(os.path.normpath(buf.value or vlc.dll._name))
private = os.path.normcase(os.path.normpath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "vlc", "libvlc.dll")))
print(f"loaded libvlc.dll : {loaded}")
print(f"private copy      : {private}")
if loaded != private:
    print("FAIL: libvlc.dll did NOT load from the private copy")
    sys.exit(1)
print("PASS: private VLC runtime in use")

# --- 3: instance creation with the app's arg list ---
from src.player import subtitle_instance_args  # noqa: E402
args = ["--ignore-config", "--no-video-title-show", "--no-stats",
        "--network-caching=1500", "--live-caching=1500",
        "--file-caching=1000", "--disc-caching=1000",
        "--avcodec-skiploopfilter=1"] + [str(a) for a in subtitle_instance_args({})]
backup = VLCRC + ".iso-test-backup"
if os.path.exists(VLCRC):
    import shutil
    shutil.copy2(VLCRC, backup)
inst = vlc.Instance(args)
if inst is None:
    print("FAIL: vlc.Instance(args) returned None")
    sys.exit(1)
print(f"PASS: vlc.Instance created (libvlc {vlc.libvlc_get_version().decode()})")
mp = inst.media_player_new()
print("PASS: media_player created")
del mp, inst

# --- 4: vlcrc untouched ---
if os.path.exists(VLCRC):
    same = filecmp.cmp(VLCRC, backup, shallow=False)
    os.remove(backup)
    if not same:
        print("FAIL: vlcrc was modified during the test")
        sys.exit(1)
    print("PASS: %APPDATA%\\vlc\\vlcrc untouched")

print("\nALL ISOLATION CHECKS PASSED")
