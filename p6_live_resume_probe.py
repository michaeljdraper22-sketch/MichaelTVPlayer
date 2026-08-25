"""LIVE probe: catch-up playback + window download must coexist now.

Reproduces the 8/25 user freeze end-to-end with the FIXED code:
  - CatchupRelay fronts the real timeshift URL (as in the app),
  - a headless VLC (no audio, dummy vout — nothing visible/audible)
    plays through the relay; its clock is sampled every 2 s,
  - a real FileDownloader pulls a 5-min window URL concurrently.

PASS criteria:
  - the download finishes OK with the full promised Content-Length,
  - VLC's clock keeps advancing across the whole overlap and is still
    advancing AFTER the download ends (the old code froze here: the
    provider killed the playback connection ~15-30 s into the download
    and nothing recovered).
Run:  .venv\\Scripts\\python.exe p6_live_resume_probe.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vlc  # noqa: E402  (venv python-vlc)

from PyQt5 import QtCore  # noqa: E402

from src.catchup_relay import CatchupRelay  # noqa: E402
from src.ui.worker import FileDownloader  # noqa: E402

PLAY = ("http://cf.534842.xyz/streaming/timeshift.php"
        "?username=726352471c&password=d809266e91"
        "&stream=497001&start=2026-08-25:02-15&duration=285&extension=ts")
WIN = ("http://cf.534842.xyz/streaming/timeshift.php"
       "?username=726352471c&password=d809266e91"
       "&stream=497001&start=2026-08-25:02-44&duration=5&extension=ts")


def main():
    app = QtCore.QCoreApplication([])
    t0 = time.time()
    relay = CatchupRelay()
    local = relay.start(PLAY)
    assert local, "relay failed to start"
    print(f"[relay] {local} total={relay.total / 1e6:.0f} MB", flush=True)

    inst = vlc.Instance(["--ignore-config", "--no-stats",
                         "--no-audio", "--vout=dummy",
                         "--network-caching=1500", "--live-caching=1500"])
    player = inst.media_player_new()
    media = inst.media_new(local)
    media.add_option("http-user-agent=MichaelTVPlayer/1.0")
    player.set_media(media)
    player.play()
    time.sleep(4.0)                       # let playback establish

    dl_result = {}
    last = {"done": 0, "total": 0}
    dl = FileDownloader()
    dl.progress.connect(lambda d, tot: last.update(done=d, total=tot))
    dl.finished.connect(lambda ok, msg: dl_result.update(ok=ok, msg=msg))
    tmp = tempfile.mkdtemp(prefix="mtp_winprobe_")
    path = os.path.join(tmp, "window.ts")
    dl.start(WIN, path, max_resume=12)
    print("[dl] started", flush=True)

    samples = []
    while not dl_result and time.time() - t0 < 300:
        time.sleep(2.0)
        app.processEvents()
        playing = bool(player.is_playing())
        t = player.get_time()
        samples.append((round(time.time() - t0, 1), playing, t))
        print(f"  t+{samples[-1][0]:6.1f}s  playing={playing}  "
              f"vlc_time={t / 1000:.1f}s  "
              f"dl={last['done'] / 1048576:.0f}/{last['total'] / 1048576:.0f}MB",
              flush=True)

    # after the download: does playback KEEP advancing for 15 more seconds?
    post = []
    tend = time.time() + 15.0
    while time.time() < tend:
        time.sleep(2.0)
        app.processEvents()
        post.append((bool(player.is_playing()), player.get_time()))
        print(f"  post  playing={post[-1][0]}  "
              f"vlc_time={post[-1][1] / 1000:.1f}s", flush=True)

    player.stop()
    relay.stop()

    ok_dl = dl_result.get("ok") is True
    size = os.path.getsize(path) if os.path.exists(path) else 0
    total = last["total"]
    print(f"[dl] finished ok={ok_dl} msg={dl_result.get('msg', '')!r}",
          flush=True)
    print(f"[dl] file={size / 1048576:.0f}MB promised={total / 1048576:.0f}MB",
          flush=True)
    live_times = [t for (_tt, _p, t) in samples if t > 0]
    advancing_during = bool(live_times) and live_times[-1] > live_times[0]
    post_times = [t for (_p, t) in post]
    advancing_after = (len(post_times) >= 2
                       and post_times[-1] > post_times[0])
    print(f"[vlc] clock advanced during download: {advancing_during} "
          f"({live_times[0] / 1000:.1f}s -> {live_times[-1] / 1000:.1f}s)",
          flush=True)
    print(f"[vlc] still advancing AFTER download: {advancing_after} "
          f"({post_times[0] / 1000:.1f}s -> {post_times[-1] / 1000:.1f}s)",
          flush=True)
    verdict = (ok_dl and size == total and total > 0
               and advancing_during and advancing_after)
    print("PROBE PASS" if verdict else "PROBE FAIL", flush=True)
    try:
        os.remove(path)
        os.rmdir(tmp)
    except OSError:
        pass
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
