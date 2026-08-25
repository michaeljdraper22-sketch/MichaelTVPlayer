# -*- coding: utf-8 -*-
"""Offscreen regression test for the Catch-Up feature:

[1] XtreamClient.timeshift_url — the legacy streaming endpoint format this
    panel accepts (probed 2026-08-24), UTC start formatting, duration
    ceil + minimum.
[2] decode_epg_text — base64 EPG titles decode; plain text survives.
[3] CatchupBrowser — only tv_archive channels list; playable fields.
[4] CatchupPickerDialog — past-programs-only within the archive window,
    day grouping, base64 titles, in-progress marking, playable building.
[5] Config — download_folder setting; last_tab migration for the new tab.
[6] PlayerView — the gold < > window markers: engage/nudge/drag/click
    clamping, nearest-marker clicks, button swap by kind, confirm ->
    window download URL math, Esc/cancel, arrow-key routing.

Run:  .venv\\Scripts\\python.exe test_catchup.py   (sets QT_QPA_PLATFORM itself)
"""
import base64
import inspect
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.models import EpgEntry  # noqa: E402
from src.ui import player_view as pv_mod  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402
from src.ui.browsers import (  # noqa: E402
    CatchupBrowser, CatchupPickerDialog)
from src.xtream import XtreamClient, decode_epg_text  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    return cond()


def b64(s):
    return base64.b64encode(s.encode()).decode()


class StubEpgClient:
    """Offline XtreamClient lookalike: no network, canned everything."""

    def __init__(self, entries):
        self.entries = entries
        self.timeshift_calls = []       # (stream_id, utc_start, dur_min)
        self.base = "http://stub:8080"
        self.username = "stubuser"
        self.password = "stubpass"

    def live_categories(self):
        return [{"category_id": 1, "category_name": "News"},
                {"category_id": 2, "category_name": "Sports"}]

    def live_streams(self, cat_id):
        return [
            {"name": "Archive Chan", "stream_id": 501, "tv_archive": 1,
             "tv_archive_duration": "3", "stream_icon": "i1",
             "category_id": cat_id or 1},
            {"name": "No Archive", "stream_id": 502, "tv_archive": 0,
             "category_id": cat_id or 1},
            {"name": "Archive 7d", "stream_id": 503, "tv_archive": "1",
             "tv_archive_duration": "7", "category_id": cat_id or 1},
        ]

    def epg_table(self, stream_id):
        return self.entries

    def timeshift_url(self, stream_id, utc_start, duration_min):
        self.timeshift_calls.append((stream_id, int(utc_start),
                                     int(duration_min)))
        return XtreamClient.timeshift_url(self, stream_id, utc_start,
                                          duration_min)


def make_epg():
    """EPG around 'now': past / in-progress / future / beyond-archive."""
    now = int(time.time())

    def e(title, start, stop):
        return EpgEntry(title=b64(title), description=b64("desc " + title),
                        start="", end="",
                        start_timestamp=str(start), stop_timestamp=str(stop))
    return [
        e("Ancient Show", now - 4 * 86400, now - 4 * 86400 + 1800),  # too old
        e("Yesterday News", now - 90000, now - 90000 + 3600),
        e("Morning Show", now - 7200, now - 7200 + 3600),
        e("Airing Now", now - 900, now + 900),                      # partial
        e("Future Show", now + 7200, now + 7200 + 1800),            # future
    ]


app = QtWidgets.QApplication(sys.argv)


def fresh_cfg():
    return Config({}, None)


