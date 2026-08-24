# -*- coding: utf-8 -*-
"""P5 live caption-engage verification (WP5): the two Off->On mid-show
paths the stage-3 driver never exercises, plus one live D1 landing
sample with whatever L the window measures.

Phases:
  REGIME  classify delivery early: frontier growth over the first 60 s
          (healthy >=0.85x / moderate 0.5-0.85x / trickle <0.5x)
  MID90   play with captions OFF, wait frontier >= 90 s, engage, measure
          time-to-first-anchor / first-paint (gate ~15 s) + join_byte > 0
          (D2: no byte-0 replay) + first painted cue near the playhead
  MIDLT90 fresh play, engage at frontier in [45, 90) — same measurements
  D1      inline-formula jump-to-live at the measured L, then time from
          landing to the next paint (captions timely)

Courtesies: audio MUTED, offscreen (never a real window), ONE provider
connection at a time. Usage:
  .venv\\Scripts\\python.exe -X utf8 p5_engage_run.py
"""
import os
import sys
import time

os.environ["MTP_SYNC_LOG"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.logging_setup import setup_logging  # noqa: E402
setup_logging()
import logging  # noqa: E402

synclog = logging.getLogger("mtp.sync")

from src.config import Config  # noqa: E402
from src.ui.player_view import (PlayerView,  # noqa: E402
                                _CHASE_SAFETY_S,
                                _CC_ADAPTIVE_MIN_L_S,
                                _CC_ADAPTIVE_PAD_S)
from src.xtream import XtreamClient  # noqa: E402

app = QtWidgets.QApplication(sys.argv)
cfg = Config.load()
view = PlayerView(cfg)
view.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
view.showMinimized()
MUTE = lambda: (view.vol_slider.setValue(0),
                view.btn_mute.setChecked(True),
                view.vlc.set_mute(True),
                view.vlc.set_volume(0))
MUTE()

# the profanity filter also starts the CC reader (_start_cc_when_buffer
# serves both) — force it OFF so "captions off" really means reader off
prof = dict(cfg.profanity)
prof["enabled"] = False
cfg.profanity = prof
view._apply_profanity_config()


def hush():
    MUTE()


def pump(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.03)
    hush()


def wait_until(pred, timeout, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    print(f"  (timeout waiting for {what} after {timeout}s)", flush=True)
    return False


def phase(name, detail=""):
    synclog.info("PHASE %s %s", name, detail)
    print(f"[{time.strftime('%H:%M:%S')}] PHASE {name} {detail}", flush=True)


def raw_s():
    try:
        t = view.vlc.get_time()
    except Exception:
        return -1.0
    return t / 1000.0 if t >= 0 else -1.0


CHECKS = []


def check(name, cond, detail="", kind="mechanism"):
    CHECKS.append((name, bool(cond), kind))
    print(f"  {'ok  ' if cond else 'FAIL'} {name}"
          + ("" if kind == "mechanism" else f"  [{kind}]")
          + (f"  [{detail}]" if detail else ""), flush=True)


T0 = time.time()


def elapsed():
    return f"{time.time() - T0:7.1f}s"


def find_channel(client, needle):
    for cat in (client.live_categories() or []):
        for s in (client.live_streams(cat.get("category_id")) or []):
            if needle in (s.get("name") or "").upper():
                return s
    return None


def start_playback(client, label):
    ch = find_channel(client, "US: NFL NETWORK")
    if not ch:
        print("NFL NETWORK not found — aborting", flush=True)
        return None
    url = client.live_url(ch["stream_id"])
    print(f"channel: {ch['name']} id={ch['stream_id']} ({label})",
          flush=True)
    view.play_media({"kind": "live", "title": ch["name"], "url": url,
                     "stream_id": ch["stream_id"]})
    hush()
    ok = wait_until(lambda: view._mode == "chase", 45, "chase mode")
    check(f"{label}: chase engages", ok)
    wait_until(lambda: view.vlc.is_playing(), 45, "chase playing")
    hush()
    return True


def frontier_growth(seconds):
    """Sample the frontier; return (rate, first, last, wall)."""
    t0 = time.time()
    f0 = view._frontier_s()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.2)
    f1 = view._frontier_s()
    wall = time.time() - t0
    hush()
    return (max(0.0, f1 - f0) / wall if wall > 0 else 0.0), f0, f1, wall


def classify(rate):
    if rate >= 0.85:
        return "healthy"
    if rate >= 0.5:
        return "moderate"
    return "trickle/degenerate"


def engage_and_measure(label, gate_s=15.0):
    """Engage captions NOW; measure anchor/paint/join. Returns dict."""
    t0 = time.time()
    off_before = view._cc_off
    src_before = view._cc_source
    on_before = view._cap_on
    join_byte = None
    t_anchor = t_paint = None
    clock_at_paint = None
    first_cue_start = None
    view._engage_caption_overlay()
    while time.time() - t0 < 60.0:
        app.processEvents()
        now = time.time()
        if join_byte is None and view._cc_source is not None:
            join_byte = view._cc_join_byte
        if t_anchor is None and view._cc_off is not None:
            t_anchor = now - t0
        if view._cap_wid._lines and t_paint is None:
            t_paint = now - t0
            clock_at_paint = view._cap_clock_s
            # coherence: which released cue is on screen right now?
            text = " ".join(view._cap_wid._lines)
            best = None
            for s, e, txt in reversed(view._cap_cues.cues[-60:]):
                if txt and (txt in text or text in txt
                            or txt.split("\n")[-1] in text):
                    best = s
                    break
            first_cue_start = best
        if t_paint is not None and t_anchor is not None and now - t0 > 3.0:
            break
        time.sleep(0.05)
    hush()
    lag = view._cc_lag
    res = dict(join=join_byte, t_anchor=t_anchor, t_paint=t_paint,
               clock_at_paint=clock_at_paint,
               first_cue_start=first_cue_start, lag=lag,
               frontier=view._frontier_s(), raw=raw_s(),
               edge=view._cap_edge_s(),
               join_app=view._cc_join_app_s)
    print(f"  {label}: join_byte={join_byte} "
          f"t_anchor={t_anchor} t_paint={t_paint} "
          f"clock@paint={clock_at_paint} "
          f"first_cue_start={first_cue_start} "
          f"L={None if lag is None else round(lag, 1)} "
          f"fr={res['frontier']:.1f} edge={res['edge']:.1f} "
          f"join_app={res['join_app']} raw={res['raw']:.1f}", flush=True)
    check(f"{label}: captions were genuinely OFF before engage "
          "(no reader running)",
          off_before is None and src_before is None and not on_before)
    check(f"{label}: D2 join is NOT byte 0 (join_byte > 0)",
          join_byte is not None and join_byte > 0,
          f"join_byte={join_byte}")
    check(f"{label}: first anchor arrives", t_anchor is not None,
          f"t={t_anchor}", kind="data-limited")
    check(f"{label}: first paint within ~{gate_s:.0f} s of engage",
          t_paint is not None and t_paint <= gate_s,
          f"t_paint={t_paint}", kind="data-limited")
    if first_cue_start is not None and clock_at_paint is not None:
        dist = clock_at_paint - first_cue_start
        check(f"{label}: first painted cue is near the playhead "
              "(joined live, not replaying the buffer)",
              -5.0 <= dist <= 30.0,
              f"cue_start={first_cue_start:.1f} "
              f"clock@paint={clock_at_paint:.1f} dist={dist:.1f}s",
              kind="data-limited")
    else:
        check(f"{label}: first painted cue is near the playhead",
              False, "no matching released cue found",
              kind="data-limited")
    return res


def health_watch(seconds, label):
    """Simple silent-stop watch after engage: blanks while cue data
    exists must stay short."""
    last_paint = time.time() if view._cap_wid._lines else None
    worst = 0.0
    blank_start = None
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        now = time.time()
        showing = bool(view._cap_wid._lines)
        if showing:
            last_paint = now
            blank_start = None
        elif last_paint is not None:
            clock = view._cap_clock_s
            has_data = any(s <= clock <= e + 1.0 for s, e, _ in
                           reversed(view._cap_cues.cues[-400:]))
            if has_data:
                if blank_start is None and now - last_paint > 0.5:
                    blank_start = now - 0.5
                if blank_start is not None:
                    worst = max(worst, now - blank_start)
            else:
                blank_start = None
        time.sleep(0.1)
    hush()
    check(f"{label}: no silent-stop > 8 s while cue data existed",
          worst <= 8.0, f"max={worst:.1f}s", kind="data-limited")
    return worst


def d1_jump_sample(label):
    """Inline the D1 formula (never the production helper) and verify
    the landing + caption timeliness at the window's measured L."""
    fr = view._frontier_s()
    edge = view._cap_edge_s()
    lag = view._cc_lag
    raw_pre = raw_s()
    back = max(_CHASE_SAFETY_S, lag + _CC_ADAPTIVE_PAD_S) \
        if (lag is not None and lag > _CC_ADAPTIVE_MIN_L_S) \
        else _CHASE_SAFETY_S
    view._jump_live()
    hush()
    want_land = edge - back
    landed = wait_until(
        lambda: abs(view._cap_content_for_raw(raw_s()) - want_land) <= 4.0,
        8, "raw at the landing target")
    p3 = raw_s()
    pump(3)
    p6 = raw_s()
    check(f"{label}: jump-live lands per the adaptive policy "
          "(raw moved/tracks)",
          landed and (abs(p3 - raw_pre) > 0.05 or p6 - p3 > 0.5),
          f"raw={p3:.1f}->{p6:.1f} want~{want_land:.1f} edge={edge:.1f} "
          f"fr={fr:.1f} L={'-' if lag is None else '%.1f' % lag} "
          f"back={back:.0f} "
          f"({'adaptive' if back > _CHASE_SAFETY_S else 'true-edge'})")
    # captions timely after landing: next paint within 20 s
    t0 = time.time()
    got_paint = False
    while time.time() - t0 < 20.0:
        app.processEvents()
        if view._cap_wid._lines:
            got_paint = True
            break
        time.sleep(0.1)
    hush()
    check(f"{label}: captions paint within 20 s after the landing",
          got_paint, f"t={time.time() - t0:.1f}s", kind="data-limited")


def main():
    client = XtreamClient(cfg.server_url, cfg.username, cfg.password)
    client.authenticate()

    # ---- MID90: play with captions OFF until frontier >= 90 s ----
    phase("MID90-START", "captions OFF from the start")
    if not start_playback(client, "MID90"):
        return
    check("MID90: no caption reader before engage",
          view._cc_source is None and not view._cap_on)
    phase("MID90-REGIME", "frontier growth over 60 s")
    rate, f0, f1, wall = frontier_growth(60.0)
    regime = classify(rate)
    print(f"  regime: growth={rate:.2f}x over {wall:.0f}s "
          f"({f0:.1f} -> {f1:.1f}s) => {regime}", flush=True)
    synclog.info("REGIME rate=%.2f %s", rate, regime)
    phase("MID90-WAIT", "waiting frontier >= 90 s (max 300 s)")
    t0 = time.time()
    while view._frontier_s() < 90.0 and time.time() - t0 < 300.0:
        app.processEvents()
        time.sleep(0.2)
    hush()
    fr_at_engage = view._frontier_s()
    if fr_at_engage < 90.0:
        print(f"  (frontier plateaued at {fr_at_engage:.1f}s — "
              "engaging anyway, case is regime-limited)", flush=True)
    check("MID90: engaged at frontier >= 90 s", fr_at_engage >= 90.0,
          f"frontier={fr_at_engage:.1f}s growth={rate:.2f}x {regime}",
          kind="data-limited")
    phase("MID90-ENGAGE", f"frontier={fr_at_engage:.1f}s")
    engage_and_measure("MID90")
    health_watch(60.0, "MID90")

    # ---- MIDLT90: fresh session, engage at frontier in [45, 90) ----
    phase("MIDLT90-START", "stop, fresh play, captions OFF")
    view.stop()
    pump(4)
    # captions stay WANTED across a channel change (correct product
    # behavior) — explicitly disengage so the fresh session really
    # starts caption-OFF and the <90 Off->On case is exercised
    view._disengage_caption_overlay()
    if not start_playback(client, "MIDLT90"):
        return
    check("MIDLT90: no caption reader before engage",
          view._cc_source is None and not view._cap_on)
    phase("MIDLT90-WAIT", "engage as soon as frontier >= 45 (and < 90)")
    t0 = time.time()
    while time.time() - t0 < 240.0:
        app.processEvents()
        fr = view._frontier_s()
        if fr >= 45.0:
            break
        time.sleep(0.1)
    hush()
    fr_at_engage = view._frontier_s()
    check("MIDLT90: engaged mid-show at frontier < 90 s",
          45.0 <= fr_at_engage < 90.0,
          f"frontier={fr_at_engage:.1f}s", kind="data-limited")
    phase("MIDLT90-ENGAGE", f"frontier={fr_at_engage:.1f}s")
    engage_and_measure("MIDLT90")
    health_watch(45.0, "MIDLT90")

    # ---- D1: one live landing sample at this window's measured L ----
    phase("D1-JUMP", "adaptive jump-to-live at the measured L")
    wait_until(lambda: view._cc_lag is not None, 30, "a lag measurement")
    d1_jump_sample("D1")
    health_watch(30.0, "D1")

    phase("END", "engage matrix complete")


try:
    main()
finally:
    npass = sum(1 for _, ok, _k in CHECKS if ok)
    mech = [c for c in CHECKS if c[2] == "mechanism"]
    dlim = [c for c in CHECKS if c[2] == "data-limited"]
    print(f"\nchecks: {npass}/{len(CHECKS)} passed "
          f"(mechanism {sum(1 for c in mech if c[1])}/{len(mech)}, "
          f"data-limited {sum(1 for c in dlim if c[1])}/{len(dlim)})",
          flush=True)
    for name, ok, _k in CHECKS:
        if not ok:
            print("  FAILED:", name, flush=True)
    try:
        view.stop()
    except Exception as exc:  # noqa: BLE001
        print("stop failed:", exc, flush=True)
    pump(2)
    try:
        logging.shutdown()
    except Exception:
        pass
print(f"{elapsed()} done — sync log: "
      f"%APPDATA%\\MichaelTVPlayer\\sync_debug.log", flush=True)
os._exit(0)
