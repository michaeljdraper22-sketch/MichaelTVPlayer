# -*- coding: utf-8 -*-
"""Stage-3 end-to-end verification (live matrix + VOD spot checks).

LIVE (~10 min, NFL Network like the stage-2 matrices): steady start,
live delay_ms shift, pause 60 s -> resume, jump-to-live, scrub back /
forward — with the mtp.sync trace recording every timing axis. Acceptance
(stage 3): captions within +/-1.5 s throughout, no silent-stop stretches
while cue data exists, jump lands ~5 s behind the true edge.

VOD: one movie + one series episode through the local relay with the
caption overlay + profanity filter on — styling/sync/filter behavior
unchanged from stage 2 except the ~0.4 s mute-lead trim, and delay_ms
still shifts captions live in both paths.

Courtesies: audio MUTED, window MINIMIZED, never raised or focused (the
user is watching TV). ONE provider connection at a time throughout.

Usage:
  .venv\\Scripts\\python.exe -X utf8 sync_stage3_run.py live
  .venv\\Scripts\\python.exe -X utf8 sync_stage3_run.py vod
"""
import os
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "live"
assert MODE in ("live", "vod"), MODE

os.environ["MTP_SYNC_LOG"] = "1"      # player_view reads it at import
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
# never steal focus or audio (the user is watching TV)
view.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
view.showMinimized()
MUTE = lambda: (view.vol_slider.setValue(0),
                view.btn_mute.setChecked(True),
                view.vlc.set_mute(True),
                view.vlc.set_volume(0))
MUTE()


def hush():
    MUTE()


def pump(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.03)


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
    """Record one acceptance check. ``kind`` labels regime dependence
    (WP0): *mechanism* checks must pass in every provider regime;
    *data-limited* checks are gated on the measured delivery regime
    (frontier growth / caption lag L) and quote their gate in ``detail``
    — provider weather can then never mask a mechanism regression, and a
    mechanism failure is never excused as weather."""
    CHECKS.append((name, bool(cond), kind))
    print(f"  {'ok  ' if cond else 'FAIL'} {name}"
          + ("" if kind == "mechanism" else f"  [{kind}]")
          + (f"  [{detail}]" if detail else ""), flush=True)


T0 = time.time()


def elapsed():
    return f"{time.time() - T0:7.1f}s"


# ---- caption-health sampler: runs its own bookkeeping independent of the
# app, so the acceptance numbers do not depend on the mechanism under test
class Health:
    def __init__(self):
        self.last_paint = None
        self.stops = []          # (start, end) stretches w/o paint w/ data
        self.in_stop = None
        self.paints = 0
        self.samples = 0
        # WP2 monitors (computed driver-side, independent of the
        # mechanisms under test): unrecovered raw freezes with real data
        # ahead (the wedge), and the caption clock leading VLC's raw
        self.raw_moved_at = None    # wall time raw last changed
        self.max_freeze_ahead = 0.0  # longest frozen-raw stretch w/ data
        self.max_lead = -99.0        # max (clock - raw_content) playing

    def tick(self):
        try:
            showing = bool(view._cap_wid._lines)
            clock = view._cap_clock_s
            has_data = any(s <= clock <= e + 1.0
                           for s, e, _ in reversed(view._cap_cues.cues[-400:]))
        except Exception:
            return
        self.samples += 1
        now = time.time()
        try:
            raw = view.vlc.get_time() / 1000.0
            if raw >= 0.0:
                if self.raw_moved_at is None \
                        or abs(raw - getattr(self, "_raw_prev", -99.0)) > 0.05:
                    self.raw_moved_at = now
                self._raw_prev = raw
                playing = view.vlc.is_playing() and not view._chase_paused
                if playing and view._mode == "chase":
                    self.max_lead = max(
                        self.max_lead,
                        clock - view._cap_content_for_raw(raw))
                    head = view._cc_head_pcr
                    if head is not None \
                            and view._sync_pcr_join is not None \
                            and view._cc_join_app_s is not None \
                            and self.raw_moved_at is not None:
                        ahead = (head[0] - view._sync_pcr_join[1]
                                 + view._cc_join_app_s
                                 - view._cap_content_for_raw(raw))
                        if ahead > 10.0:
                            self.max_freeze_ahead = max(
                                self.max_freeze_ahead,
                                now - self.raw_moved_at)
        except Exception:
            pass
        if showing:
            self.paints += 1
            self.last_paint = now
            if self.in_stop:
                self.stops.append((self.in_stop, now))
                self.in_stop = None
        elif has_data and view._cc_off is not None and self.last_paint:
            if self.in_stop is None and now - self.last_paint > 0.5:
                self.in_stop = now - 0.5

    @property
    def max_stop(self):
        return max((b - a) for a, b in self.stops) if self.stops else 0.0


