# -*- coding: utf-8 -*-
"""Smoke + click-routing verification of the SHIPPED dist\\MichaelTV.exe.

Everything so far proved the fix in the SOURCE tree (dev python). This
closes the loop on the artifact the user actually runs:

[1] the exe boots clean (process up, fresh player.log startup lines)
[2] a local-file handoff plays (muted via a temporary settings.json
    volume edit, restored afterwards)
[3] WindowFromPoint over the playing video lands on the layered overlay
    window (the new mouse layer) instead of VLC's vout children
[4] the log carries the 2.1.6 version stamp

Run:  .venv\\Scripts\\python.exe probe_exe_smoke.py
"""
import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes

user32 = ctypes.windll.user32

EXE = r"D:\Coding\MichaelTVPlayer\dist\MichaelTV.exe"
MEDIA = r"D:\Coding\MichaelTVPlayer\probe_jump_media.mkv"
SETTINGS = os.path.join(os.environ["APPDATA"],
                        "MichaelTVPlayer", "settings.json")
LOG = os.path.join(os.environ["APPDATA"], "MichaelTVPlayer", "player.log")

SW_SHOWNOACTIVATE = 4
WS_EX_LAYERED = 0x80000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080


def log_size():
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def find_windows():
    """(hwnd, title, class, exstyle) for every visible top-level window
    owned by a MichaelTV.exe process (onefile bootloader + app child)."""
    out = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                  wintypes.LPARAM)
    kernel32 = ctypes.windll.kernel32

    def _is_mtp(pid):
        h = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED
        if not h:
            return False
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf,
                                                   ctypes.byref(n)):
                return "MichaelTV.exe" in buf.value
            return False
        finally:
            kernel32.CloseHandle(h)

    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _is_mtp(pid.value):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            out.append((hwnd, buf.value, cls.value,
                        user32.GetWindowLongW(hwnd, -20)))
        return True
    user32.EnumWindows(EnumProc(cb), 0)
    return out


def window_from_point(x, y):
    pt = wintypes.POINT(x, y)
    return user32.WindowFromPoint(pt)


def main():
    # [0] mute via settings (restored at the end, even on failure)
    backup = open(SETTINGS, encoding="utf-8").read()
    try:
        data = json.loads(backup)
        data["volume"] = 0
        with open(SETTINGS, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
    except Exception as exc:
        print("settings edit failed: %r" % exc)

    log_mark = log_size()
    try:
        # [1] boot with the local file handoff, shown-no-activate
        info = subprocess.Popen(
            [EXE, MEDIA], creationflags=subprocess.CREATE_NEW_CONSOLE)
        pid = info.pid
        time.sleep(10)

        alive = info.poll() is None
        print("[1] exe alive after 10s: %s (pid %d)" % (alive, pid))
        time.sleep(5)     # let the vout children spin up over the video
        wins = find_windows()
        for h, t, c, ex in wins:
            print("    win %x class=%-16s ex=%08x title=%r"
                  % (h, c, ex, t[:40]))

        fresh = ""
        try:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                f.seek(log_mark)
                fresh = f.read()
        except OSError:
            pass
        print("[4] log version stamps:",
              [ln.strip()[:90] for ln in fresh.splitlines()
               if "2.1.6" in ln][:3] or "NONE FOUND")
        played = ("display open" in fresh) or ("handoff args" in fresh)
        print("[2] handoff/play reached the player: %s"
              % played)

        # [3] click routing: sample the main window's client area; the
        # layered tool window IS the overlay mouse layer (class prefix
        # varies by Qt build: Qt5QWindow... / Qt5152QWindow...)
        main_h = next((h for h, t, c, ex in wins
                       if c.endswith("QWindowIcon")
                       and not (ex & WS_EX_TOOLWINDOW)), None)
        overlay_h = next((h for h, t, c, ex in wins
                          if (ex & WS_EX_LAYERED)
                          and (ex & WS_EX_TOOLWINDOW)), None)
        print("    main=%s overlay=%s" % (hex(main_h or 0),
                                          hex(overlay_h or 0)))
        if main_h and overlay_h:
            # Chrome (or whatever is foreground) may COVER the app: move
            # the probe instance to a monitor the foreground window is
            # NOT on (no activation, no z-order change), else a small
            # topmost corner box for a few seconds
            fg = user32.GetForegroundWindow()
            frect = wintypes.RECT()
            user32.GetWindowRect(fg, ctypes.byref(frect))
            target = None
            mons = []
            MonEnumProc = ctypes.WINFUNCTYPE(
                wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

            def mcb(hm, _dc, rc, _l):
                mons.append((rc.contents.left, rc.contents.top,
                             rc.contents.right, rc.contents.bottom))
                return True
            user32.EnumDisplayMonitors(None, None, MonEnumProc(mcb), 0)
            for m in mons:
                inside = (frect.left < m[2] and frect.right > m[0]
                          and frect.top < m[3] and frect.bottom > m[1])
                if not inside:
                    target = (m[0] + 40, m[1] + 40)
                    break
            topmost = target is None
            if topmost:
                target = (mons[0][2] - 520, mons[0][3] - 380)
            SWP = 0x0040 | 0x0004 | 0x0010   # NOACTIVATE|NOZORDER|ASYNC
            user32.SetWindowPos(main_h, 0, target[0], target[1],
                                760, 520, SWP)
            time.sleep(1.2)     # let _poll_cursor re-glue the overlay
            if topmost:
                user32.SetWindowPos(main_h, -1, 0, 0, 0, 0,
                                    0x0001 | 0x0002 | 0x0040)  # TOPMOST

            # identify the native children under the main window: the
            # video surface (Qt native child) and any VLC vout children
            kids = []
            EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                          wintypes.LPARAM)

            def kcb(child, _l):
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(child, cls, 64)
                kids.append((child, cls.value))
                return True
            user32.EnumChildWindows(main_h, EnumProc(kcb), 0)
            surf_h = next((h for h, c in kids if "QWindowIcon" in c
                           and user32.GetParent(h) == main_h), None)
            vlc_hs = [h for h, c in kids if "VLC" in c]
            print("    surface=%s vlc-children=%s"
                  % (hex(surf_h or 0), [hex(h) for h in vlc_hs]))

            def who(hwnd):
                if hwnd == overlay_h:
                    return "OVERLAY(mouse layer)"
                if hwnd == surf_h:
                    return "surface"
                if hwnd in vlc_hs:
                    return "VLC-VOUT(bad)"
                if hwnd == main_h:
                    return "main"
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, cls, 64)
                return "other:" + cls.value

            rect = wintypes.RECT()
            user32.GetWindowRect(main_h, ctypes.byref(rect))
            tally = {}
            for fx in range(1, 10):
                for fy in range(1, 10):
                    x = rect.left + (rect.right - rect.left) * fx // 10
                    y = rect.top + (rect.bottom - rect.top) * fy // 10
                    w_ = who(window_from_point(x, y))
                    tally[w_] = tally.get(w_, 0) + 1
            print("[3] WindowFromPoint tally over the main window:")
            for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
                print("      %-24s %d" % (k, v))
        else:
            print("[3] SKIPPED: main/overlay window not found")
    finally:
        subprocess.run(["taskkill", "/IM", "MichaelTV.exe", "/F"],
                       capture_output=True)
        time.sleep(2)
        with open(SETTINGS, "w", encoding="utf-8") as f:
            f.write(backup)
        print("restored settings.json; exe closed")


if __name__ == "__main__":
    main()
