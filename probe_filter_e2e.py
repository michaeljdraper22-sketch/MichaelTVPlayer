# -*- coding: utf-8 -*-
"""E2E probe: the profanity filter's UNVERIFIED link — real libvlc, real
audio track, real QTimer evaluation — against the exact path the app uses
for EXTERNAL subtitle files (Stremio handoff / fetched-online):

    real mp4 (audio+video) -> VLCPlayer.play
    real SRT with 'shit' at 8.0-11.0s -> PlayerView._load_stremio_sub_cues
    -> real _filter_timer ticks -> ProfanityEngine.evaluate(get_time)
    -> VLCPlayer.set_filter_mute -> libvlc audio_set_mute

Asserts the engine flips muted ON inside the window and OFF after it, and
that libvlc's OWN audio_get_mute() follows (volume is held at 0 and the
window stays minimized — nothing audible, nothing focused).

User report (2026-09-22): the profanity filter fails; on internet-found
subtitles it has never worked. Log forensics the same day showed the
fetch + parse + window math all succeeding (1110 cues -> 42 windows) —
everything DOWNSTREAM of that is what this probe exercises on the real
player.

Run:  .venv\\Scripts\\python.exe -X utf8 probe_filter_e2e.py
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.config import Config, DEFAULTS  # noqa: E402
from src.profanity import find_ffmpeg  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

DUR_S = 20
CUE_S, CUE_E = 8.0, 11.0          # 'shit' window inside this cue
FF = find_ffmpeg()

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "FAIL ") + name
          + (("  " + extra) if extra else ""), flush=True)


def build_media(dirp):
    mp4 = os.path.join(dirp, "filter_e2e.mp4")
    subprocess.run([FF, "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=duration=%d:size=320x240"
                    % DUR_S,
                    "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=%d" % DUR_S,
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", mp4], check=True)
    srt = os.path.join(dirp, "filter_e2e.srt")
    with open(srt, "w", encoding="utf-8") as f:
        f.write("1\n00:00:08,000 --> 00:00:11,000\n"
                "well that is some shit right there\n\n")
    return mp4, srt


# ---- scenario B: the 12:23 incident shape -------------------------------
# resume point + the real cue that should have muted ~12 s later
INC_START_S = 2374.3
INC_CUE_S, INC_CUE_E = 2385.216, 2387.426   # "work through shit."
INC_DUR_S = 2400                            # long enough to cross the cue


class _RangeHandler(BaseHTTPRequestHandler):
    """One-file HTTP server with Range support (VLC sends Range the moment
    it seeks; SimpleHTTPRequestHandler would answer 200-full-body and make
    :start-time crawl through 2 GB of stream)."""

    path_file = ""

    def do_GET(self):    # noqa: N802 - http.server API
        try:
            size = os.path.getsize(self.path_file)
            rng = self.headers.get("Range") or ""
            if rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                if not a:                      # suffix range: bytes=-N
                    n = int(b or 0)
                    start, end = max(0, size - n), size - 1
                else:
                    start = int(a)
                    end = int(b) if b else size - 1
            else:
                start, end = 0, size - 1
            end = min(end, size - 1)
            with open(self.path_file, "rb") as f:
                f.seek(start)
                data = f.read(end - start + 1)
            code = 206 if rng else 200
            self.send_response(code)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:   # noqa: BLE001 - probe tool
            pass

    def log_message(self, *_a):
        pass


def build_long_media(dirp):
    """A 2400 s 160x120 clip + the incident's SRT cue at its real time."""
    mp4 = os.path.join(dirp, "incident.mp4")
    if not os.path.isfile(mp4):
        subprocess.run([FF, "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i",
                        "testsrc=duration=%d:size=160x120:rate=5"
                        % INC_DUR_S,
                        "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=%d" % INC_DUR_S,
                        "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", "-shortest",
                        "-movflags", "+faststart", mp4], check=True)
    srt = os.path.join(dirp, "incident.srt")
    with open(srt, "w", encoding="utf-8") as f:
        f.write("1\n00:39:45,216 --> 00:39:47,426\n"
                "when we're trying to work through shit.\n\n")
    return mp4, srt


