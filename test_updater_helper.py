# -*- coding: utf-8 -*-
"""Regression test for the update swap helper (the v1.4 -> v1.4.1 bricker).

The helper batch must contain NO PIPES and NO ``timeout``: inside a
DETACHED cmd a ``tasklist | find`` pipeline never returns and ``timeout``
fails with rc 125 (both verified empirically on 2026-08-28) — the old
wait loop hung on them forever, so the payload was never copied and the
app never restarted. This test checks the generated script statically
AND runs the real swap flow end-to-end against a scratch install folder.

Run:  .venv\\Scripts\\python.exe test_updater_helper.py   (~20 s)
"""
import os
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import updater  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def _make_stub(dirpath):
    """A silent GUI-less stand-in for MichaelTV.exe (pythonw: no console,
    no window, exits instantly) — `start` launches it during the swap."""
    stub = os.path.join(dirpath, "MichaelTV.exe")
    src = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(src):     # fallback: any silent-ish exe
        src = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                           "System32", "where.exe")
    with open(src, "rb") as f, open(stub, "wb") as o:
        o.write(f.read())
    return stub


def main():
    root = tempfile.mkdtemp(prefix="mtp_updtest_")
    # the install dir must sit OUTSIDE %TEMP%: stage_update now refuses
    # to "update" a temp-folder extraction (issue #4 hardening), and the
    # scratch install would trip that refusal
    install_root = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        f"mtp_updtest_{os.getpid()}")
    os.makedirs(install_root, exist_ok=True)
    install = os.path.join(install_root, "install")
    payload = os.path.join(root, "payload")
    os.makedirs(os.path.join(install, "vlc"))
    os.makedirs(os.path.join(payload, "vlc"))
    _make_stub(install)
    with open(os.path.join(install, "vlc", "plugin.dat"), "w") as f:
        f.write("OLD")
    with open(os.path.join(install, "stale_legacy.txt"), "w") as f:
        f.write("gone with /MIR")
    _make_stub(payload)
    with open(os.path.join(payload, "vlc", "plugin.dat"), "w") as f:
        f.write("NEW")
    with open(os.path.join(payload, "UninstallMichaelTV.exe"), "w") as f:
        f.write("stub")

    zp = os.path.join(root, "update.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.write(os.path.join(payload, "MichaelTV.exe"), "MichaelTV.exe")
        z.write(os.path.join(payload, "UninstallMichaelTV.exe"),
                "UninstallMichaelTV.exe")
        z.write(os.path.join(payload, "vlc", "plugin.dat"), "vlc/plugin.dat")

    # stage_update must believe it runs FROM the scratch install folder
    orig = updater.sys_executable
    updater.sys_executable = lambda: os.path.join(install, "MichaelTV.exe")
    try:
        helper, staging = updater.stage_update(zp)
    finally:
        updater.sys_executable = orig

    print("[1] generated swap helper: static safety checks")
    bat = open(helper).read()
    bat_bytes = open(helper, "rb").read()
    check("helper generated", bool(helper) and os.path.isfile(helper))
    check("no pipe anywhere in the helper", "|" not in bat)
    check("no timeout command in the helper", "timeout" not in bat.lower())
    for needle in ("ping -n 5", "taskkill /F /PID", "robocopy",
                   "start \"\"", "rd /s /q"):
        check("helper contains %r" % needle, needle in bat)
    # issue #4 bug 3a/3b/3c: the helper must live OUTSIDE the payload
    # (robocopy /MIR used to copy _swap.bat into the install dir, and
    # the trailing rd deleted the executing bat's folder so its last
    # line never ran), and must not carry \r\r\n line endings
    check("helper written outside the payload",
          not os.path.abspath(helper).startswith(
              os.path.abspath(staging) + os.sep))
    check("helper self-deletes (del %~f0)", 'del "%~f0"' in bat)
    check("no \\r\\r\\n line endings (bug 3c)", b"\r\r\n" not in bat_bytes)
    check("execution-time timestamps on every step",
          bat.count("[%date% %time%]") >= 4)
    check("robocopy failure is logged (x2)", bat.count("if errorlevel 8") == 2)

    print("[2] functional swap (detached, exactly like launch_helper)")
    # the "app" the helper waits on/kills: a hidden sleeping child whose
    # pid we substitute for the staging python's
    app = subprocess.Popen(
        ["ping", "-n", "30", "127.0.0.1"],
        creationflags=0x08000000,   # CREATE_NO_WINDOW
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # redirect the swap log to a SCRATCH file: the machine's real
    # %TEMP%\MichaelTV-swap.log already contains "swap done" from past
    # updates, which let the old check pass even when the helper's own
    # final line never ran (exactly how issue #4 bugs 3a/3b escaped)
    import re as _re
    m = _re.search(r'>> "([^"]+)"', bat)
    real_log = m.group(1) if m else ""
    scratch_log = os.path.join(root, "swap.log")
    bat_path = os.path.join(root, "_swap_test.bat")
    with open(bat_path, "w", newline="") as f:
        f.write(bat.replace("pid=%d" % os.getpid(), "pid=%d" % app.pid)
                   .replace("/PID %d" % os.getpid(), "/PID %d" % app.pid)
                   .replace(real_log, scratch_log))
    flags = 0x00000008  # DETACHED_PROCESS — same as updater.launch_helper
    subprocess.Popen(["cmd", "/c", bat_path], close_fds=True,
                     creationflags=flags)

    deadline = time.time() + 30
    done = False
    while time.time() < deadline:
        time.sleep(1)
        if not os.path.isdir(staging) and \
                open(os.path.join(install, "vlc", "plugin.dat")).read() == "NEW":
            done = True
            break
    time.sleep(1)
    check("payload copied over the install (vlc/plugin.dat == NEW)",
          open(os.path.join(install, "vlc", "plugin.dat")).read() == "NEW")
    check("extra payload file arrived (UninstallMichaelTV.exe)",
          os.path.isfile(os.path.join(install,
                                      "UninstallMichaelTV.exe")))
    check("stale install file removed (/MIR)",
          not os.path.exists(os.path.join(install, "stale_legacy.txt")))
    check("staging cleaned up", not os.path.isdir(staging))
    check("no _swap.bat littered into the install (bug 3a)",
          not os.path.exists(os.path.join(install, "_swap.bat")))
    check("helper self-deleted after running (bug 3b)",
          not os.path.exists(bat_path))
    logtxt = open(scratch_log).read() if os.path.isfile(scratch_log) else ""
    check("swap log written with done marker",
          "swap done" in logtxt)
    check("final log line actually RAN (bug 3b): stamped, in order",
          logtxt.find("copying payload") < logtxt.find("starting new version")
          < logtxt.find("swap done"))
    check("log lines carry execution timestamps",
          logtxt.count("] ") >= 3 and "[..]" not in logtxt)
    if app.poll() is None:
        app.kill()

    import shutil
    shutil.rmtree(install_root, ignore_errors=True)

    print()
    if FAIL:
        print("FAILED %d:" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("ALL %d CHECKS PASSED" % len(PASS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
