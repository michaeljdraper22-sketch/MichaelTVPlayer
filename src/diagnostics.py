# -*- coding: utf-8 -*-
"""Opt-in diagnostics uploads — "Help improve MichaelTV" (Settings menu).

OFF by default. When the user turns it on in Settings, the app SILENTLY
posts a report to GitHub (as an issue) when it hits trouble:

- any ERROR logged by the app (crashes, wedged players, …)
- a burst of WARNINGs (>= 8 inside 2 minutes — the chase rescue / give-up
  signature), plus a startup heartbeat at most once a day

Every upload is rate-limited (>= 4 h between reports) and runs on a
daemon thread, so it can never block or break playback; every entry
point swallows its own errors.

WHAT IS SENT (and shown verbatim in the Settings dialog):
- basic system info: Windows build, CPU, RAM, screens, bundled VLC
  version, python, frozen/source, app version, a random install id
- a few playback-relevant settings (network cache, chase delay, DVR
  window) — NEVER the account (server/username/password are not read)
- the tail of the rotating player.log (~40 KB) with credentials
  REDACTED: xtream username/password/token query params, URL
  user:pass@userinfo and Windows profile paths all become REDACTED/USER

Destination: the project's GitHub repo (public) as a labeled issue,
created with a token the user pastes in Settings (a fine-grained PAT
scoped to just "issues: write" on this one repo). No token, no upload.
"""

import ctypes
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.request

from .config import APP_VERSION

log = logging.getLogger("mtp.diagnostics")

# Where reports go. Same repo as the updater; override with the
# "telemetry_repo" setting to point at a private companion repo instead.
REPO = "michaeljdraper22-sketch/MichaelTVPlayer"

# A fine-grained PAT (issues: read/write on REPO only) pasted into
# Settings ▸ "Help improve MichaelTV…". Deliberately empty here: this
# file is public on GitHub — a baked-in token would leak instantly.
_GH_TOKEN = ""

_MIN_INTERVAL_S = 4 * 3600     # any two reports at least 4 h apart
_HEARTBEAT_S = 24 * 3600       # startup heartbeat at most once a day
_STORM_N = 8                   # WARNINGs inside _STORM_WINDOW_S = report
_STORM_WINDOW_S = 120.0
_STORM_DEBOUNCE_S = 300.0      # one report per warning storm, minimum
_LOG_TAIL_CHARS = 40_000
_CRASH_HEAD_CHARS = 4_000
_BODY_CAP = 60_000             # GitHub issue body hard limit is 65536

_STARTED_AT = time.time()
_SCREEN_INFO = ""              # captured on the Qt main thread (see below)
_busy = threading.Lock()       # one upload at a time


# ---- credential scrubbing (the log tail goes to a public repo) ----

_QUERY_CRED_RE = re.compile(
    r"(?i)\b((?:user(?:name)?|pass(?:word)?|pwd|token|key|auth|u|p)=)"
    r"[^&\s'\"<>]+")
_URL_USERINFO_RE = re.compile(r"(?i)https?://[^\s/:@]+:[^\s/@]+@")
_WIN_USER_RE = re.compile(r"(?i)([a-z]:\\+users\\+)[^\\\s\"']+")


def scrub(text: str) -> str:
    """Redact credentials from ``text`` (see module docstring)."""
    try:
        text = _QUERY_CRED_RE.sub(r"\1REDACTED", text)
        text = _URL_USERINFO_RE.sub("https://REDACTED@", text)
        text = _WIN_USER_RE.sub(r"\1USER\\", text)
    except Exception:
        pass
    return text


# ---- system info ----

def _total_ram_gb() -> float:
    class _MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64)]
    try:
        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return round(ms.ullTotalPhys / (1024 ** 3), 1)
    except Exception:
        pass
    return 0.0


def _vlc_version() -> str:
    try:
        import vlc
        v = vlc.libvlc_get_version().decode("utf-8", "replace") \
            if isinstance(vlc.libvlc_get_version(), bytes) \
            else str(vlc.libvlc_get_version())
        return v.split()[0] if v else ""
    except Exception:
        return ""