def incident_shape(app, view, dirp):
    """Scenario B: localhost HTTP + input-timeshift + :start-time resume —
    the exact playback shape of the 2026-09-22 12:23 session. The engine
    must mute inside the cue's word window and release after it."""
    mp4, srt = build_long_media(dirp)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    _RangeHandler.path_file = mp4
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/incident.mp4" % port

    view.current = {"kind": "stremio", "url": url, "sub_file": srt,
                    "title": "incident replay"}
    # play_media's per-media resets (the real flow clears the previous
    # media's engine windows + caption clock BEFORE opening the new URL —
    # without this, scenario A's leftover window fires during the opening
    # phase when get_time() still reads -1 and the clock falls back)
    view._filter_engine.clear()
    view._filter_engine.set_muted(False)
    view._cap_clock_s = 0.0
    view._cap_raw_s = None
    view._vid_s = INC_START_S
    view.vlc.set_volume(0)
    # the app's stremio open: http + timeshift, resumed at the offset
    view.vlc.play_at(url, start_seconds=INC_START_S, timeshift=True,
                     start_wait_s=30.0)
    view._load_stremio_sub_cues()
    wins = view._filter_engine.windows
    check("B: mute window built on the resumed timeline",
          any(abs(s - 2386.7) < 0.6 for s, _e in wins)
          or bool(wins), repr(wins[:2]))
    check("B: filter timer running", view._filter_timer.isActive())

    def pump(seconds):
        t0 = time.time()
        while time.time() - t0 < seconds:
            app.processEvents()
            QtCore.QThread.msleep(10)

    engaged = cleared = None
    t0 = time.time()
    while time.time() - t0 < 60:
        pump(0.25)
        try:
            t = view.vlc.get_time() / 1000.0
        except Exception:   # noqa: BLE001
            t = -1.0
        if engaged is None and view._filter_engine.muted:
            engaged = t
            print("      (flip ON: get_time=%s engine clock=%s state=%s)"
                  % (t, view._caption_clock_s(), view.vlc.state_name()),
                  flush=True)
        if engaged is not None and not view._filter_engine.muted:
            cleared = t
            print("      (flip OFF: get_time=%s engine clock=%s state=%s)"
                  % (t, view._caption_clock_s(), view.vlc.state_name()),
                  flush=True)
            break
        if t > INC_CUE_E + 5:
            break
    print("   B: engage t=%s clear t=%s (cue %.2f-%.2f)"
          % (engaged, cleared, INC_CUE_S, INC_CUE_E), flush=True)
    check("B: engine muted INSIDE the resumed cue window",
          engaged is not None and INC_CUE_S - 1.0 <= engaged
          <= INC_CUE_E + 1.0, "t=%s" % engaged)
    check("B: engine released after the window", cleared is not None)
    try:
        view.vlc.stop_and_release()
    except Exception:   # noqa: BLE001
        pass
    srv.shutdown()


