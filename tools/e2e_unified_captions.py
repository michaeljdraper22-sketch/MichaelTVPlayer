# -*- coding: utf-8 -*-
"""E2E: the unified-subtitles promise, end to end on one PlayerView.

Three legs, same window, same style system:
  [L] a captioned live channel (picked via the CAPTIONED priority list —
      same keywords as e2e_live_cc) -> chase -> CC track -> CCSource ->
      app overlay,
  [S] an SRT movie (MKV S_TEXT/UTF8) -> track pick -> local relay ->
      app overlay,
  [A] an ASS movie (MKV S_TEXT/ASS, found by probing with the app's own
      MkvSubParser over HTTP Range) -> track pick -> relay flattens ASS
      to text -> app overlay.

Every leg asserts: the overlay ENGAGED (VLC's spu forced off), captions
PAINTED, and a subtitle STYLE CHANGE applied with NO player rebuild (the
VLCPlayer object survives, playback never stops, the position holds).
Each leg logs which renderer owns the captions. Movies are discovered at
runtime — nothing about the library is hardcoded.

Run:  .venv\\Scripts\\python.exe -X utf8 tools/e2e_unified_captions.py
(a small real window flashes while the test runs; needs the provider
settings in %APPDATA% and CCExtractor — runtime is minutes, the live
first cue can take a while)
"""
import copy
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.live_cc import find_ccextractor  # noqa: E402
from src.mkv_subs import MkvSubParser, is_text_codec  # noqa: E402
from src.player import USER_AGENT  # noqa: E402
from src.ui import subtitle_dialog as sd_mod  # noqa: E402
from src.ui.caption_overlay import CaptionOverlay  # noqa: E402
from src.ui.player_view import PlayerView  # noqa: E402

# ---- provider ----
cfgj = json.load(open(os.path.join(os.environ["APPDATA"],
                                   "MichaelTVPlayer", "settings.json"),
                      encoding="utf-8"))
base, user, pw = (cfgj["server_url"].rstrip("/"), cfgj["username"],
                  cfgj["password"])


def api(action=None, **extra):
    params = {"username": user, "password": pw}
    if action:
        params["action"] = action
    params.update(extra)
    url = base + "/player_api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# 24/7 near-continuously captioned channels, in priority order (same list
# as e2e_live_cc — "4K: ESPN" is the verified carrier, HD news next;
# offshoot variants of a brand can lack caption carriage entirely).
CAPTIONED = ("4k: espn", "fox news hd", "cnn hd", "msnbc hd", "cnbc hd",
             "bbc news", "sky news", "fox news", "cnn", "msnbc", "espn")


def pick_live_channel():
    live = api("get_live_streams")
    ch = next((c for key in CAPTIONED
               for c in live if key in c["name"].lower()), None)
    assert ch, "no channel from the CAPTIONED priority list in the line-up"
    return ch