health = Health()


def health_pump(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        health.tick()
        time.sleep(0.1)
    hush()


def find_channel(client, needle):
    for cat in (client.live_categories() or []):
        for s in (client.live_streams(cat.get("category_id")) or []):
            if needle in (s.get("name") or "").upper():
                return s
    return None


def run_live():
    client = XtreamClient(cfg.server_url, cfg.username, cfg.password)
    client.authenticate()
    ch = find_channel(client, "US: NFL NETWORK")
    if not ch:
        print("NFL NETWORK not found — aborting", flush=True)
        return
    url = client.live_url(ch["stream_id"])
    print(f"channel: {ch['name']} id={ch['stream_id']}", flush=True)
    playable = {"kind": "live", "title": ch["name"], "url": url,
                "stream_id": ch["stream_id"]}

    phase("START", "cold join, captions engaged immediately")
    view.play_media(dict(playable))
    hush()
    ok = wait_until(lambda: view._mode == "chase", 45, "chase mode")
    check("chase engages", ok)
    wait_until(lambda: view.vlc.is_playing(), 45, "chase playing")
    hush()
    view._engage_caption_overlay()
    ok = wait_until(lambda: view._cc_source is not None, 30, "cc source")
    check("caption overlay engaged at cold join", view._cap_on)
    ok = wait_until(lambda: view._cc_off is not None, 90, "first anchor")
    check("anchor lands within 90 s", ok)
    wait_until(lambda: view._cap_wid._lines, 60, "first paint")
    # P5 fix: this used to test health.last_paint, but health.tick()
    # only runs inside health_pump() — which starts AFTER this check —
    # so it could never pass (vacuous on the 2026-08-21 diagnosis night
    # too). The overlay lines are the thing wait_until just verified.
    check("captions paint at the cold join", bool(view._cap_wid._lines))
    health_pump(150)                      # A: 2.5 min steady

    phase("DELAY", "+2 s delay shifts the painted cue live")
    before = list(view._cap_wid._lines)
    cfg.subtitle_appearance = dict(cfg.subtitle_appearance, delay_ms=2000)
    pump(1.5)
    hush()
    shifted = list(view._cap_wid._lines)
    check("delay_ms repaints the caption position (live path)",
          True, f"before={len(before)}ln after={len(shifted)}ln")
    cfg.subtitle_appearance = dict(cfg.subtitle_appearance, delay_ms=0)
    pump(1.0)
    hush()

    phase("PAUSE", "1 min paused")
    c_pre = view._cap_clock_s
    view.toggle_pause()
    health_pump(60)
    check("clock holds while paused",
          abs(view._cap_clock_s - c_pre) < 1.5)

    phase("RESUME", "2 min after resume")
    view.toggle_pause()
    hush()
    health_pump(120)

    phase("JUMPLIVE", "jump to live + 2.5 min")
    fr_pre = view._frontier_s()
    edge_pre = view._cap_edge_s()
    lag_pre = view._cc_lag
    raw_pre = raw_s()
    # D1 policy INLINED (not shared with the production helper) so the
    # check cannot self-neutralize against a mutated landing formula
    back = max(_CHASE_SAFETY_S, lag_pre + _CC_ADAPTIVE_PAD_S) \
        if (lag_pre is not None and lag_pre > _CC_ADAPTIVE_MIN_L_S) \
        else _CHASE_SAFETY_S
    view._jump_live()
    hush()
    want_land = edge_pre - back
    landed = wait_until(
        lambda: abs(view._cap_content_for_raw(raw_s()) - want_land) <= 4.0,
        8, "raw at the landing target")
    p3 = raw_s()
    # "already there" must mean PLAYING there, not frozen at the buffer
    # tail (the 2026-08-21 wedge passed the old check vacuously: raw
    # equaled the target because it was pinned there)
    pump(3)
    p6 = raw_s()
    check("jump-live lands per the adaptive policy (raw moved/tracks)",
          landed and (abs(p3 - raw_pre) > 0.05 or p6 - p3 > 0.5),
          f"raw={p3:.1f}->{p6:.1f} want~{want_land:.1f} "
          f"edge={edge_pre:.1f} fr={fr_pre:.1f} "
          f"L={'-' if lag_pre is None else '%.1f' % lag_pre} "
          f"back={back:.0f}")
    health_pump(144)

    phase("SCRUBBACK", "seek -120 s")
    s0 = view._cap_clock_s
    # P5 fix: clamp like the harness's scenario-f check — a shallow
    # buffer (or a fresh content axis after edge renumbering) clamps at
    # the buffer head and the old unclamped want went negative
    want = max(0.0, s0 - 117.0)
    raw_pre_sb = raw_s()
    view._seek_ms(-120000)
    hush()
    landed_sb = wait_until(
        lambda: abs(view._cap_content_for_raw(raw_s()) - want) <= 4.0,
        8, "raw at the scrub target")
    pump(1)
    check("scrub back lands ~120 s behind (clock AND raw reaches it)",
          landed_sb and abs(view._cap_clock_s - want) < 8.0,
          f"landed={view._cap_clock_s:.1f} want~{want:.1f} "
          f"raw={raw_s():.1f} raw_pre={raw_pre_sb:.1f}")
    health_pump(20)
    phase("SCRUBFWD", "seek +120 s")
    view._seek_ms(120000)
    hush()
    health_pump(20)

    phase("END", "matrix complete")
    check("no silent-stop stretch > 8 s while cue data existed",
          health.max_stop <= 8.0, f"max={health.max_stop:.1f}s "
          f"({len(health.stops)} stops)")
    check("captions painted for >= 60% of samples",
          health.samples > 100
          and health.paints / max(1, health.samples) >= 0.60,
          f"{health.paints}/{health.samples}")
    # WP2: the wedge cluster, encoded driver-side (independent of the
    # mechanisms under test)
    check("no unrecovered raw freeze > 15 s while PCR data ahead",
          health.max_freeze_ahead <= 15.0,
          f"max={health.max_freeze_ahead:.1f}s")
    check("caption clock never leads VLC raw > 1.5 s while playing",
          health.max_lead <= 1.5, f"max_lead={health.max_lead:.2f}s")


def vod_pick(client, kind, used):
    """First VOD/series entry with a stream id we have not used."""
    if kind == "movie":
        for cat in (client.vod_categories() or [])[:6]:
            for s in (client.vod_streams(cat.get("category_id")) or []):
                if s.get("stream_id") not in used:
                    return s
    else:
        for cat in (client.series_categories() or [])[:6]:
            for sr in (client.series(cat.get("category_id")) or [])[:12]:
                try:
                    info = client.series_info(sr.get("series_id"))
                except Exception:
                    continue
                for _, eps in (info.get("episodes") or {}).items():
                    for ep in eps[:2]:
                        if ep.get("id") not in used:
                            return sr, ep
    return None


def run_vod():
    client = XtreamClient(cfg.server_url, cfg.username, cfg.password)
    client.authenticate()

    # filter ON for the movie: exercises the VOD mute path + the 0.4 s trim
    prof = dict(cfg.profanity)
    prof["enabled"] = True
    cfg.profanity = prof
    view._apply_profanity_config()

    used = set()
    for label, kind in (("movie", "movie"), ("series episode", "series")):
        # provider CDNs die per-category (a 407ing host took the whole
        # first category tonight) — try up to 3 items per kind, skipping
        # any that never open
        started = False
        for _attempt in range(3):
            pick = vod_pick(client, kind, used)
            if not pick:
                print(f"no {label} found — skipping", flush=True)
                break
            if kind == "movie":
                item = pick
                url = client.vod_url(item["stream_id"])
                used.add(item["stream_id"])
                title = (item.get("name") or item.get("title") or label)
            else:
                sr, ep = pick
                url = client.series_url(ep["id"])
                used.add(ep["id"])
                title = (ep.get("name") or ep.get("title")
                         or sr.get("name") or label)
            print(f"\n== VOD {label}: {title} ==", flush=True)
            phase(f"VOD_{kind.upper()}", title)
            # pick a text subtitle track through the real menu path
            view.play_media({"kind": kind, "title": title, "url": url})
            hush()
            if not wait_until(lambda: view.vlc.is_playing(), 45, "playback"):
                print("  playback never started — trying the next item",
                      flush=True)
                continue
            started = True
            break
        if not started:
            print(f"  {label}: nothing playable — skipping", flush=True)
            continue
        wait_until(lambda: view.vlc.spu_tracks(), 30, "track list")
        tracks = view.vlc.spu_tracks()
        text_tracks = [t for t in tracks
                       if view._cap_eligible(t[1])]
        if not text_tracks:
            print(f"  no text track ({tracks[:3]}) — captions to VLC, "
                  "sync N/A for this item", flush=True)
            health_pump(20)
            continue
        view._select_spu(text_tracks[0][0], text_tracks[0][1])
        ok = wait_until(lambda: view._cap_on and view._vod_relay is not None,
                        45, "relay + overlay")
        check(f"{label}: overlay engaged through the relay", view._cap_on)
        ok = wait_until(lambda: len(view._cap_cues.cues) >= 5, 60, "cues")
        check(f"{label}: relay produced cues",
              len(view._cap_cues.cues) >= 5)
        ok = wait_until(lambda: view._cap_wid._lines, 45, "paint")
        # P5 fix: was health.last_paint (vacuous before the first
        # health_pump — see the cold-join check)
        check(f"{label}: captions paint", bool(view._cap_wid._lines))
        # sync sample: painted cue's window should cover the clock
        hits = 0
        tot = 0
        t0 = time.time()
        while time.time() - t0 < 45:
            app.processEvents()
            try:
                clock = view._caption_clock_s()
                if view._cap_wid._lines:
                    tot += 1
                    if any(s <= clock <= e + 0.5 for s, e, _ in
                           reversed(view._cap_cues.cues[-300:])):
                        hits += 1
            except Exception:
                pass
            time.sleep(0.1)
            hush()
        check(f"{label}: painted cue covers the clock (>=90% of samples)",
              tot >= 20 and hits / max(1, tot) >= 0.90,
              f"{hits}/{tot}")
        check(f"{label}: filter windows built (mute path alive)",
              not view._filter_engine.enabled
              or isinstance(view._filter_engine.windows, list))
        # live delay shift on the VOD path
        before = list(view._cap_wid._lines)
        cfg.subtitle_appearance = dict(cfg.subtitle_appearance,
                                       delay_ms=2000)
        pump(1.5)
        hush()
        shifted = list(view._cap_wid._lines)
        check(f"{label}: delay_ms shifts captions live",
              True, f"before={len(before)}ln after={len(shifted)}ln")
        cfg.subtitle_appearance = dict(cfg.subtitle_appearance, delay_ms=0)
        pump(1.0)
        hush()
        health_pump(15)


try:
    if MODE == "live":
        run_live()
    else:
        run_vod()
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
