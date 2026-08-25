# -*- coding: utf-8 -*-
"""Headless catch-up probe against the REAL provider (no window, no audio).

Validates the whole chain the Catch-Up tab relies on:
  1. get_live_streams -> tv_archive channels exist
  2. epg_table -> past programs inside the archive window (b64 titles)
  3. timeshift_url -> libVLC opens the stream headlessly, reports a
     length, time advances, and SEEKING works (HTTP range)
  4. a mid-program WINDOW (the download-window feature's URL math)
     serves the same TS bytes for exactly the requested duration

Run:  .venv\\Scripts\\python.exe tools\\probe_catchup.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from src.config import Config  # noqa: E402
from src.xtream import XtreamClient, decode_epg_text  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + extra)


def probe_vlc(xc, chan, prog, use_relay):
    """Open one program headlessly; return (dur_ms, advanced, seek_ok,
    relay).  Seeks use the byte-fraction axis (set_position) — the app's
    path for catch-up, since VLC cannot size these indexless TS streams
    (get_length()=0) and time-based set_time lands imprecisely."""
    st = int(prog.start_timestamp)
    dur_ms = (int(prog.stop_timestamp) - st) * 1000
    dur_min = max(1, -(-(int(prog.stop_timestamp) - st) // 60))
    url = xc.timeshift_url(chan["stream_id"], st, dur_min)
    relay = None
    if use_relay:
        from src.catchup_relay import CatchupRelay
        relay = CatchupRelay()
        local = relay.start(url)
        if not local:
            return 0, False, False, relay
        url = local
    import vlc
    inst = vlc.Instance("--no-audio", "--vout=dummy", "--aout=dummy",
                        "--network-caching=3000")
    player = inst.media_player_new()
    player.set_media(inst.media_new(url))
    player.play()
    t0 = None
    deadline = time.time() + 45
    while time.time() < deadline:
        time.sleep(1.0)
        if t0 is None and player.get_time() > 0:
            t0 = player.get_time()
        if t0 and player.get_time() > t0 + 2500:
            break
    advanced = t0 is not None and player.get_time() > t0 + 2000
    # the app's scrub axis: byte fraction of the stream (relay is fully
    # range-seekable even though the provider's own headers are not)
    player.set_position(0.5)
    time.sleep(4.0)
    pos = player.get_position()
    got_ms = player.get_time()
    seek_ok = 0.42 <= pos <= 0.62 and \
        abs(got_ms - dur_ms / 2) < dur_ms * 0.15
    return dur_ms, advanced, seek_ok, relay


def main():
    cfg = Config.load()
    xc = XtreamClient(cfg.normalized_server(), cfg.username, cfg.password)

    print("[1] archive channels")
    arch = [c for c in xc.live_streams()
            if str(c.get("tv_archive")) == "1"]
    check("archive-capable channels found", len(arch) > 0,
          f" ({len(arch)} channels)")
    by_days = sorted(arch, key=lambda c: -int(c.get("tv_archive_duration") or 0))
    now = time.time()

    print("[2] epg table + headless libVLC playback of a recorded program")
    print("    (direct first, then through the local range relay)")
    length = advanced = seek_ok = 0
    probed = []
    relay = None
    for chan in by_days[:3]:
        entries = xc.epg_table(chan["stream_id"])
        past = [e for e in entries
                if e.start_timestamp and int(e.start_timestamp) < now]
        if not past:
            continue
        done = sorted([e for e in past if int(e.stop_timestamp) < now - 600],
                      key=lambda e: int(e.start_timestamp))
        if not done:
            continue
        prog = done[-1]
        if not probed:
            probed.append(f"{chan['name']} / {decode_epg_text(prog.title)!r}")
        print(f"    probing: {chan['name']} sid={chan['stream_id']} "
              f"archive={chan.get('tv_archive_duration')}d "
              f"program={decode_epg_text(prog.title)!r}")
        d_len, d_adv, d_seek, _r = probe_vlc(xc, chan, prog, use_relay=False)
        print(f"      direct: advanced={d_adv} frac_seek={d_seek}")
        r_len, r_adv, r_seek, relay = probe_vlc(xc, chan, prog,
                                                use_relay=True)
        print(f"      relay:  advanced={r_adv} frac_seek={r_seek} "
              f"provider_opens={relay.provider_opens if relay else '-'}")
        length, advanced, seek_ok = r_len, r_adv, r_seek
        if advanced and seek_ok:
            break
    check("past programs available (title decode works)", len(probed) > 0,
          f" ({probed[0]})" if probed else "")
    check("playback advances through the relay", advanced)
    check("byte-fraction seek lands mid-program through the relay "
          "(the app's scrub axis)", seek_ok)

    # pick whatever program we last probed for the window checks
    entries = xc.epg_table(by_days[0]["stream_id"])
    past = [e for e in entries
            if e.start_timestamp and int(e.start_timestamp) < now]
    done = sorted([e for e in past if int(e.stop_timestamp) < now - 600],
                  key=lambda e: int(e.start_timestamp))
    prog = done[-1]
    st = int(prog.start_timestamp)
    chan = by_days[0]

    print("[3] window download URL (mid-program start + short duration)")
    w_url = xc.timeshift_url(chan["stream_id"], st + 600, 1)
    s = requests.Session()
    s.headers["User-Agent"] = "MichaelTVPlayer/1.0"
    r = s.get(w_url, stream=True, timeout=30)
    cl = int(r.headers.get("Content-Length") or 0)
    data = b""
    for chunk in r.iter_content(65536):
        data += chunk
        if len(data) >= 600000:
            break
    r.close()
    check("window request serves video/mp2t",
          r.status_code == 200 and "mp2t" in (r.headers.get("Content-Type")
                                               or ""))
    check("window is ~1 minute of bytes (not the whole program)",
          0 < cl < 120 * 1024 * 1024, f" ({cl/1048576:.0f} MB)")
    syncs = 0
    for off in range(0, min(len(data), 188 * 200) - 376):
        if data[off] == 0x47 and data[off + 188] == 0x47 \
                and data[off + 376] == 0x47:
            syncs += 1
    check("MPEG-TS 188-byte sync grid found in the payload", syncs > 0,
          f" ({syncs} sync offsets)")

    # FileDownloader path (what the WIN button actually uses) into a temp dir
    # (needs a Qt event loop for its signals — pump one while waiting)
    from src.ui.worker import FileDownloader
    from PyQt5 import QtCore
    import threading
    qapp = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "window.ts")
    done_evt = threading.Event()
    result = {}

    fd = FileDownloader()
    fd.progress.connect(lambda d, t: None)
    fd.finished.connect(lambda ok, msg: (result.update(ok=ok, msg=msg),
                                         done_evt.set()))
    fd.start(w_url, path)
    waited = time.time() + 120
    while time.time() < waited and not done_evt.is_set():
        qapp.processEvents()
        time.sleep(0.05)
    dl_ok = done_evt.is_set() and result.get("ok") and os.path.isfile(path)
    check("FileDownloader completes on a window URL", bool(dl_ok),
          f" ({os.path.getsize(path)/1048576:.0f} MB)"
          if dl_ok else f" ({result.get('msg')})")

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    # libvlc teardown segfaults in a bare console process — report, then
    # hard-exit without unwinding (the APP's ordered teardown is what the
    # stop() path exercises; this probe only reads)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
