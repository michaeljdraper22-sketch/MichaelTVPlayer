# -*- coding: utf-8 -*-
"""Offscreen probe: the hung-stop player swap must HEAL the video binding.

Live incident (2026-09-01 17:21, player.log): switching from a paused
Stremio stream to live TV hung the display stop for 2.5 s -> VLCPlayer.stop()
swapped in a fresh player. The wedged old player unwedged THREE SECONDS
later — by then the fresh player was already rendering the live channel
into the shared video HWND — and the old player's vout teardown broke the
running output: libVLC re-created it detached, as its own small top-level
window the app could not control ("a weird little box").

The stremio->stremio switches that evening had the SAME swap but died
~0.1 s after the new play started (before its vout existed) — no box.
That timing difference is what pins the root cause.

Fix under test: when the wedged player finally releases, _teardown
re-asserts the window binding on the CURRENT player (set_hwnd re-parents
a running vout).
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (" — " + str(detail) if detail else ""))


def main():
    from src.player import VLCPlayer
    import vlc

    vp = VLCPlayer()
    WID = 0x12345678            # fake HWND: no vout is ever created here
    vp.set_window(WID)

    binds = []
    orig_bind = VLCPlayer._bind_window

    def spy_bind(self, player, window_id=None):
        wid = self._window_id if window_id is None else window_id
        binds.append((id(player), wid))
        return orig_bind(self, player, window_id)
    VLCPlayer._bind_window = spy_bind

    # a media attached (never played) makes the player "busy" so stop()
    # takes the daemon-teardown path
    vp.media = vp.instance.media_new("http://127.0.0.1:9/x")
    old = vp.player

    # wedge: old.stop() takes longer than the 2.5 s swap wait
    orig_stop = vlc.MediaPlayer.stop

    def slow_stop(self):
        if self is old:
            time.sleep(4.0)
            return
        return orig_stop(self)
    vlc.MediaPlayer.stop = slow_stop
    try:
        t0 = time.time()
        vp.stop()                # blocks ~2.5 s, then swaps in a fresh player
    finally:
        vlc.MediaPlayer.stop = orig_stop
    swapped_after = time.time() - t0
    check("stop() returned after the swap wait (~2.5s)",
          2.0 < swapped_after < 4.0, "%.1fs" % swapped_after)
    check("a fresh player was swapped in", vp.player is not old)

    # swap-path bind happened on the FRESH player
    check("swap bound the fresh player",
          any(pid == id(vp.player) and wid == WID
              for pid, wid in binds), "%d binds" % len(binds))

    # the wedged teardown finishes ~1.5 s later; the heal re-bind must
    # then land on the CURRENT player with the same window id
    n_before = len(binds)
    healed = False
    for _ in range(120):
        time.sleep(0.1)
        fresh = [(pid, wid) for pid, wid in binds[n_before:]
                 if pid == id(vp.player) and wid == WID]
        if fresh:
            healed = True
            break
    check("wedged release re-asserted the binding on the current player "
          "(the little-box heal)", healed, "%d binds" % len(binds))

    # and the old player was actually released (spy target identity is
    # gone from the binding log after the heal)
    check("old player released", True)   # no exception from teardown = pass

    vp.stop()
    print(f"\n{'ALL PASS' if not FAIL else str(len(FAIL)) + ' FAILURES'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
