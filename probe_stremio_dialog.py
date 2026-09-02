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
writes outside a temp config. Addon discovery (stremio_profile) is
monkeypatched too, except one guarded live leg that runs against the
real Stremio profile ONLY when this machine actually has one.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import fileassoc, streampatch, stremio_profile  # noqa: E402
from src import watchfolder                              # noqa: E402
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

print("[6] fetch_online_subs checkbox: default on, full round-trip")
sd = make_dlg(Config(dict(DEFAULTS), None))
check("checkbox exists in the streams group, defaults to on",
      sd.subs_chk.isChecked(), sd.subs_chk.isChecked())
check("tooltip names the addon and the live-TV exemption",
      "OpenSubtitles" in sd.subs_chk.toolTip()
      and "Live TV" in sd.subs_chk.toolTip(),
      sd.subs_chk.toolTip()[:60])
stmp = Path(tempfile.mkdtemp(prefix="mtp_probe_sdlg_")) / "settings.json"
scfg = Config(dict(DEFAULTS), stmp)
sd2 = make_dlg(scfg)
sd2.subs_chk.setChecked(False)
sd2.accept()
disk = json.loads(stmp.read_text(encoding="utf-8"))
check("unchecked accept persists fetch_online_subs=false on disk",
      disk.get("fetch_online_subs") is False, disk.get("fetch_online_subs"))
data = dict(DEFAULTS)
data.update(disk)
sd3 = make_dlg(Config(data, stmp))
check("reopen shows the box unchecked", not sd3.subs_chk.isChecked())

print("[7] import button: click behavior (discovery monkeypatched — "
      "stremio_dialog resolves it off the stremio_profile module at "
      "click time, so that attribute IS the import site)")
check("button present in the picks group, tooltip explains the trade",
      dlg.btn_import.text() == "Import from installed Stremio"
      and "priority order" in dlg.btn_import.toolTip()
      and "editable" in dlg.btn_import.toolTip(),
      dlg.btn_import.toolTip()[:60])
FAKE = [
    {"url": "https://addon.debridio.com/eyJwcm92aWRlciI6InByZW1pdW1pemUifQ",
     "name": "Debridio \u2014 Premiumize", "provider": "Premiumize",
     "manifest_name": "Debridio"},
    {"url": "https://torrentio.strem.fun/premiumize=PMKEY",
     "name": "Torrentio \u2014 Premiumize", "provider": "Premiumize",
     "manifest_name": "Torrentio"},
    {"url": "https://curl.example/addon", "name": "Curl addon",
     "provider": "", "manifest_name": ""},
    {"url": "https://torrentio.strem.fun/torbox=TBKEY",
     "name": "Torrentio \u2014 TorBox", "provider": "TorBox",
     "manifest_name": "Torrentio"},
]
# priority_sort contract: Torrentio family, then Debridio, then rest;
# TorBox before Premiumize within a family
EXPECTED = [FAKE[3]["url"], FAKE[1]["url"], FAKE[0]["url"], FAKE[2]["url"]]
orig_discover = stremio_profile.discover_stream_addons
stremio_profile.discover_stream_addons = \
    lambda: [dict(a) for a in FAKE]
di = make_dlg(Config(dict(DEFAULTS), None))
di.addons_edit.setPlainText("https://old.example/replace-me")
di.btn_import.click()
check("success: textarea replaced with the priority-ordered URLs",
      di.addons_edit.toPlainText().splitlines() == EXPECTED,
      di.addons_edit.toPlainText().splitlines())
check("success: green one-line status names the count + providers",
      di.import_lbl.text().startswith("\u2713")
      and "Imported 4 addons" in di.import_lbl.text()
      and "TorBox" in di.import_lbl.text()
      and di.import_lbl.text().count("\n") == 0,
      di.import_lbl.text())
stremio_profile.discover_stream_addons = lambda: []
di2 = make_dlg(Config(dict(DEFAULTS), None))
di2.addons_edit.setPlainText("https://keep.example/a")
di2.btn_import.click()
check("empty discovery: textarea untouched, gray status",
      di2.addons_edit.toPlainText() == "https://keep.example/a"
      and di2.import_lbl.text()
      and not di2.import_lbl.text().startswith("\u2713"),
      di2.import_lbl.text())


def _boom():
    raise RuntimeError("profile unreadable")


stremio_profile.discover_stream_addons = _boom
di3 = make_dlg(Config(dict(DEFAULTS), None))
di3.addons_edit.setPlainText("https://keep.example/b")
di3.btn_import.click()
check("raising discovery: textarea untouched, gray status",
      di3.addons_edit.toPlainText() == "https://keep.example/b"
      and di3.import_lbl.text()
      and not di3.import_lbl.text().startswith("\u2713"),
      di3.import_lbl.text())
stremio_profile.discover_stream_addons = orig_discover

print("[8] live discovery (guarded: only when this PC has a Stremio "
      "WebView2 profile)")
if not stremio_profile._leveldb_dirs():
    print("  skip  no Stremio leveldb on this machine")
else:
    live = stremio_profile.discover_stream_addons()
    check("live: profile read, >= 1 stream addon",
          isinstance(live, list) and len(live) >= 1,
          "None" if live is None else len(live or []))
    check("live: entries carry a https url + a name",
          all(str(a.get("url") or "").startswith("https://")
              and str(a.get("name") or "") for a in live or []))
    ordered_urls = [a["url"] for a in stremio_profile.priority_sort(live)]
    check("live: priority order is a permutation of discovery",
          sorted(ordered_urls) == sorted(a["url"] for a in live))
    live_cfg = Config(dict(DEFAULTS), None)
    live_cfg.stremio_addons = ordered_urls
    check("live: URLs survive the config setter verbatim (textarea "
          "format parity)", live_cfg.stremio_addons == ordered_urls,
          live_cfg.stremio_addons)

print("[9] render grab (canonical healthy state)")
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
