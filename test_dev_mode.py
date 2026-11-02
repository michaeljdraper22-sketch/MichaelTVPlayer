# -*- coding: utf-8 -*-
"""Offline tests for the dev/dist launch-mode switch (2026-11-02):

[1] src/devlaunch.py — windowless pythonw discovery (venv pythonw
    first, pythonw beside sys.executable next, console python only as
    the last resort).
[2] src/streampatch.py — the dev handoff is a hidden-wscript .vbs
    relay: _patched_line() names ONE existing file, a full patch() of
    a fake server.js flips it, and the relay re-quotes args byte-exact
    (real wscript round-trip into a recorder script).
[3] src/fileassoc.py — the dev "Open with" command uses pythonw (no
    console leak) and the ProgId icon is assets\\icon.ico (not Python's
    feather); frozen mode keeps exe + ",0". Fake winreg via
    sys.modules injection.
[4] tools/launch_mode.ps1 — .lnk round-trip dev<->dist in temp dirs,
    status readout, and the dev-prerequisites refusal (a checkout
    without .venv must no-op, never half-flip).

Run:  .venv\\Scripts\\python.exe test_dev_mode.py
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.path.dirname(os.path.abspath(__file__))

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def make_fake_root(base):
    """A temp 'repo' with everything a source run needs: main.py, a
    venv pythonw (dummy files — only paths are checked), assets icon."""
    root = os.path.join(base, "fakerepo")
    for rel in ("main.py", r".venv\Scripts\pythonw.exe",
                r".venv\Scripts\python.exe", r"assets\icon.ico",
                r"src\streampatch.py"):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x")
    return root


# ---------------------------------------------------------------- [1] devlaunch

def test_devlaunch():
    print("[1] src/devlaunch.py — windowless pythonw discovery")
    from src import devlaunch

    saved = devlaunch.repo_root
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fake_root(tmp)
            devlaunch.repo_root = lambda: root
            pyw = devlaunch.pythonw_exe()
            check("venv pythonw wins over sys.executable",
                  pyw == os.path.join(root, ".venv", "Scripts",
                                      "pythonw.exe"))
            check("main_py_path() is the fake repo's main.py",
                  devlaunch.main_py_path() == os.path.join(root,
                                                           "main.py"))

            # no venv at all: pythonw beside sys.executable (real venv
            # HAS pythonw.exe next to python.exe, so this hits here)
            bare = os.path.join(tmp, "bare")
            os.makedirs(bare)
            devlaunch.repo_root = lambda: bare
            pyw = devlaunch.pythonw_exe()
            check("falls back to a pythonw beside sys.executable",
                  os.path.basename(pyw).lower() == "pythonw.exe"
                  and os.path.isfile(pyw))
    finally:
        devlaunch.repo_root = saved
    # unpatched, real repo: must point at THIS checkout's venv pythonw
    check("real repo resolves to its own venv pythonw",
          devlaunch.pythonw_exe() == os.path.join(
              REPO, ".venv", "Scripts", "pythonw.exe"))


# ------------------------------------------------------------- [2] streampatch

def test_streampatch_vbs():
    print("[2] src/streampatch.py — hidden-wscript .vbs relay")
    from src import devlaunch, streampatch

    saved_file = streampatch.__file__
    saved_root = devlaunch.repo_root
    saved_find = streampatch.find_server_js
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fake_root(tmp)
            streampatch.__file__ = os.path.join(root, "src",
                                                "streampatch.py")
            devlaunch.repo_root = lambda: root

            path = streampatch._dev_launcher()
            vbs = os.path.join(root, "stremio-handoff-dev.vbs")
            check("_dev_launcher() targets the .vbs next to main.py",
                  path == vbs and os.path.isfile(vbs))
            with open(vbs, "rb") as f:
                body = f.read().decode("utf-8")
            check("relay written WITHOUT a BOM (wscript chokes on it)",
                  not body.startswith("﻿"))
            check("relay launches venv pythonw + main.py",
                  ('cmd = """%s"" ""%s"""' % (
                      os.path.join(root, ".venv", "Scripts",
                                   "pythonw.exe"),
                      os.path.join(root, "main.py"))) in body)
            check("relay is hidden + non-waiting (style 0, False)",
                  "Run cmd, 0, False" in body)
            check("relay re-quotes args (WScript.Arguments loop)",
                  "For Each a In WScript.Arguments" in body)

            # the server.js gate: patched line must name ONE existing
            # file (fs.existsSync) — the .vbs it just wrote
            line = streampatch._patched_line()
            target = line.split("[ '", 1)[1].split("' ]", 1)[0] \
                .replace("\\\\", "\\").strip('"') if "[ '" in line else ""
            check("_patched_line() names the existing .vbs",
                  "/*mtp*/" in line and os.path.isfile(target)
                  and target.lower().endswith(".vbs"))

            # full patch() of a fake server.js (dev branch): backup,
            # re-point, is_patched() flips True
            sj = os.path.join(root, "server.js")
            original = ("var vlcDevice = {\n  "
                        + streampatch._ORIGINAL
                        + ",\n  " + streampatch._TITLE_ORIG + "\n};\n")
            with open(sj, "w", encoding="utf-8") as f:
                f.write(original)
            streampatch.find_server_js = lambda: sj
            check("dev patch() succeeds on a fake server.js",
                  streampatch.patch())
            check("backup of the original server.js kept",
                  os.path.isfile(sj + ".mtpbak"))
            with open(sj, "r", encoding="utf-8") as f:
                patched = f.read()
            check("server.js now launches the .vbs relay",
                  streampatch._patched_line() in patched)
            check("is_patched() true for this dev instance",
                  streampatch.is_patched())
            check("original is restorable byte-for-byte",
                  open(sj + ".mtpbak", encoding="utf-8").read()
                  == original)
    finally:
        streampatch.__file__ = saved_file
        devlaunch.repo_root = saved_root
        streampatch.find_server_js = saved_find


def test_vbs_arg_roundtrip():
    """The relay's arg re-quoting, proven with real wscript: relay into
    a recorder .vbs (standing in for pythonw+main.py), with args that
    contain spaces — exactly what a --sub-file path / URL can do."""
    print("[2b] wscript round-trip — args survive re-quoting")
    from src import streampatch

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "args.txt")
        recorder = os.path.join(tmp, "recorder.vbs")
        with open(recorder, "w", encoding="ascii") as f:
            f.write(
                'out = CreateObject("WScript.Shell")'
                '.ExpandEnvironmentStrings("%MTP_RECORDER_OUT%")\n'
                'Set f = CreateObject("Scripting.FileSystemObject")'
                '.CreateTextFile(out, True, False)\n'
                'f.WriteLine WScript.Arguments.Count\n'
                'For Each a In WScript.Arguments\n'
                '  f.WriteLine a\n'
                'Next\n'
                'f.Close\n')
        wscript = os.path.join(os.environ.get("SystemRoot",
                                              r"C:\Windows"),
                               "System32", "wscript.exe")
        relay = os.path.join(tmp, "relay.vbs")
        with open(relay, "w", encoding="utf-8", newline="") as f:
            f.write(streampatch._vbs_content(wscript, recorder))
        args = ["--start-time=5.5", "--no-video-title-show",
                "--sub-file=" + os.path.join(tmp, "sub file.srt"),
                "http://example.com/a b.m3u8"]
        env = dict(os.environ, MTP_RECORDER_OUT=out)
        subprocess.run([wscript, relay] + args, env=env, timeout=30)
        got = None
        for _ in range(100):     # Run ... False = async; poll for it
            if os.path.isfile(out):
                with open(out, encoding="ascii") as fh:
                    lines = fh.read().splitlines()
                if len(lines) == 1 + len(args):   # fully written
                    got = lines
                    break
            time.sleep(0.1)
        # the recorder wscript exits milliseconds after the last write
        # — let its file handle go before TemporaryDirectory cleanup
        time.sleep(0.5)
        check("recorder got all 4 args", got is not None
              and got[0] == "4")
        if got and got[0] == "4":
            check("args byte-exact after relay re-quoting",
                  got[1:] == args)


# -------------------------------------------------------------- [3] fileassoc

class _FakeKey:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWinreg:
    """Just enough of winreg for fileassoc.register(): records every
    SetValueEx under (subkey path, value name)."""

    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def CreateKeyEx(self, root, path, *a):
        return _FakeKey(path)

    def SetValueEx(self, key, name, reserved, typ, data):
        self.values[(key.path, name)] = data

    def QueryValueEx(self, key, name):
        try:
            return self.values[(key.path, name)], 1
        except KeyError:
            raise FileNotFoundError(2, "not found")


def test_fileassoc():
    print("[3] src/fileassoc.py — pythonw command + app icon (fake winreg)")
    from src import devlaunch, fileassoc

    saved_winreg = sys.modules.get("winreg")
    saved_root = devlaunch.repo_root
    saved_frozen = getattr(sys, "frozen", None)
    fake = FakeWinreg()
    sys.modules["winreg"] = fake
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fake_root(tmp)
            devlaunch.repo_root = lambda: root
            fileassoc.register()
            pyw = os.path.join(root, ".venv", "Scripts", "pythonw.exe")
            cmd = fake.values.get(
                (r"Software\Classes\%s\shell\open\command"
                 % fileassoc.PROGID, None))
            check(".m3u command runs pythonw + main.py",
                  cmd == '"%s" "%s" "%%1"' % (pyw, os.path.join(
                      root, "main.py")))
            check("no console python anywhere in the command",
                  "python.exe" not in (cmd or ""))
            icon = fake.values.get(
                (r"Software\Classes\%s\DefaultIcon" % fileassoc.PROGID,
                 None))
            check("ProgId icon is assets\\icon.ico (not the feather)",
                  icon == os.path.join(root, "assets", "icon.ico"))
            vcmd = fake.values.get(
                (r"Software\Classes\%s\shell\open\command"
                 % fileassoc.VIDEO_PROGID, None))
            vicon = fake.values.get(
                (r"Software\Classes\%s\DefaultIcon"
                 % fileassoc.VIDEO_PROGID, None))
            check("video ProgId: same pythonw command",
                  vcmd == cmd)
            check("video ProgId: same app icon", vicon == icon)

            # no assets/ beside the checkout -> keep exe icon fallback
            bare = os.path.join(tmp, "bare")
            os.makedirs(os.path.join(bare, "src"))
            with open(os.path.join(bare, "src", "__init__.py"), "wb"):
                pass
            devlaunch.repo_root = lambda: bare
            fileassoc.register()
            check("without assets\\icon.ico, icon falls back to "
                  "sys.executable\",0",
                  fake.values.get(
                      (r"Software\Classes\%s\DefaultIcon"
                       % fileassoc.PROGID, None))
                  == sys.executable + ",0")

            # frozen: exe itself, icon from the exe
            sys.frozen = True
            fileassoc.register()
            check("frozen: command is the exe alone",
                  fake.values.get(
                      (r"Software\Classes\%s\shell\open\command"
                       % fileassoc.PROGID, None))
                  == '"%s" "%%1"' % sys.executable)
            check("frozen: icon is the exe's own",
                  fake.values.get(
                      (r"Software\Classes\%s\DefaultIcon"
                       % fileassoc.PROGID, None))
                  == sys.executable + ",0")
            del sys.frozen
    finally:
        if saved_winreg is not None:
            sys.modules["winreg"] = saved_winreg
        else:
            sys.modules.pop("winreg", None)
        devlaunch.repo_root = saved_root
        if hasattr(sys, "frozen"):
            if saved_frozen is None:
                del sys.frozen
            else:
                sys.frozen = saved_frozen


# ---------------------------------------------------------- [4] launch_mode.ps1

READER = r"""param([string]$Lnk)
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($Lnk)
"TargetPath=$($s.TargetPath)"
"Arguments=$($s.Arguments)"
"WorkingDirectory=$($s.WorkingDirectory)"
"IconLocation=$($s.IconLocation)"
"""


def read_lnk(reader_ps1, lnk):
    out = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", reader_ps1, "-Lnk", lnk],
        capture_output=True, text=True, timeout=60).stdout
    fields = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k] = v
    return fields


def run_tool(mode, desktop=None, startmenu=None, root=None):
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", os.path.join(REPO, "tools", "launch_mode.ps1"),
           "-Mode", mode]
    if desktop:
        cmd += ["-DesktopDir", desktop]
    if startmenu:
        cmd += ["-StartMenuDir", startmenu]
    if root:
        cmd += ["-Root", root]
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=120)


def test_launch_mode_tool():
    print("[4] tools/launch_mode.ps1 — .lnk round-trip + refusal")
    pyw = os.path.join(REPO, ".venv", "Scripts", "pythonw.exe")
    exe = os.path.join(REPO, "dist", "MichaelTV.exe")
    have_exe = os.path.isfile(exe)

    with tempfile.TemporaryDirectory() as tmp:
        desktop = os.path.join(tmp, "desktop")
        startmenu = os.path.join(tmp, "programs")
        os.makedirs(desktop)
        os.makedirs(startmenu)
        reader = os.path.join(tmp, "reader.ps1")
        with open(reader, "w", encoding="ascii") as f:
            f.write(READER)

        if have_exe:
            # a pre-existing Start-menu entry pointing at the dist exe
            # (the pre-flip world) must be repointed too
            seed = os.path.join(startmenu, "MichaelTV.lnk")
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$s=(New-Object -ComObject WScript.Shell)"
                 ".CreateShortcut('%s'); $s.TargetPath='%s'; $s.Save()"
                 % (seed, exe.replace("'", "''"))],
                capture_output=True, timeout=60)

        r = run_tool("dev", desktop, startmenu)
        check("dev flip exits 0", r.returncode == 0)
        lnk = os.path.join(desktop, "MichaelTV.lnk")
        f = read_lnk(reader, lnk)
        check("desktop .lnk created",
              os.path.isfile(lnk) and f.get("TargetPath") == pyw)
        check("desktop .lnk args = \"<repo>\\main.py\"",
              f.get("Arguments") == '"%s"' % os.path.join(REPO,
                                                          "main.py"))
        check("desktop .lnk workdir = repo",
              f.get("WorkingDirectory") == REPO)
        check("desktop .lnk icon = assets\\icon.ico",
              f.get("IconLocation", "").lower().startswith(
                  os.path.join(REPO, "assets", "icon.ico").lower()))
        if have_exe:
            fs = read_lnk(reader, seed)
            check("existing start-menu .lnk repointed to dev too",
                  fs.get("TargetPath") == pyw)

        r = run_tool("status", desktop, startmenu)
        check("status reports DEV (source)",
              r.returncode == 0 and "DEV (source)" in r.stdout)

        if have_exe:
            r = run_tool("dist", desktop, startmenu)
            check("dist flip exits 0", r.returncode == 0)
            f = read_lnk(reader, lnk)
            check("desktop .lnk back on the exe, args empty",
                  f.get("TargetPath") == exe
                  and f.get("Arguments", "") == "")
            r = run_tool("status", desktop, startmenu)
            check("status reports DIST (built exe)",
                  "DIST (built exe)" in r.stdout)
            # and back to dev (the machine's resting state)
            run_tool("dev", desktop, startmenu)

        # refusal: a checkout without .venv must no-op, never half-flip
        bare = os.path.join(tmp, "bare")
        os.makedirs(bare)
        r = run_tool("dev", desktop, startmenu, root=bare)
        check("dev flip refuses without .venv/pythonw (exit 1)",
              r.returncode == 1 and "UNAVAILABLE" in r.stdout)
        check("refusal leaves the desktop .lnk untouched",
              read_lnk(reader, lnk).get("TargetPath") == pyw)


def main():
    test_devlaunch()
    test_streampatch_vbs()
    test_vbs_arg_roundtrip()
    test_fileassoc()
    test_launch_mode_tool()
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
