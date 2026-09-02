# -*- coding: utf-8 -*-
"""Offscreen probe: the redesigned Stremio-handoff dialog.

v1.5.16-era user report: "I don't love the size (it's too big) or design
of the stremio handoff settings menu." Root cause measured: the old
layout's natural sizeHint was 6261 logical px wide (a checkbox plus
labels carrying whole paragraphs, none of them wrapping), which Windows
clamped to the full screen width (1280 logical px at this machine's
300% scaling) — a screen-wide wall of prose. The redesign: two group
boxes, one short line per control, prose in tooltips, one action button
per setup row, width pinned 560-720.

No window, no focus, no audio: WA_DontShowOnScreen + grab() for the
render. Environment-touching modules (streampatch, fileassoc,
watchfolder) are monkeypatched — no registry reads, no watcher, no
writes outside a temp config.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import fileassoc, streampatch, watchfolder  # noqa: E402
from src.config import DEFAULTS, Config              # noqa: E402
from src.ui.stremio_dialog import StremioDialog      # noqa: E402

from PyQt5 import QtCore, QtWidgets                 # noqa: E402

fails = [0]


def check(name, cond, extra=""):
    print(("  ok   " if cond else "FAIL ") + name
          + ("" if cond or not extra else "  [%s]" % extra))
    if not cond:
        fails[0] += 1


# ---- fake the three environment-touching modules ----------------------
PATCH_STATE = {"status": {"found": True, "patched": True, "backup": True,
                          "titled": True}}
FA_STATE = {"is_default": False}
DL_DIR = r"C:\Users\tv\Downloads"

streampatch.status = lambda: dict(PATCH_STATE["status"])
streampatch.patch = lambda: PATCH_STATE["status"].update(patched=True) or True
streampatch.restore = lambda: PATCH_STATE["status"].update(
    patched=False) or True
fileassoc.is_default = lambda: FA_STATE["is_default"]
fileassoc.register = lambda: True
fileassoc.try_set_default = lambda: FA_STATE.update(is_default=True)
fileassoc.PROGID = "MTP.Fake.ProgID"    # _registered() finds nothing
watchfolder.downloads_dir = lambda: DL_DIR


def make_dlg(cfg, parent=None):
    dlg = StremioDialog(cfg, parent)
    dlg.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    dlg.show()
    return dlg


app = QtWidgets.QApplication(sys.argv)

print("[1] size: bounded, never screen-wide (the complaint)")
cfg = Config(dict(DEFAULTS), None)
dlg = make_dlg(cfg)
w, h = dlg.width(), dlg.height()
check("width within the pinned 560-720", 560 <= w <= 720, w)
check("height sane (< 560 logical)", h < 560, h)
check("width cap property in place (the screen-wide bug was a "
      "sizeHint clamp)", dlg.maximumWidth() == 720, dlg.maximumWidth())

print("[2] watch status variants")
check("no watcher yet: Watches <folder> while MichaelTV runs",
      dlg.watch_lbl.text() == "Watches %s while MichaelTV runs" % DL_DIR,
      dlg.watch_lbl.text())
fake_win = QtWidgets.QWidget()
fake_win._downloads_watcher = object()
dlg2 = make_dlg(cfg, fake_win)
check("watcher running: green check names the folder",
      dlg2.watch_lbl.text().startswith("\u2713")
      and DL_DIR in dlg2.watch_lbl.text(), dlg2.watch_lbl.text())
dlg2.watch_chk.setChecked(False)
check("toggle off -> Off line, no check",
      dlg2.watch_lbl.text().startswith("Off"),
      dlg2.watch_lbl.text())

print("[3] patch row: one toggle button, three states")
check("patched: green status names MichaelTV",
      dlg.patch_lbl.text().startswith("\u2713")
      and "MichaelTV" in dlg.patch_lbl.text(), dlg.patch_lbl.text())
check("patched: button offers Restore VLC",
      dlg.btn_patch.text() == "Restore VLC", dlg.btn_patch.text())
dlg.btn_patch.click()
check("click Restore -> restore() ran, row flips",
      not PATCH_STATE["status"]["patched"]
      and not dlg.patch_lbl.text().startswith("\u2713")
      and dlg.btn_patch.text() == "Redirect to MichaelTV",
      dlg.patch_lbl.text() + " | " + dlg.btn_patch.text())
dlg.btn_patch.click()
check("click Redirect -> patch() ran, row flips back",
      PATCH_STATE["status"]["patched"]
      and dlg.btn_patch.text() == "Restore VLC")
PATCH_STATE["status"] = {"found": False, "patched": False, "backup": False,
                         "titled": False}
dlg._refresh_patch_status()
check("server.js missing: neutral status, button disabled",
      dlg.patch_lbl.text() == "Stremio server not found"
      and not dlg.btn_patch.isEnabled(),
      dlg.patch_lbl.text())
PATCH_STATE["status"] = {"found": True, "patched": False, "backup": True,
                         "titled": False}
dlg._refresh_patch_status()
check("partly set up: says so, redirect offered",
      "partly set up" in dlg.patch_lbl.text()
      and dlg.btn_patch.text() == "Redirect to MichaelTV",
      dlg.patch_lbl.text())

print("[4] .m3u default row: healthy state hides the button")
FA_STATE["is_default"] = False
dlg._refresh_status()
check("not default: status + Make default visible",
      dlg.status_lbl.text() == "Not registered"
      and dlg.btn_default.isVisibleTo(dlg), dlg.status_lbl.text())
FA_STATE["is_default"] = True
dlg._refresh_status()
check("default: green check, button hidden",
      dlg.status_lbl.text().startswith("\u2713")
      and not dlg.btn_default.isVisibleTo(dlg),
      dlg.status_lbl.text())

print("[5] round-trip: widgets -> config -> disk (probe_stremio parity)")
tmp = Path(tempfile.mkdtemp(prefix="mtp_probe_sdlg_")) / "settings.json"
dcfg = Config(dict(DEFAULTS), tmp)
A1, A2 = ("https://a1.example/manifest.json",
          "https://a2.example/manifest.json")
dd = make_dlg(dcfg)
check("resolution combo starts at config 1080p",
      dd.res_combo.currentData() == "1080", dd.res_combo.currentData())
check("size combo starts at the config's 25 GB",
      dd.size_combo.currentData() == 25, dd.size_combo.currentData())
dd.res_combo.setCurrentIndex(dd.res_combo.findData("match"))
dd.size_combo.setCurrentIndex(dd.size_combo.findData(50))
dd.addons_edit.setPlainText(A2 + "\n" + A1)
dd.srv_edit.setText("http://127.0.0.1:9999")
dd.watch_chk.setChecked(False)
dd.accept()
check("accept persists match mode",
      dcfg.stremio_resolution_pref == "match",
      dcfg.stremio_resolution_pref)
check("accept persists the 50 GB demote threshold",
      dcfg.stremio_size_demote_gb == 50, dcfg.stremio_size_demote_gb)
check("accept persists addon order (line 1 preferred; manifest.json"
      " suffixes normalize away by design)",
      dcfg.stremio_addons == ["https://a2.example",
                              "https://a1.example"], dcfg.stremio_addons)
check("accept persists server + watcher off",
      dcfg.stremio_server == "http://127.0.0.1:9999"
      and dcfg.stremio_watch_downloads is False)
disk = json.loads(tmp.read_text(encoding="utf-8"))
check("preferences land on disk",
      disk.get("stremio_resolution_pref") == "match"
      and disk.get("stremio_size_demote_gb") == 50
      and disk.get("stremio_addons") == ["https://a2.example",
                                         "https://a1.example"])
data = dict(DEFAULTS)
data["stremio_size_demote_gb"] = 33     # a hand-edited value: keep it
dd2 = StremioDialog(Config(data, None), None)
check("hand-edited 33 GB gets a visible combo entry",
      dd2.size_combo.currentData() == 33
      and dd2.size_combo.currentText() == "33 GB",
      dd2.size_combo.currentText())

print("[6] render grab (canonical healthy state)")
PATCH_STATE["status"] = {"found": True, "patched": True, "backup": True,
                         "titled": True}
FA_STATE["is_default"] = True
dlg._refresh_patch_status()
dlg._refresh_status()
out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "_stremio_dialog_new.png")
ok = dlg.grab().save(out)
check("render saved", ok and os.path.isfile(out))
check("render is small (not screen-wide)",
      dlg.grab().width() <= 720 * 3)

print()
if fails[0]:
    print("FAILURES: %d" % fails[0])
    sys.exit(1)
print("ALL PASS")
