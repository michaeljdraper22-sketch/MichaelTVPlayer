# -*- coding: utf-8 -*-
"""Sandbox swap test for the v2.0.1 updater helper (GitHub issue #4).

Rebuilds the brother-machine sandbox harness against the REAL
stage_update()/launch_helper(): a fake install dir, a sacrificial
process standing in for the running app, and the byte-identical DETACHED
spawn.  Verifies the three v2.0 bugs stay fixed and the swap itself
still works end to end:

  3a  the helper is NOT inside the payload -> robocopy /MIR never copies
      _swap.bat into the install dir (and /MIR now DELETES any _swap.bat
      littered by a previous v2.0 update)
  3b  the bat's final "swap done" log line RUNS (staging deletion no
      longer pulls the directory out from under the executing bat) and
      the bat deletes itself
  3c  the generated bat has clean \\r\\n line endings, no \\r\\r\\n
  +   temp-run guard: an app running from a temp folder refuses to update
  +   /MIR still mirrors (new files copied, stale files deleted), the old
      process is killed, the new exe started, staging cleaned up

Nothing visible happens: the payload exe is a pythonw.exe copy (starts
detached, exits silently), the sacrificial process is created with
CREATE_NO_WINDOW.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import updater  # noqa: E402

fails = [0]


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else extra))
    if not cond:
        fails[0] += 1


def find_pythonw():
    for cand in (os.path.join(os.path.dirname(sys.executable), "pythonw.exe"),
                 os.path.join(sys.base_prefix, "pythonw.exe")):
        if os.path.isfile(cand):
            return cand
    return None


def make_zip(path):
    """Release-shaped payload: MichaelTV.exe at the zip ROOT (like the
    shipped zips), so stage_update's payload == staging path is taken."""
    pythonw = find_pythonw()
    with zipfile.ZipFile(path, "w") as z:
        z.write(pythonw, "MichaelTV.exe")
        z.writestr("newfile.txt", "payload marker")
    return pythonw


def _kill_sandbox_procs(root):
    """Kill payload stubs still running from THIS sandbox (by exact path
    — never by name: the real dist\\MichaelTV.exe must not be touched).
    pythonw copied as MichaelTV.exe does not reliably exit when `start`ed
    from a detached cmd — it can linger and hold the sandbox locked."""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process MichaelTV -ErrorAction SilentlyContinue | "
             f"Where-Object {{$_.Path -like '{root}*'}} | "
             "Stop-Process -Force"],
            capture_output=True, timeout=60)
    except Exception:  # noqa: BLE001
        pass