def capture_screen_info() -> None:
    """Record the screen layout from the Qt MAIN thread (call once after
    the QApplication exists; the upload thread must not touch Qt)."""
    global _SCREEN_INFO
    try:
        from PyQt5 import QtWidgets
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        parts = []
        for scr in app.screens():
            g = scr.geometry()
            parts.append("%dx%d@%gx" % (g.width(), g.height(),
                                        scr.devicePixelRatio()))
        _SCREEN_INFO = ", ".join(parts)
    except Exception:
        pass


def _win_build() -> str:
    try:
        wv = sys.getwindowsversion()
        return "build %s (%s)" % (wv.build, wv.service_pack or "no SP")
    except Exception:
        return ""


def collect_system_info(config) -> dict:
    """Everything about the machine/runtime a bug report benefits from.
    Only playback-relevant settings are included — never the account."""
    info = {
        "app_version": APP_VERSION,
        "install_id": config.telemetry_id,
        "machine": platform.node(),
        "os": "%s %s %s (%s)" % (platform.system(), platform.release(),
                                 _win_build(), platform.machine()),
        "cpu": (platform.processor()
                or os.environ.get("PROCESSOR_IDENTIFIER", "?")),
        "cpu_cores": os.cpu_count(),
        "ram_gb": _total_ram_gb(),
        "screens": _SCREEN_INFO or "?",
        "vlc": _vlc_version() or "?",
        "python": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "uptime_min": round((time.time() - _STARTED_AT) / 60.0, 1),
        "settings": {
            "network_caching": config.network_caching,
            "chase_delay": config.chase_delay,
            "dvr_max_minutes": config.dvr_max_minutes,
            "timeshift": config.timeshift,
        },
    }
    return info


# ---- report building ----

def _tail(path: str, max_chars: int) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_chars:
                f.seek(-max_chars, os.SEEK_END)
                data = f.read()
                # drop the partial first line
                nl = data.find(b"\n")
                data = data[nl + 1:] if nl >= 0 else data
            else:
                data = f.read()
        return scrub(data.decode("utf-8", "replace"))
    except Exception:
        return "(unavailable)"


def _crash_section(log_dir: str) -> str:
    out = []
    for name in ("crash.dump", "crash.dump.prev"):
        p = os.path.join(log_dir, name)
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                out.append("%s: %d bytes — head:\n%s"
                           % (name, os.path.getsize(p),
                              _tail(p, _CRASH_HEAD_CHARS)))
        except Exception:
            pass
    return "\n\n".join(out) if out else "(no crash dumps present)"


def build_report(config, reason: str) -> tuple:
    """(title, markdown body) for a diagnostics issue."""
    from .logging_setup import LOG_DIR
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    info = collect_system_info(config)
    lines = ["## Reason", scrub(str(reason)).strip() or "(unspecified)", ""]
    lines += ["## System", ""]
    for k, v in info.items():
        if isinstance(v, dict):
            lines.append("- **%s:** %s" % (k, json.dumps(v, sort_keys=True)))
        else:
            lines.append("- **%s:** %s" % (k, scrub(str(v))))
    lines += ["", "## Recent log (redacted)", "",
              "```", _tail(os.path.join(LOG_DIR, "player.log"),
                           _LOG_TAIL_CHARS), "```", "",
              "## Crash dumps", "", _crash_section(LOG_DIR)]
    title = "Diag %s %s — %s" % (
        time.strftime("%Y%m%d-%H%M"), info["install_id"],
        scrub(str(reason)).strip().replace("\n", " ")[:60] or "report")
    return title[:120], "\n".join(lines)


# ---- upload ----

def _post_issue(token: str, repo: str, title: str, body: str) -> None:
    if len(body) > _BODY_CAP:
        body = body[:_BODY_CAP] + "\n…(truncated)"
    url = "https://api.github.com/repos/%s/issues" % repo
    payload = json.dumps({"title": title, "body": body,
                          "labels": ["diagnostics"]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "MichaelTVPlayer-diagnostics",
            "Content-Type": "application/json",
        })
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status not in (200, 201):
            raise RuntimeError("GitHub returned HTTP %d" % r.status)