def probe_movie_codecs(url, nbytes=1 << 20):
    """First ~1 MB over HTTP Range -> the MKV's subtitle codec ids (the
    Tracks element lives at the file head). Uses the app's own parser —
    no ffprobe dependency."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Range": f"bytes=0-{nbytes - 1}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            blob = r.read(nbytes)
    except Exception:  # noqa: BLE001
        return {}
    p = MkvSubParser()
    p.feed(blob)
    return dict(p._track_meta)      # {number: {codec, lang, name}}


def find_movies(budget_s=420.0, max_probes=240):
    """One SRT movie + one ASS movie, discovered at runtime. Anime/
    animation categories are probed first — fansubbed MKVs reliably carry
    ASS tracks; plain categories yield SRT within a few probes. ASS is
    best-effort: libraries rotate, and the leg is skipped (not failed)
    when none surfaces within the budget."""
    want_srt, want_ass = {"S_TEXT/UTF8", "S_UTF8"}, {"S_TEXT/ASS",
                                                     "S_TEXT/SSA",
                                                     "S_ASS", "S_SSA"}
    cats = api("get_vod_categories")
    prio = [c for c in cats if any(k in c["category_name"].lower()
                                   for k in ("anime", "animation", "cartoon"))]
    order = prio + [c for c in cats if c not in prio]
    srt = ass = None
    probes = 0
    t0 = time.time()
    for cat in order:
        if srt and ass:
            break
        if probes >= max_probes or time.time() - t0 > budget_s:
            break
        try:
            vod = api("get_vod_streams", category_id=cat["category_id"])
        except Exception:  # noqa: BLE001
            continue
        for m in vod[:30]:
            if (srt and ass) or probes >= max_probes \
                    or time.time() - t0 > budget_s:
                break
            if (m.get("container_extension") or "mp4") != "mkv":
                continue
            probes += 1
            url = f"{base}/movie/{user}/{pw}/{m['stream_id']}.mkv"
            meta = probe_movie_codecs(url)
            text = {n: t for n, t in meta.items() if is_text_codec(t["codec"])}
            if not text:
                continue
            item = {"kind": "vod", "url": url, "title": m["name"],
                    "text_langs": sorted({t["lang"].lower()
                                          for t in text.values()})}
            if srt is None and any(t["codec"].upper() in want_srt
                                   for t in text.values()):
                srt = item
                print(f"  SRT movie: {m['name']!r} "
                      f"({[t['codec'] for t in text.values()]})", flush=True)
            if ass is None and any(t["codec"].upper() in want_ass
                                   for t in text.values()):
                ass = item
                print(f"  ASS movie: {m['name']!r} "
                      f"({[t['codec'] for t in text.values()]})", flush=True)
    print(f"  (probed {probes} movies in {time.time() - t0:.0f}s)",
          flush=True)
    return srt, ass


print("[0] provider: pick channel + discover movies", flush=True)
assert find_ccextractor(), "CCExtractor not found"
ch = pick_live_channel()
print(f"  live channel: {ch['name']!r}", flush=True)
SRT_MOVIE, ASS_MOVIE = find_movies()
assert SRT_MOVIE, "no SRT (S_TEXT/UTF8) MKV movie found — widen find_movies"
if not ASS_MOVIE:
    print("  (no ASS/SSA MKV in the library right now — leg A will be "
          "SKIPPED, not failed)", flush=True)

app = QtWidgets.QApplication(sys.argv)
cfg = Config.load()
orig_data = copy.deepcopy(cfg.data)      # restored in finally (the style
cfg.data["chase_delay"] = 5              # probe SAVES the config)
cfg.data["profanity"] = {"enabled": False}   # isolate captions from the
#                                           # filter (its relay routing
#                                           # would mask the restart path)
view = PlayerView(cfg)
view._filter_engine.enabled = False
view._attach_done = True
view._attached = True
view.resize(960, 540)
view.show()

PASS, FAIL = [], []
# The style probe is baseline-relative: it drives the config size from
# whatever the user actually saved to a clearly different size, then
# checks the PAINT followed. Near/above the dialog's 96 px ceiling the
# probe shrinks instead of growing.
BASE_SIZE = int((cfg.subtitle_appearance or {}).get("size") or 0)
PROBE_SIZE = 96 if BASE_SIZE <= 53 else max(24, BASE_SIZE // 2)
PROBE_LINE = "Style probe 0123"          # short: never wraps, so ink
#                                           height == the font size


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name, flush=True)


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


def ink_rows(widget):
    """Top/bottom pixel rows carrying paint in a grab of the widget (the
    overlay paints text over transparency — rows with ink = text)."""
    img = widget.grab().toImage()
    if img.format() != img.Format_ARGB32:
        img = img.convertToFormat(img.Format_ARGB32)
    data = img.constBits().asstring(img.sizeInBytes())
    bpl = img.bytesPerLine()
    rows = [y for y in range(img.height())
            if any(data[y * bpl + 3:(y + 1) * bpl:4])]
    return (rows[0], rows[-1]) if rows else None


def probe_line_ink():
    """Paint the probe line through a FRESH CaptionOverlay (same parent,
    same picture rect, same config getter) and measure its ink height.
    A throwaway widget is the point: the real overlay paints without
    erasing (WA_NoSystemBackground), so grabs of it can carry ink from
    earlier cues and pollute the measurement — a new widget has no
    paint history."""
    wid = CaptionOverlay(view)
    wid.bind_config(lambda: view.config.subtitle_appearance)
    wid.setGeometry(view._cap_wid.geometry())
    wid.set_lines([PROBE_LINE])
    pump(0.15)
    r = ink_rows(wid)
    wid.set_lines([])
    wid.deleteLater()
    app.processEvents()
    return (r[1] - r[0]) if r else -1


class ScriptedStyleDialog:
    """Stand-in for SubtitleDialog that OKs a size change (BASE_SIZE ->
    PROBE_SIZE): drives the REAL PlayerView._open_sub_settings decision
    path — while the overlay owns captions the style must apply by
    repaint alone."""
    calls = 0

    def __init__(self, config, apply_delay, parent=None, apply_live=None):
        self._config = config
        self._apply_live = apply_live

    def exec_(self):
        ScriptedStyleDialog.calls += 1
        ap = dict(self._config.subtitle_appearance)
        ap["size"] = PROBE_SIZE
        self._config.subtitle_appearance = ap
        self._config.save()
        if self._apply_live:
            self._apply_live(ap)   # the real dialog applies mid-dialog now
        return 1                      # QDialog.Accepted


sd_mod.SubtitleDialog = ScriptedStyleDialog


def style_change_without_rebuild(tag, pos_tolerance_s=15):
    """The unified promise's third leg: a style change applies with NO
    player rebuild while the overlay owns the captions. The painted
    before/after comes from fresh probe widgets, so the measurement is
    deterministic whatever the real overlay showed before."""
    h_before = probe_line_ink()
    vlc_before = view.vlc
    t_before = view.vlc.get_time()
    calls_before = ScriptedStyleDialog.calls
    view._open_sub_settings()         # real path: config -> repaint only
    h_after = probe_line_ink()

    print(f"  renderer: app overlay keeps captions through the style "
          f"change ({tag})", flush=True)
    check(f"[{tag}] style dialog went through the scripted OK",
          ScriptedStyleDialog.calls == calls_before + 1)
    check(f"[{tag}] overlay re-read the new style (size {PROBE_SIZE})",
          view._cap_wid._appearance().get("size") == PROBE_SIZE)
    grew = PROBE_SIZE > BASE_SIZE
    check(f"[{tag}] painted text followed the size change "
          f"({h_before}px -> {h_after}px; size {BASE_SIZE} -> {PROBE_SIZE})",
          h_before > 0 and (h_after > h_before * 1.15 if grew
                            else h_after < h_before * 0.87))
    check(f"[{tag}] NO player rebuild (same VLCPlayer object)",
          view.vlc is vlc_before)
    check(f"[{tag}] playback never stopped", view.vlc.is_playing())
    if view._is_vod():
        dt = abs(view.vlc.get_time() - t_before)
        check(f"[{tag}] position held through the style change "
              f"({dt / 1000.0:.1f}s drift)", dt < pos_tolerance_s * 1000)
    else:
        check(f"[{tag}] CCSource survived the style change",
              view._cc_source is not None and view._cc_source._alive)
    # back to the user's baseline size for the next leg
    ap = dict(cfg.subtitle_appearance)
    ap["size"] = BASE_SIZE
    cfg.subtitle_appearance = ap
    view._cap_wid.update()


def report_renderer(tag, source):
    owner = "app overlay" if view._cap_on else "VLC spu"
    print(f"  renderer: {owner} ({source})", flush=True)
    check(f"[{tag}] renderer is the app overlay", view._cap_on)
    check(f"[{tag}] VLC's own spu stays OFF under the overlay",
          view.vlc.active_spu() == -1)


def pick_vod_track(tracks, langs):
    """Best CC-menu pick for a movie: an ASS/SSA-named or English text
    track first (full track over a Forced variant), then any track the
    overlay can render, then anything at all."""
    def score(n):
        low = n.lower()
        return (0 if re.search(r"\bass\b|\bssa\b", low)
                or "english" in low else 1, 1 if "forced" in low else 0)
    eligible = [(t, n) for t, n in tracks if view._cap_eligible(n)]
    for pool in (eligible, tracks):
        usable = [tn for tn in pool if score(tn[1])[1] == 0] or pool
        usable.sort(key=lambda tn: score(tn[1]))
        if usable:
            return usable[0]
    return tracks[0]


def seek_into_latest_cue():
    """Deterministic paint: jump the playhead into the newest cue's
    window, like e2e_captions does."""
    c = view._cap_cues.cues[-1]
    mid = (c[0] + min(c[1], c[0] + 2.0)) / 2.0
    if view._is_vod():
        view.vlc.set_time(int(mid * 1000))
    else:
        view._chase_seek(mid)
    return wait_until(lambda: bool(view._cap_wid._lines), 15,
                      "overlay lines in cue window")


try:
    # ================= leg L: live closed captions =================
    print(f"[L] live channel {ch['name']!r} via the priority list",
          flush=True)
    view.play_media({"kind": "live",
                     "url": f"{base}/live/{user}/{pw}/{ch['stream_id']}.ts",
                     "title": ch["name"], "stream_id": ch["stream_id"]})
    ok = wait_until(lambda: view._mode == "chase", 30, "chase mode")
    check("[L] always-on chase engages on play", ok)
    ok = wait_until(lambda: view.vlc.is_playing(), 30, "chase playing")
    check("[L] chase playback running", ok)

    ok = wait_until(
        lambda: any("caption" in n.lower() or n.lower().startswith("cc")
                    for _, n in view.vlc.spu_tracks()), 20, "CC tracks")
    tracks = view.vlc.spu_tracks()
    check(f"[L] CC track listed ({[n for _, n in tracks][:3]})", ok)
    if not ok:
        raise SystemExit("no CC tracks on this channel — pick another")
    tid, name = next((t, n) for t, n in tracks
                     if "caption" in n.lower() or n.lower().startswith("cc"))
    view._select_spu(tid, name)
    check("[L] overlay engaged by the CC pick", view._cap_on)
    check("[L] CCSource started on the DVR buffer",
          view._cc_source is not None and view._cc_source._alive)
    report_renderer("L", "live CC, CCExtractor tailing the DVR buffer")

    # NOTE: channels run long un-captioned stretches — the first cue can
    # take minutes of content (same caveat as e2e_captions).
    ok = wait_until(lambda: len(view._cap_cues.cues) > 0, 180, "first cue")
    check(f"[L] cues arriving ({len(view._cap_cues.cues)} so far)", ok)
    if ok:
        ok2 = seek_into_latest_cue()
        check(f"[L] overlay paints inside the cue window "
              f"({view._cap_wid._lines[:1]})", ok2)
    else:
        check("[L] overlay paints inside the cue window", False)
    check("[L] no fallback latched", not view._cap_fail)

    style_change_without_rebuild("L")
    view._select_spu(-1, "")
    check("[L] Off drops the overlay, playback keeps running",
          not view._cap_on and view.vlc.is_playing())

    # ================= leg S: SRT movie through the relay ============
    print(f"[S] SRT movie {SRT_MOVIE['title']!r}", flush=True)
    view.play_media(dict(SRT_MOVIE))
    ok = wait_until(lambda: view.vlc.is_playing(), 40, "direct playback")
    check("[S] direct playback starts (no captions wanted yet)", ok)
    check("[S] no relay until captions are wanted", view._vod_relay is None)

    ok = wait_until(lambda: view.vlc.spu_tracks(), 30, "subtitle tracks")
    tracks = view.vlc.spu_tracks()
    check(f"[S] tracks listed ({len(tracks)})", ok)
    tid, name = pick_vod_track(tracks, SRT_MOVIE["text_langs"])
    print(f"  picking {name!r}", flush=True)
    view._select_spu(tid, name)
    ok = wait_until(lambda: view._vod_relay is not None, 30, "relay start")
    check("[S] playback rerouted through the local relay", ok)
    ok = wait_until(lambda: view.vlc.is_playing(), 40, "relay playback")
    check("[S] playback running through the relay", ok)
    report_renderer("S", "VOD relay, SRT track")

    pump(3)
    if view.vlc.get_length() > 240000:      # skip the sparse opening
        view.vlc.set_time(105 * 1000)
        pump(5)
    ok = wait_until(lambda: len(view._cap_cues.cues) > 5, 90, "cue stream")
    check(f"[S] cues flowing ({len(view._cap_cues.cues)})", ok)
    ok = wait_until(
        lambda: any(c.upper().startswith("S_TEXT/UTF8") or c.upper() == "S_UTF8"
                    for c in (view._vod_relay.parser_tracks or {}).values()),
        20, "SRT codec confirmed by the MKV parser")
    check("[S] relay parser confirms an SRT text track", ok)
    check("[S] no fallback latched", not view._cap_fail)
    if view._cap_cues.cues:
        ok = seek_into_latest_cue()
        check(f"[S] overlay paints ({view._cap_wid._lines[:1]})", ok)

    style_change_without_rebuild("S")
    view._select_spu(-1, "")
    check("[S] Off drops the overlay, playback keeps running",
          not view._cap_on and view.vlc.is_playing())

    # ================= leg A: ASS movie, flattened by the relay ======
    if not ASS_MOVIE:
        print("[A] SKIPPED — no ASS/SSA MKV movie in the library during "
              "discovery (the flattening path is covered by "
              "test_vod_splitter's ASS e2e against a generated file)",
              flush=True)
    else:
        print(f"[A] ASS movie {ASS_MOVIE['title']!r}", flush=True)
        view.play_media(dict(ASS_MOVIE))
        ok = wait_until(lambda: view.vlc.is_playing(), 40, "direct playback")
        check("[A] direct playback starts (no captions wanted yet)", ok)
        check("[A] no relay until captions are wanted", view._vod_relay is None)

        ok = wait_until(lambda: view.vlc.spu_tracks(), 30, "subtitle tracks")
        tracks = view.vlc.spu_tracks()
        check(f"[A] tracks listed ({len(tracks)})", ok)
        tid, name = pick_vod_track(tracks, ASS_MOVIE["text_langs"])
        print(f"  picking {name!r}", flush=True)
        view._select_spu(tid, name)
        ok = wait_until(lambda: view._vod_relay is not None, 30, "relay start")
        check("[A] playback rerouted through the local relay", ok)
        ok = wait_until(lambda: view.vlc.is_playing(), 40, "relay playback")
        check("[A] playback running through the relay", ok)
        ok = wait_until(
            lambda: any("ASS" in c.upper() or "SSA" in c.upper()
                        for c in (view._vod_relay.parser_tracks or {}).values()),
            20, "ASS codec confirmed by the MKV parser")
        check("[A] relay parser confirms a genuine ASS/SSA track", ok)
        report_renderer("A", "VOD relay, ASS track flattened to text")

        pump(3)
        if view.vlc.get_length() > 240000:      # skip the sparse opening
            view.vlc.set_time(105 * 1000)
            pump(5)
        # ASS tracks are often signs/forced-part tracks — a handful of cues
        # is normal content, not a failure. The claims that matter below:
        # the flattened text PAINTS and the codec is genuine ASS/SSA.
        ok = wait_until(lambda: view._cap_cues.cues, 120, "cue stream")
        sample = (view._cap_cues.cues[-1][2][:40] if view._cap_cues.cues
                  else "")
        check(f"[A] ASS cues flowing, flattened "
              f"({len(view._cap_cues.cues)} — e.g. {sample!r})", ok)
        check("[A] no fallback latched", not view._cap_fail)
        if view._cap_cues.cues:
            ok = seek_into_latest_cue()
            check(f"[A] overlay paints the flattened ASS "
                  f"({view._cap_wid._lines[:1]})", ok)

        style_change_without_rebuild("A")
        view._select_spu(-1, "")
        check("[A] Off drops the overlay, playback keeps running",
              not view._cap_on and view.vlc.is_playing())
finally:
    try:
        view.stop()
        pump(1)
    except Exception:  # noqa: BLE001
        pass
    cfg.data = orig_data          # the style probe saved the config —
    cfg.save()                    # put the user's settings back

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed", flush=True)
for f in FAIL:
    print("  FAILED:", f, flush=True)
os._exit(1 if FAIL else 0)
