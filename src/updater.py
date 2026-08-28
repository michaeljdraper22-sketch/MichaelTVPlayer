"""Manual, opt-in update from GitHub Releases (Settings ▸ Check for updates).

Design constraints (user requirements):
- NEVER prompts on its own and never announces "update available": the ONLY
  entry point is the user clicking the button.
- On a new version the user gets a Yes/No choice; nothing auto-starts.
- Updating must not lose ANY data: settings, Xtream login, favorites,
  recents etc. all live in %APPDATA%\\MichaelTVPlayer\\settings.json, which
  the updater never touches. Only the INSTALL folder (exe + vlc runtime) is
  replaced.
- The replacement is a true mirror: stale files from older builds are
  deleted (feature removals leave nothing behind).

Mechanics: the release ships a zip (MichaelTV-<version>.zip) containing
MichaelTV.exe, UninstallMichaelTV.exe and the vlc\\ runtime. The app
downloads it, extracts to a staging folder and spawns a small batch helper;
the app then exits, the helper waits for the process to die, mirrors the
payload over the install folder and restarts the new exe.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile

from .config import APP_VERSION

REPO = "michaeljdraper22-sketch/MichaelTVPlayer"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4])


def is_newer(remote, local=APP_VERSION):
    try:
        return _ver_tuple(remote) > _ver_tuple(local)
    except Exception:  # noqa: BLE001
        return False


def fetch_latest(timeout=15):
    """Return (version, notes, asset_url) for the latest GitHub release.

    Raises on any network/parse problem — the caller shows a plain message.
    """
    req = urllib.request.Request(
        API_LATEST, headers={"User-Agent": "MichaelTVPlayer-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    tag = (data.get("tag_name") or "").strip().lstrip("vV")
    asset_url = None
    for a in data.get("assets") or []:
        name = (a.get("name") or "").lower()
        if name.endswith(".zip") and "michaeltv" in name:
            asset_url = a.get("browser_download_url")
            break
    if asset_url is None:
        for a in data.get("assets") or []:
            if (a.get("name") or "").lower().endswith(".zip"):
                asset_url = a.get("browser_download_url")
                break
    if not tag or not asset_url:
        raise RuntimeError("release has no MichaelTV zip asset")
    notes = (data.get("body") or "").strip()
    return tag, notes, asset_url


def download(asset_url, dest, progress=None, chunk=1 << 16):
    """Stream the release zip to ``dest``; progress(done, total, -1 while
    the size is unknown)."""
    req = urllib.request.Request(
        asset_url, headers={"User-Agent": "MichaelTVPlayer-updater"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            b = r.read(chunk)
            if not b:
                break
            f.write(b)
            done += len(b)
            if progress:
                progress(done, total)
    return dest


def stage_update(zip_path):
    """Extract the payload and write the swap helper. Returns
    (helper_bat, staging_dir) or raises.

    The helper contains NO PIPES and NO ``timeout``: inside a DETACHED
    cmd (how launch_helper spawns it) a ``tasklist | find`` pipeline
    NEVER RETURNS and ``timeout`` fails outright (rc 125) — the original
    wait loop hung forever on both, which is what bricked the first-ever
    in-app update (v1.4 → v1.4.1): the download "completed", the app
    exited, and the helper sat in its loop so the old exe stayed put and
    nothing restarted. Delays now use ``ping`` (console-free), and the
    app's exit is guaranteed by the caller's threading.Timer hard-exit;
    the unconditional ``taskkill`` only matters if even that failed.
    Every step appends to %TEMP%\\MichaelTV-swap.log for diagnosability.
    """
    staging = os.path.join(
        tempfile.gettempdir(), f"MichaelTV-update-{os.getpid()}")
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(staging)
    # tolerate a single top-level folder inside the zip
    entries = os.listdir(staging)
    payload = staging
    if len(entries) == 1:
        cand = os.path.join(staging, entries[0])
        if os.path.isdir(cand) and os.path.isfile(
                os.path.join(cand, "MichaelTV.exe")):
            payload = cand
    if not os.path.isfile(os.path.join(payload, "MichaelTV.exe")):
        raise RuntimeError("update zip has no MichaelTV.exe")
    install_dir = os.path.dirname(os.path.abspath(sys_executable()))
    if not os.path.isfile(os.path.join(install_dir, "MichaelTV.exe")):
        # running from source / unusual layout — refuse rather than mirror
        # an arbitrary folder
        raise RuntimeError("install folder not detected "
                           "(no MichaelTV.exe beside the running app)")
    helper = os.path.join(staging, "_swap.bat")
    pid = os.getpid()
    logf = os.path.join(tempfile.gettempdir(), "MichaelTV-swap.log")
    rc = ("/MIR /R:2 /W:2 /NFL /NDL /NJH /NJS /NP >nul")
    with open(helper, "w") as f:
        f.write(
            "@echo off\r\n"
            f"echo [{time.strftime('%Y-%m-%d %H:%M:%S')}] swap start"
            f" pid={pid} >> \"{logf}\"\r\n"
            # ~4 s for the app's hard-exit timer (+ teardown stragglers)
            "ping -n 5 127.0.0.1 >nul\r\n"
            # if the app still wedged mid-shutdown, take it down; rc 128
            # (no such process) is the normal, already-exited case
            f"taskkill /F /PID {pid} >nul 2>&1\r\n"
            f"echo [..] copying payload >> \"{logf}\"\r\n"
            f"robocopy \"{payload}\" \"{install_dir}\" {rc}\r\n"
            # one retry a beat later: AV suites can briefly hold the
            # fresh exe after writing it
            "ping -n 4 127.0.0.1 >nul\r\n"
            f"robocopy \"{payload}\" \"{install_dir}\" {rc}\r\n"
            f"echo [..] starting new version >> \"{logf}\"\r\n"
            f"start \"\" \"{install_dir}\\MichaelTV.exe\"\r\n"
            f"rd /s /q \"{staging}\"\r\n"
            f"echo [..] swap done >> \"{logf}\"\r\n"
        )
    return helper, staging


def sys_executable():
    """Frozen exe path, or sys.executable when running from source."""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.argv[0]) or sys.executable
    return sys.executable


def launch_helper(helper_bat):
    """Spawn the swap helper detached and return True (caller exits)."""
    import subprocess
    flags = 0x00000008 if sys.platform == "win32" else 0  # DETACHED_PROCESS
    subprocess.Popen(["cmd", "/c", helper_bat], close_fds=True,
                     creationflags=flags)
    return True