def maybe_upload(config, reason: str, force: bool = False) -> bool:
    """Rate-limited upload (runs on a worker thread; never raises)."""
    try:
        if not config.telemetry_enabled:
            return False
        token = (config.telemetry_token or "").strip() or _GH_TOKEN
        if not token:
            log.info("diagnostics: enabled but no token — nothing sent")
            return False
        now = time.time()
        if not force and now - config.telemetry_last_sent < _MIN_INTERVAL_S:
            return False
        repo = (config.data.get("telemetry_repo") or REPO).strip()
        title, body = build_report(config, reason)
        _post_issue(token, repo, title, body)
        config.data["telemetry_last_sent"] = time.time()
        try:
            config.save()
        except Exception:
            pass
        log.info("diagnostics: report sent (%s)", reason)
        return True
    except Exception as exc:  # noqa: BLE001
        try:
            log.warning("diagnostics: upload failed: %r", exc)
        except Exception:
            pass
        return False


def schedule_upload(config, reason: str, force: bool = False) -> bool:
    """Spawn the upload thread (one at a time; returns False if busy)."""

    def _run():
        if not _busy.acquire(False):
            return
        try:
            maybe_upload(config, reason, force)
        finally:
            _busy.release()

    t = threading.Thread(target=_run, name="mtp-diagnostics", daemon=True)
    t.start()
    return True


def upload_now_blocking(config, reason: str) -> str:
    """Synchronous upload for the Settings dialog's test button.
    Returns a short human result (never raises)."""
    try:
        if not config.telemetry_enabled:
            return "Turn on the checkbox first."
        if not ((config.telemetry_token or "").strip() or _GH_TOKEN):
            return "No token set — paste a GitHub token first."
        ok = maybe_upload(config, reason, force=True)
        return "Report sent — check the repo's Issues." if ok \
            else "Upload failed (see player.log, logger mtp.diagnostics)."
    except Exception as exc:  # noqa: BLE001
        return "Upload failed: %r" % exc


# ---- automatic triggers ----

class _TriggerHandler(logging.Handler):
    """Fire one report per trouble: any ERROR, or a WARNING burst."""

    def __init__(self, config):
        super().__init__(level=logging.WARNING)
        self._cfg = config
        self._warn_n = 0
        self._warn_t0 = 0.0
        self._last_fire = 0.0

    def emit(self, record):
        try:
            if record.name.startswith("mtp.diagnostics"):
                return   # never feed our own logs back into a trigger
            now = time.time()
            if record.levelno >= logging.ERROR:
                reason = "error: " + (record.getMessage() or "?")
            else:
                if now - self._warn_t0 > _STORM_WINDOW_S:
                    self._warn_t0 = now
                    self._warn_n = 0
                self._warn_n += 1
                if self._warn_n < _STORM_N:
                    return
                reason = "warning burst (%d in %.0fs)" \
                         % (self._warn_n, now - self._warn_t0)
            if now - self._last_fire < _STORM_DEBOUNCE_S:
                return
            self._last_fire = now
            self._warn_n = 0
            schedule_upload(self._cfg, reason)
        except Exception:
            pass


def install_trigger(config) -> None:
    """Attach the WARNING/ERROR trigger to the app's logger."""
    try:
        logging.getLogger("mtp").addHandler(_TriggerHandler(config))
    except Exception:
        pass


def startup_heartbeat(config) -> None:
    """Once a day when enabled: a fresh report with current system info
    (also carries the PREVIOUS session's log tail, which is where the
    interesting failures are)."""
    try:
        if not config.telemetry_enabled:
            return
        if time.time() - config.telemetry_last_sent < _HEARTBEAT_S:
            return
        schedule_upload(config, "startup heartbeat")
    except Exception:
        pass


def open_repo_issues() -> None:
    """Open the diagnostics destination in the browser (Settings dialog)."""
    try:
        url = "https://github.com/%s/issues?q=label%%3Adiagnostics" % REPO
        if sys.platform == "win32":
            subprocess.Popen(["cmd", "/c", "start", "", url], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", url], close_fds=True)
    except Exception:
        pass