def main():
    print("[1] timeshift_url format (legacy streaming endpoint, UTC)")
    xc = XtreamClient("http://host:8080/", "us er", "pa:ss")
    # 2026-08-24 23:40:00 UTC == 1787547600-ish; use a fixed epoch:
    epoch = 1756076400          # 2025-08-24 23:00:00 UTC
    url = xc.timeshift_url(497001, epoch, 30)
    fmt = datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%d:%H-%M")
    check("uses the streaming/timeshift.php endpoint",
          "/streaming/timeshift.php?" in url)
    check("credentials quoted + stream + start + duration + extension",
          "username=us%20er" in url and "password=pa%3Ass" in url
          and "stream=497001" in url and f"start={fmt}" in url
          and "duration=30" in url and "extension=ts" in url)
    check("duration floors at 1 minute",
          "duration=1" in xc.timeshift_url(1, epoch, 0))
    check("duration ceils (90 s -> 2 min)",
          "duration=2" in xc.timeshift_url(1, epoch, 1.5))
    check("start is UTC not local",
          datetime.now(timezone.utc).utcoffset() is None
          or f"start={fmt}" in xc.timeshift_url(1, epoch, 5))

    print("[2] decode_epg_text")
    check("base64 decodes", decode_epg_text(b64("Have I Got News")) ==
          "Have I Got News")
    check("plain text survives", decode_epg_text("Plain News") == "Plain News")
    check("empty stays empty", decode_epg_text("") == "")

    print("[3] CatchupBrowser filters archive channels")
    cfg = fresh_cfg()
    # a LIVE-style country filter must NOT empty the Catch-Up tab (the
    # archive channels cluster outside the user's Live picks)
    cfg.data["countries_configured"] = True
    cfg.data["enabled_countries"] = ["US"]
    client = StubEpgClient([])
    tab = CatchupBrowser(cfg, client, "catchup")
    ok = wait_until(lambda: tab.list.count() >= 1, 5.0)
    check("lists only the archive-capable channels", ok and tab.list.count() == 2)
    names = [tab.list.item(i).text() for i in range(tab.list.count())]
    check("archive depth shown in the row",
          any("(3d)" in n for n in names) and any("(7d)" in n for n in names))
    playable = tab.make_playable(
        {"name": "Archive Chan", "stream_id": 501, "tv_archive": 1,
         "tv_archive_duration": "3", "stream_icon": "i1"})
    check("channel playable is a catchup_channel meta",
          playable["kind"] == "catchup_channel"
          and playable["stream_id"] == 501
          and playable["archive_days"] == "3"
          and playable["fav_key"] == "catchup:501")

    print("[4] CatchupPickerDialog: grouping / filtering / playable")
    now = int(time.time())
    dlg = CatchupPickerDialog(StubEpgClient(make_epg()), cfg,
                              {"name": "Archive Chan", "stream_id": 501,
                               "tv_archive_duration": "3"})
    ok = wait_until(lambda: not dlg.status.text().startswith("Loading"), 5.0)
    check("epg loaded", ok)
    # 3 keepers: Yesterday News, Morning Show, Airing Now
    check(f"exactly the 3 in-window programs kept "
          f"({dlg.tree.topLevelItemCount()} days)",
          sum(dlg.tree.topLevelItem(i).childCount()
              for i in range(dlg.tree.topLevelItemCount())) == 3)
    all_rows = []
    for i in range(dlg.tree.topLevelItemCount()):
        root = dlg.tree.topLevelItem(i)
        for j in range(root.childCount()):
            all_rows.append(root.child(j))
    titles = [r.text(1) for r in all_rows]
    check("base64 titles decoded", "Airing Now" in titles
          and "Morning Show" in titles and "Yesterday News" in titles)
    check("no raw base64 left", not any(t.startswith("SGF") or "=" in t
                                        for t in titles))
    airing = next(r for r in all_rows if r.text(1) == "Airing Now")
    check("in-progress program marked", "in progress" in airing.text(2))
    prog = airing.data(0, QtCore.Qt.UserRole)
    pl = dlg._make_playable(prog)
    check("program playable kind/url/utc fields",
          pl["kind"] == "catchup"
          and pl["stream_id"] == 501
          and pl["utc_start"] == now - 900
          and pl["utc_end"] == now + 900
          and "streaming/timeshift.php" in pl["url"]
          and pl["fav_key"] == f"catchup:501:{now - 900}")
    dur_min = 30  # 1800 s program
    check("program URL asks for the full program",
          f"duration={dur_min}" in pl["url"])

    print("[5] Config: download_folder + last_tab migration")
    cfg5 = fresh_cfg()
    check("download_folder defaults empty", cfg5.download_folder == "")
    cfg5.download_folder = "D:/dl"
    cfg5.save()
    check("download_folder persists", cfg5.data["download_folder"] == "D:/dl")
    import src.config as cfg_mod
    from pathlib import Path
    tmp = tempfile.mkdtemp()
    orig_dd = cfg_mod._data_dir
    cfg_mod._data_dir = lambda: Path(tmp)
    try:
        (Path(tmp) / "settings.json").write_text(
            '{"last_tab": 4}', encoding="utf-8")
        mig = Config.load()
        check("last_tab 4 (Custom) migrates to 5", mig.last_tab == 5)
        (Path(tmp) / "settings.json").write_text(
            '{"last_tab": 1}', encoding="utf-8")
        mig = Config.load()
        check("last_tab 1 (Movies) unchanged", mig.last_tab == 1)
        (Path(tmp) / "settings.json").write_text(
            '{"last_tab": 3, "_catchup_tab_migrated": true}',
            encoding="utf-8")
        mig = Config.load()
        check("explicit marker skips migration", mig.last_tab == 3)
    finally:
        cfg_mod._data_dir = orig_dd

    print("[6] PlayerView: gold < > window markers + button swap")
    cfg6 = fresh_cfg()
    view = PlayerView(cfg6)
    view.resize(1280, 720)
    view.show()
    app.processEvents()

    # -- button swap by kind --
    view.current = {"kind": "live", "title": "L", "url": "http://x"}
    view._apply_button_visibility()
    check("live: REC visible, DL + WIN hidden",
          not view.btn_rec.isHidden() and view.btn_dl.isHidden()
          and view.btn_win.isHidden())
    view.current = {"kind": "vod", "title": "M", "url": "http://x"}
    view._apply_button_visibility()
    check("movie: DL visible, REC + WIN hidden",
          not view.btn_dl.isHidden() and view.btn_rec.isHidden()
          and view.btn_win.isHidden())
    view.current = {"kind": "catchup", "title": "C", "url": "http://x",
                    "stream_id": 501, "utc_start": 1756076400}
    view._apply_button_visibility()
    check("catchup: WIN visible, REC + DL hidden",
          not view.btn_win.isHidden() and view.btn_rec.isHidden()
          and view.btn_dl.isHidden())
    check("catchup counts as VOD for scrub/transport",
          view._is_vod() and view._is_catchup())

    # -- marker math: pixel <-> value round trip --
    view.slider.blockSignals(True)
    view.slider.setRange(0, 1800000)
    view.slider.blockSignals(False)
    view.slider.resize(400, 24)
    app.processEvents()
    _handle, _span = view._slider_metrics()
    _tol = int(1800000 / _span) + 2      # one pixel's worth of milliseconds
    for v in (0, 1, 900000, 1799999, 1800000):
        x = view._x_for_value(v)
        check(f"x<->value round trip at {v} ms (±1 px)",
              abs(view._value_for_x(int(round(x))) - v) <= _tol)

    # -- engage: markers spawn, < at current pos, > at end, < selected --
    view._vid_s = 300.0                     # watching at 5:00
    view._win_engage()
    app.processEvents()
    check("window mode engaged", view._win_sel is True)
    check("markers visible + slider in win mode",
          all(not m.isHidden() for m in view._win_markers.values())
          and view.slider._win_mode is True)
    check("< starts at current position, > at the end",
          view._win_start_ms == 300000 and view._win_end_ms == 1800000)
    check("start marker selected first",
          view._win_sel_side == "start"
          and view._win_markers["start"].selected)
    check("pill explains the interaction",
          "Download window" in view._dvr_status.text()
          and "Esc" in view._dvr_status.text())
    xs = view._win_markers["start"].x()
    xe = view._win_markers["end"].x()
    check("markers positioned left-to-right", xs < xe)

    # -- nudge clamping --
    view._win_nudge(-1000 * 400)            # way past 0
    check("< clamps at 0", view._win_start_ms == 0)
    view._win_select("end")
    view._win_nudge(-1000 * 4000)           # way past start
    check("> clamps at < + 1 s gap",
          view._win_end_ms == view._win_start_ms + pv_mod._WIN_GAP_MS)
    check("pill still live", view._dvr_status.isVisible())

    # -- nearest-marker click on the bar --
    view._on_win_slider_click(1700000)
    check("bar click moves the NEAREST marker (>)",
          view._win_end_ms == 1700000 and view._win_sel_side == "end")
    view._on_win_slider_click(650000)
    check("bar click nearer to < moves it",
          view._win_start_ms == 650000 and view._win_sel_side == "start")

    # -- drag via pixels --
    view._win_drag("end", int(round(view._x_for_value(1200000))))
    check("drag moves > to the dragged position",
          view._win_end_ms == 1200000)

    # -- arrow keys route through seek_or_nudge --
    view._win_select("start")               # click a marker, then arrows
    view.seek_or_nudge(10, 1)               # -> +1 s on THAT marker
    check("arrow nudge moves the selected marker 1 s",
          view._win_start_ms == 651000)
    view._win_cancel()
    check("cancel hides markers + leaves win mode",
          view._win_sel is False and not view.slider._win_mode
          and all(m.isHidden() for m in view._win_markers.values()))

    # -- confirm -> window download URL math --
    captured = {}

    class FakeDownloader(QtCore.QObject):
        progress = QtCore.pyqtSignal(int, int)
        finished = QtCore.pyqtSignal(bool, str)

        def start(self, url, path):
            captured["url"] = url
            captured["path"] = path
            self.finished.emit(True, path)

    orig_fd = pv_mod.FileDownloader
    pv_mod.FileDownloader = FakeDownloader
    tmpdir = tempfile.mkdtemp()
    cfg6.data["download_folder"] = tmpdir
    view.client = StubEpgClient([])
    try:
        view._vid_s = 0.0
        view._win_engage()
        view._win_drag("end", int(round(view._x_for_value(600000))))
        utc0 = 1756076400
        view.current = dict(view.current, utc_start=utc0)
        view._win_confirm()
        app.processEvents()
        check("confirm exits select mode", view._win_sel is False)
        check("download started exactly once",
              len(view.client.timeshift_calls) == 1)
        sid, ust, dur = (view.client.timeshift_calls[0] if
                         view.client.timeshift_calls else (0, 0, 0))
        check("window start maps to utc_start + marker ms",
              ust == utc0 and sid == 501)
        check("duration is ceil of the window (10 min for 600 s)",
              dur == 10)
        check("download lands in the download folder as .ts",
              captured.get("path", "").startswith(tmpdir)
              and captured["path"].endswith(".ts"))
        check("window download finished pill shows the file",
              view._dvr_status.text().startswith("Downloaded"))
        check("btn_win re-enabled after finish", view.btn_win.isEnabled())
    finally:
        pv_mod.FileDownloader = orig_fd

    # -- guards: engage without length, Esc path, reset in play_media/stop --
    view._win_cancel(silent=True)
    view.slider.blockSignals(True)
    view.slider.setRange(0, 0)
    view.slider.blockSignals(False)
    view._win_engage()
    check("no engage before the stream length is known",
          view._win_sel is False)
    src_pm = inspect.getsource(pv_mod.PlayerView.play_media)
    check("play_media resets a live window selection",
          "self._win_cancel(silent=True)" in src_pm)
    src_stop = inspect.getsource(pv_mod.PlayerView.stop)
    check("stop() resets a live window selection",
          "self._win_cancel(silent=True)" in src_stop)
    src_mw = inspect.getsource(__import__(
        "src.ui.main_window", fromlist=["MainWindow"]).MainWindow
        ._setup_shortcuts)
    check("Left/Right shortcuts route through seek_or_nudge",
          "seek_or_nudge" in src_mw)

    print("[7] CatchupRelay: range-header normalization (localhost proxy)")
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from src.catchup_relay import CatchupRelay
    payload = bytes(range(256)) * 64            # 16 KB fake "recording"

    class FakeProvider(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def do_GET(self):
            # mimics the real panel: range requests honored, but the
            # Accept-Ranges header is malformed (a byte span, not "bytes")
            rng = self.headers.get("Range") or ""
            start, end = 0, len(payload) - 1
            import re as _re
            m = _re.match(r"bytes=(\d+)-(\d*)", rng)
            partial = bool(m)
            if m:
                start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
            body = payload[start:end + 1]
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Accept-Ranges", f"0-{len(payload) - 1}")
            self.send_header("Content-Length", str(len(body)))
            if partial:
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{end}/{len(payload)}")
            self.end_headers()
            self.wfile.write(body)

    prov = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    prov.daemon_threads = True
    import threading as _th
    _th.Thread(target=prov.serve_forever, daemon=True).start()
    import urllib.request as _url
    try:
        purl = f"http://127.0.0.1:{prov.server_address[1]}/x.ts"
        relay = CatchupRelay()
        local = relay.start(purl)
        check("relay starts against a provider URL", bool(local))
        check("relay learned the total size", relay.total == len(payload))
        req = _url.Request(local)
        with _url.urlopen(req, timeout=10) as r:
            check("plain GET: 200 + Accept-Ranges: bytes + full length",
                  r.status == 200
                  and r.headers.get("Accept-Ranges") == "bytes"
                  and r.headers.get("Content-Length") == str(len(payload))
                  and r.read() == payload)
        req = _url.Request(local, headers={"Range": "bytes=100-199"})
        with _url.urlopen(req, timeout=10) as r:
            data = r.read()
            check("ranged GET: 206 + Content-Range + exact bytes",
                  r.status == 206
                  and r.headers.get("Content-Range")
                  == f"bytes 100-199/{len(payload)}"
                  and data == payload[100:200])
        req = _url.Request(local, headers={"Range": "bytes=999999-"})
        try:
            with _url.urlopen(req, timeout=10) as r:
                tail_ok = r.status == 416 and r.read() == b""
            check("out-of-range GET answered (416), not hung", tail_ok)
        except Exception as exc:  # noqa: BLE001
            check("out-of-range GET answered (416), not hung",
                  getattr(exc, "code", None) == 416)
        relay.stop()
        check("relay.stop() closes the server",
              relay._server is None and not relay._alive)
    finally:
        prov.shutdown()
        prov.server_close()

    print("[8] catch-up scrub: seeded length + fraction seek")
    view2 = PlayerView(fresh_cfg())
    view2.show()
    app.processEvents()

    class StubVlc:
        """VLCPlayer lookalike: no media, reports a live time position,
        records fraction seeks. Unknown methods behave like the wrapped
        VLCPlayer (never raise, return a safe default)."""

        def __init__(self):
            self.pos_calls = []
            self._t = 45000        # ms into the recording

        def set_position(self, frac):
            self.pos_calls.append(frac)
            self._t = int(frac * 1800000)

        def is_playing(self):
            return True

        def get_length(self):
            return 0               # the indexless-TS case being seeded

        def get_time(self):
            return self._t

        def video_size(self):
            return (0, 0)

        def __getattr__(self, name):
            def _stub(*_a, **_k):
                return 0
            return _stub

    stub = StubVlc()
    orig_vlc = view2.vlc
    view2.vlc = stub
    try:
        view2.current = {"kind": "catchup", "title": "C",
                         "stream_id": 501, "utc_start": 1000,
                         "utc_end": 1000 + 1800}
        check("known duration derived from the EPG window",
              view2._catchup_dur_ms() == 1800000)
        # _tick seeds the scrubber even though VLC reports no length
        view2._tick()
        app.processEvents()
        check("_tick seeds the scrubber from the program window",
              view2._scrub_on and view2.slider.maximum() == 1800000)
        check("right-hand time label shows the program length",
              view2.time_right.text() == "30:00")
        # seeks go through the byte-fraction axis
        view2._catchup_seek_to(900000)
        check("seek converts ms to a byte fraction",
              stub.pos_calls and abs(stub.pos_calls[-1] - 0.5) < 1e-6)
        check("seek rebases the tracked position",
              abs(view2._vid_s - 900.0) < 1e-6)
        view2._seek_ms(-60000)
        check("arrow/button seeks use the fraction axis too",
              abs(stub.pos_calls[-1] - 840000 / 1800000) < 1e-6)
        view2._jump_begin()
        check("jump-to-begin seeks to fraction 0",
              stub.pos_calls and stub.pos_calls[-1] == 0.0)
    finally:
        view2.vlc = orig_vlc

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
