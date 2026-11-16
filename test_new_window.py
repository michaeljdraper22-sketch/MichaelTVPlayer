# -*- coding: utf-8 -*-
"""Offscreen test for multi-window support (File > New Window, Nov 2026):

[1] the top-right corner wiring: [new window] [hide channels] on the tab
    bar's TopRightCorner, a File-menu action, and the Ctrl+Shift+N shortcut
[2] opening a second window: registered, secondary-flagged, SHARED client
    + config, distinct numbered title, shared process-wide vlc.Instance
[3] persistence guards: a secondary never writes last_tab /
    window_geometry (single-slot keys shared by every window)
[4] handoff routing: most recently ACTIVATED window, falling back to the
    oldest live one after closes
[5] File > Quit closes every window; PlayerView._views drains on stop()
[6] src/player.py instance registry: same args -> same instance, new
    freetype args -> fresh instance, explicit instance= honored

Run:  .venv\\Scripts\\python.exe test_new_window.py   (sets QT_QPA_PLATFORM itself)
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from src.config import Config  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402
from src.ui.player_view import PlayerView, _OVERLAY_QSS  # noqa: E402

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


app = QtWidgets.QApplication(sys.argv)


def fresh_cfg():
    return Config({"server_url": "http://127.0.0.1", "username": "u",
                   "password": "p"}, None)


def main():
    print("[1] channel-bar corner + File menu + shortcut")
    cfg = fresh_cfg()
    win1 = MainWindow(cfg)
    corner = win1.tabs.cornerWidget(QtCore.Qt.TopRightCorner)
    check("channel-bar corner is just hide-channels (no new-window button)",
          corner is win1.btn_hide_channels
          and not hasattr(win1, "btn_new_window"))
    file_actions = [a.text() for a in win1.menuBar().actions()[0].menu().actions()]
    check("File menu has a New Window item",
          any(t.startswith("New Window") for t in file_actions))
    # (the offscreen platform does not deliver synthetic key events to
    # QShortcuts, so exercise the registered shortcut's signal directly)
    seq = QtGui.QKeySequence("Ctrl+Shift+N")
    nw_shortcuts = [s for s in win1.findChildren(QtWidgets.QShortcut)
                    if s.key().matches(seq) == QtGui.QKeySequence.ExactMatch]
    check("Ctrl+Shift+N shortcut registered on the window",
          len(nw_shortcuts) == 1)
    if nw_shortcuts:
        nw_shortcuts[0].activated.emit()
    check("Ctrl+Shift+N opens a window", len(MainWindow._instances) == 2)
    if len(MainWindow._instances) == 2:
        MainWindow._instances[-1].close()
        app.processEvents()

    print("[1b] on-video overlay button (visible even when the channel "
          "list is folded away, e.g. Stremio handoffs)")
    ov = win1.player_view
    check("overlay new-window button exists with the right tooltip",
          hasattr(ov, "_btn_newwin")
          and "New window" in ov._btn_newwin.toolTip())
    ov.request_new_window.emit()
    check("overlay button opens a window",
          len(MainWindow._instances) == 2)
    if len(MainWindow._instances) == 2:
        MainWindow._instances[-1].close()
        app.processEvents()

    print("[2a] window numbers count the windows OPEN, not a running total")
    churn = []
    for _ in range(3):
        extra = win1.open_new_window()
        app.processEvents()
        churn.append(extra.windowTitle())
        extra.close()
        app.processEvents()
    check("open/close churn reuses 'MichaelTV 2' every time (no climbing)",
          churn == ["MichaelTV 2"] * 3)
    check("closing a secondary leaves one plain-title window",
          win1.windowTitle() == "MichaelTV")

    print("[2] the second window: registry, sharing, title, vlc instance")
    win2 = win1.open_new_window()
    app.processEvents()
    check("open_new_window returned a window", win2 is not None)
    check("two windows registered", len(MainWindow._instances) == 2)
    check("second window is flagged secondary", win2._secondary is True)
    check("second window shares the provider client",
          win2.client is win1.client)
    check("second window shares the config", win2.config is cfg)
    t2 = win2.windowTitle()
    check("titles differ and carry the number",
          t2 != win1.windowTitle()
          and t2 == "MichaelTV 2")
    check("both PlayerViews are registered",
          win1.player_view in PlayerView._views
          and win2.player_view in PlayerView._views)
    check("overlay QSS installed exactly once",
          app.styleSheet().count(_OVERLAY_QSS) == 1)
    check("each window has its OWN vlc.Instance (audio separation)",
          win1.player_view.vlc.instance is not win2.player_view.vlc.instance)

    print("[3] persistence guards: the secondary never writes window state")
    cfg.data["last_tab"] = 0
    win2._on_tab_changed(3)
    check("secondary tab change does not persist last_tab",
          cfg.data["last_tab"] == 0)
    win1._on_tab_changed(4)
    check("primary tab change still persists last_tab",
          cfg.data["last_tab"] == 4)
    cfg.data["window_geometry"] = [11, 22, 33, 44]      # sentinel
    win2._save_state()
    check("secondary _save_state leaves window_geometry alone",
          cfg.data["window_geometry"] == [11, 22, 33, 44])
    win1._save_state()
    check("primary _save_state still writes window_geometry",
          cfg.data["window_geometry"] != [11, 22, 33, 44])

    print("[3b] volume separation: rebuilds use THIS window's slider")
    cfg.data["volume"] = 7            # a poisoned SHARED value
    win2.player_view.vol_slider.setValue(55)
    vol_calls = []
    orig_sv = win2.player_view.vlc.set_volume
    win2.player_view.vlc.set_volume = vol_calls.append
    try:
        win2.player_view.rebuild()
    finally:
        win2.player_view.vlc.set_volume = orig_sv
    check("rebuild applies the view's slider volume, not config.volume",
          vol_calls == [55])

    print("[4] handoff routing: last ACTIVE window, then oldest alive")
    check("routing falls back sensibly before any activation",
          MainWindow.last_active() in (win1, win2))
    win2.changeEvent(QtCore.QEvent(QtCore.QEvent.WindowActivate))
    check("activation routes handoffs to that window",
          MainWindow.last_active() is win2)
    win1.close()
    app.processEvents()
    check("closing the primary leaves the secondary registered",
          MainWindow._instances == [win2])
    MainWindow._last_active = None
    check("with no activation recorded, oldest alive window is used",
          MainWindow.last_active() is win2)

    print("[4b] external handoff on a BUSY window opens a NEW window")
    import types as _types
    football = {"kind": "live", "url": "http://x/live.ts",
                "title": "Football"}
    win2.player_view.current = football          # win2 is "watching"
    spawned_plays = []
    stub = _types.SimpleNamespace(
        play=lambda p, s=0.0: spawned_plays.append((p.get("url"), s)))
    win2.open_new_window = lambda: stub
    self_plays = []
    orig_play = win2.play
    win2.play = lambda *a, **k: self_plays.append((a, k))
    win2.handle_handoff(["--start-time=77",
                         "http://127.0.0.1:9/movie.mkv"])
    check("busy window: handoff played in the NEW window",
          len(spawned_plays) == 1)
    check("busy window: --start-time survived the hop",
          spawned_plays and float(spawned_plays[0][1]) == 77.0)
    check("busy window: the football window was left alone",
          not self_plays and win2.player_view.current is football)
    del win2.open_new_window                     # back to the real method
    win2.play = orig_play
    win2.player_view.current = None

    idle_plays = []

    def _no_spawn():
        raise AssertionError("idle window must not spawn a new one")

    win2.open_new_window = _no_spawn
    win2.play = lambda p, s=0.0, nav_source="live": idle_plays.append(
        (p.get("url"), s))
    win2.handle_handoff(["http://127.0.0.1:9/idle.mkv"])
    check("idle window: handoff still plays in place",
          len(idle_plays) == 1)
    del win2.open_new_window
    win2.play = orig_play

    print("[5] File > Quit closes every window")
    check("after the primary closed, the survivor is plain 'MichaelTV'",
          win2.windowTitle() == "MichaelTV")
    win3 = win2.open_new_window()
    app.processEvents()
    check("second live window registered", win3 is not None
          and len(MainWindow._instances) == 2)
    check("second live window is 'MichaelTV 2'", win3.windowTitle()
          == "MichaelTV 2")
    win4 = win2.open_new_window()
    app.processEvents()
    check("third live window is 'MichaelTV 3'", win4.windowTitle()
          == "MichaelTV 3")
    win3.close()
    app.processEvents()
    check("closing the middle window renumbers the window after it",
          win4.windowTitle() == "MichaelTV 2"
          and win2.windowTitle() == "MichaelTV")
    win2.quit_app()
    app.processEvents()
    check("quit_app closed all windows", MainWindow._instances == [])
    check("PlayerView registry drained on stop", PlayerView._views == [])

    print("[6] per-player vlc.Instance (per-window audio separation)")
    from src.player import VLCPlayer
    a1 = VLCPlayer(timeshift=False, volume=40, network_caching=1500,
                   sub_args=["--freetype-font=ProbeA"])
    a2 = VLCPlayer(timeshift=True, volume=90, network_caching=1500,
                   sub_args=["--freetype-font=ProbeA"])
    check("every player gets its OWN instance (same args too)",
          a1.instance is not a2.instance)
    a3 = VLCPlayer(timeshift=False, volume=40, network_caching=1500,
                   sub_args=["--freetype-font=ProbeA"],
                   instance=a1.instance)
    check("explicit instance= is honored (dvr pattern)",
          a3.instance is a1.instance)
    a4 = VLCPlayer(timeshift=False, volume=40, network_caching=1500,
                   sub_args=["--freetype-font=ProbeB"])
    check("different freetype args -> a fresh instance",
          a4.instance is not a1.instance)
    for p in (a1, a2, a3, a4):
        p.stop_and_release()

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
