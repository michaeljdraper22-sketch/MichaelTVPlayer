# -*- coding: utf-8 -*-
"""Extract compiled modules from the PyInstaller exe and check whether the
popup-overhaul code is really in there (vs a stale cached Analysis).

Run:  .venv\\Scripts\\python.exe tools\\extract_exe.py
"""
import marshal
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".venv", "Lib", "site-packages"))

from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader  # noqa: E402

EXE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "dist", "MichaelTV.exe")
PYZ = os.path.join(os.path.dirname(EXE), "..", "build", "MichaelTVPlayer",
                   "PYZ-00.pyz")

# PyInstaller 6: the PYZ is embedded in the exe; easiest reliable source is
# the build dir's own archive if present, else parse the exe's CArchive.
from PyInstaller.archive.readers import CArchiveReader  # noqa: E402

r = CArchiveReader(EXE)
print("CArchive entries:", len(r.toc))
pyz_name, pyz_data = None, None
for name, item in r.toc.items():
    if name.endswith(".pyz") or item[3] == "z":
        pass
# find the PYZ payload
import PyInstaller.archive.readers as readers  # noqa: E402

pyz = None
for cand in ("PYZ-00.pyz",):
    try:
        pyz = r.extract(cand)
        print("extracted", cand, len(pyz), "bytes")
        break
    except Exception as e:
        print("no", cand, ":", e)
if pyz is None:
    # search toc for the pyz
    for name in r.toc:
        if "pyz" in name.lower():
            print("toc has:", name)
            pyz = r.extract(name)
            break

tmp = os.path.join(os.path.dirname(EXE), "_probe_pyz.tmp")
with open(tmp, "wb") as f:
    f.write(pyz)
z = ZlibArchiveReader(tmp)
names = [n for n in z.toc]
print("pyz modules:", len(names))

for mod in ("src.ui.player_view", "src.ui.track_panel",
            "src.ui.main_window", "src.ui.browsers"):
    try:
        data = z.extract(mod)
        code = data if not isinstance(data, (bytes, bytearray)) \
            else marshal.loads(data)
        src = None
        # look for the overhaul markers in the constants
        blob = repr(code.co_consts)
        import dis  # noqa: F401
        def walk(c, depth=0, out=None):
            if out is None:
                out = []
            out.append(c)
            for k in c.co_consts:
                if hasattr(k, "co_consts"):
                    walk(k, depth + 1, out)
            return out
        codes = walk(code)
        names_all = set()
        for c in codes:
            names_all.update(c.co_names)
        strs = set()
        for c in codes:
            for k in c.co_consts:
                if isinstance(k, str):
                    strs.add(k)
        print()
        print(f"== {mod} ==")
        for marker in ("_ctl_panel", "TrackPanel", "_open_ctl_panel",
                       "_ctl_menu", "aboutToHide", "set_rows", "CatchupRelay",
                       "CatchupBrowser", "win_picked", "_OutsideCloser",
                       "close_panel", "popup"):
            m = marker in names_all or marker in strs
            print(f"   {marker:20s}: {'PRESENT' if m else '-'}")
    except Exception as e:
        print(f"== {mod} == FAILED: {e!r}")

os.remove(tmp)
