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
    """Return (version, notes, asset_url, sha256_url) for the latest
    GitHub release.  sha256_url is None when the release carries no
    checksum asset (all releases before it existed) — verification is
    strictly verify-when-present and never blocks an update.

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
    sha256_url = None
    for a in data.get("assets") or []:
        if (a.get("name") or "").lower().endswith(".sha256"):
            sha256_url = a.get("browser_download_url")
            break
    if not tag or not asset_url:
        raise RuntimeError("release has no MichaelTV zip asset")
    notes = (data.get("body") or "").strip()
    return tag, notes, asset_url, sha256_url


def verify_sha256(zip_path, sha_path):
    """Check the downloaded zip against the LOCAL .sha256 file the caller
    already downloaded (it is a plain path, NOT a URL — the original
    draft urlopen'd it and crashed on "unknown url type: c" the moment
    a release actually shipped a checksum).  Raises RuntimeError on
    mismatch or an unreadable checksum; returns True on a match.  A
    250 MB download with no integrity check accepts any truncated or
    hijacked transfer as an update (issue #4 hardening)."""
    import hashlib
    expected = ""
    with open(sha_path, "r", encoding="utf-8", errors="replace") as f:
        tokens = f.read().strip().lower().split()
        expected = tokens[0] if tokens else ""
    if len(expected) < 32:
        raise RuntimeError("checksum file is unreadable or empty")
    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got != expected:
        raise RuntimeError(
            f"download failed its integrity check "
            f"(sha256 {got[:12]}…, expected {expected[:12]}…)")
    return True


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

    The helper is written OUTSIDE the payload (issue #4 bug 3a/3b): when
    it lived in staging, ``robocopy /MIR`` copied _swap.bat into the
    install dir on every update, and the trailing ``rd /s /q`` of the
    staging folder deleted the still-executing bat's directory, so its
    last log line ("swap done") never ran and the log could not tell a
    finished swap from a half-done one.  It now self-deletes via
    ``del "%~f0"``.  The file is written with newline="" so the literal
    \\r\\n in the lines is not translated a second time to \\r\\r\\n
    (issue #4 bug 3c — cmd tolerated it, but the stray \\r leaked into
    the swap log).
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
    # refuse to "update" a disposable extraction: running straight out of
    # a temp folder (e.g. the 7-Zip preview) would mirror the new version
    # into a folder disk cleanup later deletes (issue #4 hardening)
    _tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    _inst = os.path.normcase(os.path.abspath(install_dir))
    if _inst == _tmp or _inst.startswith(_tmp + os.sep):
        raise RuntimeError("MichaelTV is running from a temporary folder — "
                           "extract or install it properly before updating")
    helper = os.path.join(
        tempfile.gettempdir(), f"MichaelTV-swap-{os.getpid()}.bat")
    pid = os.getpid()
    logf = os.path.join(tempfile.gettempdir(), "MichaelTV-swap.log")
    rc = ("/MIR /R:2 /W:2 /NFL /NDL /NJH /NJS /NP >nul")
    with open(helper, "w", newline="") as f:
        f.write(
            "@echo off\r\n"
            f"echo [{time.strftime('%Y-%m-%d %H:%M:%S')}] swap start"
            f" pid={pid} >> \"{logf}\"\r\n"
            # ~4 s for the app's hard-exit timer (+ teardown stragglers)
            "ping -n 5 127.0.0.1 >nul\r\n"
            # if the app still wedged mid-shutdown, take it down; rc 128
            # (no such process) is the normal, already-exited case
            f"taskkill /F /PID {pid} >nul 2>&1\r\n"
            # every step is stamped at EXECUTION time ([%date% %time%]
            # expand inside cmd): a stuck update is diagnosable from the
            # log alone — how long the copy took, whether the AV-hold
            # retry saved it, where things stopped (field lesson of the
            # v1.4.1 bricked update, which had no log at all)
            f"echo [%date% %time%] copying payload >> \"{logf}\"\r\n"
            f"robocopy \"{payload}\" \"{install_dir}\" {rc}\r\n"
            # rc >= 8 is a robocopy FAILURE (1-7 are success variants);
            # logged loudly but the swap continues — with the old exe
            # still in place, starting it leaves a working app and the
            # next update can retry, whereas skipping the start would
            # strand the user with nothing running after the app exited
            f"if errorlevel 8 echo [%date% %time%]!! robocopy FAILED"
            f" rc=%errorlevel% >> \"{logf}\"\r\n"
            # one retry a beat later: AV suites can briefly hold the
            # fresh exe after writing it
            "ping -n 4 127.0.0.1 >nul\r\n"
            f"robocopy \"{payload}\" \"{install_dir}\" {rc}\r\n"
            f"if errorlevel 8 echo [%date% %time%]!! robocopy FAILED"
            f" rc=%errorlevel% >> \"{logf}\"\r\n"
            f"echo [%date% %time%] starting new version >> \"{logf}\"\r\n"
            f"start \"\" \"{install_dir}\\MichaelTV.exe\"\r\n"
            f"rd /s /q \"{staging}\"\r\n"
            f"echo [%date% %time%] swap done >> \"{logf}\"\r\n"
            # the helper lives in %TEMP%, not staging — deleting its own
            # folder is no longer a concern; this last line now RUNS
            f"del \"%~f0\"\r\n"
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