def main():
    # unique root per run: a previous run's locked sandbox must not
    # collide with this one's setup
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f".swaptest-{os.getpid()}")
    shutil.rmtree(root, ignore_errors=True)
    install = os.path.join(root, "install")
    os.makedirs(install)
    zip_path = os.path.join(root, "payload.zip")
    pythonw = make_zip(zip_path)
    with open(pythonw, "rb") as f:
        payload_exe_bytes = f.read()

    # the "currently installed" app: a dummy exe (never executed), stale
    # files /MIR must delete, and _swap.bat litter from a v2.0-era update
    with open(os.path.join(install, "MichaelTV.exe"), "wb") as f:
        f.write(b"OLD-EXE-PLACEHOLDER")
    with open(os.path.join(install, "stale.txt"), "w") as f:
        f.write("delete me")
    with open(os.path.join(install, "_swap.bat"), "w") as f:
        f.write("rem litter from a previous v2.0 update")

    # sacrificial "app" process (CREATE_NO_WINDOW: nothing flashes)
    sacr = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        creationflags=0x08000000)

    real_exe = updater.sys_executable
    real_getpid = os.getpid
    helper = staging = None
    try:
        updater.sys_executable = lambda: os.path.join(
            install, "MichaelTV.exe")
        os.getpid = lambda: sacr.pid       # the bat must kill the "app"
        helper, staging = updater.stage_update(zip_path)
    finally:
        updater.sys_executable = real_exe
        os.getpid = real_getpid

    print("[A] generated helper (issue #4 bugs 3a/3c)")
    check("helper lives in %TEMP%, NOT inside the payload/staging",
          os.path.normcase(os.path.dirname(helper)) ==
          os.path.normcase(tempfile.gettempdir()) and
          not os.path.normcase(helper).startswith(
              os.path.normcase(staging + os.sep)),
          f"({helper})")
    check("staging (payload) contains no bat at all",
          not [e for e in os.listdir(staging) if e.endswith(".bat")])
    with open(helper, "rb") as f:
        bat = f.read()
    check("clean \\r\\n endings only (no \\r\\r\\n)",
          b"\r\r\n" not in bat and bat.count(b"\r\n") >= 10)
    check("self-deletes as its last line", bat.rstrip().endswith(
        b'del "%~f0"'))

    print("[B] live swap (DETACHED, exactly as the app spawns it)")
    real_log = os.path.join(tempfile.gettempdir(), "MichaelTV-swap.log")
    log_before = ""
    if os.path.isfile(real_log):
        with open(real_log, "r", errors="replace") as f:
            log_before = f.read()

    check("launch_helper spawned the swap", updater.launch_helper(helper))

    deadline = time.time() + 90
    done = False
    while time.time() < deadline:
        if not os.path.exists(helper) and sacr.poll() is not None:
            done = True
            break
        time.sleep(1.0)

    log_after = ""
    if os.path.isfile(real_log):
        with open(real_log, "r", errors="replace") as f:
            log_after = f.read()

    check("old process was killed", sacr.poll() is not None)
    check("swap finished within the timeout", done)
    with open(os.path.join(install, "MichaelTV.exe"), "rb") as f:
        new_exe = f.read()
    check("install exe replaced by the payload exe",
          new_exe == payload_exe_bytes)
    check("new payload file mirrored in",
          os.path.isfile(os.path.join(install, "newfile.txt")))
    check("/MIR deleted stale files",
          not os.path.exists(os.path.join(install, "stale.txt")))
    check("/MIR deleted the old _swap.bat litter (3a regression)",
          not os.path.exists(os.path.join(install, "_swap.bat")))
    check("staging folder cleaned up", not os.path.exists(staging))
    check("helper deleted itself", not os.path.exists(helper))
    new_log = log_after[len(log_before):]
    idx = new_log.rfind(f"pid={sacr.pid}")
    check('"swap done" logged AFTER our swap start (3b regression)',
          idx >= 0 and "swap done" in new_log[idx:],
          f"(log tail: {new_log[idx:idx + 300]!r})")

    print("[C] temp-run guard refuses to update a disposable extraction")
    tmpdir = tempfile.mkdtemp(prefix="mtv-swaptest-temp-")
    fake_tmp_exe = os.path.join(tmpdir, "MichaelTV.exe")
    with open(fake_tmp_exe, "wb") as f:
        f.write(b"X")
    guard_hit = False
    try:
        updater.sys_executable = lambda: fake_tmp_exe
        os.getpid = lambda: 4 << 20          # unrelated dummy pid
        updater.stage_update(zip_path)
    except RuntimeError as e:
        guard_hit = "temporary folder" in str(e)
    finally:
        updater.sys_executable = real_exe
        os.getpid = real_getpid
        shutil.rmtree(tmpdir, ignore_errors=True)
    check("running from a temp folder raises 'temporary folder'",
          guard_hit)

    # lingering payload stub first (path-scoped), then the sweep
    _kill_sandbox_procs(root)
    for _ in range(30):
        shutil.rmtree(root, ignore_errors=True)
        if not os.path.exists(root):
            break
        time.sleep(1.0)
    check("sandbox cleaned up", not os.path.exists(root))
    print()
    print(f"test_swap_helper: "
          f"{'ALL PASS' if fails[0] == 0 else f'{fails[0]} FAILURE(S)'}")
    return 1 if fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