def relay_shape(app, view, dirp):
    """Scenario C: the 2026-09-22 incident's FULL playback shape —
    _effective_url routes the stremio media through the REAL VodRelay
    (resume marker set), VLC opens the relay URL with :start-time, and the
    externally-fetched subtitle engages MID-PLAYBACK exactly like
    _on_subs_fetched delivers it (engage + _load_stremio_sub_cues).
    Asserts the engine mutes inside the cue's window, releases after it,
    and the OVERLAY paints the SUBSTITUTED cue text (the user-visible
    half of the filter)."""
    mp4, srt = build_long_media(dirp)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    _RangeHandler.path_file = mp4
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/incident.mp4" % port

    view.current = {"kind": "stremio", "url": url, "title": "relay replay"}
    view._filter_engine.clear()
    view._filter_engine.set_muted(False)
    view._cap_clock_s = 0.0
    view._cap_raw_s = None
    view._vid_s = INC_START_S
    view.vlc.set_volume(0)
    # play_media's resume marker (start_at > 3.0 -> offset 1) + the app's
    # own relay routing for kind=stremio with the filter enabled
    view._relay_start_offset = 1
    local = view._effective_url(url, "stremio")
    check("C: relay engaged by _effective_url",
          local != url and view._vod_relay is not None, local[:40])
    view.vlc.play(local, timeshift=False, start_seconds=INC_START_S,
                  start_wait_s=60.0)

    def pump(seconds):
        t0 = time.time()
        while time.time() - t0 < seconds:
            app.processEvents()
            QtCore.QThread.msleep(10)

    # playback up: wait for get_time to go positive (relay start is async)
    t0 = time.time()
    while time.time() - t0 < 60:
        pump(0.25)
        try:
            t = view.vlc.get_time() / 1000.0
        except Exception:   # noqa: BLE001
            t = -1.0
        if t > 0:
            break
    print("   C: playback up, get_time=%s (waited %.0fs)"
          % (t, time.time() - t0), flush=True)

    # the fetched-subtitle delivery, a few seconds into playback (the
    # dead-end funnel's timing: engage + filter feed)
    pump(2.0)
    cur = view.current or {}
    cur["sub_file"] = srt
    cur["_fetched_lang"] = "English"
    view._engage_stremio_external()
    check("C: overlay engaged for the fetched file", view._cap_on)
    view._load_stremio_sub_cues()
    check("C: mute windows armed from the external SRT",
          bool(view._filter_engine.windows),
          repr(view._filter_engine.windows[:1]))
    check("C: filter timer running", view._filter_timer.isActive())

    engaged = cleared = None
    saw_raw = saw_sub = False
    t0 = time.time()
    while time.time() - t0 < 90:
        pump(0.25)
        for ln in list(getattr(view._cap_wid, "_lines", []) or []):
            if "work through" in ln:
                if "shit" in ln:
                    saw_raw = True
                if "crap" in ln:
                    saw_sub = True
        try:
            t = view.vlc.get_time() / 1000.0
        except Exception:   # noqa: BLE001
            t = -1.0
        if engaged is None and view._filter_engine.muted:
            engaged = t
            print("      (flip ON: get_time=%s state=%s)"
                  % (t, view.vlc.state_name()), flush=True)
        if engaged is not None and not view._filter_engine.muted:
            cleared = t
            print("      (flip OFF: get_time=%s state=%s)"
                  % (t, view.vlc.state_name()), flush=True)
            break
        if t > INC_CUE_E + 8:
            break
    print("   C: engage t=%s clear t=%s (cue %.2f-%.2f) raw=%s sub=%s"
          % (engaged, cleared, INC_CUE_S, INC_CUE_E, saw_raw, saw_sub),
          flush=True)
    check("C: engine muted INSIDE the cue window",
          engaged is not None and INC_CUE_S - 1.0 <= engaged
          <= INC_CUE_E + 1.0, "t=%s" % engaged)
    check("C: engine released after the window", cleared is not None)
    check("C: overlay painted the SUBSTITUTED cue", saw_sub and not saw_raw)
    try:
        view.vlc.stop_and_release()
    except Exception:   # noqa: BLE001
        pass
    try:
        if view._vod_relay is not None:
            view._vod_relay.stop()
    except Exception:   # noqa: BLE001
        pass
    srv.shutdown()


def _make_view(app):
    """PlayerView with the filter ON (pads like the user's settings)."""
    cfg = Config(dict(DEFAULTS), None)
    cfg.data["profanity"] = dict(cfg.data.get("profanity") or {})
    cfg.data["profanity"]["enabled"] = True
    cfg.data["profanity"]["pad_before_ms"] = 450
    cfg.data["profanity"]["pad_after_ms"] = 250
    view = PlayerView(cfg)
    view.resize(320, 240)
    view._apply_profanity_config()
    return view


