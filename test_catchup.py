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

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

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

    # xtream.timeshift_url reads the negotiated form off the client; the
    # stub never logs in, so pin the legacy default explicitly
    timeshift_form = "legacy"

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
    _handle2, _span2 = view._slider_metrics()
    _tol2 = int(1800000 / _span2) + 2   # one pixel's worth of milliseconds
    check("drag moves > to the dragged position",
          abs(view._win_end_ms - 1200000) <= _tol2)

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
              dur == -(-(view._win_end_ms - view._win_start_ms) // 60000))
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

    print("[7b] CatchupRelay: byte-exact resume when the provider kills "
          "the body mid-stream")
    import src.catchup_relay as cr_mod
    orig_backoff = cr_mod._RESUME_BACKOFF_S
    orig_stalls = cr_mod._RESUME_STALLS
    cr_mod._RESUME_BACKOFF_S = 0.01     # keep the test fast
    payload2 = bytes(range(256)) * 1024            # 256 KB for this section

    class TruncatingProvider(BaseHTTPRequestHandler):
        """Mimics the real CDN's sibling-kill: every request is answered
        with only the FIRST 32 KB of the asked range, then the connection
        ends short of the promised Content-Range length. (32 KB per dial
        keeps the deliveries comfortably above the relay's starvation
        threshold, like the real CDN's ~15 s worth of bytes.)"""
        protocol_version = "HTTP/1.1"
        chunk = 32768
        opens = []

        def log_message(self, *_a):
            pass

        def do_GET(self):
            import re as _re
            rng = self.headers.get("Range") or ""
            m = _re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1)) if m else 0
            end = (int(m.group(2)) if m and m.group(2)
                   else len(payload2) - 1)
            body = payload2[start:min(end, start + self.chunk - 1) + 1]
            TruncatingProvider.opens.append((start, end, len(body)))
            self.send_response(206 if m else 200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(body)))
            if m:
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{start + len(body) - 1}/{len(payload2)}")
            self.end_headers()
            self.wfile.write(body)

    prov2 = ThreadingHTTPServer(("127.0.0.1", 0), TruncatingProvider)
    prov2.daemon_threads = True
    _th.Thread(target=prov2.serve_forever, daemon=True).start()
    try:
        relay2 = CatchupRelay()
        opens_before = len(TruncatingProvider.opens)
        local2 = relay2.start(
            f"http://127.0.0.1:{prov2.server_address[1]}/x.ts")
        opens_before = len(TruncatingProvider.opens)   # skip the size probe
        check("relay starts against the truncating provider", bool(local2))
        with _url.urlopen(_url.Request(local2), timeout=15) as r:
            body = r.read()
        check("client receives the FULL recording despite per-request "
              "truncation", body == payload2)
        check("provider was re-dialed for the missing bytes",
              len(TruncatingProvider.opens) >= len(payload2) // 32768
              and relay2.provider_opens == len(TruncatingProvider.opens))
        # every re-dial starts exactly where the previous body stopped
        covered = 0
        contiguous = True
        for (s, _e, n) in TruncatingProvider.opens[opens_before:]:
            if s != covered:
                contiguous = False
            covered += n
        check("re-dials are byte-exact (no gaps, no repeats)",
              contiguous and covered == len(payload2))
        relay2.stop()

        # give-up path: the provider serves one chunk, then every re-dial
        # fails — the relay must stop retrying instead of hanging, and the
        # client must not be left waiting on the keep-alive connection
        class DeadThenGoneProvider(TruncatingProvider):
            served = 0

            def do_GET(self):
                probe = self.headers.get("Range") == "bytes=0-0"
                if DeadThenGoneProvider.served and not probe:
                    self.close_connection = True
                    return                  # connection just dies
                if not probe:
                    DeadThenGoneProvider.served = 1
                super().do_GET()
        TruncatingProvider.opens = []

        prov2b = ThreadingHTTPServer(("127.0.0.1", 0), DeadThenGoneProvider)
        prov2b.daemon_threads = True
        _th.Thread(target=prov2b.serve_forever, daemon=True).start()
        try:
            relay2b = CatchupRelay()
            local2b = relay2b.start(
                f"http://127.0.0.1:{prov2b.server_address[1]}/x.ts")
            got = b""
            try:
                with _url.urlopen(_url.Request(local2b), timeout=15) as r:
                    got = r.read()
            except Exception as exc:  # noqa: BLE001 - short body is the
                partial = getattr(exc, "partial", None)  # point; keep bytes
                if isinstance(partial, (bytes, bytearray)):
                    got = bytes(partial)
            check("dead provider: partial body served, no hang",
                  0 < len(got) < len(payload2))
            relay2b.stop()
        finally:
            prov2b.shutdown()
            prov2b.server_close()
    finally:
        prov2.shutdown()
        prov2.server_close()
        cr_mod._RESUME_BACKOFF_S = orig_backoff
        cr_mod._RESUME_STALLS = orig_stalls

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

    print("[9] FileDownloader: survives mid-body kills, never fakes success")
    from src.ui.worker import FileDownloader as RealDownloader

    class KillOnceProvider(BaseHTTPRequestHandler):
        """First GET promises the full size but dies at 60 % (the sibling
        kill); the resume GET is answered properly with 206."""
        protocol_version = "HTTP/1.1"
        data = bytes(range(256)) * 256          # 64 KB
        first_done = False
        saw_range = None

        def log_message(self, *_a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range") or ""
            if not rng:
                # full request: promise everything, deliver 60 %, close
                import re as _re
                m = _re.match(r"bytes=(\d+)-", rng)
                start = int(m.group(1)) if m else 0
                if m:
                    self.send_response(206)
                    self.send_header(
                        "Content-Range",
                        f"bytes {start}-{len(self.data) - 1}/"
                        f"{len(self.data)}")
                else:
                    self.send_response(200)
                cut = start + int(len(self.data) * 0.6)
                self.send_header("Content-Length", str(len(self.data) - start))
                self.end_headers()
                self.wfile.write(self.data[start:cut])
                self.close_connection = True
                return
            import re as _re
            m = _re.match(r"bytes=(\d+)-$", rng)
            KillOnceProvider.saw_range = rng
            start = int(m.group(1))
            body = self.data[start:]
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(self.data) - 1}/{len(self.data)}")
            self.end_headers()
            self.wfile.write(body)

    prov3 = ThreadingHTTPServer(("127.0.0.1", 0), KillOnceProvider)
    prov3.daemon_threads = True
    _th.Thread(target=prov3.serve_forever, daemon=True).start()
    try:
        result = {}
        dl = RealDownloader()
        dl.finished.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
        tmpdir3 = tempfile.mkdtemp()
        dl._BACKOFF_S = 0.01
        dl.start(f"http://127.0.0.1:{prov3.server_address[1]}/f.bin",
                 os.path.join(tmpdir3, "f.bin"))
        ok = wait_until(lambda: "ok" in result, 10.0)
        check("truncated body resumes to a complete download",
              ok and result.get("ok") is True)
        with open(result["msg"], "rb") as f:
            check("resumed file is byte-identical to the source",
                  f.read() == KillOnceProvider.data)
        check("resume used a byte-exact Range request",
              KillOnceProvider.saw_range
              == f"bytes={int(len(KillOnceProvider.data) * 0.6)}-")
    finally:
        prov3.shutdown()
        prov3.server_close()

    class NoResumeProvider(BaseHTTPRequestHandler):
        """Ignores Range on resume (answers 200 from byte 0)."""
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def do_GET(self):
            body = b"x" * 4096
            self.send_response(200)
            self.send_header("Content-Length", "16384")   # dies at 25 %
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

    prov4 = ThreadingHTTPServer(("127.0.0.1", 0), NoResumeProvider)
    prov4.daemon_threads = True
    _th.Thread(target=prov4.serve_forever, daemon=True).start()
    try:
        result4 = {}
        dl4 = RealDownloader()
        dl4.finished.connect(lambda ok, msg: result4.update(ok=ok, msg=msg))
        dl4._BACKOFF_S = 0.01
        dl4.start(f"http://127.0.0.1:{prov4.server_address[1]}/n.bin",
                  os.path.join(tempfile.mkdtemp(), "n.bin"))
        ok = wait_until(lambda: "ok" in result4, 10.0)
        check("server that will not resume = loud failure, not a short file",
              ok and result4.get("ok") is False
              and "resume" in result4.get("msg", ""))
    finally:
        prov4.shutdown()
        prov4.server_close()

    print("[10] window button: white at rest, gold while engaged/downloading")
    from src.ui import icons as ic_mod
    white = ic_mod.download_window()
    gold = ic_mod.download_window(ic_mod.GOLD)
    gold_keep = ic_mod.download_window(ic_mod.GOLD, keep_disabled=True)
    check("white and gold variants are distinct cached icons",
          white is not gold and gold is not gold_keep)
    pm_w = white.pixmap(24, 24)
    img_w = pm_w.toImage()
    # collect the solid glyph pixels (alpha > 200) and classify the color
    def solid_pixels(img):
        out = []
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                if c.alpha() > 200:
                    out.append((c.red(), c.green(), c.blue()))
        return out

    glyph_w = solid_pixels(img_w)
    check("default icon glyph is white, not gold",
          glyph_w and all(r > 230 and g > 230 and b > 230
                          for r, g, b in glyph_w))
    glyph_g = solid_pixels(gold.pixmap(24, 24).toImage())
    check("engaged icon glyph is gold",
          glyph_g and all(abs(r - 245) < 25 and abs(g - 197) < 40 and b < 90
                          for r, g, b in glyph_g))
    img_n = gold_keep.pixmap(
        24, 24, QtGui.QIcon.Normal).toImage()
    img_d = gold_keep.pixmap(
        24, 24, QtGui.QIcon.Disabled).toImage()
    check("downloading icon carries an explicit disabled pixmap",
          gold_keep.availableSizes(QtGui.QIcon.Disabled)
          and img_n == img_d)
    check("rest icon has none (state-free, graying left to the style)",
          not white.availableSizes(QtGui.QIcon.Disabled))

    # -- button state flow on the view (icons compared by cacheKey: a
    #    setIcon copy shares the engine with the cached module icon) --
    def icon_key(btn):
        return btn.icon().cacheKey()

    view3 = view                      # reuse the [6] view (catchup current)
    view3._win_cancel(silent=True)
    app.processEvents()
    check("idle button shows the white icon",
          icon_key(view3.btn_win) == white.cacheKey())
    view3.slider.blockSignals(True)
    view3.slider.setRange(0, 1800000)
    view3.slider.blockSignals(False)
    view3._win_engage()
    check("engaged selection turns the icon gold",
          icon_key(view3.btn_win) == gold.cacheKey())
    view3._win_cancel()
    check("cancel restores the white icon",
          icon_key(view3.btn_win) == white.cacheKey())

    captured2 = {}

    class FakeDownloader2(QtCore.QObject):
        progress = QtCore.pyqtSignal(int, int)
        finished = QtCore.pyqtSignal(bool, str)

        def start(self, url, path):
            captured2["icon_at_start"] = view3.btn_win.icon().cacheKey()
            self.finished.emit(True, path)

    orig_fd2 = pv_mod.FileDownloader
    pv_mod.FileDownloader = FakeDownloader2
    try:
        view3._win_engage()
        view3._win_confirm()
        app.processEvents()
        check("download in flight keeps the gold icon (disabled-safe)",
              captured2.get("icon_at_start") == gold_keep.cacheKey())
        check("finished download restores the white icon",
              icon_key(view3.btn_win) == white.cacheKey())
    finally:
        pv_mod.FileDownloader = orig_fd2

    print("[11] catch-up stall watchdog: frozen / stopped -> rescue reopen")
    view4 = PlayerView(fresh_cfg())
    view4.show()
    app.processEvents()

    class WatchVlc:
        """Clock we can freeze; records play() rescues + fraction seeks."""

        def __init__(self):
            self.pos_calls = []
            self.plays = []
            self._t = 0
            self.frozen_at = None
            self.playing = True

        def set_position(self, frac):
            self.pos_calls.append(frac)
            self._t = int(frac * 1800000)

        def play(self, url, timeshift=None, start_seconds=0.0):
            self.plays.append(url)

        def is_playing(self):
            return self.playing

        def get_length(self):
            return 0

        def get_time(self):
            return self._t

        def video_size(self):
            return (0, 0)

        def __getattr__(self, name):
            def _stub(*_a, **_k):
                return 0
            return _stub

    wstub = WatchVlc()
    orig_vlc4 = view4.vlc
    view4.vlc = wstub
    orig_freeze = PlayerView._CU_FREEZE_S
    orig_ticks = PlayerView._CU_STOP_TICKS
    PlayerView._CU_FREEZE_S = 0.6
    PlayerView._CU_STOP_TICKS = 3
    try:
        view4.current = {"kind": "catchup", "title": "W",
                         "stream_id": 501, "utc_start": 1000,
                         "utc_end": 1000 + 1800, "url": "http://relay/x"}
        view4._catchup_local_url = "http://relay/x"
        # healthy playback: clock advances, no rescue
        for t in (0.0, 0.5, 1.0):
            wstub._t = int(t * 1000)
            view4._tick()
        check("healthy playback never triggers a rescue",
              wstub.plays == [])
        # frozen mid-program (provider killed the connection). Walk the
        # tracked position to 10:00 first — the tracker only snaps to VLC's
        # clock when they AGREE (<=3 s apart), a 600 s teleport is rejected
        # just like garbage broadcast PTS would be.
        view4._vid_s = 600.0
        wstub._t = 600000                       # playing at 10:00
        view4._tick()
        check("agreed clock snaps the tracked position",
              abs(view4._vid_s - 600.0) < 1e-6)
        wstub._t = 600000                       # ...and now it sticks
        view4._tick()
        time.sleep(0.8)
        view4._tick()
        ok = wait_until(lambda: len(wstub.plays) == 1, 3.0)
        check("frozen stream is reopened through the relay URL",
              ok and wstub.plays and wstub.plays[0] == "http://relay/x")
        ok = wait_until(lambda: len(wstub.pos_calls) >= 1, 4.0)
        check("rescue re-seeks to the frozen position",
              ok and wstub.pos_calls
              and abs(wstub.pos_calls[-1] * 1800.0 - 600.0) < 25.0)
        # cooldown: an immediate second freeze must not re-rescue
        n_plays = len(wstub.plays)
        view4._tick()
        check("rescue respects the reopen cooldown",
              len(wstub.plays) == n_plays)
        # stopped mid-program (VLC ended/error while the program continues)
        view4._last_reopen = 0.0                # outside the cooldown
        wstub.playing = False
        for _ in range(4):
            view4._tick()
        ok = wait_until(lambda: len(wstub.plays) == n_plays + 1, 3.0)
        check("dead player mid-program is reopened too", ok)
        # near the program end, stopping is natural: no rescue
        n_plays = len(wstub.plays)
        view4._last_reopen = 0.0
        view4._vid_s = 1790.0                   # 10 s before the end
        wstub.playing = False
        for _ in range(5):
            view4._tick()
        check("natural end-of-program never rescues",
              len(wstub.plays) == n_plays)
    finally:
        PlayerView._CU_FREEZE_S = orig_freeze
        PlayerView._CU_STOP_TICKS = orig_ticks
        view4.vlc = orig_vlc4

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