def main():
    app = QtWidgets.QApplication(sys.argv)
    tmp = tempfile.mkdtemp(prefix="mtp_filter_e2e_")
    mp4, srt = build_media(tmp)

    only_b = len(sys.argv) > 1 and sys.argv[1].upper() == "B"
    only_c = len(sys.argv) > 1 and sys.argv[1].upper() == "C"

    if only_b:
        print("-- scenario B ONLY (fresh process) --", flush=True)
        view = _make_view(app)
        incident_shape(app, view, tmp)
        print("\n%d/%d checks passed" % (len(PASS), len(PASS) + len(FAIL)),
              flush=True)
        return 1 if FAIL else 0

    if only_c:
        print("-- scenario C ONLY (fresh process) --", flush=True)
        view = _make_view(app)
        relay_shape(app, view, tmp)
        print("\n%d/%d checks passed" % (len(PASS), len(PASS) + len(FAIL)),
              flush=True)
        return 1 if FAIL else 0

    view = _make_view(app)
    check("engine enabled after _apply_profanity_config",
          view._filter_engine.enabled)

    # real playback: local file, no relay, volume 0 (silent per house rules)
    view.vlc.set_volume(0)
    view.current = {"kind": "stremio", "url": mp4, "sub_file": srt,
                    "title": "filter e2e"}
    view.vlc.play(mp4, timeshift=False)
    view._load_stremio_sub_cues()      # the app's external-sub feed path
    wins = view._filter_engine.windows
    check("mute window built from external SRT", bool(wins), repr(wins[:2]))
    check("filter timer running", view._filter_timer.isActive())

    def pump(seconds):
        t0 = time.time()
        while time.time() - t0 < seconds:
            app.processEvents()
            QtCore.QThread.msleep(10)

    def state():
        try:
            vlc_mute = bool(view.vlc.player.audio_get_mute())
        except Exception as exc:  # noqa: BLE001
            vlc_mute = "err:%r" % exc
        try:
            t = view.vlc.get_time() / 1000.0
        except Exception:  # noqa: BLE001
            t = -1.0
        return t, view._filter_engine.muted, vlc_mute, \
            view.vlc._filter_mute

    # wait for playback to reach the cue (or the window to engage)
    engaged_t = engaged_mute = None
    cleared = False
    t0 = time.time()
    while time.time() - t0 < DUR_S + 8:
        pump(0.25)
        t, m, vm, fm = state()
        if m and engaged_t is None:
            engaged_t, engaged_mute = t, vm
        if engaged_t is not None and not m:
            cleared = True
            cleared_state = (t, vm, fm)
            break
    print("   engage at t=%s vlc_mute=%s; clear at %s"
          % (engaged_t, engaged_mute,
             cleared_state if cleared else None), flush=True)

    check("engine muted INSIDE the cue window",
          engaged_t is not None and CUE_S - 1.0 <= engaged_t <= CUE_E + 1.0,
          "t=%s" % engaged_t)
    check("libvlc audio_get_mute() followed the filter ON",
          engaged_mute is True, repr(engaged_mute))
    check("engine UNMUTED after the window passed", cleared)
    if cleared:
        check("libvlc audio_get_mute() released after the window",
              cleared_state[1] is False, repr(cleared_state))
    check("user mute untouched by the filter",
          view.vlc.is_mute() is False)

    try:
        view.vlc.stop_and_release()
    except Exception:  # noqa: BLE001
        pass

    print("\n-- scenario B: incident shape (http + timeshift + resume) --",
          flush=True)
    # B runs in a FRESH subprocess: a second media in one process makes
    # libvlc's vout fail to register its window classes (RegisterClass
    # err 1410) and the decode collapses — probe noise, not app truth.
    r = subprocess.run([sys.executable, "-X", "utf8",
                        os.path.abspath(__file__), "B"],
                       capture_output=True, text=True, timeout=600)
    for ln in (r.stdout or "").splitlines():
        if ln.startswith(("  ok", "FAIL", "   B", "      (flip")):
            print(ln, flush=True)
    b_ok = r.returncode == 0
    for name in ("B: engine muted INSIDE the resumed cue window",
                 "B: engine released after the window"):
        check(name, b_ok, "(from subprocess exit=%d)" % r.returncode)

    print("\n-- scenario C: relay shape (real VodRelay + fetched subs) --",
          flush=True)
    # C also runs in a fresh subprocess (same RegisterClass constraint)
    r = subprocess.run([sys.executable, "-X", "utf8",
                        os.path.abspath(__file__), "C"],
                       capture_output=True, text=True, timeout=600)
    for ln in (r.stdout or "").splitlines():
        if ln.startswith(("  ok", "FAIL", "   C", "      (flip")):
            print(ln, flush=True)
    c_ok = r.returncode == 0
    for name in ("C: engine muted INSIDE the cue window",
                 "C: engine released after the window",
                 "C: overlay painted the SUBSTITUTED cue"):
        check(name, c_ok, "(from subprocess exit=%d)" % r.returncode)

    print("\n%d/%d checks passed" % (len(PASS), len(PASS) + len(FAIL)),
          flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
